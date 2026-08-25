#!/usr/bin/env python3
"""Bounded order-18 construction lanes seeded by certified negatives."""

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
    benchmark_graphs,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
    weighted_hub_statistics,
)


NEGATIVE_PATHS = [
    Path("results/graphs/quotient-r1/Q1-00012.graph.json"),
    Path("results/graphs/quotient-r1/Q1-00014.graph.json"),
]
NEAR_MISS_SEEDS = [
    ("Q2-00132", "results/quotient-r2.json"),
    ("Q2-00144", "results/quotient-r2.json"),
    ("Q2-00185", "results/quotient-r2.json"),
    ("Q2-00196", "results/quotient-r2.json"),
    ("Q2-00261", "results/quotient-r2.json"),
    ("Q2-00263", "results/quotient-r2.json"),
    ("Q2-00287", "results/quotient-r2.json"),
    ("Q3-01006", "results/quotient-r3.json"),
    ("Q3-04474", "results/quotient-r3.json"),
]


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def append_json_line(path: Path, value: dict) -> None:
    """Durably record completed work without rewriting prior classifications."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def durable_classification_rows(events_path: Path, expected_by_rank: dict[int, str]) -> list[dict]:
    """Recover only validated, fsync'd completion events for a resumed window."""
    if not events_path.exists():
        return []
    rows_by_rank: dict[int, dict] = {}
    hashes: set[str] = set()
    with events_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid durable event at {events_path}:{line_number}"
                ) from exc
            if event.get("event") != "classification_completed":
                continue
            row = event.get("row")
            if not isinstance(row, dict):
                raise ValueError(f"classification event {line_number} has no row")
            rank = row.get("rank")
            digest = row.get("canonical_sha256")
            if rank not in expected_by_rank:
                raise ValueError(f"classification event {line_number} has unexpected rank {rank}")
            if digest != expected_by_rank[rank]:
                raise ValueError(f"classification event {line_number} has a rank/hash mismatch")
            if rank in rows_by_rank:
                raise ValueError(f"duplicate durable classification rank {rank}")
            if digest in hashes:
                raise ValueError(f"duplicate durable classification hash {digest}")
            rows_by_rank[rank] = row
            hashes.add(digest)
    return [rows_by_rank[rank] for rank in sorted(rows_by_rank)]


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


def valid_candidate(graph: Graph) -> bool:
    return (
        graph.n == 18
        and graph.bipartition is not None
        and nx.is_connected(graph._nx)
        and min(graph.degrees.values()) >= 2
    )


def identify_pairs(base: Graph, parent_id: str):
    for side_number, side in enumerate(base.bipartition):
        for first, second in itertools.combinations(sorted(side), 2):
            owner = {vertex: vertex for vertex in base.vertices}
            merged = f"{first}&{second}"
            owner[first] = merged
            owner[second] = merged
            edge_set = set()
            if any(owner[u] == owner[v] for u, v in base.edges):
                continue
            edge_set = {tuple(sorted((owner[u], owner[v]))) for u, v in base.edges}
            old_side = list(base.bipartition[side_number])
            identified = {first, second}
            reduced_side = [merged] + [
                owner[v] for v in old_side if v not in identified
            ]
            other_side = list(base.bipartition[1 - side_number])
            left, right = (
                (reduced_side, other_side)
                if side_number == 0
                else (other_side, reduced_side)
            )
            yield Graph(
                sorted(left + right),
                sorted(edge_set),
                [sorted(left), sorted(right)],
                {
                    "lane": "one-pair-same-side-identification",
                    "parent": parent_id,
                    "identified": [first, second],
                    "source_metadata": dict(base.metadata),
                },
            )


def missing_cross_edges(graph: Graph) -> list[tuple[str, str]]:
    present = set(map(tuple, graph.edges))
    return sorted(
        tuple(sorted((u, v)))
        for u in graph.bipartition[0]
        for v in graph.bipartition[1]
        if tuple(sorted((u, v))) not in present
    )


def delete_then_restore(
    base: Graph,
    parent_id: str,
    max_additions: int,
    max_deleted_degree: int = 3,
):
    retained_base = [edge for edge in map(tuple, base.edges)]
    for deleted in sorted(base.vertices):
        if base.degrees[deleted] > max_deleted_degree:
            continue
        remaining = [vertex for vertex in base.vertices if vertex != deleted]
        retained = [edge for edge in retained_base if deleted not in edge]
        survivor = Graph(remaining, retained, None)
        possible = missing_cross_edges(survivor)
        maximum = min(max_additions, len(possible))
        for count in range(1, maximum + 1):
            for additions in itertools.combinations(possible, count):
                yield Graph(
                    remaining,
                    retained + list(additions),
                    None,
                    {
                        "lane": "delete-vertex-add-cross-edges",
                        "parent": parent_id,
                        "deleted_vertex": deleted,
                        "deleted_vertex_degree": base.degrees[deleted],
                        "added_edges": [list(edge) for edge in additions],
                    },
                )


def switch_edges(base: Graph):
    original = set(map(tuple, base.edges))
    edges = sorted(original)
    for first, second in itertools.combinations(edges, 2):
        if len({*first, *second}) != 4:
            continue
        replacements = [
            (
                tuple(sorted((first[0], second[0]))),
                tuple(sorted((first[1], second[1]))),
            ),
            (
                tuple(sorted((first[0], second[1]))),
                tuple(sorted((first[1], second[0]))),
            ),
        ]
        for replacement in replacements:
            if any(edge in original for edge in replacement):
                continue
            candidate_edges = original.difference((first, second)).union(replacement)
            try:
                candidate = Graph(
                    base.vertices,
                    candidate_edges,
                    base.bipartition,
                    {
                        "lane": "two-edge-switch-parent",
                        "removed_edges": [list(first), list(second)],
                        "added_edges": [list(edge) for edge in replacement],
                        "replacement_index": replacements.index(replacement),
                    },
                )
            except ValueError:
                continue
            if nx.is_connected(candidate._nx) and min(candidate.degrees.values()) >= 2:
                yield Graph(
                    candidate.vertices,
                    candidate.edges,
                    candidate.bipartition,
                    dict(candidate.metadata),
                )


def edge_rewires(base: Graph, parent_id: str, limit: int):
    for number, candidate in enumerate(switch_edges(base)):
        if number >= limit:
            break
        candidate.metadata = {
            **candidate.metadata,
            "lane": "edge-rewire",
            "parent": parent_id,
        }
        yield candidate


def switch_then_identify(base: Graph, parent_id: str):
    for switched in switch_edges(base):
        for identified in identify_pairs(switched, parent_id):
            identified.metadata["lane"] = "two-edge-switch-then-identification"
            identified.metadata["switch"] = dict(
                identified.metadata.get("source_metadata", switched.metadata)
            )
            yield identified


def load_near_miss(candidate_id: str, report_path: Path) -> Graph:
    document = json.loads(report_path.read_text())
    row = next(
        row for row in document["rows"] if row["candidate_id"] == candidate_id
    )
    blocks = [tuple(block) for block in row["metadata"]["blocks"]]
    parent = benchmark_graphs()["hat_K34_prime_Delta11"]
    owner = {vertex: vertex for vertex in parent.vertices}
    for block in blocks:
        merged = "&".join(block)
        for vertex in block:
            owner[vertex] = merged
    edge_set = {
        tuple(sorted((owner[u], owner[v])))
        for u, v in parent.edges
        if owner[u] != owner[v]
    }
    sides = []
    for side in parent.bipartition:
        names = []
        seen = set()
        for vertex in sorted(side):
            name = owner[vertex]
            if name not in seen:
                names.append(name)
                seen.add(name)
        sides.append(names)
    return Graph(
        sorted(sides[0] + sides[1]),
        sorted(edge_set),
        sides,
        {
            "lane": "reverse-extension-seed",
            "parent": candidate_id,
            "source_report": str(report_path),
            "seed_blocks": [list(block) for block in blocks],
        },
    )


def reverse_extensions(seed: Graph, seed_id: str, extension_limit: int):
    if seed.n < 18:
        smaller_side_index = (
            0 if len(seed.bipartition[0]) <= len(seed.bipartition[1]) else 1
        )
        opposite = list(seed.bipartition[1 - smaller_side_index])
        subsets = [
            subset
            for size in range(2, min(len(opposite), 6) + 1)
            for subset in itertools.combinations(opposite, size)
        ]
        subsets.sort(key=lambda values: (-len(values), values))
        for neighbors in subsets[:extension_limit]:
            name = "NEW"
            left, right = seed.bipartition
            new_left = list(left) + ([name] if smaller_side_index == 0 else [])
            new_right = list(right) + ([name] if smaller_side_index == 1 else [])
            yield Graph(
                list(seed.vertices) + [name],
                list(seed.edges) + [(name, neighbor) for neighbor in neighbors],
                [new_left, new_right],
                {
                    "lane": "reverse-extension-new-vertex",
                    "parent": seed_id,
                    "new_vertex_neighbors": list(neighbors),
                },
            )
    else:
        possible = missing_cross_edges(seed)
        for count in range(1, min(3, len(possible)) + 1):
            for additions in itertools.combinations(possible, count):
                yield Graph(
                    seed.vertices,
                    list(seed.edges) + list(additions),
                    seed.bipartition,
                    {
                        "lane": "reverse-extension-edge-additions",
                        "parent": seed_id,
                        "added_edges": [list(edge) for edge in additions],
                    },
                )


def generate_candidates(args):
    raw = []
    bases = [
        (Graph.from_json(json.loads(path.read_text())), path.stem)
        for path in NEGATIVE_PATHS
    ]
    if args.lanes in ("all", "identifications"):
        for base, parent_id in bases:
            raw.extend(identify_pairs(base, parent_id))
    if args.lanes in ("all", "delete-restore"):
        for base, parent_id in bases:
            raw.extend(
                delete_then_restore(
                    base,
                    parent_id,
                    args.max_additions,
                    args.max_deleted_degree,
                )
            )
    if args.lanes in ("all", "edge-rewires"):
        for base, parent_id in bases:
            raw.extend(edge_rewires(base, parent_id, args.max_rewires))
    if args.lanes in ("all", "switch-identify"):
        for base, parent_id in bases:
            raw.extend(switch_then_identify(base, parent_id))
    if args.lanes in ("all", "reverse-extension"):
        for seed_id, report_name in NEAR_MISS_SEEDS:
            seed = load_near_miss(seed_id, Path(report_name))
            raw.extend(reverse_extensions(seed, seed_id, args.extension_limit))

    unique = {}
    rejected_filters = 0
    for graph in raw:
        if not valid_candidate(graph):
            rejected_filters += 1
            continue
        digest = nauty_canonical_hash(graph)
        unique.setdefault(digest, graph)

    ranked = []
    for digest, graph in unique.items():
        metrics = graph_metrics(graph)
        margin = metrics["hub_best_margin"]
        margin_tier = 0 if margin >= -1.5 else (1 if margin >= -2.5 else 2)
        ranked.append(
            (
                margin_tier,
                -margin,
                -metrics["degree_variance_normalized"],
                graph.delta,
                digest,
                graph,
                metrics,
            )
        )
    ranked.sort(key=lambda item: item[:5])
    retained = ranked[: args.candidate_cap]
    selected = retained[args.rank_start :]
    args._selected_count = len(selected)
    generated_lanes = collections.Counter(
        item.metadata["lane"] for item in unique.values()
    )
    raw_lanes = collections.Counter(item.metadata["lane"] for item in raw)
    selected_lanes = collections.Counter(
        item[-2].metadata["lane"] for item in selected
    )
    diagnostics = {
        "generated_raw": len(raw),
        "passed_filters_before_deduplication": len(raw) - rejected_filters,
        "unique_after_nauty": len(unique),
        "rejected_by_filters": rejected_filters,
        "duplicates_removed": len(raw) - rejected_filters - len(unique),
        "ranked_candidates": len(ranked),
        "retained_candidate_count": len(retained),
        "rank_window_zero_based": [args.rank_start, args.rank_start + len(selected)],
    }
    return selected, raw_lanes, generated_lanes, selected_lanes, diagnostics


def confirm_negative(graph: Graph, span_limit: float, workers: int):
    spans = {}
    lower = graph.delta
    upper = max(graph.delta, graph.n - 1)
    for span in range(lower, upper + 1):
        status, coloring = fixed_span_sat_solve(graph, span, span_limit, workers)
        spans[str(span)] = {
            "solver_status": status,
            "has_coloring": coloring is not None,
        }
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


def minimality_profile(graph: Graph, time_limit: float, workers: int) -> dict:
    checks = []
    for removed in map(tuple, graph.edges):
        child_edges = [edge for edge in graph.edges if tuple(edge) != removed]
        child = Graph(graph.vertices, child_edges, None)
        if not nx.is_connected(child._nx):
            checks.append({"operation": "delete_edge", "removed_edge": list(removed), "status": "disconnected"})
            continue
        result = rank_potential_solve(child, time_limit, workers)
        checks.append({
            "operation": "delete_edge",
            "removed_edge": list(removed),
            "status": result.status,
            "span": result.span,
        })
    for deleted in graph.vertices:
        remaining = [vertex for vertex in graph.vertices if vertex != deleted]
        retained = [edge for edge in graph.edges if deleted not in edge]
        try:
            child = Graph(remaining, retained, None)
        except ValueError:
            checks.append({"operation": "delete_vertex", "deleted_vertex": deleted, "status": "invalid_child"})
            continue
        if not nx.is_connected(child._nx):
            checks.append({"operation": "delete_vertex", "deleted_vertex": deleted, "status": "disconnected"})
            continue
        result = rank_potential_solve(child, time_limit, workers)
        checks.append({
            "operation": "delete_vertex",
            "deleted_vertex": deleted,
            "status": result.status,
            "span": result.span,
        })
    relevant = [check for check in checks if check["status"] in ("colorable", "non-colorable")]
    return {
        "method": "rank-potential CP-SAT",
        "time_limit_seconds_per_child": time_limit,
        "checks": checks,
        "noncolorable_children": [
            check for check in relevant if check["status"] == "non-colorable"
        ],
        "timeout_children": [check for check in checks if check["status"] == "timeout"],
        "minimal_under_checked_single_deletions": all(
            check["status"] == "colorable"
            for check in checks
            if check["status"] in ("colorable", "non-colorable")
        ) and not any(check["status"] == "timeout" for check in checks),
    }


def classify_one(task: dict) -> dict:
    graph = Graph.from_json(task["graph"])
    if not valid_candidate(graph):
        raise AssertionError("candidate failed validation")
    started = time.perf_counter()
    primary = rank_potential_solve(graph, task["primary_time_limit"], task["workers"])
    row = {
        "candidate_id": task["candidate_id"],
        "canonical_sha256": task["canonical_sha256"],
        "order": graph.n,
        "size": graph.m,
        "bipartition_sizes": list(map(len, graph.bipartition)),
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "degrees": graph.degrees,
        "metadata": graph.metadata,
        **graph_metrics(graph),
        "primary_status": primary.status,
        "primary_span": primary.span,
        "primary_solver_status": primary.solver_status,
        "primary_elapsed_seconds": primary.elapsed_seconds,
    }
    if primary.status == "colorable":
        coloring = {tuple(edge): color for edge, color in (primary.coloring or {}).items()}
        ok, reason = verify_coloring(graph, coloring)
        if not ok:
            raise AssertionError(reason)
        row["status"] = "colorable"
    elif primary.status == "timeout":
        row["status"] = "timeout"
    elif primary.status == "non-colorable":
        result, spans = confirm_negative(graph, task["span_time_limit"], task["workers"])
        row["independent_confirmation"] = {
            "encoding": "fixed-span CP-SAT",
            "span_range_inclusive": [graph.delta, graph.n - 1],
            "spans": spans,
            "time_limit_per_span_seconds": task["span_time_limit"],
        }
        row["status"] = result
        if result == "confirmed_non_colorable" and task["check_minimality"]:
            row["minimality"] = minimality_profile(
                graph, task["minimality_time_limit"], task["workers"]
            )
    else:
        raise AssertionError(f"unexpected primary status {primary.status}")
    row["classification_elapsed_seconds"] = time.perf_counter() - started
    return row


def make_report(args, diagnostics, raw_lanes, generated_lanes, selected_lanes, rows, counts, stopped_reason, negative_events, started, complete):
    classified = len(rows)
    return {
        "schema_version": 1,
        "goal": "find an interval-non-colorable simple connected bipartite graph on exactly 18 vertices",
        "configuration": {
            "lanes_requested": args.lanes,
            "bounded_candidate_cap": args.candidate_cap,
            "max_processes": args.max_processes,
            "solver_workers_per_process": args.solver_workers,
            "primary_solver": "rank-potential CP-SAT",
            "primary_time_limit_seconds": args.primary_time_limit,
            "retained_candidate_cap": args.candidate_cap,
            "classification_cap": min(args.classify_cap, args.candidate_cap),
            "rank_start_zero_based": args.rank_start,
            "rank_window_one_based": [args.rank_start + 1, args.candidate_cap],
            "classification_rank_window_one_based": [
                args.rank_start + 1,
                min(args.candidate_cap, args.rank_start + args.classify_cap),
            ],
            "independent_confirmation": "fixed-span CP-SAT over every legal span",
            "span_time_limit_seconds": args.span_time_limit,
            "timeout_policy": "UNKNOWN is unresolved and is never counted non-colorable",
            "deduplication": "Nauty bipartition-colored canonical SHA-256",
            "filters": ["exactly 18 vertices", "simple bipartite", "connected", "minimum degree >= 2"],
            "ranking": ["hub_best_margin descending", "delta descending", "canonical hash"],
            "structural_ranking_source": "results/structural-obstruction-findings.json",
            "maximum_additions_after_deletion": args.max_additions,
            "maximum_rewires_per_negative": args.max_rewires,
            "reverse_extension_limit_per_seed": args.extension_limit,
            "minimality_checks_enabled": not args.skip_minimality,
            "minimality_time_limit_seconds": args.minimality_time_limit,
            "deadline_seconds": args.deadline_seconds,
            "resume_from_durable_events": args.resume,
        },
        "generation": {
            **diagnostics,
            "generated_raw_by_lane": dict(sorted(raw_lanes.items())),
            "unique_ranked_by_lane": dict(sorted(generated_lanes.items())),
            "selected_by_lane": dict(sorted(selected_lanes.items())),
        },
        "counts": {
            "generated": diagnostics["generated_raw"],
            "unique": diagnostics["unique_after_nauty"],
            "selected_for_classification": getattr(args, "_selected_count", classified),
            "classified": classified,
            "colorable": counts["colorable"],
            "primary_noncolorable": counts["primary_noncolorable"],
            "non_colorable_certified": counts["confirmed_non_colorable"],
            "confirmation_timeout": counts["confirmation_timeout"],
            "primary_timeout": counts["timeout"],
            "completion_count": classified,
        },
        "generated": diagnostics["generated_raw"],
        "unique": diagnostics["unique_after_nauty"],
        "classified": classified,
        "colorable": counts["colorable"],
        "non_colorable": counts["confirmed_non_colorable"],
        "timeout": counts["timeout"] + counts["confirmation_timeout"],
        "completion": "complete" if complete else "partial_classification_complete",
        "completion_details": {
            "status": "complete" if complete else "partial_classification_complete",
            "stopped_reason": stopped_reason,
        },
        "negative_events": negative_events,
        "elapsed_seconds": time.monotonic() - started,
        "rows": rows,
    }


def compact_status(report: dict) -> dict:
    """Keep a small, frequently refreshed status beside the full checkpoint."""
    return {
        "schema_version": report["schema_version"],
        "goal": report["goal"],
        "completion": report["completion"],
        "completion_details": report["completion_details"],
        "elapsed_seconds": report["elapsed_seconds"],
        "configuration": {
            key: report["configuration"][key]
            for key in (
                "lanes_requested",
                "rank_window_one_based",
                "classification_rank_window_one_based",
                "primary_solver",
                "primary_time_limit_seconds",
                "span_time_limit_seconds",
                "filters",
            )
        },
        "generation": report["generation"],
        "counts": report["counts"],
        "negative_events": report["negative_events"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("results/order18-targeted-search.json"))
    parser.add_argument("--negative-dir", type=Path, default=Path("results/graphs/order18-targeted-search"))
    parser.add_argument(
        "--lanes",
        choices=(
            "all",
            "identifications",
            "delete-restore",
            "edge-rewires",
            "switch-identify",
            "reverse-extension",
        ),
        default="all",
    )
    parser.add_argument("--candidate-cap", type=int, default=2000)
    parser.add_argument(
        "--rank-start",
        type=int,
        default=0,
        help="Zero-based offset into the deterministically ranked retained queue.",
    )
    parser.add_argument("--classify-cap", type=int, default=500)
    parser.add_argument("--max-processes", type=int, default=3)
    parser.add_argument("--solver-workers", type=int, default=2)
    parser.add_argument("--primary-time-limit", type=float, default=7.5)
    parser.add_argument("--span-time-limit", type=float, default=15.0)
    parser.add_argument("--max-additions", type=int, default=2)
    parser.add_argument("--max-deleted-degree", type=int, default=3)
    parser.add_argument("--max-rewires", type=int, default=500)
    parser.add_argument("--extension-limit", type=int, default=80)
    parser.add_argument("--skip-minimality", action="store_true")
    parser.add_argument("--minimality-time-limit", type=float, default=3.0)
    parser.add_argument("--deadline-seconds", type=float, default=3600.0)
    parser.add_argument(
        "--events-path",
        type=Path,
        help="Append-only JSONL log for generation and per-candidate completion events.",
    )
    parser.add_argument(
        "--status-path",
        type=Path,
        help="Small atomically refreshed summary written alongside the full checkpoint.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse only validated completion rows already durably recorded in events-path.",
    )
    args = parser.parse_args()

    run_started = time.monotonic()
    counts = collections.Counter()
    rows = []
    negative_events = []
    stopped_reason = "all_selected_classified"
    interrupted = False
    selected: list = []
    raw_lanes = collections.Counter()
    generated_lanes = collections.Counter()
    selected_lanes = collections.Counter()
    diagnostics = {
        "generated_raw": 0,
        "passed_filters_before_deduplication": 0,
        "unique_after_nauty": 0,
        "rejected_by_filters": 0,
        "duplicates_removed": 0,
    }
    executor = concurrent.futures.ProcessPoolExecutor(max_workers=args.max_processes)
    try:
        if args.rank_start < 0 or args.rank_start >= args.candidate_cap:
            raise ValueError("rank-start must be in [0, candidate-cap)")
        if args.events_path:
            append_json_line(args.events_path, {
                "event": "run_started",
                "candidate_cap": args.candidate_cap,
                "rank_start_zero_based": args.rank_start,
                "lanes": args.lanes,
            })
        initial = make_report(
            args,
            diagnostics,
            raw_lanes,
            generated_lanes,
            selected_lanes,
            rows,
            counts,
            "generating_candidates",
            negative_events,
            run_started,
            complete=False,
        )
        atomic_json(args.output.with_suffix(".checkpoint.json"), initial)
        atomic_json(args.output, initial)
        if args.status_path:
            atomic_json(args.status_path, compact_status(initial))
        print(json.dumps({"phase": "generation_started"}), flush=True)

        (
            selected,
            raw_lanes,
            generated_lanes,
            selected_lanes,
            diagnostics,
        ) = generate_candidates(args)
        selected = selected[: min(args.classify_cap, len(selected))]
        expected_by_rank = {
            args.rank_start + number + 1: digest
            for number, (_, _, _, _, digest, _graph, _metrics) in enumerate(selected)
        }
        if args.resume:
            if not args.events_path:
                raise ValueError("--resume requires --events-path")
            rows = durable_classification_rows(args.events_path, expected_by_rank)
            for row in rows:
                counts[row["status"]] += 1
                if row["primary_status"] == "non-colorable":
                    counts["primary_noncolorable"] += 1
        generation_complete = make_report(
            args,
            diagnostics,
            raw_lanes,
            generated_lanes,
            selected_lanes,
            rows,
            counts,
            "classification_started",
            negative_events,
            run_started,
            complete=False,
        )
        atomic_json(args.output.with_suffix(".checkpoint.json"), generation_complete)
        atomic_json(args.output, generation_complete)
        if args.status_path:
            atomic_json(args.status_path, compact_status(generation_complete))
        print(json.dumps({"phase": "classification_started", **diagnostics}), flush=True)

        futures = {}
        completed_ranks = {row["rank"] for row in rows}
        for number, (_, _, _, _, digest, graph, _metrics) in enumerate(selected):
            task = {
                "candidate_id": f"O18-R{args.rank_start + number + 1:05d}",
                "rank": args.rank_start + number + 1,
                "canonical_sha256": digest,
                "graph": graph.to_json(),
                "primary_time_limit": args.primary_time_limit,
                "span_time_limit": args.span_time_limit,
                "workers": args.solver_workers,
                "check_minimality": not args.skip_minimality,
                "minimality_time_limit": args.minimality_time_limit,
            }
            if task["rank"] in completed_ranks:
                continue
            futures[executor.submit(classify_one, task)] = (task, graph)
        for future in concurrent.futures.as_completed(futures):
            task, graph = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                stopped_reason = f"worker_error:{type(exc).__name__}:{exc}"
                interrupted = True
                break
            rows.append(row)
            row["rank"] = task["rank"]
            counts[row["status"]] += 1
            if row["primary_status"] == "non-colorable":
                counts["primary_noncolorable"] += 1
            if args.events_path:
                append_json_line(args.events_path, {"event": "classification_completed", "row": row})
            checkpoint = make_report(args, diagnostics, raw_lanes, generated_lanes, selected_lanes, rows, counts, stopped_reason, negative_events, run_started, complete=False)
            atomic_json(args.output.with_suffix(".checkpoint.json"), checkpoint)
            if args.status_path:
                atomic_json(args.status_path, compact_status(checkpoint))
            print(json.dumps({"completed": len(rows), "total": len(selected), "counts": dict(counts)}), flush=True)
            if row["status"] == "confirmed_non_colorable":
                path = args.negative_dir / f"{row['candidate_id']}.graph.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                graph.save(path)
                negative_events.append({
                    "candidate_id": row["candidate_id"],
                    "canonical_sha256": row["canonical_sha256"],
                    "path": str(path),
                    "independently_confirmed": True,
                    "minimality_status": row.get("minimality", {}).get("minimal_under_checked_single_deletions"),
                })
            if time.monotonic() - run_started >= args.deadline_seconds:
                stopped_reason = "deadline_reached"
                interrupted = True
                break
        if not interrupted and len(rows) == len(selected):
            stopped_reason = "all_selected_classified"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        try:
            report = make_report(
                args,
                diagnostics,
                raw_lanes,
                generated_lanes,
                selected_lanes,
                rows,
                counts,
                stopped_reason,
                negative_events,
                run_started,
                complete=not interrupted and stopped_reason == "all_selected_classified",
            )
            atomic_json(args.output, report)
            if args.status_path:
                atomic_json(args.status_path, compact_status(report))
        except Exception as report_exc:
            print(json.dumps({"report_error": str(report_exc)}), flush=True)
            raise

    print(json.dumps({key: report[key] for key in ("counts", "completion", "negative_events")}, indent=2), flush=True)


if __name__ == "__main__":
    main()
