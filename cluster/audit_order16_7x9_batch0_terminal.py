#!/usr/bin/env python3
"""Strict acceptance audit for terminal chunks in order-16 7+9 batch 0.

Chunk zero is intentionally excluded: it is owned by its dedicated auditor.
Only Slurm-completed chunks with an unchanged result file and an exact final
worker summary are considered.  The two published files are atomically
replaced, so a monitor never observes a partial ledger.
"""

from __future__ import annotations

import collections
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/mnt/weka/hrant/interval-search")
RESULTS = ROOT / "results/order16-7x9"
SOURCE = ROOT / "data/order16-7x9-d2to11.g6"
JOB = "228989"
TOTAL = 3_604_370_591
CHUNK_SIZE = 439_987
BATCH_LAST = 999
EXCLUDED = {0}
EXACT_KEYS = {
    "canonical_sha256", "delta", "index", "minimum_degree", "order", "size",
    "solver_seconds", "span", "status",
}
HASH = re.compile(r"9:7:[0-9a-f]{64}\Z")
STATUSES = {"colorable", "non-colorable", "timeout", "regular-skipped"}


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    atomic_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")


def prior_accepted() -> tuple[dict[int, dict[str, Any]], list[str]]:
    """Recover only internally consistent prior acceptances for incremental runs."""
    path = RESULTS / "batch0-terminal-audit.json"
    if not path.is_file():
        return {}, []
    try:
        document = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {}, [f"unreadable_prior_ledger:{type(error).__name__}"]
    if document.get("audit") != "order16-7x9-batch0-terminal-strict":
        return {}, ["incompatible_prior_ledger"]
    listed = document.get("accepted_chunks")
    entries = document.get("chunks")
    if not isinstance(listed, list) or not isinstance(entries, list):
        return {}, ["malformed_prior_ledger"]
    by_chunk = {
        entry.get("chunk"): entry for entry in entries
        if isinstance(entry, dict) and type(entry.get("chunk")) is int
    }
    accepted: dict[int, dict[str, Any]] = {}
    for chunk in listed:
        entry = by_chunk.get(chunk)
        if (
            type(chunk) is int and chunk not in EXCLUDED and 0 <= chunk <= BATCH_LAST
            and isinstance(entry, dict) and entry.get("accepted") is True
            and entry.get("status") == "ACCEPTED"
            and entry.get("expected_rows") == CHUNK_SIZE
            and entry.get("rows", {}).get("valid") == CHUNK_SIZE
        ):
            accepted[chunk] = entry
    if len(accepted) != len(set(listed)):
        return {}, ["inconsistent_prior_acceptances"]
    return accepted, []


def signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def chunk_range(chunk: int) -> tuple[int, int]:
    start = chunk * CHUNK_SIZE
    return start, min(TOTAL, start + CHUNK_SIZE)


def sacct_states() -> tuple[dict[int, dict[str, str]], collections.Counter[str]]:
    command = ["sacct", "-X", "-j", JOB, "--format=JobID,State,ExitCode", "-P", "-n"]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "sacct failed")
    tasks: dict[int, dict[str, str]] = {}
    counts: collections.Counter[str] = collections.Counter()
    for raw in completed.stdout.splitlines():
        fields = raw.split("|")
        if len(fields) < 3 or not fields[0].startswith(f"{JOB}_"):
            continue
        suffix = fields[0][len(JOB) + 1:]
        if not suffix.isdigit():
            continue
        chunk = int(suffix)
        if 0 <= chunk <= BATCH_LAST:
            state = fields[1].split(" ", 1)[0]
            tasks[chunk] = {"state": state, "exit_code": fields[2]}
            counts[state] += 1
    return tasks, counts


def squeue_counts() -> collections.Counter[str]:
    completed = subprocess.run(["squeue", "-h", "-j", JOB, "-o", "%T"], text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "squeue failed")
    return collections.Counter(line.strip().split("+", 1)[0] for line in completed.stdout.splitlines() if line.strip())


def final_summary(chunk: int, start: int, stop: int) -> dict[str, Any]:
    log = ROOT / f"slurm-{JOB}_{chunk}.out"
    if not log.is_file():
        return {"valid": False, "reason": "missing_terminal_log"}
    summary: dict[str, Any] | None = None
    with log.open(encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            try:
                candidate = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "processed" in candidate and "index_range" in candidate:
                summary = candidate
    if summary is None:
        return {"valid": False, "reason": "missing_final_summary"}
    counts = summary.get("counts")
    valid = (
        summary.get("n1") == 7 and summary.get("n2") == 9
        and summary.get("input") == "data/order16-7x9-d2to11.g6"
        and summary.get("index_range") == [start, stop]
        and summary.get("processed") == stop - start
        and isinstance(counts, dict)
        and all(type(counts.get(status, 0)) is int and counts.get(status, 0) >= 0 for status in ("colorable", "non-colorable", "timeout"))
        and sum(counts.get(status, 0) for status in ("colorable", "non-colorable", "timeout")) == stop - start
    )
    return {"valid": valid, "summary": summary}


def row_problem(row: Any, start: int, stop: int) -> str | None:
    if not isinstance(row, dict) or set(row) != EXACT_KEYS:
        return "exact_schema"
    if type(row["index"]) is not int or not start <= row["index"] < stop:
        return "index"
    if type(row["canonical_sha256"]) is not str or not HASH.fullmatch(row["canonical_sha256"]):
        return "canonical_sha256"
    if type(row["order"]) is not int or row["order"] != 16:
        return "order"
    if type(row["size"]) is not int or not 16 <= row["size"] <= 63:
        return "size"
    if type(row["delta"]) is not int or not 2 <= row["delta"] <= 9:
        return "delta"
    if type(row["minimum_degree"]) is not int or not 2 <= row["minimum_degree"] <= row["delta"]:
        return "minimum_degree"
    if type(row["status"]) is not str or row["status"] not in STATUSES:
        return "status"
    if row["status"] == "regular-skipped":
        return "regular_skipped_impossible_for_7x9"
    seconds = row["solver_seconds"]
    if type(seconds) is not float or not math.isfinite(seconds) or not 0.0 <= seconds <= 10.0:
        return "solver_seconds"
    if row["status"] == "colorable":
        if type(row["span"]) is not int or not row["delta"] <= row["span"] < row["order"]:
            return "colorable_span"
    elif row["span"] is not None:
        return "noncolorable_or_timeout_span"
    return None


def source_lines(indices: set[int]) -> dict[int, str]:
    if not indices:
        return {}
    found: dict[int, str] = {}
    ceiling = max(indices)
    with SOURCE.open(encoding="ascii") as stream:
        for index, line in enumerate(itertools.islice(stream, ceiling + 1)):
            if index in indices:
                found[index] = line.rstrip("\n")
    return found


def confirm_negative(graph6: str, primary: dict[str, Any]) -> dict[str, Any]:
    from interval_edge_coloring import Graph, fixed_span_sat_solve, from_graph6, nauty_canonical_hash, verify_coloring

    order, edges = from_graph6(graph6)
    vertices = [f"V{index}" for index in range(order)]
    graph = Graph(vertices, [(vertices[left], vertices[right]) for left, right in edges])
    degrees = graph.degrees
    if nauty_canonical_hash(graph) != primary["canonical_sha256"]:
        return {"status": "source_hash_mismatch"}
    if (graph.n, graph.m, graph.delta, min(degrees.values())) != (primary["order"], primary["size"], primary["delta"], primary["minimum_degree"]):
        return {"status": "source_domain_mismatch"}
    spans: dict[str, str] = {}
    contradictory = unresolved = False
    for span in range(graph.delta, graph.n):
        status, coloring = fixed_span_sat_solve(graph, span, 3600.0, workers=4)
        spans[str(span)] = status
        if status in {"OPTIMAL", "FEASIBLE"}:
            valid, reason = verify_coloring(graph, coloring)
            if not valid:
                return {"status": "independent_witness_invalid", "reason": reason, "spans": spans}
            contradictory = True
        elif status != "INFEASIBLE":
            unresolved = True
    if contradictory:
        result = "primary_contradicted_colorable"
    elif unresolved:
        result = "unresolved_independent_solver"
    else:
        result = "confirmed_noncolorable"
    return {"status": result, "spans": spans}


def audit_chunk(chunk: int, evidence: dict[str, str]) -> dict[str, Any]:
    start, stop = chunk_range(chunk)
    path = RESULTS / f"chunk-{chunk}.jsonl"
    log = final_summary(chunk, start, stop)
    if not path.is_file():
        return {"chunk": chunk, "state": evidence, "range": [start, stop], "status": "REJECTED", "reason": "missing_output", "terminal_log": log}
    before = signature(path)
    expected = stop - start
    statuses: collections.Counter[str] = collections.Counter()
    invalid: collections.Counter[str] = collections.Counter()
    examples: list[dict[str, Any]] = []
    negatives: dict[int, dict[str, Any]] = {}
    timeouts: list[int] = []
    seen = bytearray(expected)
    duplicate_indices = malformed = valid_rows = 0
    with tempfile.TemporaryDirectory(prefix=f"order16-7x9-batch0-{chunk}-", dir=RESULTS) as temporary:
        hashes_raw = Path(temporary) / "hashes.raw"
        with path.open("rb") as input_stream, hashes_raw.open("w", encoding="ascii") as hash_stream:
            for line_number, raw in enumerate(input_stream, 1):
                if not raw.endswith(b"\n"):
                    malformed += 1; invalid["unterminated_line"] += 1; continue
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    malformed += 1; invalid["invalid_json"] += 1; continue
                problem = row_problem(row, start, stop)
                if problem:
                    invalid[problem] += 1
                    if len(examples) < 20:
                        examples.append({"line": line_number, "problem": problem})
                    continue
                position = row["index"] - start
                if seen[position]:
                    duplicate_indices += 1
                else:
                    seen[position] = 1
                valid_rows += 1
                hash_stream.write(row["canonical_sha256"] + "\n")
                statuses[row["status"]] += 1
                if row["status"] == "timeout":
                    timeouts.append(row["index"])
                elif row["status"] == "non-colorable":
                    negatives[row["index"]] = row
        ordered = Path(temporary) / "hashes.sorted"
        subprocess.run(["sort", "-S", "512M", "-o", str(ordered), str(hashes_raw)], check=True, env=dict(os.environ, LC_ALL="C"))
        digest = hashlib.sha256(); hash_rows = duplicate_hashes = 0; previous: bytes | None = None
        with ordered.open("rb") as stream:
            for raw in stream:
                hash_rows += 1; digest.update(raw)
                if raw == previous:
                    duplicate_hashes += 1
                previous = raw
    missing = expected - sum(seen)
    source = source_lines(set(negatives))
    confirmations = {str(index): confirm_negative(source[index], row) if index in source else {"status": "source_line_missing"} for index, row in sorted(negatives.items())}
    after = signature(path)
    stable = before == after
    confirmed = all(item["status"] == "confirmed_noncolorable" for item in confirmations.values())
    accepted = (evidence["state"] == "COMPLETED" and evidence["exit_code"] == "0:0" and log["valid"] and stable and malformed == 0 and not invalid and valid_rows == expected and duplicate_indices == 0 and missing == 0 and hash_rows == expected and duplicate_hashes == 0 and not timeouts and confirmed)
    return {
        "chunk": chunk, "state": evidence, "range": [start, stop], "expected_rows": expected,
        "terminal_log": log, "file_signature_before": before, "file_signature_after": after, "stable_during_audit": stable,
        "rows": {"valid": valid_rows, "malformed": malformed, "invalid_by_reason": dict(sorted(invalid.items())), "invalid_examples": examples, "duplicate_indices": duplicate_indices, "missing_indices": missing},
        "status_counts": dict(sorted(statuses.items())), "timeouts": {"policy": "unresolved; any primary timeout rejects acceptance", "count": len(timeouts), "indices": timeouts[:100]},
        "primary_negatives": {"count": len(negatives), "confirmations": confirmations},
        "canonical_hashes": {"rows": hash_rows, "sorted_list_sha256": digest.hexdigest(), "duplicate_canonical_hash_rows": duplicate_hashes},
        "status": "ACCEPTED" if accepted else "REJECTED", "accepted": accepted,
    }


def main() -> None:
    accounting, accounting_counts = sacct_states()
    queue_counts = squeue_counts()
    prior, prior_warnings = prior_accepted()
    terminal = [chunk for chunk, state in accounting.items() if chunk not in EXCLUDED and state["state"] == "COMPLETED" and state["exit_code"] == "0:0"]
    new_candidates = [chunk for chunk in terminal if chunk not in prior]
    new_audits = [audit_chunk(chunk, accounting[chunk]) for chunk in sorted(new_candidates)]
    audits = [prior[chunk] for chunk in sorted(prior)] + new_audits
    accepted = sorted(prior) + [entry["chunk"] for entry in new_audits if entry["accepted"]]
    rejected = [entry["chunk"] for entry in new_audits if not entry["accepted"]]
    report = {
        "schema_version": 1, "audit": "order16-7x9-batch0-terminal-strict", "audit_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "contract": {"array_job": JOB, "batch_chunks": [0, BATCH_LAST], "excluded_chunks": {"0": "dedicated chunk-0 auditor"}, "expected_rows_per_non_tail_chunk": CHUNK_SIZE, "exact_row_keys": sorted(EXACT_KEYS), "hash_pattern": HASH.pattern, "timeout_policy": "unresolved; any primary timeout rejects acceptance", "negative_policy": "every primary negative is independently checked at all legal spans delta..15"},
        "incremental": {"prior_accepted_chunks": sorted(prior), "new_terminal_candidates": sorted(new_candidates), "prior_ledger_warnings": prior_warnings},
        "scheduler": {
            "sacct_states": dict(sorted(accounting_counts.items())),
            "squeue_compacted_rows": dict(sorted(queue_counts.items())),
            "batch_counts": {
                "terminal_completed": accounting_counts["COMPLETED"],
                "running": accounting_counts["RUNNING"],
                "pending_not_yet_materialized": (BATCH_LAST + 1) - len(accounting),
            },
            "terminal_completed_candidates": sorted(terminal),
        },
        "chunks": audits, "accepted_chunks": accepted, "rejected_chunks": rejected,
        "accepted_scope": {"chunks": accepted, "rows": sum(entry["rows"]["valid"] for entry in audits if entry["accepted"])},
    }
    atomic_json(RESULTS / "batch0-terminal-audit.json", report)
    counts = report["scheduler"]["batch_counts"]
    lines = ["# Order-16 7+9 Batch 0 Terminal Audit", "", f"Updated: `{report['audit_utc']}`", "", "- Chunk `0` is excluded because its dedicated auditor owns it.", f"- Scheduler snapshot: terminal completed `{counts['terminal_completed']}`, running `{counts['running']}`, pending `{counts['pending_not_yet_materialized']}`.", f"- Prior accepted chunks retained without re-audit: `{sorted(prior)}`.", f"- Newly audited terminal candidates: `{sorted(new_candidates)}`.", f"- Accepted chunks: `{accepted}`; accepted rows: `{report['accepted_scope']['rows']}`.", f"- Rejected terminal chunks from this increment: `{rejected}`.", "", "Per-chunk status:"]
    for entry in audits:
        rows = entry["rows"]
        lines.append(f"- Chunk `{entry['chunk']}`: **{entry['status']}**; rows `{rows['valid']}/{entry['expected_rows']}`, missing `{rows['missing_indices']}`, duplicate indices `{rows['duplicate_indices']}`, malformed/schema `{rows['malformed']}/{sum(rows['invalid_by_reason'].values())}`, timeout `{entry['timeouts']['count']}`, negative `{entry['primary_negatives']['count']}`, duplicate hashes `{entry['canonical_hashes']['duplicate_canonical_hash_rows']}`.")
    if not audits:
        lines.append("- No newly completed eligible chunks were available for audit.")
    lines.extend(["", "Evidence: `batch0-terminal-audit.json`.", ""])
    atomic_text(RESULTS / "coverage.md", "\n".join(lines))
    print(json.dumps({"accepted_chunks": accepted, "rejected_chunks": rejected, "accepted_rows": report["accepted_scope"]["rows"]}, sort_keys=True))


if __name__ == "__main__":
    main()
