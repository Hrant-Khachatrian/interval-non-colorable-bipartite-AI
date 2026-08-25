#!/usr/bin/env python3
"""A disjoint order-18 interval-colorability campaign based on edge surgery.

This family intentionally avoids the first queue's reverse-extension focus.  It
starts with the two certified 19-vertex quotient witnesses and the documented
Q2/Q3 near misses.  Every output is a simple connected bipartite graph on 18
vertices with minimum degree at least two.
"""

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
from types import SimpleNamespace

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


ROOT = Path(__file__).resolve().parents[1]
CERTIFIED_WITNESSES = (
    ROOT / "results/candidates/Q1-00012/Q1-00012.graph.json",
    ROOT / "results/candidates/Q1-00014/Q1-00014.graph.json",
)
NEAR_MISS_IDS = (
    ("Q2-00132", ROOT / "results/quotient-r2.json"),
    ("Q2-00144", ROOT / "results/quotient-r2.json"),
    ("Q2-00185", ROOT / "results/quotient-r2.json"),
    ("Q2-00196", ROOT / "results/quotient-r2.json"),
    ("Q2-00261", ROOT / "results/quotient-r2.json"),
    ("Q2-00263", ROOT / "results/quotient-r2.json"),
    ("Q2-00287", ROOT / "results/quotient-r2.json"),
    ("Q3-01006", ROOT / "results/quotient-r3.json"),
    ("Q3-04474", ROOT / "results/quotient-r3.json"),
)


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


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(event, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def valid(graph: Graph) -> bool:
    return (
        graph.n == 18
        and graph.bipartition is not None
        and nx.is_connected(graph._nx)
        and min(graph.degrees.values()) >= 2
    )


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


def identify_same_side(base: Graph, parent: str):
    """One 19 -> 18 quotient; the subsequent edge surgery is the new lane."""
    for side_number, side in enumerate(base.bipartition):
        for first, second in itertools.combinations(sorted(side), 2):
            owner = {vertex: vertex for vertex in base.vertices}
            merged = f"{first}&{second}"
            owner[first] = merged
            owner[second] = merged
            edges = {tuple(sorted((owner[u], owner[v]))) for u, v in base.edges}
            old = list(base.bipartition[side_number])
            reduced = [merged] + [owner[v] for v in old if v not in {first, second}]
            other = list(base.bipartition[1 - side_number])
            left, right = (reduced, other) if side_number == 0 else (other, reduced)
            yield Graph(
                sorted(left + right),
                sorted(edges),
                [sorted(left), sorted(right)],
                {"parent": parent, "identified": [first, second]},
            )


def missing_cross_edges(graph: Graph) -> list[tuple[str, str]]:
    present = set(map(tuple, graph.edges))
    return [
        tuple(sorted((u, v)))
        for u in graph.bipartition[0]
        for v in graph.bipartition[1]
        if tuple(sorted((u, v))) not in present
    ]


def delete_restore(base: Graph, limit: int):
    """Remove one protected edge and restore degree with a distinct cross edge."""
    original = set(map(tuple, base.edges))
    degree = base.degrees
    emitted = 0
    for removed in sorted(original):
        if min(degree[removed[0]], degree[removed[1]]) < 3:
            continue
        for restored in missing_cross_edges(base):
            edges = original.difference({removed}) | {restored}
            candidate = Graph(
                base.vertices,
                edges,
                base.bipartition,
                {
                    **base.metadata,
                    "lane": "identify-then-edge-delete-restore",
                    "removed_edge": list(removed),
                    "restored_edge": list(restored),
                },
            )
            if valid(candidate):
                yield candidate
                emitted += 1
                if emitted >= limit:
                    return


def three_edge_switches(base: Graph, limit: int, lane: str):
    """Deterministic cyclic three-edge switches preserve all degrees exactly."""
    left = set(base.bipartition[0])
    original = set(map(tuple, base.edges))
    oriented = [
        (u, v) if u in left else (v, u)
        for u, v in sorted(original)
    ]
    emitted = 0
    for triple in itertools.combinations(oriented, 3):
        if len({vertex for edge in triple for vertex in edge}) != 6:
            continue
        for permutation in ((1, 2, 0), (2, 0, 1)):
            replacements = tuple(
                tuple(sorted((triple[i][0], triple[permutation[i]][1])))
                for i in range(3)
            )
            if len(set(replacements)) != 3 or any(edge in original for edge in replacements):
                continue
            candidate = Graph(
                base.vertices,
                original.difference(triple).union(replacements),
                base.bipartition,
                {
                    **base.metadata,
                    "lane": lane,
                    "removed_edges": [list(tuple(sorted(edge))) for edge in triple],
                    "added_edges": [list(edge) for edge in replacements],
                },
            )
            if valid(candidate):
                yield candidate
                emitted += 1
                if emitted >= limit:
                    return


def quotient_from_row(candidate_id: str, report: Path) -> Graph:
    document = json.loads(report.read_text(encoding="utf-8"))
    row = next(row for row in document["rows"] if row["candidate_id"] == candidate_id)
    parent = benchmark_graphs()["hat_K34_prime_Delta11"]
    owner = {vertex: vertex for vertex in parent.vertices}
    for block in row["metadata"]["blocks"]:
        merged = "&".join(sorted(block))
        for vertex in block:
            owner[vertex] = merged
    edges = {
        tuple(sorted((owner[u], owner[v])))
        for u, v in parent.edges
        if owner[u] != owner[v]
    }
    sides = []
    for side in parent.bipartition:
        seen = set()
        result = []
        for vertex in sorted(side):
            name = owner[vertex]
            if name not in seen:
                result.append(name)
                seen.add(name)
        sides.append(result)
    return Graph(
        sorted(sides[0] + sides[1]), sorted(edges), sides,
        {
            "parent": candidate_id,
            "source_report": str(report.relative_to(ROOT)),
            "source_blocks": row["metadata"]["blocks"],
        },
    )


def prior_queue_hashes() -> set[str]:
    """Reconstruct the whole completed 12,987-candidate queue by its generator."""
    import order18_targeted_search as prior

    args = SimpleNamespace(
        lanes="all",
        max_additions=1,
        max_deleted_degree=3,
        max_rewires=750,
        extension_limit=18,
        candidate_cap=20000,
        rank_start=0,
    )
    selected, *_ = prior.generate_candidates(args)
    return {item[4] for item in selected}


def generate(restore_limit: int, witness_switch_limit: int, near_switch_limit: int):
    raw: list[Graph] = []
    for path in CERTIFIED_WITNESSES:
        witness = Graph.from_json(json.loads(path.read_text(encoding="utf-8")))
        for quotient in identify_same_side(witness, path.stem):
            raw.extend(delete_restore(quotient, restore_limit))
            raw.extend(three_edge_switches(
                quotient, witness_switch_limit, "identify-then-three-edge-switch"
            ))
    for candidate_id, report in NEAR_MISS_IDS:
        near_miss = quotient_from_row(candidate_id, report)
        raw.extend(three_edge_switches(
            near_miss, near_switch_limit, "near-miss-three-edge-switch"
        ))

    valid_raw = [graph for graph in raw if valid(graph)]
    raw_lanes = collections.Counter(graph.metadata["lane"] for graph in raw)
    valid_lanes = collections.Counter(graph.metadata["lane"] for graph in valid_raw)
    own = {}
    for graph in valid_raw:
        own.setdefault(nauty_canonical_hash(graph), graph)
    prior_hashes = prior_queue_hashes()
    overlap = sorted(set(own) & prior_hashes)
    fresh = {digest: graph for digest, graph in own.items() if digest not in prior_hashes}
    ranked = []
    for digest, graph in fresh.items():
        metrics = graph_metrics(graph)
        ranked.append((
            -metrics["hub_best_margin"],
            -graph.delta,
            -metrics["degree_variance_normalized"],
            digest,
            graph,
            metrics,
        ))
    ranked.sort(key=lambda row: row[:4])
    diagnostics = {
        "generated_raw": len(raw),
        "generated_raw_by_lane": dict(sorted(raw_lanes.items())),
        "passed_graph_filters": len(valid_raw),
        "passed_graph_filters_by_lane": dict(sorted(valid_lanes.items())),
        "unique_before_prior_queue_filter": len(own),
        "self_duplicates_removed": len(valid_raw) - len(own),
        "prior_queue_hash_count": len(prior_hashes),
        "overlap_with_completed_first_queue": len(overlap),
        "overlap_hash_sample": overlap[:20],
        "unique_new_after_global_filter": len(fresh),
    }
    return ranked, diagnostics


def confirm_negative(graph: Graph, seconds: float, workers: int) -> tuple[str, dict]:
    spans = {}
    for span in range(graph.delta, graph.n):
        status, coloring = fixed_span_sat_solve(graph, span, seconds, workers=workers)
        spans[str(span)] = {"solver_status": status, "has_coloring": coloring is not None}
        if status in {"OPTIMAL", "FEASIBLE"}:
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
    started = time.perf_counter()
    primary = rank_potential_solve(graph, task["primary_seconds"], task["workers"])
    row = {
        "rank": task["rank"],
        "candidate_id": f"O18-AF-{task['rank']:05d}",
        "canonical_sha256": task["digest"],
        "order": graph.n,
        "size": graph.m,
        "bipartition_sizes": list(map(len, graph.bipartition)),
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "metadata": graph.metadata,
        **graph_metrics(graph),
        "primary_status": primary.status,
        "primary_span": primary.span,
        "primary_solver_status": primary.solver_status,
        "primary_elapsed_seconds": primary.elapsed_seconds,
    }
    if primary.status == "colorable":
        ok, reason = verify_coloring(graph, primary.coloring or {})
        if not ok:
            raise AssertionError(reason)
        row["status"] = "colorable"
    elif primary.status == "timeout":
        row["status"] = "timeout"
    elif primary.status == "non-colorable":
        outcome, spans = confirm_negative(graph, task["span_seconds"], task["workers"])
        row["status"] = outcome
        row["independent_confirmation"] = {
            "encoding": "fixed-span CP-SAT",
            "span_range_inclusive": [graph.delta, graph.n - 1],
            "time_limit_per_span_seconds": task["span_seconds"],
            "spans": spans,
        }
    else:
        raise AssertionError(f"unexpected primary result {primary.status}")
    row["classification_elapsed_seconds"] = time.perf_counter() - started
    return row


def load_completed(events: Path, expected: dict[int, str]) -> list[dict]:
    if not events.exists():
        return []
    completed = {}
    expected_hashes = set(expected.values())
    for number, line in enumerate(events.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        if event.get("event") != "classification_completed":
            continue
        row = event.get("row", {})
        rank, digest = row.get("rank"), row.get("canonical_sha256")
        # Queue ordering can change when multiple constructions collapse to an
        # isomorphism class.  The canonical graph hash, rather than its rank,
        # is the durable identity of a completed classification.
        if digest not in expected_hashes or rank in completed:
            raise ValueError(f"invalid or duplicate durable event at line {number}")
        completed[rank] = row
    return [completed[rank] for rank in sorted(completed)]


def completed_hashes(events: Path) -> set[str]:
    """Read durable classifications from an earlier queue without trusting rank."""
    if not events.exists():
        raise FileNotFoundError(f"missing prior classification state: {events}")
    result = set()
    for number, line in enumerate(events.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        if event.get("event") != "classification_completed":
            continue
        digest = event.get("row", {}).get("canonical_sha256")
        if not isinstance(digest, str):
            raise ValueError(f"missing canonical hash in prior state at line {number}")
        result.add(digest)
    return result


def report(args, diagnostics, rows, complete: bool, reason: str, elapsed: float) -> dict:
    counts = collections.Counter(row["status"] for row in rows)
    lane_counts = collections.Counter(row["metadata"]["lane"] for row in rows)
    return {
        "schema_version": 1,
        "goal": "classify a globally disjoint structured family of simple connected bipartite order-18 candidates",
        "completion": "complete" if complete else "partial",
        "completion_details": {"status": "complete" if complete else "partial", "reason": reason},
        "configuration": {
            "primary_solver": "rank-potential CP-SAT",
            "primary_time_limit_seconds": args.primary_seconds,
            "independent_negative_confirmation": "fixed-span CP-SAT across every legal span",
            "fixed_span_time_limit_seconds": args.span_seconds,
            "operations": [
                "same-side identification followed by one-edge deletion/restoration",
                "deterministic cyclic three-edge switches",
            ],
            "filters": ["exactly 18 vertices", "simple bipartite", "connected", "minimum degree >= 2"],
            "global_deduplication": "Nauty bipartition-colored canonical SHA-256 against reconstructed first queue",
            "timeout_policy": "Only UNKNOWN outcomes are timeout/unresolved; no timeout is counted as non-colorable.",
        },
        "generation": diagnostics,
        "counts": {
            "generated": diagnostics["generated_raw"],
            "unique": diagnostics["unique_new_after_global_filter"],
            "classified": len(rows),
            "colorable": counts["colorable"],
            "non_colorable": counts["confirmed_non_colorable"],
            "timeout": counts["timeout"] + counts["confirmation_timeout"],
            "primary_noncolorable": sum(row["primary_status"] == "non-colorable" for row in rows),
            "classified_by_lane": dict(sorted(lane_counts.items())),
        },
        "negative_events": [
            {key: row[key] for key in ("rank", "candidate_id", "canonical_sha256", "status")}
            for row in rows if row["status"] == "confirmed_non_colorable"
        ],
        "ranked_preview": [
            {key: row[key] for key in ("rank", "candidate_id", "canonical_sha256", "delta", "size", "hub_best_margin", "degree_variance_normalized", "status")}
            for row in sorted(rows, key=lambda row: row["rank"])[:12]
        ],
        "events_path": str(args.events.relative_to(ROOT)),
        "elapsed_seconds": elapsed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/order18-alternate-family-v1")
    parser.add_argument("--classify-cap", type=int, default=1200)
    parser.add_argument("--restore-limit", type=int, default=14)
    parser.add_argument("--witness-switch-limit", type=int, default=20)
    parser.add_argument("--near-switch-limit", type=int, default=180)
    parser.add_argument("--primary-seconds", type=float, default=3.0)
    parser.add_argument("--span-seconds", type=float, default=5.0)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--solver-workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--exclude-events", type=Path, action="append", default=[],
        help="append-only event logs whose completed graph hashes must not be revisited",
    )
    parser.add_argument(
        "--minimum-selected", type=int, default=1000,
        help="fail if fewer than this many globally fresh, previously unsolved candidates remain",
    )
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    args.events = args.output_dir / "classification-events.jsonl"
    started = time.monotonic()
    ranked, diagnostics = generate(args.restore_limit, args.witness_switch_limit, args.near_switch_limit)
    excluded_hashes = set()
    for path in args.exclude_events:
        excluded_hashes.update(completed_hashes(path.resolve()))
    if excluded_hashes:
        ranked = [row for row in ranked if row[3] not in excluded_hashes]
    diagnostics["previously_solved_excluded"] = len(excluded_hashes)
    diagnostics["new_unique_after_prior_solutions"] = len(ranked)
    selected = ranked[:args.classify_cap]
    if len(selected) < args.minimum_selected:
        raise RuntimeError(
            f"only {len(selected)} globally fresh, previously unsolved candidates; "
            f"require at least {args.minimum_selected}"
        )
    expected = {index + 1: row[3] for index, row in enumerate(selected)}
    rows = load_completed(args.events, expected) if args.resume else []
    if not args.resume and args.events.exists():
        raise FileExistsError(f"state already exists at {args.events}; rerun with --resume")
    append_event(args.events, {"event": "run_started", "selected": len(selected), "resume": args.resume})
    complete = False
    reason = "classification_running"
    try:
        completed = {row["rank"] for row in rows}
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.processes) as executor:
            pending = {}
            for index, item in enumerate(selected, 1):
                if index in completed:
                    continue
                _, _, _, digest, graph, _ = item
                task = {
                    "rank": index, "digest": digest, "graph": graph.to_json(),
                    "primary_seconds": args.primary_seconds, "span_seconds": args.span_seconds,
                    "workers": args.solver_workers,
                }
                pending[executor.submit(classify, task)] = task
            for future in concurrent.futures.as_completed(pending):
                row = future.result()
                rows.append(row)
                append_event(args.events, {"event": "classification_completed", "row": row})
                current = report(args, diagnostics, rows, False, "classification_running", time.monotonic() - started)
                atomic_json(args.output_dir / "status.json", current)
                print(json.dumps({"classified": len(rows), "counts": current["counts"]}), flush=True)
        complete = len(rows) == len(selected)
        reason = "all_selected_classified" if complete else "incomplete"
    except Exception as exc:
        reason = f"worker_error:{type(exc).__name__}:{exc}"
        raise
    finally:
        final = report(args, diagnostics, rows, complete, reason, time.monotonic() - started)
        atomic_json(args.output_dir / "report.json", final)
        atomic_json(args.output_dir / "status.json", final)
    print(json.dumps(final["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
