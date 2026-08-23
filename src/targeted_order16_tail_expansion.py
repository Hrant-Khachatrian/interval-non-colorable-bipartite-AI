#!/usr/bin/env python3
"""Bounded tail expansion for the targeted order-16 bipartite census."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import networkx as nx

from interval_edge_coloring import (
    Graph,
    all_spans_solve,
    from_graph6,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
    weighted_hub_statistics,
)

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "results/targeted-order16/order16-5x11-d2to11.g6"
CENSUS_SUMMARY = ROOT / "results/targeted-order16/5x11-summary.json"
PRIOR_RESULTS = ROOT / "results/targeted-order16/classification/classification-results.jsonl"
OUTPUT_JSON = ROOT / "results/targeted-order16/tail-expansion-v2.json"
CHECKPOINT_DIR = ROOT / "results/targeted-order16/tail-expansion"
CHECKPOINT_JSONL = CHECKPOINT_DIR / "tail-expansion-v2.jsonl"
PROGRESS_JSON = CHECKPOINT_DIR / "scan-progress.json"


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, allow_nan=False, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def final_records(path: Path, count: int) -> list[bytes]:
    size = path.stat().st_size
    window = max(8 * 1024 * 1024, count * 160)
    offset = max(0, size - window)
    with path.open("rb") as handle:
        handle.seek(offset)
        payload = handle.read()
    records = payload.splitlines()
    if offset and records:
        records = records[1:]
    if len(records) < count:
        raise RuntimeError(f"tail window found only {len(records)} complete records")
    return records[-count:]


def original_indices() -> set[int]:
    indices = set()
    with PRIOR_RESULTS.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                indices.add(int(json.loads(line)["index"]))
    return indices


def make_graph(record: bytes, index: int) -> Graph | None:
    text = record.decode("ascii")
    order, numeric_edges = from_graph6(text)
    if order != 16 or len(numeric_edges) != len(set(numeric_edges)):
        return None
    vertices = [f"v{i}" for i in range(order)]
    edges = [(f"v{u}", f"v{v}") for u, v in numeric_edges]
    probe = nx.Graph()
    probe.add_nodes_from(vertices)
    probe.add_edges_from(edges)
    if not nx.is_connected(probe) or not nx.is_bipartite(probe):
        return None
    degrees = dict(probe.degree())
    if len(set(degrees.values())) == 1:
        return None
    if min(degrees.values()) < 2 or max(degrees.values()) > 11:
        return None
    return Graph(vertices, edges, metadata={"source_index": index, "source_line": text})


def graph_priority(graph: Graph) -> tuple[dict, list[dict]]:
    hubs = weighted_hub_statistics(graph)
    best_margin = max(row["margin"] for row in hubs)
    priority = {
        "best_margin": int(best_margin),
        "positive_margin_sum": int(sum(max(row["margin"], 0) for row in hubs)),
        "delta": graph.delta,
        "size": graph.m,
    }
    return priority, hubs


def coloring_json(coloring: dict[tuple[str, str], int] | None) -> dict[str, int] | None:
    if coloring is None:
        return None
    return {f"{u}|{v}": color for (u, v), color in coloring.items()}


def classify(graph: Graph, primary_seconds: float, workers: int, span_seconds: float) -> dict:
    started = time.perf_counter()
    primary = rank_potential_solve(graph, time_limit=primary_seconds, workers=workers)
    elapsed = time.perf_counter() - started
    verified = False
    if primary.coloring is not None:
        verified, _reason = verify_coloring(graph, primary.coloring)
        if not verified:
            raise AssertionError("primary CP-SAT coloring failed independent verification")

    row = {
        "source_index": graph.metadata["source_index"],
        "source_line": graph.metadata["source_line"],
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "maximum_degree": max(graph.degrees.values()),
        "canonical_sha256": graph.metadata["canonical_sha256"],
        "priority": graph.metadata["priority"],
        "weighted_hub_statistics": graph.metadata["hub_statistics"],
        "primary_encoding": primary.encoding,
        "primary_status": primary.status,
        "primary_solver_status": primary.solver_status,
        "primary_span": primary.span,
        "primary_solver_seconds": elapsed,
        "primary_solver_wall_time": primary.wall_time,
        "primary_time_limit_seconds": primary_seconds,
        "solver_workers": workers,
        "primary_coloring": coloring_json(primary.coloring),
        "primary_coloring_verified": verified,
        "negative_confirmation_required": primary.status == "non-colorable",
    }

    if primary.status == "non-colorable":
        confirmation = all_spans_solve(
            graph,
            time_limit_per_span=span_seconds,
            workers=workers,
            stop_on_timeout=True,
        )
        row.update(
            {
                "negative_confirmation_encoding": confirmation["encoding"],
                "negative_confirmation_decision": confirmation["decision"],
                "negative_confirmed": confirmation["decision"] == "non-colorable",
                "negative_confirmation_contradicted": confirmation["decision"] == "colorable",
                "negative_confirmation_timeout": confirmation["decision"] == "timeout",
                "negative_confirmation_spans": confirmation["spans"],
                "span_time_limit_seconds": span_seconds,
            }
        )
    else:
        row.update(
            {
                "negative_confirmation_encoding": None,
                "negative_confirmation_decision": None,
                "negative_confirmed": False,
                "negative_confirmation_contradicted": False,
                "negative_confirmation_timeout": False,
                "negative_confirmation_spans": {},
                "span_time_limit_seconds": span_seconds,
            }
        )
    return row


def load_checkpoint() -> list[dict]:
    if not CHECKPOINT_JSONL.exists():
        return []
    rows = []
    with CHECKPOINT_JSONL.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def update_counts(state: dict, rows: list[dict]) -> None:
    state["classified"] = len(rows)
    state["colorable"] = sum(row["primary_status"] == "colorable" for row in rows)
    state["non_colorable"] = sum(row["primary_status"] == "non-colorable" for row in rows)
    state["timeout"] = sum(row["primary_status"] == "timeout" for row in rows)
    state["negative_confirmations"] = sum(bool(row.get("negative_confirmed")) for row in rows)
    state["negative_confirmation_contradictions"] = sum(
        bool(row.get("negative_confirmation_contradicted")) for row in rows
    )
    state["negative_confirmation_timeouts"] = sum(
        bool(row.get("negative_confirmation_timeout")) for row in rows
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan-limit", type=int, default=20000)
    parser.add_argument("--select-limit", type=int, default=50)
    parser.add_argument("--primary-time-limit", type=float, default=8.0)
    parser.add_argument("--solver-workers", type=int, default=4)
    parser.add_argument("--span-time-limit", type=float, default=30.0)
    args = parser.parse_args()

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    census = json.loads(CENSUS_SUMMARY.read_text(encoding="utf-8"))
    generated_total = int(census["generation"]["generated_count"])
    expected_digest = census["generation"].get("sha256")
    input_digest = sha256_file(INPUT)
    if expected_digest and input_digest != expected_digest:
        raise RuntimeError("input graph6 checksum differs from generation summary")

    excluded = original_indices()
    completed_rows = load_checkpoint()
    completed_keys = {(row["source_index"], row["canonical_sha256"]) for row in completed_rows}
    records = final_records(INPUT, args.scan_limit)
    start_index = generated_total - len(records)

    seen_hashes: set[str] = set()
    candidates: list[dict] = []
    filter_rejections = {
        "wrong_order_or_duplicate_edges": 0,
        "not_connected_or_not_bipartite": 0,
        "regular": 0,
        "degree_range": 0,
    }
    eligible = 0
    eligible_original: list[int] = []
    duplicates_skipped = 0
    progress_state = {
        "schema_version": 2,
        "phase": "tail_scan",
        "tail_records_requested": args.scan_limit,
        "scanned_so_far": 0,
        "eligible_so_far": 0,
    }
    atomic_write_json(PROGRESS_JSON, progress_state)
    last_progress_write = time.perf_counter()

    for relative, record in enumerate(records):
        index = start_index + relative
        if time.perf_counter() - last_progress_write >= 10:
            progress_state.update(
                {
                    "scanned_so_far": relative + 1,
                    "eligible_so_far": eligible,
                    "duplicates_skipped_so_far": duplicates_skipped,
                }
            )
            atomic_write_json(PROGRESS_JSON, progress_state)
            last_progress_write = time.perf_counter()
        graph = make_graph(record, index)
        if graph is None:
            text = record.decode("ascii")
            order, numeric_edges = from_graph6(text)
            vertices = [f"v{i}" for i in range(order)]
            probe = nx.Graph()
            probe.add_nodes_from(vertices)
            probe.add_edges_from([(f"v{u}", f"v{v}") for u, v in numeric_edges])
            if order != 16 or len(numeric_edges) != len(set(numeric_edges)):
                filter_rejections["wrong_order_or_duplicate_edges"] += 1
            elif not nx.is_connected(probe) or not nx.is_bipartite(probe):
                filter_rejections["not_connected_or_not_bipartite"] += 1
            elif len(set(dict(probe.degree()).values())) == 1:
                filter_rejections["regular"] += 1
            else:
                filter_rejections["degree_range"] += 1
            continue

        eligible += 1
        if index in excluded:
            eligible_original.append(index)
        digest = nauty_canonical_hash(graph)
        if digest in seen_hashes:
            duplicates_skipped += 1
            continue
        seen_hashes.add(digest)
        priority, hubs = graph_priority(graph)
        candidates.append(
            {
                "source_index": index,
                "source_line": record.decode("ascii"),
                "canonical_sha256": digest,
                "priority": priority,
                "weighted_hub_statistics": hubs,
            }
        )

    fresh_candidates = [row for row in candidates if row["source_index"] not in excluded]
    fresh_candidates.sort(key=lambda row: (-row["priority"]["best_margin"], row["source_index"]))
    selected = fresh_candidates[: args.select_limit]
    state = {
        "schema_version": 2,
        "completion": "in_progress",
        "input": {
            "path": str(INPUT.relative_to(ROOT)),
            "sha256": input_digest,
            "generated_total": generated_total,
        },
        "scope": {
            "records": "final bounded tail; prefix not read",
            "tail_records_requested": args.scan_limit,
            "start_source_index": start_index,
            "end_source_index": start_index + len(records) - 1,
        },
        "filters": {
            "order": 16,
            "connected_simple_bipartite": True,
            "nonregular": True,
            "minimum_degree_at_least": 2,
            "maximum_degree_at_most": 11,
        },
        "parameters": {
            "selection": "descending best weighted-hub margin, then source index",
            "deduplication": "pynauty bipartite canonical certificate sha256",
            "original_100_excluded": True,
            "primary_solver": "rank_potential_solve CP-SAT",
            "primary_time_limit_seconds": args.primary_time_limit,
            "solver_workers": args.solver_workers,
            "independent_span_time_limit_seconds": args.span_time_limit,
        },
        "scanned": len(records),
        "eligible": eligible,
        "eligible_unique": len(candidates),
        "duplicates_skipped": duplicates_skipped,
        "filter_rejections": filter_rejections,
        "original_100_eligible_in_tail": len(eligible_original),
        "original_100_eligible_indices": eligible_original,
        "selected": len(selected),
        "selected_unique": len(selected),
        "selected_overlap_with_original_100": sum(
            row["source_index"] in excluded for row in selected
        ),
    }
    state.update({"candidates": selected, "classified_rows": completed_rows})
    update_counts(state, completed_rows)
    state["completion"] = "completed" if len(completed_rows) == args.select_limit else "in_progress"
    atomic_write_json(OUTPUT_JSON, state)
    PROGRESS_JSON.unlink(missing_ok=True)

    for candidate in selected:
        key = (candidate["source_index"], candidate["canonical_sha256"])
        if key in completed_keys:
            continue
        graph = make_graph(candidate["source_line"].encode("ascii"), candidate["source_index"])
        if graph is None:
            raise RuntimeError(f"selected record became ineligible: {candidate['source_index']}")
        graph.metadata["canonical_sha256"] = candidate["canonical_sha256"]
        graph.metadata["priority"] = candidate["priority"]
        graph.metadata["hub_statistics"] = candidate["weighted_hub_statistics"]
        result = classify(
            graph,
            args.primary_time_limit,
            args.solver_workers,
            args.span_time_limit,
        )
        completed_rows.append(result)
        state["classified_rows"] = completed_rows
        update_counts(state, completed_rows)
        state["completion"] = "completed" if len(completed_rows) == args.select_limit else "in_progress"
        atomic_write_jsonl(CHECKPOINT_JSONL, completed_rows)
        atomic_write_json(OUTPUT_JSON, state)

    if len(completed_rows) != args.select_limit:
        raise RuntimeError(f"only {len(completed_rows)} fresh unique candidates were available")


if __name__ == "__main__":
    main()
