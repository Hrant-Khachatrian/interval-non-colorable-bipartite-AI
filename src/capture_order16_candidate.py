#!/usr/bin/env python3
"""Capture and verify non-colorable candidates from order-16 chunk results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    from_graph6,
    nauty_canonical_hash,
    rank_potential_solve,
    to_graph6,
    verify_coloring,
)


REMOTE = "hrant@cluster.ysu.am"
REMOTE_ROOT = "/mnt/weka/hrant/interval-search"
NEGATIVE_STATUSES = frozenset({"non-colorable", "non_colorable"})
ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = ROOT / "results" / "candidates"


class CaptureError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


REMOTE_SCAN_PYTHON = r'''
import fnmatch
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

dataset, pattern, limit_text = sys.argv[1:]
chunk_limit = None if limit_text == "None" else int(limit_text)
directory = os.path.join("results", dataset)
if not os.path.isdir(directory):
    raise SystemExit("missing remote directory: " + directory)

files = [name for name in os.listdir(directory) if fnmatch.fnmatch(name, pattern)]
chunk_re = re.compile(r"chunk-(\d+)\.jsonl")

def chunk_key(name):
    match = chunk_re.fullmatch(name)
    return (0, int(match.group(1)), name) if match else (1, name, "")

files.sort(key=chunk_key)
if chunk_limit is not None:
    if chunk_limit < 0:
        raise SystemExit("chunk limit cannot be negative")
    files = files[:chunk_limit]
if not files:
    raise SystemExit("no remote chunks match: " + repr(pattern))

for filename in files:
    path = os.path.join(directory, filename)
    before = os.stat(path)
    remaining = before.st_size
    partial = b""
    line_number = 0
    rows = 0
    malformed = 0
    timeout_rows = 0
    examples = []
    histogram = Counter()
    with open(path, "rb") as handle:
        while remaining:
            block = handle.read(min(1 << 20, remaining))
            if not block:
                break
            remaining -= len(block)
            pieces = (partial + block).split(b"\n")
            partial = pieces.pop()
            for raw in pieces:
                line_number += 1
                if not raw.strip():
                    malformed += 1
                    if len(examples) < 5:
                        examples.append({"line": line_number, "reason": "blank row"})
                    continue
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    malformed += 1
                    if len(examples) < 5:
                        examples.append({"line": line_number, "reason": str(exc)[:500]})
                    continue
                if not isinstance(row, dict):
                    malformed += 1
                    if len(examples) < 5:
                        examples.append({"line": line_number, "reason": "row is not an object"})
                    continue
                index = row.get("index")
                canonical = row.get("canonical_sha256")
                status = row.get("status")
                required_ok = (
                    isinstance(index, int)
                    and not isinstance(index, bool)
                    and index >= 0
                    and isinstance(canonical, str)
                    and bool(canonical)
                    and isinstance(status, str)
                    and bool(status)
                )
                if not required_ok:
                    malformed += 1
                    if len(examples) < 5:
                        examples.append({
                            "line": line_number,
                            "reason": "missing or invalid index/hash/status",
                        })
                    continue
                rows += 1
                histogram[status] += 1
                if status == "timeout":
                    timeout_rows += 1
                elif status in ("non-colorable", "non_colorable"):
                    record = {
                        "canonical_sha256": canonical,
                        "delta": row.get("delta"),
                        "file": filename,
                        "index": index,
                        "line_number": line_number,
                        "minimum_degree": row.get("minimum_degree"),
                        "order": row.get("order"),
                        "size": row.get("size"),
                        "solver_seconds": row.get("solver_seconds"),
                        "span": row.get("span"),
                        "status": status,
                    }
                    print("C\t" + json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)
    if partial.strip():
        malformed += 1
        examples.append({
            "line": line_number + 1,
            "reason": "incomplete final line at snapshot boundary",
        })
    after = os.stat(path)
    inventory = {
        "changed_during_scan": (
            after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns
        ),
        "end_bytes": after.st_size,
        "file": filename,
        "malformed_examples": examples,
        "malformed_rows": malformed,
        "mtime_utc": datetime.fromtimestamp(
            before.st_mtime_ns / 1e9, timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "rows": rows,
        "snapshot_bytes": before.st_size,
        "status_histogram": dict(sorted(histogram.items())),
        "timeout_rows": timeout_rows,
    }
    print("S\t" + json.dumps(inventory, sort_keys=True, separators=(",", ":")), flush=True)
'''


def python_over_ssh(code: str, arguments: list[str]) -> str:
    rendered_args = " ".join(shlex.quote(value) for value in arguments)
    script = (
        "set -euo pipefail\n"
        f"cd {shlex.quote(REMOTE_ROOT)}\n"
        f"python3 - {rendered_args} <<'PY_CAPTURE_BOUNDARY'\n"
        f"{code.rstrip()}\n"
        "PY_CAPTURE_BOUNDARY\n"
    )
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errors:
        process = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", REMOTE, "bash -s"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
            bufsize=1 << 20,
        )
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(script)
            process.stdin.close()
        except BrokenPipeError:
            pass
        output = "".join(process.stdout)
        return_code = process.wait()
        errors.seek(0)
        stderr = errors.read()
    if return_code != 0:
        detail_lines = stderr.strip().splitlines()
        detail = detail_lines[-1] if detail_lines else f"exit code {return_code}"
        raise CaptureError(f"remote operation failed: {detail}")
    return output


def scan_snapshot(dataset: str, chunk_glob: str, chunk_limit: int | None) -> dict[str, Any]:
    output = python_over_ssh(
        REMOTE_SCAN_PYTHON,
        [dataset, chunk_glob, "None" if chunk_limit is None else str(chunk_limit)],
    )
    inventories = []
    candidate_records = []
    unexpected: Counter[str] = Counter()
    for raw in output.splitlines():
        kind, separator, payload = raw.partition("\t")
        if not separator:
            unexpected["unstructured"] += 1
            continue
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            unexpected["bad-" + kind] += 1
            continue
        if kind == "S" and isinstance(value, dict):
            inventories.append(value)
        elif kind == "C" and isinstance(value, dict):
            candidate_records.append(value)
        else:
            unexpected[kind] += 1
    if unexpected:
        raise CaptureError(f"unexpected remote scan records: {dict(unexpected)}")

    statuses: Counter[str] = Counter()
    for item in inventories:
        statuses.update(item["status_histogram"])
    return {
        "candidate_records": candidate_records,
        "changed_files": sorted(
            item["file"] for item in inventories if item["changed_during_scan"]
        ),
        "files_scanned": len(inventories),
        "file_inventory": inventories,
        "malformed_rows": sum(item["malformed_rows"] for item in inventories),
        "rows_scanned": sum(item["rows"] for item in inventories),
        "status_histogram": dict(sorted(statuses.items())),
        "timeout_rows": sum(item["timeout_rows"] for item in inventories),
    }


def snapshot_fingerprint(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    inventory = tuple(
        (
            item["file"],
            item["snapshot_bytes"],
            item["mtime_utc"],
            item["changed_during_scan"],
        )
        for item in snapshot["file_inventory"]
    )
    candidates = tuple(sorted(
        (record["index"], record["canonical_sha256"], record["status"])
        for record in snapshot["candidate_records"]
    ))
    core = (
        snapshot["files_scanned"],
        snapshot["rows_scanned"],
        snapshot["malformed_rows"],
        tuple(snapshot["status_histogram"].items()),
        snapshot["timeout_rows"],
    )
    return inventory, candidates, core


def stable_snapshot(args: argparse.Namespace) -> tuple[dict[str, Any], float]:
    scans = 0
    stable = 0
    previous: tuple[Any, ...] | None = None
    latest = None
    started = time.monotonic()
    while True:
        latest = scan_snapshot(args.dataset, args.chunk_glob, args.chunk_limit)
        scans += 1
        current = snapshot_fingerprint(latest)
        stable = stable + 1 if current == previous else 1
        previous = current
        reached_expected = args.expected is not None and latest["rows_scanned"] == args.expected
        unchanged = not latest["changed_files"]
        if not args.live or (stable >= args.stable_scans and unchanged):
            break
        if reached_expected and unchanged:
            break
        if args.deadline_seconds is not None and time.monotonic() - started >= args.deadline_seconds:
            assert latest is not None
            latest["polling_stopped"] = "deadline-reached"
            break
        time.sleep(args.poll_seconds)
    assert latest is not None
    latest["scan_count"] = scans
    latest["stable_scans_observed"] = stable
    return latest, time.monotonic() - started


def deduplicate_candidates(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    index_hashes: dict[int, set[str]] = {}
    hash_indexes: dict[str, set[int]] = {}
    duplicate_key_rows = 0
    inconsistent_duplicate_keys = 0
    errors: list[str] = []
    comparison_fields = ("status", "order", "size", "delta", "minimum_degree", "span")

    for record in records:
        key = (record["index"], record["canonical_sha256"])
        index_hashes.setdefault(key[0], set()).add(key[1])
        hash_indexes.setdefault(key[1], set()).add(key[0])
        if key in by_key:
            duplicate_key_rows += 1
            existing = by_key[key]
            existing["occurrences"] += 1
            location = {"file": record["file"], "line_number": record["line_number"]}
            if len(existing["source_locations"]) < 100:
                existing["source_locations"].append(location)
            if any(existing["row"].get(field) != record.get(field) for field in comparison_fields):
                inconsistent_duplicate_keys += 1
                if len(errors) < 50:
                    errors.append(
                        f"inconsistent duplicate at index {key[0]} hash {key[1]}"
                    )
            continue
        by_key[key] = {
            "key": {"canonical_sha256": key[1], "index": key[0]},
            "occurrences": 1,
            "row": record,
            "source_locations": [
                {"file": record["file"], "line_number": record["line_number"]}
            ],
        }

    for index, hashes in sorted(index_hashes.items()):
        if len(hashes) > 1:
            errors.append(f"index {index} has {len(hashes)} distinct canonical hashes")
    for canonical, indexes in sorted(hash_indexes.items()):
        if len(indexes) > 1:
            preview = canonical[:16]
            errors.append(
                f"canonical hash {preview}... occurs at indices {sorted(indexes)}"
            )

    candidates = sorted(
        by_key.values(),
        key=lambda item: (item["key"]["index"], item["key"]["canonical_sha256"]),
    )
    summary = {
        "duplicate_hash_count": sum(len(x) > 1 for x in hash_indexes.values()),
        "duplicate_index_count": sum(len(x) > 1 for x in index_hashes.values()),
        "duplicate_key_rows": duplicate_key_rows,
        "inconsistent_duplicate_keys": inconsistent_duplicate_keys,
        "negative_rows": len(records),
        "unique_candidate_keys": len(candidates),
    }
    return candidates, summary, errors


REMOTE_FETCH_PYTHON = r'''
import hashlib
import json
import os
import sys

relative = sys.argv[1]
path = os.path.realpath(relative)
root = os.path.realpath(os.getcwd())
if os.path.commonpath([path, root]) != root:
    raise SystemExit("input path escapes the remote checkout")
if not os.path.isfile(path):
    raise SystemExit("missing graph6 input: " + relative)
wanted = set(json.loads(sys.argv[2]))
if not wanted:
    raise SystemExit("no requested indices")

payload = bytearray()
with open(path, "rb") as handle:
    for block in iter(lambda: handle.read(1 << 20), b""):
        payload.extend(block)
lines = payload.splitlines()
found = wanted.intersection(range(len(lines)))
if found != wanted:
    missing = sorted(wanted - found)
    raise SystemExit(
        f"input has {len(lines)} lines; missing indices: {missing[:20]}"
    )

metadata = {
    "bytes": len(payload),
    "line_count": len(lines),
    "path": relative,
    "sha256": hashlib.sha256(payload).hexdigest(),
}
print("SOURCE\t" + json.dumps(metadata, sort_keys=True, separators=(",", ":")))
for index in sorted(wanted):
    text = lines[index].decode("ascii").strip()
    print("GRAPH6\t" + json.dumps([index, text], separators=(",", ":")))
'''


def fetch_graph6_lines(
    input_path: str,
    indexes: list[int],
) -> tuple[dict[str, Any], dict[int, str]]:
    if not indexes:
        raise CaptureError("cannot fetch an empty index set")
    output = python_over_ssh(
        REMOTE_FETCH_PYTHON,
        [input_path, json.dumps(sorted(indexes), separators=(",", ":"))],
    )
    source = None
    graphs: dict[int, str] = {}
    for raw in output.splitlines():
        kind, separator, payload = raw.partition("\t")
        if not separator:
            raise CaptureError("unstructured remote graph6 output")
        value = json.loads(payload)
        if kind == "SOURCE" and isinstance(value, dict):
            if source is not None:
                raise CaptureError("remote graph6 source metadata was repeated")
            source = value
        elif kind == "GRAPH6":
            index, text = value
            index = int(index)
            if index in graphs:
                raise CaptureError(f"duplicate fetched graph6 index {index}")
            graphs[index] = str(text)
        else:
            raise CaptureError(f"unexpected remote graph6 record {kind}")
    if source is None or set(graphs) != set(indexes):
        raise CaptureError("remote graph6 fetch was incomplete")
    return source, graphs


def expected_side_sizes(dataset: str) -> list[int] | None:
    match = re.fullmatch(r"order16-(\d+)x(\d+)", dataset)
    if not match:
        return None
    return sorted((int(match.group(1)), int(match.group(2))))


def connected(graph: Graph) -> bool:
    adjacency = graph.adjacency()
    start = graph.vertices[0]
    seen = {start}
    pending = [start]
    while pending:
        vertex = pending.pop()
        for neighbor, _ in adjacency[vertex]:
            if neighbor not in seen:
                seen.add(neighbor)
                pending.append(neighbor)
    return len(seen) == graph.n


def check_invariants(
    graph: Graph,
    graph6: str,
    source_row: dict[str, Any],
    dataset: str,
) -> tuple[bool, dict[str, Any]]:
    checks: dict[str, Any] = {}
    degrees = graph.degrees
    minimum_degree = min(degrees.values(), default=0)
    side_sizes = sorted((len(graph.bipartition[0]), len(graph.bipartition[1])))
    labelled_hash = graph.canonical_hash()
    canonical_hash = nauty_canonical_hash(graph)
    round_trip = to_graph6(graph.vertices, graph.edges).strip()

    checks["simple"] = (
        graph.n == len(set(graph.vertices))
        and graph.m == len({tuple(sorted(edge)) for edge in graph.edges})
        and all(u != v for u, v in graph.edges)
    )
    checks["connected"] = connected(graph)
    checks["bipartite"] = True
    checks["order_16"] = graph.n == 16
    checks["side_sizes_match_dataset"] = expected_side_sizes(dataset) in (
        None,
        side_sizes,
    )
    checks["minimum_degree_at_least_2"] = minimum_degree >= 2
    checks["stored_order_matches"] = source_row.get("order") == graph.n
    checks["stored_size_matches"] = source_row.get("size") == graph.m
    checks["stored_delta_matches"] = source_row.get("delta") == graph.delta
    checks["stored_minimum_degree_matches"] = (
        source_row.get("minimum_degree") == minimum_degree
    )
    checks["canonical_hash_matches"] = (
        source_row.get("canonical_sha256") == canonical_hash
    )
    checks["graph6_round_trip_matches"] = round_trip == graph6
    checks["side_sizes_from_reconstruction"] = side_sizes
    checks["expected_side_sizes"] = expected_side_sizes(dataset)
    checks["degrees"] = degrees
    checks["delta"] = graph.delta
    checks["minimum_degree"] = minimum_degree
    checks["order"] = graph.n
    checks["size"] = graph.m
    checks["labelled_sha256"] = labelled_hash
    checks["bipartition_canonical_sha256"] = canonical_hash
    explicit_pass_keys = (
        "simple",
        "connected",
        "bipartite",
        "order_16",
        "side_sizes_match_dataset",
        "minimum_degree_at_least_2",
        "stored_order_matches",
        "stored_size_matches",
        "stored_delta_matches",
        "stored_minimum_degree_matches",
        "canonical_hash_matches",
        "graph6_round_trip_matches",
    )
    passed = all(checks[key] is True for key in explicit_pass_keys)
    return passed, checks


def serialize_solve_result(result: Any) -> dict[str, Any]:
    return {
        "status": result.status,
        "encoding": result.encoding,
        "solver_status": result.solver_status,
        "witness_span": result.span,
        "elapsed_seconds": round(float(result.elapsed_seconds), 6),
    }


def rank_check(
    graph: Graph,
    time_limit: float,
    workers: int,
) -> tuple[dict[str, Any], bool]:
    result = rank_potential_solve(graph, time_limit, workers)
    report = serialize_solve_result(result)
    if result.status == "colorable":
        assert result.coloring is not None
        ok, reason = verify_coloring(graph, dict(result.coloring))
        report["witness_verified"] = ok
        report["witness_reason"] = reason
        if not ok:
            raise CaptureError(f"rank-potential witness failed verification: {reason}")
        return report, False
    if result.status == "non-colorable":
        return report, True
    report["conclusive"] = False
    return report, False


def fixed_span_checks(
    graph: Graph,
    time_limit: float,
    workers: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = []
    satisfiable_spans = []
    inconclusive_spans = []
    legal_spans = list(range(graph.delta, graph.n))
    started = time.monotonic()
    for span in legal_spans:
        check_started = time.monotonic()
        status, coloring = fixed_span_sat_solve(graph, span, time_limit, workers)
        row: dict[str, Any] = {
            "span": span,
            "cp_sat_status": status,
            "elapsed_seconds": round(time.monotonic() - check_started, 6),
        }
        if status == "satisfiable":
            if coloring is None:
                raise CaptureError(f"fixed-span {span} returned no coloring")
            ok, reason = verify_coloring(graph, dict(coloring))
            row["witness_verified"] = ok
            row["witness_reason"] = reason
            if not ok:
                raise CaptureError(f"fixed-span {span} witness failed verification: {reason}")
            satisfiable_spans.append(span)
        elif status == "unsatisfiable":
            row["conclusive"] = True
        else:
            row["conclusive"] = False
            inconclusive_spans.append(span)
        rows.append(row)
    summary = {
        "all_legal_spans_checked": len(rows) == len(legal_spans),
        "all_infeasible": bool(rows) and all(row["cp_sat_status"] == "unsatisfiable" for row in rows),
        "inconclusive_spans": inconclusive_spans,
        "legal_spans": legal_spans,
        "satisfiable_spans": satisfiable_spans,
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }
    return rows, summary


def reduced_graph(
    graph: Graph,
    delete_edge: tuple[str, str] | None = None,
    delete_vertex: str | None = None,
) -> Graph:
    vertices = tuple(v for v in graph.vertices if v != delete_vertex)
    edges = [
        edge
        for edge in graph.edges
        if (delete_edge is None or tuple(sorted(edge)) != tuple(sorted(delete_edge)))
        and (delete_vertex is None or delete_vertex not in edge)
    ]
    left = [v for v in graph.bipartition[0] if v != delete_vertex]
    right = [v for v in graph.bipartition[1] if v != delete_vertex]
    return Graph(vertices, edges, [left, right])


def edge_deletion_check(
    graph: Graph,
    edge: tuple[str, str],
    time_limit: float,
    workers: int,
) -> dict[str, Any]:
    started = time.monotonic()
    reduced = reduced_graph(graph, delete_edge=edge)
    result = rank_potential_solve(reduced, time_limit, workers)
    report: dict[str, Any] = {
        "deleted_edge": sorted(edge),
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "order": reduced.n,
        "size": reduced.m,
        "status": result.status,
    }
    if result.status == "colorable":
        assert result.coloring is not None
        ok, reason = verify_coloring(reduced, dict(result.coloring))
        report.update({"coloring_verified": ok, "reason": reason})
        if not ok:
            raise CaptureError(f"edge-deletion witness failed for {edge}: {reason}")
    return report


def vertex_deletion_check(
    graph: Graph,
    vertex: str,
    time_limit: float,
    workers: int,
) -> dict[str, Any]:
    started = time.monotonic()
    reduced = reduced_graph(graph, delete_vertex=vertex)
    result = rank_potential_solve(reduced, time_limit, workers)
    report = {
        "deleted_vertex": vertex,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "order": reduced.n,
        "size": reduced.m,
        "status": result.status,
    }
    if result.status == "colorable":
        assert result.coloring is not None
        ok, reason = verify_coloring(reduced, dict(result.coloring))
        report.update({"coloring_verified": ok, "reason": reason})
        if not ok:
            raise CaptureError(f"vertex-deletion witness failed for {vertex}: {reason}")
    return report


def deletion_checks(
    graph: Graph,
    time_limit: float,
    workers: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    edges_started = time.monotonic()
    edge_rows = []
    for edge in sorted(map(tuple, graph.edges)):
        edge_rows.append(edge_deletion_check(graph, edge, time_limit, workers))
    vertices_started = time.monotonic()
    vertex_rows = []
    for vertex in graph.vertices:
        vertex_rows.append(vertex_deletion_check(graph, vertex, time_limit, workers))

    edge_complete = all(row["status"] in {"colorable", "non-colorable"} for row in edge_rows)
    vertex_complete = all(
        row["status"] in {"colorable", "non-colorable"} for row in vertex_rows
    )
    summary = {
        "complete": edge_complete and vertex_complete,
        "edge": {
            "checks": edge_rows,
            "checked_count": len(edge_rows),
            "colorable_count": sum(row["status"] == "colorable" for row in edge_rows),
            "inconclusive_count": sum(
                row["status"] not in {"colorable", "non-colorable"} for row in edge_rows
            ),
            "minimal_if_all_colorable": edge_complete and all(
                row["status"] == "colorable" for row in edge_rows
            ),
            "elapsed_seconds": round(time.monotonic() - edges_started, 6),
        },
        "vertex": {
            "checks": vertex_rows,
            "checked_count": len(vertex_rows),
            "colorable_count": sum(row["status"] == "colorable" for row in vertex_rows),
            "inconclusive_count": sum(
                row["status"] not in {"colorable", "non-colorable"} for row in vertex_rows
            ),
            "minimal_if_all_colorable": vertex_complete and all(
                row["status"] == "colorable" for row in vertex_rows
            ),
            "elapsed_seconds": round(time.monotonic() - vertices_started, 6),
        },
    }
    top_level = {
        "complete": summary["complete"],
        "edge_minimality": summary["edge"]["minimal_if_all_colorable"],
        "vertex_minimality": summary["vertex"]["minimal_if_all_colorable"],
        "minimality": (
            True
            if summary["complete"]
            and summary["edge"]["minimal_if_all_colorable"]
            and summary["vertex"]["minimal_if_all_colorable"]
            else False
            if summary["complete"]
            else None
        ),
    }
    return summary, top_level


def verify_candidate(
    graph: Graph,
    graph6: str,
    source_row: dict[str, Any],
    dataset: str,
    time_limit: float,
    workers: int,
) -> dict[str, Any]:
    started = time.monotonic()
    invariants_ok, invariant_report = check_invariants(
        graph, graph6, source_row, dataset
    )
    report: dict[str, Any] = {
        "generated_at_utc": utc_now(),
        "graph_invariants": invariant_report,
        "parameters": {
            "cp_sat_time_limit_seconds": time_limit,
            "cp_sat_workers": workers,
        },
        "schema": "order16-candidate-verification/v1",
    }
    if not invariants_ok:
        failed = [
            key
            for key, value in invariant_report.items()
            if key
            in {
                "simple",
                "connected",
                "bipartite",
                "order_16",
                "side_sizes_match_dataset",
                "minimum_degree_at_least_2",
                "stored_order_matches",
                "stored_size_matches",
                "stored_delta_matches",
                "stored_minimum_degree_matches",
                "canonical_hash_matches",
                "graph6_round_trip_matches",
            }
            and value is not True
        ]
        report["decision"] = "invariant_failed"
        report["errors"] = ["failed invariants: " + ", ".join(failed)]
        report["verification_duration_seconds"] = round(time.monotonic() - started, 3)
        return report

    errors: list[str] = []
    try:
        rank_report_value, rank_noncolorable = rank_check(graph, time_limit, workers)
        fixed_rows, fixed_summary = fixed_span_checks(graph, time_limit, workers)
        deletion_report, minimality_report = deletion_checks(graph, time_limit, workers)
    except Exception as exc:
        report["decision"] = "verification_error"
        report["errors"] = [f"{type(exc).__name__}: {exc}"]
        report["verification_duration_seconds"] = round(time.monotonic() - started, 3)
        return report

    report["rank_potential_cp_sat"] = rank_report_value
    report["fixed_span_cp_sat"] = {
        "checks": fixed_rows,
        **fixed_summary,
    }
    report["single_deletion_checks"] = deletion_report
    report["minimality"] = minimality_report

    saw_witness = (
        rank_report_value.get("status") == "colorable"
        or bool(fixed_summary["satisfiable_spans"])
    )
    noncolorable_proofs = rank_noncolorable and fixed_summary["all_infeasible"]
    workflow_complete = (
        rank_report_value.get("status") in {"colorable", "non-colorable"}
        and fixed_summary["all_legal_spans_checked"]
        and not fixed_summary["inconclusive_spans"]
        and deletion_report["complete"]
    )
    if saw_witness:
        decision = "colorable_claim_rejected"
    elif noncolorable_proofs and workflow_complete:
        decision = "verified_non_colorable"
    else:
        decision = "verification_incomplete"
    if rank_report_value.get("status") == "non-colorable":
        errors.extend(rank_report_value.get("errors", []))
    report["certificate_ready"] = decision == "verified_non_colorable"
    report["decision"] = decision
    report["errors"] = errors
    report["verification_duration_seconds"] = round(time.monotonic() - started, 3)
    return report


def candidate_identity(dataset: str, index: int) -> tuple[str, str]:
    match = re.fullmatch(r"order16-(.+)", dataset)
    class_name = match.group(1) if match else re.sub(r"[^A-Za-z0-9_.-]+", "_", dataset)
    safe_class = re.sub(r"[^A-Za-z0-9_.-]+", "_", class_name)
    identity = f"ORDER16-{safe_class}-index-{index}"
    safe_identity = re.sub(r"[^A-Za-z0-9_.-]+", "_", identity)
    return identity, safe_identity


def tracked_files(directory: Path) -> list[dict[str, str]]:
    evidence = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        if path.name in {"manifest.json", ".certificate-ready.json"}:
            continue
        evidence.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "sha256": sha256_path(path),
            }
        )
    return evidence


def make_summary(
    identity: str,
    source_record: dict[str, Any],
    source_metadata: dict[str, Any],
    verification: dict[str, Any],
) -> str:
    invariant_report = verification.get("graph_invariants", {})
    fixed = verification.get("fixed_span_cp_sat", {})
    deletions = verification.get("single_deletion_checks", {})
    minimality = verification.get("minimality", {})
    lines = [
        f"# {identity}",
        "",
        f"Decision: `{verification.get('decision', 'missing')}`",
        f"Certificate ready: {'yes' if verification.get('certificate_ready') else 'no'}",
        "",
        "## Source",
        "",
        f"- Remote dataset: `{source_record.get('dataset')}`",
        f"- Zero-based index: {source_record.get('index')}",
        f"- Canonical SHA-256: `{source_record.get('canonical_sha256')}`",
        f"- Input: `{source_metadata.get('path', 'unknown')}`",
        f"- Input SHA-256: `{source_metadata.get('sha256', 'unknown')}`",
        "",
        "## Verification",
        "",
        f"- Order / size / delta: {invariant_report.get('order')} / "
        f"{invariant_report.get('size')} / {invariant_report.get('delta')}",
        f"- Bipartition side sizes: {invariant_report.get('side_sizes_from_reconstruction')}",
        f"- Minimum degree: {invariant_report.get('minimum_degree')}",
        f"- Rank-potential CP-SAT: `{verification.get('rank_potential_cp_sat', {}).get('status', 'not run')}`",
        f"- Legal spans checked: {len(fixed.get('checks', []))} of {len(fixed.get('legal_spans', []))}",
        f"- All fixed spans infeasible: {fixed.get('all_infeasible')}",
        f"- Edge deletion minimality: {minimality.get('edge_minimality')}",
        f"- Vertex deletion minimality: {minimality.get('vertex_minimality')}",
        "",
        "## Artifacts",
        "",
        "- `graph.json` - reconstructed labelled graph and canonical hashes",
        "- `source.graph6` - exact source line retained for replay",
        "- `capture-record.json` - source chunk locations and duplicate counts",
        "- `verification.json` - complete solver and minimality reports",
        "- `manifest.json` - artifact hashes and reproducibility metadata",
        "",
        "Timeouts are unresolved outcomes and are never counted as candidates.",
        "",
    ]
    return "\n".join(lines)


def save_bundle(
    dataset: str,
    index: int,
    graph: Graph,
    graph6: str,
    source_row: dict[str, Any],
    source_locations: list[dict[str, Any]],
    occurrences: int,
    source_metadata: dict[str, Any],
    verification: dict[str, Any],
) -> tuple[Path, str]:
    identity, safe_identity = candidate_identity(dataset, index)
    final = BUNDLE_ROOT / safe_identity
    if final.exists():
        raise CaptureError(f"refusing to overwrite candidate bundle: {final}")
    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix="." + safe_identity + ".", dir=str(final.parent))
    )
    try:
        graph.metadata.update(
            {
                "candidate_id": identity,
                "source_canonical_sha256": source_row["canonical_sha256"],
                "source_chunk_rows": source_locations,
                "source_dataset": dataset,
                "source_graph6": graph6,
                "source_index": index,
                "source_input": source_metadata,
            }
        )
        atomic_json(temporary / "graph.json", graph.to_json())
        (temporary / "source.graph6").write_text(graph6 + "\n", encoding="ascii")
        capture_record = {
            "canonical_sha256": source_row["canonical_sha256"],
            "identity": identity,
            "index": index,
            "occurrences": occurrences,
            "schema": "order16-capture-record/v1",
            "source_input": source_metadata,
            "source_locations": source_locations,
            "status": source_row["status"],
        }
        atomic_json(temporary / "capture-record.json", capture_record)
        atomic_json(temporary / "verification.json", verification)
        (temporary / "SUMMARY.md").write_text(
            make_summary(identity, {**source_row, "dataset": dataset}, source_metadata, verification),
            encoding="utf-8",
        )
        evidence = tracked_files(temporary)
        digest = hashlib.sha256()
        for item in evidence:
            digest.update(f"{item['path']}  {item['sha256']}\n".encode())
        manifest = {
            "artifact_count": len(evidence),
            "artifacts": evidence,
            "bundle_digest_sha256": digest.hexdigest(),
            "bundle_id": identity,
            "certificate_ready": verification.get("certificate_ready") is True,
            "decision": verification.get("decision"),
            "generated_at_utc": utc_now(),
            "remote": REMOTE,
            "schema": "order16-candidate-bundle-manifest/v1",
            "source": capture_record,
            "tool": {
                "capture_tool_sha256": sha256_path(ROOT / "src/capture_order16_candidate.py"),
                "graph_library_sha256": sha256_path(ROOT / "src/interval_edge_coloring.py"),
            },
        }
        atomic_json(temporary / "manifest.json", manifest)
        if verification.get("certificate_ready") is True:
            atomic_json(
                temporary / ".certificate-ready.json",
                {
                    "bundle_id": identity,
                    "decision": verification["decision"],
                    "manifest_sha256": sha256_path(temporary / "manifest.json"),
                    "schema": "order16-certificate-ready/v1",
                },
            )
        os.replace(temporary, final)
        return final, identity
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def build_status(
    args: argparse.Namespace,
    snapshot: dict[str, Any],
    scan_seconds: float,
    deduplication: dict[str, Any],
    selected_count: int,
    outcomes: list[dict[str, Any]],
    source_metadata: dict[str, Any] | None,
    total_seconds: float,
    error: str | None = None,
) -> dict[str, Any]:
    malformed_examples = []
    for item in snapshot.get("file_inventory", []):
        for example in item.get("malformed_examples", []):
            if len(malformed_examples) < 20:
                malformed_examples.append({**example, "file": item["file"]})
    expected = args.expected
    partial = args.chunk_limit is not None
    if error is not None:
        validation = {"passed": False}
    elif expected is None:
        validation = {
            "expected_records": None,
            "passed": snapshot["malformed_rows"] == 0,
            "rows_match_expected": None,
            "rows_minus_expected": None,
            "scope": "partial-chunk-scan" if partial else "full-dataset",
        }
    else:
        difference = snapshot["rows_scanned"] - expected
        passed = (
            not partial
            and difference == 0
            and snapshot["malformed_rows"] == 0
        )
        validation = {
            "expected_records": expected,
            "passed": passed,
            "rows_match_expected": False if partial else difference == 0,
            "rows_minus_expected": difference,
            "scope": "partial-chunk-scan" if partial else "full-dataset",
        }
    outcome_counts = Counter(item["decision"] for item in outcomes)
    status: dict[str, Any] = {
        "candidate_limit": args.candidate_limit,
        "candidate_outcomes": outcomes,
        "certificate_ready_count": sum(
            item.get("certificate_ready") is True for item in outcomes
        ),
        "dataset": args.dataset,
        "deduplication": deduplication,
        "error": error,
        "files_scanned": snapshot.get("files_scanned", 0),
        "generated_at_utc": utc_now(),
        "input": source_metadata,
        "configured_input": args.input or f"data/{args.dataset}-d2to11.g6",
        "malformed_examples": malformed_examples,
        "malformed_rows": snapshot.get("malformed_rows", 0),
        "negative_unique_candidates": deduplication.get("unique_candidate_keys", 0),
        "remote": REMOTE,
        "remote_root": REMOTE_ROOT,
        "rows_scanned": snapshot.get("rows_scanned", 0),
        "scan_duration_seconds": round(scan_seconds, 3),
        "scan_count": snapshot.get("scan_count", 0),
        "schema": "order16-candidate-capture-status/v1",
        "selected_for_verification": selected_count,
        "stable_scans_observed": snapshot.get("stable_scans_observed", 0),
        "status_histogram": snapshot.get("status_histogram", {}),
        "timeout_rows": snapshot.get("timeout_rows", 0),
        "timeouts_are_candidates": False,
        "total_duration_seconds": round(total_seconds, 3),
        "unique_negative_keys_after_deduplication": deduplication.get(
            "unique_candidate_keys", 0
        ),
        "validation": validation,
        "verification_outcome_counts": dict(sorted(outcome_counts.items())),
    }
    return status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="remote results/<dataset> name")
    parser.add_argument(
        "--input",
        help="remote graph6 path relative to the checkout; defaults to data/<dataset>-d2to11.g6",
    )
    parser.add_argument("--expected", type=int, help="required full-scan JSONL row count")
    parser.add_argument("--output-status", required=True, help="compact status JSON path")
    parser.add_argument("--candidate-limit", type=int, help="maximum candidates to verify; 0 scans only")
    parser.add_argument("--solver-time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--stable-scans", type=int, default=2)
    parser.add_argument("--deadline-seconds", type=float)
    parser.add_argument("--chunk-glob", default="chunk-*.jsonl")
    parser.add_argument("--chunk-limit", type=int, help="bounded scan of the first N chunks")
    args = parser.parse_args(argv)
    if args.candidate_limit is not None and args.candidate_limit < 0:
        parser.error("--candidate-limit must be at least 0")
    if args.solver_time_limit <= 0 or args.workers < 1:
        parser.error("--solver-time-limit must be positive and --workers at least 1")
    if args.poll_seconds <= 0 or args.stable_scans < 1:
        parser.error("--poll-seconds must be positive and --stable-scans at least 1")
    if args.chunk_limit is not None and args.chunk_limit < 0:
        parser.error("--chunk-limit must be at least 0")
    if args.deadline_seconds is not None and args.deadline_seconds <= 0:
        parser.error("--deadline-seconds must be positive")
    return args


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    snapshot, scan_seconds = stable_snapshot(args)
    candidates, deduplication, integrity_errors = deduplicate_candidates(
        snapshot["candidate_records"]
    )
    source_metadata = None
    outcomes = []

    expected_incomplete = (
        args.expected is not None
        and args.chunk_limit is None
        and snapshot["rows_scanned"] != args.expected
    )
    if integrity_errors or expected_incomplete or snapshot["malformed_rows"]:
        reasons = list(integrity_errors[:20])
        if expected_incomplete:
            reasons.append(
                f"expected {args.expected} rows, scanned {snapshot['rows_scanned']}"
            )
        if snapshot["malformed_rows"]:
            reasons.append(f"found {snapshot['malformed_rows']} malformed rows")
        status = build_status(
            args,
            snapshot,
            scan_seconds,
            deduplication,
            0,
            [],
            None,
            time.monotonic() - started,
            "; ".join(reasons) if reasons else "validation failed",
        )
        atomic_json(Path(args.output_status), status)
        print(json.dumps({"error": "; ".join(reasons), "output": args.output_status}, sort_keys=True))
        return 2

    if args.candidate_limit is None:
        selected = candidates
    else:
        selected = candidates[: args.candidate_limit]
    indexes = [item["key"]["index"] for item in selected]
    graph_lines: dict[int, str] = {}
    if indexes:
        input_path = args.input or f"data/{args.dataset}-d2to11.g6"
        source_metadata, graph_lines = fetch_graph6_lines(input_path, indexes)

    for candidate in selected:
        index = candidate["key"]["index"]
        graph6 = graph_lines[index]
        order, integer_edges = from_graph6(graph6)
        vertices = tuple(f"v{i}" for i in range(order))
        edges = [(vertices[u], vertices[v]) for u, v in integer_edges]
        graph = Graph(vertices, edges)
        verification = verify_candidate(
            graph,
            graph6,
            candidate["row"],
            args.dataset,
            args.solver_time_limit,
            args.workers,
        )
        bundle_path, identity = save_bundle(
            args.dataset,
            index,
            graph,
            graph6,
            candidate["row"],
            candidate["source_locations"],
            candidate["occurrences"],
            source_metadata or {},
            verification,
        )
        outcomes.append(
            {
                "bundle": bundle_path.relative_to(ROOT).as_posix(),
                "canonical_sha256": candidate["key"]["canonical_sha256"],
                "certificate_ready": verification.get("certificate_ready") is True,
                "decision": verification.get("decision"),
                "identity": identity,
                "index": index,
            }
        )
        print(
            json.dumps(
                {
                    "bundle": str(bundle_path),
                    "decision": verification.get("decision"),
                    "index": index,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    status = build_status(
        args,
        snapshot,
        scan_seconds,
        deduplication,
        len(selected),
        outcomes,
        source_metadata,
        time.monotonic() - started,
    )
    atomic_json(Path(args.output_status), status)
    complete = all(
        item.get("decision") in {"verified_non_colorable", "colorable_claim_rejected"}
        for item in outcomes
    )
    print(
        json.dumps(
            {
                "candidates": len(selected),
                "certificate_ready": status["certificate_ready_count"],
                "files": status["files_scanned"],
                "output": args.output_status,
                "rows": status["rows_scanned"],
                "status": "complete" if complete else "inconclusive",
            },
            sort_keys=True,
        )
    )
    return 0 if complete else 3


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        try:
            atomic_json(
                Path(args.output_status),
                {
                    "dataset": args.dataset,
                    "error": f"{type(exc).__name__}: {exc}",
                    "generated_at_utc": utc_now(),
                    "remote": REMOTE,
                    "schema": "order16-candidate-capture-status/v1",
                },
            )
        except Exception:
            pass
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
