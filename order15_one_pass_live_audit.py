#!/usr/bin/env python3
"""One-pass point-in-time audit of live order15-7x8 result chunks."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = (
    "canonical_sha256",
    "delta",
    "index",
    "minimum_degree",
    "order",
    "size",
    "solver_seconds",
    "span",
    "status",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="order15-7x8")
    parser.add_argument("--input", default="data/order15-7x8-d2.g6")
    parser.add_argument("--expected", type=int, default=243_304_742)
    parser.add_argument("--sort-memory", default="8G")
    parser.add_argument("--issue-limit", type=int, default=100)
    parser.add_argument("--candidate-limit", type=int, default=1000)
    parser.add_argument("--negative-solver-time-limit", type=float, default=300.0)
    parser.add_argument("--negative-solver-workers", type=int, default=4)
    parser.add_argument("--rerun-time-limit", type=float, default=3600.0)
    parser.add_argument("--rerun-workers", type=int, default=4)
    parser.add_argument("--timeout-command-preview", type=int, default=20)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def chunk_key(path: Path) -> tuple[int, str]:
    match = re.fullmatch(r"chunk-(\d+)\.jsonl", path.name)
    return (int(match.group(1)) if match else 10**18, path.name)


def merge_ranges(values: list[int]) -> list[list[int]]:
    if not values:
        return []
    ordered = sorted(set(values))
    ranges: list[list[int]] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append([start, previous])
        start = previous = value
    ranges.append([start, previous])
    return ranges


def utc_from_ns(value: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value / 1e9))


def add_invalid(
    examples: list[dict[str, Any]], limit: int, filename: str, line_number: int, error: str
) -> None:
    if len(examples) < limit:
        examples.append({"file": filename, "line_number": line_number, "error": error})


def scan_once(
    chunk_paths: list[Path],
    work_dir: Path,
    issue_limit: int,
    candidate_limit: int,
) -> dict[str, Any]:
    hash_path = work_dir / "canonical-hashes.txt"
    index_path = work_dir / "indices.txt"
    rows = 0
    total_lines = 0
    malformed_rows = 0
    truncated_final_rows = 0
    missing_field_counts: Counter[str] = Counter()
    invalid_type_counts: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    invalid_examples: list[dict[str, Any]] = []
    negative_examples: list[dict[str, Any]] = []
    negative_count = 0
    timeout_indices: set[int] = set()
    first_valid_row: dict[str, Any] | None = None
    first_valid_fields: list[str] = []
    minimum_index: int | None = None
    maximum_index: int | None = None
    file_inventory: list[dict[str, Any]] = []

    with hash_path.open("w", encoding="ascii") as hash_out, index_path.open(
        "w", encoding="ascii"
    ) as index_out:
        for path in chunk_paths:
            stat_before = path.stat()
            remaining = stat_before.st_size
            line_number = 0
            partial = b""
            with path.open("rb") as handle:
                while remaining > 0:
                    block = handle.read(min(1 << 20, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    parts = (partial + block).split(b"\n")
                    partial = parts.pop()
                    for raw in parts:
                        line_number += 1
                        total_lines += 1
                        outcome = classify_row(raw, missing_field_counts, invalid_type_counts)
                        if outcome is None:
                            malformed_rows += 1
                            add_invalid(
                                invalid_examples,
                                issue_limit,
                                path.name,
                                line_number,
                                "invalid JSON/blank/non-object row",
                            )
                            continue
                        rows += 1
                        valid = outcome
                        if first_valid_row is None:
                            first_valid_row = valid
                            first_valid_fields = list(valid)
                        status = valid.get("status")
                        statuses[status if isinstance(status, str) else repr(status)] += 1
                        canonical = valid.get("canonical_sha256")
                        if isinstance(canonical, str) and canonical:
                            hash_out.write(canonical + "\n")
                        index = valid.get("index")
                        if isinstance(index, int) and not isinstance(index, bool):
                            index_out.write(f"{index}\n")
                            minimum_index = index if minimum_index is None else min(minimum_index, index)
                            maximum_index = index if maximum_index is None else max(maximum_index, index)
                        if status == "timeout" and isinstance(index, int):
                            timeout_indices.add(index)
                        if status == "non-colorable":
                            negative_count += 1
                            if len(negative_examples) < candidate_limit:
                                negative_examples.append(
                                    {
                                        "index": valid.get("index"),
                                        "canonical_sha256": canonical,
                                        "file": path.name,
                                        "line_number": line_number,
                                    }
                                )
            if partial.strip():
                truncated_final_rows += 1
                total_lines += 1
                malformed_rows += 1
                add_invalid(
                    invalid_examples,
                    issue_limit,
                    path.name,
                    line_number + 1,
                    "incomplete final line in byte-limited snapshot",
                )
            stat_after = path.stat()
            file_inventory.append(
                {
                    "file": path.name,
                    "snapshot_bytes": stat_before.st_size,
                    "end_bytes": stat_after.st_size,
                    "changed_during_scan": stat_after.st_size != stat_before.st_size,
                    "mtime_utc_before": utc_from_ns(stat_before.st_mtime_ns),
                }
            )

    return {
        "rows": rows,
        "total_lines": total_lines,
        "malformed_rows": malformed_rows,
        "truncated_final_rows": truncated_final_rows,
        "missing_field_counts": missing_field_counts,
        "invalid_type_counts": invalid_type_counts,
        "statuses": statuses,
        "invalid_examples": invalid_examples,
        "negative_count": negative_count,
        "negative_examples": negative_examples,
        "timeout_indices": timeout_indices,
        "first_valid_row": first_valid_row,
        "first_valid_fields": first_valid_fields,
        "minimum_index": minimum_index,
        "maximum_index": maximum_index,
        "file_inventory": file_inventory,
        "hash_path": hash_path,
        "index_path": index_path,
    }


def classify_row(
    raw: bytes, missing_field_counts: Counter[str], invalid_type_counts: Counter[str]
) -> dict[str, Any] | None:
    if not raw.strip():
        return None
    try:
        row = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(row, dict):
        return None
    for field in REQUIRED_FIELDS:
        if field not in row:
            missing_field_counts[field] += 1
    expected_types = {
        "canonical_sha256": str,
        "delta": int,
        "index": int,
        "minimum_degree": int,
        "order": int,
        "size": int,
        "solver_seconds": (int, float),
        "status": str,
    }
    for field, expected in expected_types.items():
        if field in row and (not isinstance(row[field], expected) or isinstance(row[field], bool)):
            invalid_type_counts[field] += 1
    if "span" in row and not (row["span"] is None or isinstance(row["span"], int)):
        invalid_type_counts["span"] += 1
    return row


def sorted_duplicates(
    source: Path,
    work_dir: Path,
    stem: str,
    sort_memory: str,
    example_limit: int,
) -> dict[str, Any]:
    sorted_path = work_dir / f"{stem}-sorted.txt"
    env = dict(os.environ, LC_ALL="C")
    with sorted_path.open("w", encoding="ascii") as out:
        subprocess.run(
            ["sort", "-S", sort_memory, "-T", str(work_dir), str(source)],
            check=True,
            env=env,
            stdout=out,
        )
    unique_values = 0
    duplicate_values = 0
    duplicate_rows = 0
    duplicate_examples: list[dict[str, Any]] = []
    previous: str | None = None
    occurrences = 0
    with sorted_path.open("r", encoding="ascii") as handle:
        for line in handle:
            value = line.rstrip("\n")
            if value == previous:
                occurrences += 1
                continue
            if previous is not None:
                unique_values += 1
                if occurrences > 1:
                    duplicate_values += 1
                    duplicate_rows += occurrences - 1
                    if len(duplicate_examples) < example_limit:
                        duplicate_examples.append({"value": previous, "occurrences": occurrences})
            previous = value
            occurrences = 1
    if previous is not None:
        unique_values += 1
        if occurrences > 1:
            duplicate_values += 1
            duplicate_rows += occurrences - 1
            if len(duplicate_examples) < example_limit:
                duplicate_examples.append({"value": previous, "occurrences": occurrences})
    sorted_path.unlink()
    return {
        "unique_values": unique_values,
        "duplicate_values": duplicate_values,
        "duplicate_rows": duplicate_rows,
        "duplicate_examples": duplicate_examples,
    }


def load_graph_at_index(input_path: Path, index: int):
    from interval_edge_coloring import Graph, from_graph6, nauty_canonical_hash

    with input_path.open(encoding="ascii") as handle:
        for number, line in enumerate(handle):
            if number == index:
                count, raw_edges = from_graph6(line.rstrip("\n"))
                names = [f"V{i}" for i in range(count)]
                graph = Graph(names, [(names[i], names[j]) for i, j in raw_edges])
                return line.rstrip("\n"), graph, nauty_canonical_hash(graph)
    raise IndexError(f"index {index} not found in {input_path}")


def verify_negative(
    row: dict[str, Any],
    input_path: Path,
    certificate_dir: Path,
    dataset: str,
    time_limit: float,
    workers: int,
) -> dict[str, Any]:
    from interval_edge_coloring import all_spans_solve

    index = int(row["index"])
    source_line, graph, canonical_hash = load_graph_at_index(input_path, index)
    if canonical_hash != row.get("canonical_sha256"):
        raise RuntimeError(
            f"canonical hash mismatch at index {index}: row={row.get('canonical_sha256')} input={canonical_hash}"
        )
    result = all_spans_solve(
        graph,
        time_limit_per_span=time_limit,
        workers=workers,
        stop_on_timeout=False,
    )
    confirmed = result["decision"] == "non-colorable"
    certificate_path = certificate_dir / f"{dataset}-index-{index:09d}.graph.json"
    if confirmed:
        certificate_dir.mkdir(parents=True, exist_ok=True)
        graph.save(certificate_path)
    return {
        "index": index,
        "source_line": source_line,
        "canonical_sha256": canonical_hash,
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "encoding": result["encoding"],
        "independent_decision": result["decision"],
        "spans": {
            span: {"solver_status": summary["status"]}
            for span, summary in result["spans"].items()
        },
        "certificate_ready": confirmed,
        "certificate_graph_json": str(certificate_path) if confirmed else None,
        "all_legal_spans_checked": True,
    }


def rerun_plan(
    timeout_indices: set[int],
    dataset: str,
    input_path: str,
    rerun_time_limit: float,
    workers: int,
    command_preview: int,
) -> dict[str, Any]:
    existing_outputs: list[int] = []
    pending_indices: list[int] = []
    rerun_root = Path("results") / f"{dataset}-reruns"
    for index in sorted(timeout_indices):
        output = rerun_root / f"index-{index:09d}.json"
        (existing_outputs if output.is_file() else pending_indices).append(index)
    pending_ranges = merge_ranges(pending_indices)
    preview = [
        "PYTHONPATH=pydeps .venv/bin/python src/rerun_graph6_index.py "
        f"{input_path} {index} --time-limit {rerun_time_limit:g} --workers {workers} "
        f"--output results/{dataset}-reruns/index-{index:09d}.json"
        for index in pending_indices[:command_preview]
    ]
    range_specs = " ".join(f"{start}:{end}" for start, end in pending_ranges)
    locked_script = ["# Do not run automatically; each lock prevents a repeated start."]
    if range_specs:
        locked_script.extend(
            [
                f"mkdir -p results/{dataset}-reruns/.locks",
                f"for range_spec in {range_specs}; do",
                '  IFS=: read -r start end <<<"$range_spec"',
                '  for ((idx=start; idx<=end; idx++)); do',
                f"  out=results/{dataset}-reruns/index-$(printf '%09d' \"$idx\").json",
                f"  lock=results/{dataset}-reruns/.locks/index-$(printf '%09d' \"$idx\").lock",
                '  mkdir "$lock" 2>/dev/null || continue',
                f"  PYTHONPATH=pydeps .venv/bin/python src/rerun_graph6_index.py {input_path} \"$idx\" "
                f"--time-limit {rerun_time_limit:g} --workers {workers} --output \"$out\"",
                "  done",
                "done",
            ]
        )
    return {
        "timeout_indices_count": len(timeout_indices),
        "pending_rerun_indices_count": len(pending_indices),
        "already_have_output_indices_count": len(existing_outputs),
        "pending_timeout_index_ranges": pending_ranges,
        "already_have_timeout_output_ranges": merge_ranges(existing_outputs),
        "exact_rerun_command_preview": preview,
        "locked_exact_rerun_script_lines": locked_script,
        "note": "Timeouts are unresolved rather than non-colorable; locks prevent duplicate starts.",
    }


def collect_negative_candidates(
    results_dir: Path, file_inventory: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    candidates_by_index: dict[int, dict[str, Any]] = {}
    for inventory in file_inventory:
        path = results_dir / inventory["file"]
        remaining = inventory["snapshot_bytes"]
        partial = b""
        line_number = 0
        with path.open("rb") as handle:
            while remaining > 0:
                block = handle.read(min(1 << 20, remaining))
                if not block:
                    break
                remaining -= len(block)
                parts = (partial + block).split(b"\n")
                partial = parts.pop()
                for raw in parts:
                    line_number += 1
                    try:
                        row = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(row, dict) and row.get("status") == "non-colorable":
                        index = row.get("index")
                        if isinstance(index, int) and index not in candidates_by_index:
                            candidates_by_index[index] = {
                                **row,
                                "_file": path.name,
                                "_line_number": line_number,
                            }
    return sorted(candidates_by_index.values(), key=lambda item: item["index"])


def main() -> None:
    args = parse_args()
    started = time.time()
    results_dir = Path("results") / args.dataset
    input_path = Path(args.input)
    chunk_paths = sorted(results_dir.glob("chunk-*.jsonl"), key=chunk_key)
    if not chunk_paths:
        raise RuntimeError(f"No chunks found under {results_dir}")
    work_root = Path("results") / ".order15-audit-work"
    work_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="live-", dir=work_root))
    try:
        scan = scan_once(chunk_paths, work_dir, args.issue_limit, args.candidate_limit)
        hash_audit = sorted_duplicates(
            scan["hash_path"],
            work_dir,
            "canonical-hashes",
            args.sort_memory,
            args.issue_limit,
        )
        index_audit = sorted_duplicates(
            scan["index_path"],
            work_dir,
            "indices",
            args.sort_memory,
            args.issue_limit,
        )

        verified_negatives: list[dict[str, Any]] = []
        confirmed_negatives: list[dict[str, Any]] = []
        if scan["negative_count"]:
            certificate_dir = Path("results") / f"{args.dataset}-negative-certificates"
            candidates = collect_negative_candidates(results_dir, scan["file_inventory"])
            for row in candidates:
                verified = verify_negative(
                    row,
                    input_path,
                    certificate_dir,
                    args.dataset,
                    args.negative_solver_time_limit,
                    args.negative_solver_workers,
                )
                verified_negatives.append(verified)
                if verified["certificate_ready"]:
                    confirmed_negatives.append(
                        {
                            "index": verified["index"],
                            "canonical_sha256": verified["canonical_sha256"],
                            "certificate_graph_json": verified["certificate_graph_json"],
                        }
                    )

        expected = args.expected
        report = {
            "schema_version": 2,
            "audit_kind": "one_pass_live_snapshot",
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "dataset": args.dataset,
            "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
            "expected_records": expected,
            "scanned_files": len(chunk_paths),
            "rows": scan["rows"],
            "progress_fraction": scan["rows"] / expected if expected else None,
            "remaining_records": max(0, expected - scan["rows"]),
            "minimum_zero_based_index": scan["minimum_index"],
            "maximum_zero_based_index": scan["maximum_index"],
            "observed_range_missing_indices_count": (
                max(0, (scan["maximum_index"] - scan["minimum_index"] + 1) - index_audit["unique_values"])
                if scan["minimum_index"] is not None
                else None
            ),
            "unique_zero_based_indices": index_audit["unique_values"],
            "duplicate_index_values": index_audit["duplicate_values"],
            "duplicate_index_rows": index_audit["duplicate_rows"],
            "duplicate_index_examples": index_audit["duplicate_examples"],
            "unique_canonical_hashes": hash_audit["unique_values"],
            "duplicate_canonical_hash_values": hash_audit["duplicate_values"],
            "duplicate_hash_rows": hash_audit["duplicate_rows"],
            "duplicate_hash_examples": hash_audit["duplicate_examples"],
            "status_histogram": dict(sorted(scan["statuses"].items(), key=lambda item: str(item[0]))),
            "malformed_json_rows": scan["malformed_rows"],
            "total_nonblank_lines_seen": scan["total_lines"],
            "incomplete_snapshot_final_rows": scan["truncated_final_rows"],
            "missing_field_counts": dict(sorted(scan["missing_field_counts"].items())),
            "field_type_mismatch_counts": dict(sorted(scan["invalid_type_counts"].items())),
            "invalid_row_examples": scan["invalid_examples"],
            "negative_classified_rows": scan["negative_count"],
            "negative_candidate_examples": scan["negative_examples"],
            "verified_negative_details": verified_negatives,
            "confirmed_negative_certificates": confirmed_negatives,
            "timeout_rows": scan["statuses"].get("timeout", 0),
            "unique_timeout_indices": len(scan["timeout_indices"]),
            "rerun_plan": rerun_plan(
                scan["timeout_indices"],
                args.dataset,
                str(input_path),
                args.rerun_time_limit,
                args.rerun_workers,
                args.timeout_command_preview,
            ),
            "observed_schema_fields": scan["first_valid_fields"],
            "first_valid_record": scan["first_valid_row"],
            "chunk_file_inventory": scan["file_inventory"],
            "method": {
                "chunk_passes": 1,
                "byte_scope": "pre-open size of each live chunk",
                "hash_and_index_duplicate_detection": "external disk sort -u",
                "negative_encoding": "fixed-span CP-SAT over delta through n-1",
            },
            "parameters": {
                "input": str(input_path),
                "external_sort_memory": args.sort_memory,
                "negative_solver_time_limit_per_span": args.negative_solver_time_limit,
                "negative_solver_workers": args.negative_solver_workers,
                "rerun_time_limit": args.rerun_time_limit,
                "rerun_workers": args.rerun_workers,
            },
            "audit_elapsed_seconds": round(time.time() - started, 3),
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        output.write_text(rendered, encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(output),
                    "bytes": output.stat().st_size,
                    "rows": scan["rows"],
                    "files": len(chunk_paths),
                    "malformed": scan["malformed_rows"],
                    "duplicate_index_rows": index_audit["duplicate_rows"],
                    "duplicate_hash_rows": hash_audit["duplicate_rows"],
                    "negatives": scan["negative_count"],
                    "timeouts": len(scan["timeout_indices"]),
                },
                sort_keys=True,
            )
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
