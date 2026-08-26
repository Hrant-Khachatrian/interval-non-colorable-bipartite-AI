#!/usr/bin/env python3
"""Fail-closed acceptance audit for terminal order-16 6x10 chunk ranges.

This is intentionally separate from the bounded incremental telemetry audit.
It validates whole terminal chunk files and only publishes an accepted cache and
sorted-hash sidecar after every required condition, including independent
fixed-span confirmation of every primary negative, is satisfied.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path("/mnt/weka/hrant/interval-search")
RESULT = ROOT / "results" / "order16-6x10"
AUDIT_DIR = RESULT / ".audit"
SOURCE = ROOT / "data" / "order16-6x10-d2to11.g6"
JOB_ID = "228788"
EXPECTED_ROWS = 291_917_907
CHUNKS = 512
CHUNK_SIZE = (EXPECTED_ROWS + CHUNKS - 1) // CHUNKS
EXACT_KEYS = {
    "canonical_sha256",
    "delta",
    "index",
    "minimum_degree",
    "order",
    "size",
    "solver_seconds",
    "span",
    "status",
}
HASH = re.compile(r"10:6:[0-9a-f]{64}\Z")
STATUSES = {"colorable", "non-colorable", "timeout", "regular-skipped"}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
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
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def active_scheduler_states() -> dict[int, str]:
    """Read active array tasks from squeue, which is reliable for live state."""
    command = ["/opt/slurm/bin/squeue", "-h", "-j", JOB_ID, "-o", "%K %T"]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "squeue array-state query failed")
    states: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            task = int(fields[0].rsplit("_", 1)[-1])
        except ValueError:
            continue
        states[task] = fields[1].split("+", 1)[0]
    return states


def terminal_log_state(chunk: int, start: int, stop: int) -> str:
    """Require the target worker's final exact summary after it left squeue.

    sacct task expansion is known to return unrelated jobs on this cluster, so
    this worker-owned terminal record is the fail-closed completion evidence.
    """
    path = ROOT / f"slurm-{JOB_ID}_{chunk}.out"
    if not path.is_file():
        return "MISSING_TERMINAL_LOG_EVIDENCE"
    try:
        with path.open(encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return "UNREADABLE_TERMINAL_LOG_EVIDENCE"
    for line in reversed(lines):
        try:
            summary = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(summary, dict) or "processed" not in summary:
            continue
        counts = summary.get("counts")
        if (
            summary.get("n1") == 6
            and summary.get("n2") == 10
            and summary.get("edge_range") == [2, 999]
            and summary.get("input") == "data/order16-6x10-d2to11.g6"
            and summary.get("index_range") == [start, stop]
            and summary.get("processed") == stop - start
            and counts == {"colorable": stop - start, "non-colorable": 0, "timeout": 0}
        ):
            return "COMPLETED_LOG_EVIDENCE"
        return "INVALID_TERMINAL_LOG_EVIDENCE"
    return "MISSING_FINAL_SUMMARY"


def row_problem(row: Any, start: int, stop: int, solver_limit: float, tolerance: float) -> str | None:
    if not isinstance(row, dict) or set(row) != EXACT_KEYS:
        return "schema"
    if type(row["index"]) is not int or not start <= row["index"] < stop:
        return "index"
    if type(row["canonical_sha256"]) is not str or not HASH.fullmatch(row["canonical_sha256"]):
        return "canonical_sha256"
    if type(row["order"]) is not int or row["order"] != 16:
        return "order"
    if type(row["size"]) is not int or not 20 <= row["size"] <= 60:
        return "size"
    if type(row["delta"]) is not int or not 2 <= row["delta"] <= 10:
        return "delta"
    if type(row["minimum_degree"]) is not int or not 2 <= row["minimum_degree"] <= row["delta"]:
        return "minimum_degree"
    if type(row["status"]) is not str or row["status"] not in STATUSES:
        return "status"
    seconds = row["solver_seconds"]
    if type(seconds) is not float or not math.isfinite(seconds) or not 0.0 <= seconds <= solver_limit + tolerance:
        return "solver_seconds"
    span = row["span"]
    if row["status"] == "colorable":
        if type(span) is not int or not row["delta"] <= span <= row["size"]:
            return "colorable_span"
    elif span is not None:
        return "noncolorable_or_timeout_span"
    # A positive regular bipartite graph cannot have unequal 6- and 10-vertex
    # sides, and the canonical census has minimum degree at least two.
    if row["status"] == "regular-skipped":
        return "regular_skipped_impossible_for_6x10"
    return None


def line_at_indices(source: Path, indices: set[int]) -> dict[int, str]:
    if not indices:
        return {}
    found: dict[int, str] = {}
    ceiling = max(indices)
    with source.open(encoding="ascii") as handle:
        for index, line in enumerate(itertools.islice(handle, ceiling + 1)):
            if index in indices:
                found[index] = line.rstrip("\n")
    return found


def confirm_negative(graph_line: str, primary: dict[str, Any], seconds: float, workers: int) -> dict[str, Any]:
    from interval_edge_coloring import Graph, fixed_span_sat_solve, from_graph6, nauty_canonical_hash, verify_coloring

    order, edges = from_graph6(graph_line)
    vertices = [f"V{index}" for index in range(order)]
    graph = Graph(vertices, [(vertices[left], vertices[right]) for left, right in edges])
    actual_hash = nauty_canonical_hash(graph)
    degrees = graph.degrees
    if actual_hash != primary["canonical_sha256"]:
        return {"status": "source_hash_mismatch", "actual_canonical_sha256": actual_hash}
    if graph.n != primary["order"] or graph.m != primary["size"] or graph.delta != primary["delta"] or min(degrees.values()) != primary["minimum_degree"]:
        return {"status": "source_domain_mismatch"}

    spans: dict[str, dict[str, Any]] = {}
    confirmed = True
    contradictory = False
    unresolved = False
    # Legal interval spans for an order-16 graph are delta through n - 1.
    # Every span is run even after an UNKNOWN result; a negative requires all
    # of them to be independently INFEASIBLE.
    for span in range(graph.delta, graph.n):
        solver_status, coloring = fixed_span_sat_solve(graph, span, seconds, workers=workers)
        item: dict[str, Any] = {"solver_status": solver_status, "has_coloring": coloring is not None}
        spans[str(span)] = item
        if solver_status in {"OPTIMAL", "FEASIBLE"}:
            valid, reason = verify_coloring(graph, coloring)
            if not valid:
                return {"status": "independent_witness_invalid", "spans": spans, "reason": reason}
            contradictory = True
            confirmed = False
        elif solver_status != "INFEASIBLE":
            unresolved = True
            confirmed = False
    if contradictory:
        status = "primary_contradicted_colorable"
    elif unresolved:
        status = "unresolved_independent_solver"
    elif confirmed:
        status = "confirmed_noncolorable"
    else:
        status = "unresolved"
    return {"status": status, "spans": spans}


def sorted_hash_metadata(raw_hashes: Path, audit_dir: Path) -> tuple[Path, dict[str, Any]]:
    sorted_path = audit_dir / f".{raw_hashes.name}.sorted"
    duplicates_path = audit_dir / f".{raw_hashes.name}.duplicates"
    environment = dict(os.environ, LC_ALL="C")
    subprocess.run(["sort", "-S", "2G", "-o", str(sorted_path), str(raw_hashes)], check=True, env=environment)
    with duplicates_path.open("wb") as handle:
        subprocess.run(["uniq", "-d", str(sorted_path)], stdout=handle, check=True, env=environment)
    duplicate_count = 0
    duplicate_samples: list[str] = []
    with duplicates_path.open(encoding="ascii") as handle:
        for duplicate_count, value in enumerate(handle, 1):
            if len(duplicate_samples) < 20:
                duplicate_samples.append(value.rstrip("\n"))
    duplicates_path.unlink(missing_ok=True)
    digest = hashlib.sha256()
    line_count = 0
    with sorted_path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            line_count += 1
    return sorted_path, {
        "rows": line_count,
        "sha256": digest.hexdigest(),
        "duplicate_canonical_hash_count": duplicate_count,
        "duplicate_canonical_hash_samples": duplicate_samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first-chunk", type=int, default=120)
    parser.add_argument("--last-chunk", type=int, default=143)
    parser.add_argument("--solver-limit", type=float, default=5.0)
    parser.add_argument("--solver-tolerance", type=float, default=5.0)
    parser.add_argument("--confirmation-seconds", type=float, default=3600.0)
    parser.add_argument("--confirmation-workers", type=int, default=4)
    args = parser.parse_args()
    if not 0 <= args.first_chunk <= args.last_chunk < CHUNKS:
        parser.error("chunk range is outside the fixed 512-task order16 6x10 array")
    if args.solver_limit <= 0 or args.solver_tolerance < 0 or args.confirmation_seconds <= 0 or args.confirmation_workers < 1:
        parser.error("solver limits must be positive and tolerance nonnegative")

    selected = list(range(args.first_chunk, args.last_chunk + 1))
    label = f"strict-{args.first_chunk}-{args.last_chunk}"
    report_path = AUDIT_DIR / f"{label}-report.json"
    cache_path = AUDIT_DIR / f"{label}-cache.json"
    accepted_hashes_path = AUDIT_DIR / f"{label}-hashes.sorted"
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    if not SOURCE.is_file():
        raise SystemExit(f"missing canonical source: {SOURCE}")

    initial_signatures: dict[int, dict[str, int]] = {}
    active_states = active_scheduler_states()
    chunk_states: dict[int, str] = {}
    missing_files: list[int] = []
    for chunk in selected:
        path = RESULT / f"chunk-{chunk}.jsonl"
        start = chunk * CHUNK_SIZE
        stop = min(EXPECTED_ROWS, start + CHUNK_SIZE)
        chunk_states[chunk] = active_states.get(chunk, terminal_log_state(chunk, start, stop))
        if path.is_file():
            initial_signatures[chunk] = signature(path)
        else:
            missing_files.append(chunk)

    total_expected = 0
    rows = malformed_json = duplicate_index_rows = missing_index_rows = invalid_rows = 0
    timeouts: list[int] = []
    negatives: dict[int, dict[str, Any]] = {}
    status_counts: collections.Counter[str] = collections.Counter()
    invalid_examples: list[dict[str, Any]] = []
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{label}-", suffix=".hashes", dir=AUDIT_DIR, text=True)
    os.close(descriptor)
    temporary_hashes = Path(temporary_name)
    try:
        with temporary_hashes.open("w", encoding="ascii") as hash_output:
            for chunk in selected:
                start = chunk * CHUNK_SIZE
                stop = min(EXPECTED_ROWS, start + CHUNK_SIZE)
                expected_rows = stop - start
                total_expected += expected_rows
                path = RESULT / f"chunk-{chunk}.jsonl"
                if not path.is_file():
                    missing_index_rows += expected_rows
                    continue
                seen = bytearray(expected_rows)
                with path.open(encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if not line.endswith("\n"):
                            malformed_json += 1
                            if len(invalid_examples) < 20:
                                invalid_examples.append({"chunk": chunk, "line": line_number, "problem": "unterminated_line"})
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            malformed_json += 1
                            if len(invalid_examples) < 20:
                                invalid_examples.append({"chunk": chunk, "line": line_number, "problem": "invalid_json"})
                            continue
                        problem = row_problem(row, start, stop, args.solver_limit, args.solver_tolerance)
                        if problem is not None:
                            invalid_rows += 1
                            if len(invalid_examples) < 20:
                                invalid_examples.append({"chunk": chunk, "line": line_number, "problem": problem})
                            continue
                        rows += 1
                        index = row["index"]
                        position = index - start
                        if seen[position]:
                            duplicate_index_rows += 1
                        else:
                            seen[position] = 1
                        digest = row["canonical_sha256"]
                        hash_output.write(digest + "\n")
                        status = row["status"]
                        status_counts[status] += 1
                        if status == "timeout":
                            timeouts.append(index)
                        elif status == "non-colorable":
                            negatives[index] = row
                missing_index_rows += expected_rows - sum(seen)

        sorted_hashes, hash_metadata = sorted_hash_metadata(temporary_hashes, AUDIT_DIR)
        negative_lines = line_at_indices(SOURCE, set(negatives))
        confirmations: dict[str, dict[str, Any]] = {}
        for index, primary in sorted(negatives.items()):
            if index not in negative_lines:
                confirmations[str(index)] = {"status": "source_line_missing"}
            else:
                confirmations[str(index)] = confirm_negative(
                    negative_lines[index], primary, args.confirmation_seconds, args.confirmation_workers
                )

        changed_files = [chunk for chunk, before in initial_signatures.items() if signature(RESULT / f"chunk-{chunk}.jsonl") != before]
        scheduler_ok = all(state == "COMPLETED_LOG_EVIDENCE" for state in chunk_states.values())
        negative_ok = all(record.get("status") == "confirmed_noncolorable" for record in confirmations.values())
        acceptance_ready = (
            scheduler_ok
            and not missing_files
            and not changed_files
            and malformed_json == 0
            and invalid_rows == 0
            and rows == total_expected
            and duplicate_index_rows == 0
            and missing_index_rows == 0
            and hash_metadata["rows"] == total_expected
            and hash_metadata["duplicate_canonical_hash_count"] == 0
            and not timeouts
            and negative_ok
        )
        report: dict[str, Any] = {
            "schema_version": 1,
            "audit": "order16-6x10-strict-post-audit",
            "audit_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "range": {"first_chunk": args.first_chunk, "last_chunk": args.last_chunk, "chunks": selected},
            "contract": {
                "job_id": JOB_ID,
                "scheduler_completion_evidence": "squeue exclusion plus each worker-owned final exact summary; sacct task expansion is not trusted on this cluster",
                "expected_rows": total_expected,
                "exact_row_keys": sorted(EXACT_KEYS),
                "canonical_hash_pattern": HASH.pattern,
                "primary_solver_limit_seconds": args.solver_limit,
                "primary_solver_tolerance_seconds": args.solver_tolerance,
                "timeout_policy": "any primary timeout rejects acceptance",
                "negative_policy": "every primary negative must be independently INFEASIBLE at every legal span delta..n-1",
                "cache_policy": "accepted cache and hash sidecar are replaced only when acceptance_ready is true",
            },
            "scheduler_states": {str(chunk): chunk_states[chunk] for chunk in selected},
            "missing_files": missing_files,
            "files_changed_during_audit": changed_files,
            "rows_validated": rows,
            "malformed_json_rows": malformed_json,
            "invalid_schema_or_domain_rows": invalid_rows,
            "invalid_examples": invalid_examples,
            "duplicate_index_rows": duplicate_index_rows,
            "missing_index_rows": missing_index_rows,
            "status_counts": dict(sorted(status_counts.items())),
            "timeout_indices": timeouts[:100],
            "timeout_count": len(timeouts),
            "primary_negative_indices": sorted(negatives),
            "negative_confirmations": confirmations,
            "hashes": hash_metadata,
            "acceptance_ready": acceptance_ready,
        }
        atomic_json(report_path, report)

        if acceptance_ready:
            cache = {
                "schema_version": 1,
                "accepted_at_utc": report["audit_utc"],
                "report": str(report_path),
                "range": report["range"],
                "chunk_signatures": {str(chunk): initial_signatures[chunk] for chunk in selected},
                "hashes": hash_metadata,
            }
            atomic_json(cache_path, cache)
            os.replace(sorted_hashes, accepted_hashes_path)
        else:
            sorted_hashes.unlink(missing_ok=True)

        print(json.dumps(report, sort_keys=True))
        if not acceptance_ready:
            raise SystemExit(2)
    finally:
        temporary_hashes.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
