#!/usr/bin/env python3
"""Focused exact order-17 search from certified and high-priority nearby seeds."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import itertools
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    from_graph6,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
    weighted_hub_statistics,
)
from order18_targeted_search import load_near_miss


ROOT = Path(__file__).resolve().parents[1]
Q1_PATHS = [
    ROOT / "results/graphs/quotient-r1/Q1-00012.graph.json",
    ROOT / "results/graphs/quotient-r1/Q1-00014.graph.json",
]
Q2_IDS = ("Q2-00132", "Q2-00144", "Q2-00185", "Q2-00196", "Q2-00261", "Q2-00263", "Q2-00287")
Q2_REPORT = ROOT / "results/quotient-r2.json"
ORDER18_REPORT = ROOT / "results/order18-targeted-v3.json"
ORDER16_REPORT = ROOT / "results/targeted-order16/expansion-v3-summary.json"


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
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


def append_event(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def graph_metrics(graph: Graph) -> dict:
    adjacency = graph.adjacency()
    graph.adjacency = lambda: adjacency
    try:
        hubs = weighted_hub_statistics(graph)
    finally:
        del graph.adjacency
    degrees = list(graph.degrees.values())
    mean = sum(degrees) / len(degrees)
    variance = sum((degree - mean) ** 2 for degree in degrees) / len(degrees)
    return {
        "hub_best_margin": max(row["margin"] for row in hubs),
        "degree_variance_normalized": variance / (graph.n - 1),
        "weighted_hubs_best": hubs[:4],
    }


def valid(graph: Graph) -> bool:
    return (
        graph.n == 17
        and graph.bipartition is not None
        and nx.is_connected(graph._nx)
        and min(graph.degrees.values()) >= 2
    )


def replace_metadata(graph: Graph, metadata: dict) -> Graph:
    return Graph(graph.vertices, graph.edges, graph.bipartition, metadata)


def delete_vertex(base: Graph, vertex: str, metadata: dict) -> Graph:
    sides = [[v for v in side if v != vertex] for side in base.bipartition]
    return Graph(
        [v for v in base.vertices if v != vertex],
        [edge for edge in base.edges if vertex not in edge],
        sides,
        metadata,
    )


def identify_pair(base: Graph, first: str, second: str, metadata: dict) -> Graph | None:
    side_index = next((i for i, side in enumerate(base.bipartition) if first in side and second in side), None)
    if side_index is None:
        return None
    merged = f"({first}+{second})"
    owner = {vertex: (merged if vertex in (first, second) else vertex) for vertex in base.vertices}
    edge_set = {tuple(sorted((owner[u], owner[v]))) for u, v in base.edges}
    if any(u == v for u, v in edge_set):
        return None
    sides = []
    for index, side in enumerate(base.bipartition):
        sides.append(([merged] + [v for v in side if v not in (first, second)]) if index == side_index else list(side))
    return Graph(sorted(sides[0] + sides[1]), sorted(edge_set), sides, metadata)


def missing_cross_edges(graph: Graph) -> list[tuple[str, str]]:
    present = set(map(tuple, graph.edges))
    return [
        tuple(sorted((u, v)))
        for u in graph.bipartition[0]
        for v in graph.bipartition[1]
        if tuple(sorted((u, v))) not in present
    ]


def add_edges(base: Graph, additions: tuple[tuple[str, str], ...], metadata: dict) -> Graph:
    return Graph(base.vertices, list(base.edges) + list(additions), base.bipartition, metadata)


def two_edge_switches(base: Graph, limit: int):
    left = set(base.bipartition[0])
    normalized = []
    for u, v in base.edges:
        normalized.append((u, v) if u in left else (v, u))
    original = set(map(tuple, base.edges))
    emitted = 0
    for (a, x), (b, y) in itertools.combinations(sorted(normalized), 2):
        if len({a, b, x, y}) != 4:
            continue
        replacements = (tuple(sorted((a, y))), tuple(sorted((b, x))))
        if any(edge in original for edge in replacements):
            continue
        yield Graph(
            base.vertices,
            sorted(original.difference((tuple(sorted((a, x))), tuple(sorted((b, y))))).union(replacements)),
            base.bipartition,
            {"removed_edges": [list(sorted((a, x))), list(sorted((b, y)))], "added_edges": [list(edge) for edge in replacements]},
        )
        emitted += 1
        if emitted >= limit:
            return


def low_degree_vertices(graph: Graph, ceiling: int) -> list[str]:
    return sorted(v for v, degree in graph.degrees.items() if degree <= ceiling)


def q1_bases() -> list[tuple[str, Graph]]:
    return [(path.name.removesuffix(".graph.json"), Graph.from_json(json.loads(path.read_text()))) for path in Q1_PATHS]


def q2_bases() -> list[tuple[str, Graph]]:
    return [(candidate_id, load_near_miss(candidate_id, Q2_REPORT)) for candidate_id in Q2_IDS]


def order16_bases(limit: int) -> list[tuple[str, Graph]]:
    document = json.loads(ORDER16_REPORT.read_text())
    bases = []
    for row in document.get("candidates", [])[:limit]:
        order, raw_edges = from_graph6(row["source_line"])
        if order != 16:
            continue
        names = [f"x{i}" for i in range(order)]
        graph = Graph(names, [(names[u], names[v]) for u, v in raw_edges], None,
                      {"source": "order16-expansion-v3", "source_index": row["source_index"]})
        if nx.is_connected(graph._nx) and min(graph.degrees.values()) >= 2:
            bases.append((f"O16-{row['source_index']}", graph))
    return bases


def reconstruct_order18(limit: int) -> list[tuple[str, Graph]]:
    document = json.loads(ORDER18_REPORT.read_text())
    q1 = dict(q1_bases())
    q2 = dict(q2_bases())
    rebuilt = []
    for row in document.get("rows", []):
        if len(rebuilt) >= limit:
            break
        meta = row.get("metadata", {})
        parent = meta.get("parent", "").removesuffix(".graph")
        lane = meta.get("lane")
        graph = None
        if lane == "one-pair-same-side-identification" and parent in q1:
            pair = meta.get("identified", [])
            if len(pair) == 2:
                graph = identify_pair(q1[parent], pair[0], pair[1], {"source": "order18-v3", "source_id": row["candidate_id"]})
        elif lane == "delete-vertex-add-cross-edges" and parent in q1:
            reduced = delete_vertex(q1[parent], meta["deleted_vertex"], {"source": "order18-v3", "source_id": row["candidate_id"]})
            graph = add_edges(reduced, tuple(tuple(edge) for edge in meta.get("added_edges", [])), dict(reduced.metadata))
        elif lane == "reverse-extension-edge-additions" and parent in q2:
            graph = add_edges(q2[parent], tuple(tuple(edge) for edge in meta.get("added_edges", [])), {"source": "order18-v3", "source_id": row["candidate_id"]})
        if graph is not None and graph.n == 18 and nx.is_connected(graph._nx):
            rebuilt.append((row["candidate_id"], graph))
    return rebuilt


def emit_reductions(base: Graph, parent: str, source: str, args):
    """Yield bounded 17-vertex reductions with enough provenance to replay each lane."""
    if base.n == 19:
        lows = low_degree_vertices(base, args.low_degree_ceiling)
        for first, second in itertools.combinations(lows, 2):
            one = delete_vertex(base, first, {"lane": "q1-delete-two-low-degree", "parent": parent, "source": source, "deleted_vertices": [first, second]})
            yield delete_vertex(one, second, dict(one.metadata))
        pairs = [pair for side in base.bipartition for pair in itertools.combinations(side, 2)
                 if pair[0] in lows and pair[1] in lows]
        for first, second in pairs[:args.q1_pair_limit]:
            one = identify_pair(base, first, second, {"lane": "q1-two-same-side-identifications", "parent": parent, "source": source, "identified_pairs": [[first, second]]})
            if one is None:
                continue
            for side in one.bipartition:
                for third, fourth in itertools.islice((pair for pair in itertools.combinations(side, 2) if f"({first}+{second})" not in pair), args.q1_second_pair_limit):
                    two = identify_pair(one, third, fourth, {**one.metadata, "identified_pairs": one.metadata["identified_pairs"] + [[third, fourth]]})
                    if two is not None:
                        yield two
        for deleted in lows[:args.q1_delete_limit]:
            one = delete_vertex(base, deleted, {"lane": "q1-delete-then-identify", "parent": parent, "source": source, "deleted_vertex": deleted})
            for side in one.bipartition:
                for first, second in itertools.islice(itertools.combinations(side, 2), args.q1_second_pair_limit):
                    candidate = identify_pair(one, first, second, {**one.metadata, "identified": [first, second]})
                    if candidate is not None:
                        yield candidate
    elif base.n == 18:
        lows = low_degree_vertices(base, args.low_degree_ceiling)
        for deleted in lows:
            reduced = delete_vertex(base, deleted, {"lane": "delete-low-degree", "parent": parent, "source": source, "deleted_vertex": deleted, "deleted_degree": base.degrees[deleted]})
            yield reduced
            possible = missing_cross_edges(reduced)
            addition_sets = itertools.chain(itertools.combinations(possible, 1), itertools.combinations(possible, 2))
            for additions in itertools.islice(addition_sets, args.cross_addition_cap):
                yield add_edges(reduced, additions, {**reduced.metadata, "lane": "delete-add-cross-edges", "added_edges": [list(e) for e in additions]})
            for number, switched in enumerate(two_edge_switches(reduced, args.switches_per_reduction)):
                yield replace_metadata(switched, {**reduced.metadata, "lane": "delete-two-edge-switch", "switch_index": number, **switched.metadata})
        for side in base.bipartition:
            for first, second in itertools.combinations(side, 2):
                candidate = identify_pair(base, first, second, {"lane": "same-side-identification", "parent": parent, "source": source, "identified": [first, second]})
                if candidate is not None:
                    yield candidate
    elif base.n == 16:
        small_side = min(base.bipartition, key=len)
        other = base.bipartition[0] if small_side is base.bipartition[1] else base.bipartition[1]
        # Extend on the smaller class; capped high-degree neighborhoods preserve the targeted motif.
        ordered = sorted(other, key=lambda v: (-base.degrees[v], v))
        for size in range(2, min(5, len(ordered)) + 1):
            for neighbors in itertools.combinations(ordered[:args.order16_neighbor_pool], size):
                name = "new17"
                sides = [list(base.bipartition[0]), list(base.bipartition[1])]
                sides[0 if small_side is base.bipartition[0] else 1].append(name)
                yield Graph(list(base.vertices) + [name], list(base.edges) + [(name, v) for v in neighbors], sides,
                            {"lane": "order16-one-vertex-extension", "parent": parent, "source": source, "new_vertex_neighbors": list(neighbors)})


def generate(args, queue_cap: int | None = None):
    raw = []
    sources = []
    sources.extend((name, graph, "Q1-certified-negative") for name, graph in q1_bases())
    sources.extend((name, graph, "Q2-near-negative") for name, graph in q2_bases())
    sources.extend((name, graph, "order18-ranked") for name, graph in reconstruct_order18(args.order18_source_limit))
    sources.extend((name, graph, "order16-strong") for name, graph in order16_bases(args.order16_source_limit))
    for parent, graph, source in sources:
        cap = args.per_source_cap * (4 if source == "Q1-certified-negative" else 1)
        raw.extend(itertools.islice(emit_reductions(graph, parent, source, args), cap))
    raw_lanes = collections.Counter(graph.metadata["lane"] for graph in raw)
    unique = {}
    rejected = 0
    for graph in raw:
        if not valid(graph):
            rejected += 1
            continue
        unique.setdefault(nauty_canonical_hash(graph), graph)
    ranked = []
    for digest, graph in unique.items():
        metrics = graph_metrics(graph)
        ranked.append((-metrics["hub_best_margin"], -metrics["degree_variance_normalized"], -graph.delta, digest, graph, metrics))
    ranked.sort(key=lambda row: row[:4])
    selected = ranked[:queue_cap if queue_cap is not None else args.classify_cap]
    return selected, {
        "sources": collections.Counter(source for _parent, _graph, source in sources),
        "generated_raw": len(raw),
        "passed_filters_before_deduplication": len(raw) - rejected,
        "unique_after_nauty": len(unique),
        "rejected_by_filters": rejected,
        "duplicates_removed": len(raw) - rejected - len(unique),
        "generated_raw_by_lane": dict(sorted(raw_lanes.items())),
        "selected_by_lane": dict(sorted(collections.Counter(row[4].metadata["lane"] for row in selected).items())),
    }


def confirm_negative(graph: Graph, time_limit: float, workers: int) -> tuple[str, dict]:
    spans = {}
    for span in range(graph.delta, graph.n):
        status, coloring = fixed_span_sat_solve(graph, span, time_limit, workers)
        spans[str(span)] = {"solver_status": status, "has_coloring": coloring is not None}
        if status in ("OPTIMAL", "FEASIBLE"):
            ok, reason = verify_coloring(graph, coloring)
            if not ok:
                raise AssertionError(reason)
            return "colorable", spans
        if status == "UNKNOWN":
            return "confirmation_timeout", spans
        if status != "INFEASIBLE":
            raise AssertionError(f"unexpected fixed-span status {status}")
    return "confirmed_non_colorable", spans


def classify(task: dict) -> dict:
    graph = Graph.from_json(task["graph"])
    if not valid(graph):
        raise AssertionError("candidate failed exact order-17 filters")
    started = time.perf_counter()
    primary = rank_potential_solve(graph, task["primary_time_limit"], task["workers"])
    row = {
        "candidate_id": task["candidate_id"], "rank": task["rank"], "canonical_sha256": task["canonical_sha256"],
        "order": graph.n, "size": graph.m, "bipartition_sizes": list(map(len, graph.bipartition)),
        "delta": graph.delta, "minimum_degree": min(graph.degrees.values()), "metadata": graph.metadata,
        **graph_metrics(graph), "primary_status": primary.status, "primary_span": primary.span,
        "primary_solver_status": primary.solver_status, "primary_elapsed_seconds": primary.elapsed_seconds,
    }
    if primary.status == "colorable":
        ok, reason = verify_coloring(graph, primary.coloring or {})
        if not ok:
            raise AssertionError(reason)
        row["status"] = "colorable"
    elif primary.status == "timeout":
        row["status"] = "timeout"
    elif primary.status == "non-colorable":
        decision, spans = confirm_negative(graph, task["span_time_limit"], task["workers"])
        row["independent_confirmation"] = {"encoding": "fixed-span CP-SAT", "span_range_inclusive": [graph.delta, graph.n - 1], "spans": spans, "time_limit_per_span_seconds": task["span_time_limit"]}
        row["status"] = decision
    else:
        raise AssertionError(f"unexpected primary status {primary.status}")
    row["classification_elapsed_seconds"] = time.perf_counter() - started
    return row


def report(args, diagnostics, rows, counts, started, completion, stopped_reason, negatives, rank_window, overlap) -> dict:
    rows = sorted(rows, key=lambda row: row["rank"])
    classified_by_lane = collections.Counter(row["metadata"]["lane"] for row in rows)
    return {
        "schema_version": 1,
        "goal": "focused exact search for an interval-non-colorable simple connected bipartite graph on exactly 17 vertices; this is not an exhaustive order-17 census",
        "completion": completion,
        "stopped_reason": stopped_reason,
        "elapsed_seconds": time.monotonic() - started,
        "configuration": {
            "primary_solver": "rank-potential CP-SAT", "primary_time_limit_seconds": args.primary_time_limit,
            "independent_confirmation": "fixed-span CP-SAT over every legal span", "span_time_limit_seconds": args.span_time_limit,
            "timeout_policy": "timeouts remain unresolved and are never counted non-colorable",
            "deduplication": "Nauty bipartition-colored canonical SHA-256",
            "filters": ["exactly 17 vertices", "simple bipartite", "connected", "minimum degree >= 2"],
            "ranking": ["hub_best_margin descending", "degree_variance_normalized descending", "delta descending", "canonical hash"],
            "classification_target": len(rank_window),
            "rank_window": [args.rank_start, args.rank_end],
        },
        "generation": diagnostics,
        "counts": {
            "generated": diagnostics.get("generated_raw", 0), "unique": diagnostics.get("unique_after_nauty", 0),
            "selected_for_classification": len(rank_window),
            "classified": len(rows), "colorable": counts["colorable"],
            "primary_noncolorable": counts["primary_noncolorable"], "non_colorable": counts["confirmed_non_colorable"],
            "timeout": counts["timeout"] + counts["confirmation_timeout"],
            "confirmation_timeout": counts["confirmation_timeout"],
        },
        "reconciliation": overlap,
        "classified_by_lane": dict(sorted(classified_by_lane.items())),
        "negative_events": negatives,
        "rows": rows,
    }


def completed_rows(events: Path) -> dict[int, dict]:
    """Replay durable completions; later duplicate rank events are rejected by the caller."""
    rows = {}
    if not events.exists():
        return rows
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        event = json.loads(line)
        if event.get("event") != "classification_completed":
            continue
        row = event["row"]
        rank = row["rank"]
        previous = rows.setdefault(rank, row)
        if previous["canonical_sha256"] != row["canonical_sha256"]:
            raise RuntimeError(f"conflicting durable classification events for rank {rank}")
    return rows


def reconcile_prior(report_path: Path | None, selected, rank_start: int) -> dict:
    result = {"prior_report": str(report_path) if report_path else None, "prior_classified": 0,
              "prefix_matches_prior": None, "window_overlap_hashes": 0}
    if report_path is None:
        return result
    prior = json.loads(report_path.read_text(encoding="utf-8"))
    prior_rows = prior.get("rows", [])
    prior_by_rank = {row["rank"]: row["canonical_sha256"] for row in prior_rows}
    if len(prior_by_rank) != len(prior_rows):
        raise RuntimeError("prior report has duplicate ranks")
    prefix = {rank: digest for rank, (_a, _b, _c, digest, _graph, _m) in enumerate(selected, 1) if rank < rank_start}
    result["prior_classified"] = len(prior_by_rank)
    result["prefix_matches_prior"] = prior_by_rank == prefix
    if not result["prefix_matches_prior"]:
        raise RuntimeError("deterministic queue prefix does not match prior report")
    prior_hashes = set(prior_by_rank.values())
    window_hashes = {digest for rank, (_a, _b, _c, digest, _graph, _m) in enumerate(selected, 1) if rank >= rank_start}
    result["window_overlap_hashes"] = len(prior_hashes & window_hashes)
    if result["window_overlap_hashes"]:
        raise RuntimeError("rank window overlaps canonical hashes already classified by prior report")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/order17-targeted-v1")
    parser.add_argument("--classify-cap", type=int, default=1200)
    parser.add_argument("--rank-start", type=int, default=1,
                        help="inclusive global deterministic rank to classify")
    parser.add_argument("--rank-end", type=int,
                        help="inclusive global deterministic rank to classify; defaults to classify-cap")
    parser.add_argument("--prior-report", type=Path,
                        help="completed earlier report whose ranks below rank-start must match exactly")
    parser.add_argument("--max-processes", type=int, default=4)
    parser.add_argument("--solver-workers", type=int, default=1)
    parser.add_argument("--primary-time-limit", type=float, default=3.0)
    parser.add_argument("--span-time-limit", type=float, default=8.0)
    parser.add_argument("--deadline-seconds", type=float, default=5400.0)
    parser.add_argument("--low-degree-ceiling", type=int, default=4)
    parser.add_argument("--switches-per-reduction", type=int, default=20)
    parser.add_argument("--cross-addition-cap", type=int, default=30)
    parser.add_argument("--per-source-cap", type=int, default=120)
    parser.add_argument("--q1-pair-limit", type=int, default=24)
    parser.add_argument("--q1-second-pair-limit", type=int, default=24)
    parser.add_argument("--q1-delete-limit", type=int, default=8)
    parser.add_argument("--order18-source-limit", type=int, default=40)
    parser.add_argument("--order16-source-limit", type=int, default=16)
    parser.add_argument("--order16-neighbor-pool", type=int, default=7)
    args = parser.parse_args()
    if args.rank_start < 1:
        raise ValueError("rank-start must be positive")
    if args.rank_end is None:
        args.rank_end = args.classify_cap
    if args.rank_end < args.rank_start:
        raise ValueError("rank-end must be at least rank-start")
    queue_cap = max(args.classify_cap, args.rank_end)
    out = args.output_dir
    events = out / "events.jsonl"
    status = out / "status.json"
    full = out / "report.json"
    started = time.monotonic()
    existing = completed_rows(events)
    event_configuration = {key: (str(value) if isinstance(value, Path) else value) for key, value in vars(args).items()}
    append_event(events, {"event": "run_started", "configuration": event_configuration,
                          "resume_completed_ranks": len(existing)})
    selected, diagnostics = generate(args, queue_cap)
    if len(selected) < args.rank_end:
        raise RuntimeError(f"only {len(selected)} unique candidates survived; need rank {args.rank_end}")
    rank_window = list(range(args.rank_start, args.rank_end + 1))
    overlap = reconcile_prior(args.prior_report, selected, args.rank_start)
    diagnostics["ranked_queue_size_reconstructed"] = len(selected)
    diagnostics["rank_window"] = [args.rank_start, args.rank_end]
    diagnostics["rank_window_size"] = len(rank_window)
    diagnostics["rank_window_by_lane"] = dict(sorted(collections.Counter(
        selected[rank - 1][4].metadata["lane"] for rank in rank_window).items()))
    append_event(events, {"event": "generation_completed", "generation": diagnostics, "selected": len(selected),
                          "reconciliation": overlap})
    unexpected_existing = set(existing) - set(rank_window)
    if unexpected_existing:
        raise RuntimeError(f"output events contain ranks outside requested window: {sorted(unexpected_existing)[:5]}")
    for rank, row in existing.items():
        expected = selected[rank - 1][3]
        if row["canonical_sha256"] != expected:
            raise RuntimeError(f"durable row hash does not match reconstructed queue at rank {rank}")
    rows = list(existing.values())
    counts, negatives = collections.Counter(), []
    for row in rows:
        counts[row["status"]] += 1
        if row["primary_status"] == "non-colorable":
            counts["primary_noncolorable"] += 1
        if row["status"] == "confirmed_non_colorable":
            negatives.append({"candidate_id": row["candidate_id"], "canonical_sha256": row["canonical_sha256"],
                              "path": str(out / "negatives" / f"{row['candidate_id']}.graph.json"),
                              "independently_confirmed": True})
    initial = report(args, diagnostics, rows, counts, started, "running", "classification_started", negatives, rank_window, overlap)
    atomic_json(status, {key: initial[key] for key in ("goal", "completion", "stopped_reason", "elapsed_seconds", "generation", "counts")})
    atomic_json(full.with_suffix(".checkpoint.json"), initial)
    stopped_reason, interrupted = "all_selected_classified", False
    executor = concurrent.futures.ProcessPoolExecutor(max_workers=args.max_processes)
    try:
        futures = {}
        for rank in rank_window:
            if rank in existing:
                continue
            _margin, _variance, _delta, digest, graph, _metrics = selected[rank - 1]
            task = {"candidate_id": f"O17-R{rank:05d}", "rank": rank, "canonical_sha256": digest, "graph": graph.to_json(), "primary_time_limit": args.primary_time_limit, "span_time_limit": args.span_time_limit, "workers": args.solver_workers}
            futures[executor.submit(classify, task)] = (task, graph)
        for future in concurrent.futures.as_completed(futures):
            task, graph = futures[future]
            row = future.result()
            rows.append(row)
            counts[row["status"]] += 1
            if row["primary_status"] == "non-colorable":
                counts["primary_noncolorable"] += 1
            if row["status"] == "confirmed_non_colorable":
                path = out / "negatives" / f"{row['candidate_id']}.graph.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                graph.save(path)
                negatives.append({"candidate_id": row["candidate_id"], "canonical_sha256": row["canonical_sha256"], "path": str(path), "independently_confirmed": True})
            append_event(events, {"event": "classification_completed", "row": row})
            checkpoint = report(args, diagnostics, rows, counts, started, "running", stopped_reason, negatives, rank_window, overlap)
            atomic_json(full.with_suffix(".checkpoint.json"), checkpoint)
            atomic_json(status, {key: checkpoint[key] for key in ("goal", "completion", "stopped_reason", "elapsed_seconds", "generation", "counts")})
            if time.monotonic() - started >= args.deadline_seconds:
                stopped_reason, interrupted = "deadline_reached", True
                break
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    completion = "complete" if not interrupted else "stable_checkpoint"
    final = report(args, diagnostics, rows, counts, started, completion, stopped_reason, negatives, rank_window, overlap)
    atomic_json(full, final)
    atomic_json(status, {key: final[key] for key in ("goal", "completion", "stopped_reason", "elapsed_seconds", "generation", "counts")})
    append_event(events, {"event": "run_completed", "completion": completion, "counts": final["counts"]})
    print(json.dumps(final["counts"], indent=2))


if __name__ == "__main__":
    main()
