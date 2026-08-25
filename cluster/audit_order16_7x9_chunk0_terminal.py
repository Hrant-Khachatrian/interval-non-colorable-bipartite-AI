#!/usr/bin/env python3
"""Fail-closed terminal acceptance audit for order-16 7+9 chunk 0."""

from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interval_edge_coloring import Graph, fixed_span_sat_solve, from_graph6, nauty_canonical_hash, verify_coloring


ROOT = Path("/mnt/weka/hrant/interval-search")
RESULT = ROOT / "results/order16-7x9/chunk-0.jsonl"
SOURCE = ROOT / "data/order16-7x9-d2to11.g6"
LOG = ROOT / "slurm-228989_0.out"
REPORT = ROOT / "results/order16-7x9/chunk-0-audit.json"
MARKDOWN = ROOT / "results/order16-7x9/chunk-0-audit.md"
EXPECTED = 439_987
JOB_ID = "229469"
EXACT_KEYS = {
    "canonical_sha256", "delta", "index", "minimum_degree", "order", "size",
    "solver_seconds", "span", "status",
}
HASH = re.compile(r"9:7:[0-9a-f]{64}\Z")
STATUSES = {"colorable", "non-colorable", "timeout", "regular-skipped"}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def scheduler_state() -> dict[str, Any]:
    active = subprocess.run(["squeue", "-h", "-j", JOB_ID], text=True, capture_output=True, check=False)
    if active.returncode:
        raise RuntimeError(active.stderr.strip() or "squeue failed")
    accounting = subprocess.run(
        ["sacct", "-j", JOB_ID, "--format=JobIDRaw,State,ExitCode", "-P", "-n"],
        text=True,
        capture_output=True,
        check=False,
    )
    if accounting.returncode:
        raise RuntimeError(accounting.stderr.strip() or "sacct failed")
    records = [line.split("|") for line in accounting.stdout.splitlines() if line.strip()]
    primary = next((fields for fields in records if fields and fields[0] == JOB_ID), None)
    return {
        "active_squeue_rows": len(active.stdout.splitlines()),
        "sacct_primary": primary,
        "terminal_completed": not active.stdout.strip() and primary is not None and primary[1] == "COMPLETED" and primary[2] == "0:0",
    }


def terminal_log_evidence() -> dict[str, Any]:
    if not LOG.is_file():
        return {"valid": False, "reason": "missing_log"}
    final: dict[str, Any] | None = None
    for raw in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and "processed" in item and "index_range" in item:
            final = item
    if final is None:
        return {"valid": False, "reason": "missing_final_summary"}
    counts = final.get("counts")
    valid = (
        final.get("n1") == 7
        and final.get("n2") == 9
        and final.get("input") == "data/order16-7x9-d2to11.g6"
        and final.get("index_range") == [0, EXPECTED]
        and final.get("processed") == EXPECTED
        and isinstance(counts, dict)
        and all(type(counts.get(status, 0)) is int and counts.get(status, 0) >= 0 for status in ("colorable", "non-colorable", "timeout"))
        and sum(counts.get(status, 0) for status in ("colorable", "non-colorable", "timeout")) == EXPECTED
    )
    return {"valid": valid, "summary": final}


def row_problem(row: Any) -> str | None:
    if not isinstance(row, dict) or set(row) != EXACT_KEYS:
        return "exact_schema"
    if type(row["index"]) is not int or not 0 <= row["index"] < EXPECTED:
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
        if type(row["span"]) is not int or not row["delta"] <= row["span"] <= row["size"]:
            return "colorable_span"
    elif row["span"] is not None:
        return "noncolorable_or_timeout_span"
    return None


def source_lines(indices: set[int]) -> dict[int, str]:
    if not indices:
        return {}
    result: dict[int, str] = {}
    ceiling = max(indices)
    with SOURCE.open(encoding="ascii") as handle:
        for index, line in enumerate(handle):
            if index > ceiling:
                break
            if index in indices:
                result[index] = line.rstrip("\n")
    return result


def confirm_negative(graph6: str, primary: dict[str, Any]) -> dict[str, Any]:
    order, edges = from_graph6(graph6)
    vertices = [f"V{index}" for index in range(order)]
    graph = Graph(vertices, [(vertices[left], vertices[right]) for left, right in edges])
    degrees = graph.degrees
    if nauty_canonical_hash(graph) != primary["canonical_sha256"]:
        return {"status": "source_hash_mismatch"}
    if (graph.n, graph.m, graph.delta, min(degrees.values())) != (
        primary["order"], primary["size"], primary["delta"], primary["minimum_degree"],
    ):
        return {"status": "source_domain_mismatch"}
    spans: dict[str, str] = {}
    contradictory = unresolved = False
    # A negative needs an independent infeasibility result for every legal span.
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
        status = "primary_contradicted_colorable"
    elif unresolved:
        status = "unresolved_independent_solver"
    else:
        status = "confirmed_noncolorable"
    return {"status": status, "spans": spans}


def sorted_hash_summary(raw: Path, scratch: Path) -> dict[str, Any]:
    ordered = scratch / "hashes.sorted"
    subprocess.run(["sort", "-S", "512M", "-o", str(ordered), str(raw)], check=True, env=dict(os.environ, LC_ALL="C"))
    digest = hashlib.sha256()
    rows = duplicate_rows = 0
    previous: bytes | None = None
    with ordered.open("rb") as handle:
        for line in handle:
            rows += 1
            digest.update(line)
            if line == previous:
                duplicate_rows += 1
            previous = line
    return {"rows": rows, "sorted_list_sha256": digest.hexdigest(), "duplicate_canonical_hash_rows": duplicate_rows}


def main() -> None:
    state = scheduler_state()
    if not state["terminal_completed"]:
        raise SystemExit("refusing terminal audit before successful Slurm completion")
    log = terminal_log_evidence()
    if not log["valid"]:
        raise SystemExit("refusing terminal audit without a valid final worker summary")
    if not RESULT.is_file() or not SOURCE.is_file():
        raise SystemExit("missing result or canonical graph6 source")
    before = signature(RESULT)
    status_counts: collections.Counter[str] = collections.Counter()
    invalid: collections.Counter[str] = collections.Counter()
    invalid_examples: list[dict[str, Any]] = []
    negatives: dict[int, dict[str, Any]] = {}
    timeouts: list[int] = []
    duplicate_indices = 0
    malformed = 0
    valid_rows = 0
    seen = bytearray(EXPECTED)
    with tempfile.TemporaryDirectory(prefix="order16-7x9-chunk0-audit-", dir=RESULT.parent) as temporary:
        scratch = Path(temporary)
        raw_hashes = scratch / "hashes.raw"
        with RESULT.open("rb") as input_stream, raw_hashes.open("w", encoding="ascii") as hash_stream:
            for line_number, raw in enumerate(input_stream, 1):
                if not raw.endswith(b"\n"):
                    malformed += 1
                    invalid["unterminated_line"] += 1
                    continue
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    malformed += 1
                    invalid["invalid_json"] += 1
                    continue
                problem = row_problem(row)
                if problem is not None:
                    invalid[problem] += 1
                    if len(invalid_examples) < 20:
                        invalid_examples.append({"line": line_number, "problem": problem})
                    continue
                index = row["index"]
                if seen[index]:
                    duplicate_indices += 1
                else:
                    seen[index] = 1
                valid_rows += 1
                hash_stream.write(row["canonical_sha256"] + "\n")
                status_counts[row["status"]] += 1
                if row["status"] == "timeout":
                    timeouts.append(index)
                elif row["status"] == "non-colorable":
                    negatives[index] = row
        hashes = sorted_hash_summary(raw_hashes, scratch)
    missing_indices = EXPECTED - sum(seen)
    negative_sources = source_lines(set(negatives))
    confirmations = {str(index): confirm_negative(negative_sources[index], row) if index in negative_sources else {"status": "source_line_missing"} for index, row in sorted(negatives.items())}
    after = signature(RESULT)
    stable = before == after
    negative_statuses = collections.Counter(record["status"] for record in confirmations.values())
    accepted = (
        stable
        and malformed == 0
        and not invalid
        and valid_rows == EXPECTED
        and duplicate_indices == 0
        and missing_indices == 0
        and hashes["rows"] == EXPECTED
        and hashes["duplicate_canonical_hash_rows"] == 0
        and not timeouts
        and all(record["status"] == "confirmed_noncolorable" for record in confirmations.values())
    )
    report = {
        "schema_version": 1,
        "audit": "order16-7x9-chunk0-terminal-strict",
        "audit_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "job": {"array_job": "228989_0", "job_id": JOB_ID, "scheduler": state, "terminal_log": log},
        "range": {"start": 0, "stop_exclusive": EXPECTED, "expected_rows": EXPECTED},
        "file_signature_before": before,
        "file_signature_after": after,
        "stable_during_audit": stable,
        "rows": {"valid": valid_rows, "malformed": malformed, "invalid_by_reason": dict(sorted(invalid.items())), "invalid_examples": invalid_examples, "duplicate_indices": duplicate_indices, "missing_indices": missing_indices},
        "status_counts": dict(sorted(status_counts.items())),
        "timeouts": {"policy": "unresolved; any primary timeout rejects acceptance", "count": len(timeouts), "indices": timeouts[:100]},
        "primary_negatives": {"count": len(negatives), "confirmation_status_counts": dict(sorted(negative_statuses.items())), "confirmations": confirmations},
        "canonical_hashes": hashes,
        "acceptance": {"accepted": accepted, "status": "ACCEPTED" if accepted else "REJECTED"},
    }
    atomic_write(REPORT, json.dumps(report, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Order-16 7+9 Chunk 0 Terminal Audit",
        "",
        f"Status: **{report['acceptance']['status']}**",
        "",
        f"- Job: `228989_0` (Slurm job `{JOB_ID}`), successful terminal state: `{state['terminal_completed']}`",
        f"- Range: `[0, {EXPECTED})`; valid rows: `{valid_rows}`; missing indices: `{missing_indices}`; duplicate indices: `{duplicate_indices}`",
        f"- Malformed rows: `{malformed}`; schema/domain failures: `{sum(invalid.values())}`; output stable during audit: `{stable}`",
        f"- Canonical hashes: `{hashes['rows']}` checked; duplicate rows: `{hashes['duplicate_canonical_hash_rows']}`; sorted-list SHA-256: `{hashes['sorted_list_sha256']}`",
        f"- Statuses: `{dict(sorted(status_counts.items()))}`; unresolved primary timeouts: `{len(timeouts)}`",
        f"- Primary negatives: `{len(negatives)}`; independent all-legal-span confirmation: `{dict(sorted(negative_statuses.items()))}`",
        "",
        f"Evidence is recorded in `{REPORT.name}`.",
        "",
    ]
    atomic_write(MARKDOWN, "\n".join(lines))
    print(json.dumps({"accepted": accepted, "report": str(REPORT), "rows": valid_rows}, sort_keys=True))
    if not accepted:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
