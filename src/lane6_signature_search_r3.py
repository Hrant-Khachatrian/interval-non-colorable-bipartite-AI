#!/usr/bin/env python3
"""Round 3: two disjoint five-vertex synchronizers with paired extremes.

The family is deliberately narrower than a raw six-internal-vertex sweep.  Each
synchronizer has exactly five internal vertices, simple degree-two terminal
boundaries.  The default boundary table contains both a maximum-rank0 to
minimum-rank1 state and the reverse state; round 2 accepted either one alone.
The seed-hub connectors use an exactly balanced six-and-six split.  The two
synchronizers are vertex-disjoint except for the two hub terminals.
"""

from __future__ import annotations

import argparse
import itertools
import json
import platform
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import networkx as nx

from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)
from lane6_signature_search import Signature, terminal_signatures
from lane1_search import seed_graph


TOTAL_TIME_LIMIT_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class Synchronizer:
    vertices: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    signature: tuple[Signature, ...]
    key: str
    maximum_internal_degree: int


def _ordered_edge(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _connected(vertices: Iterable[str], edges: Iterable[tuple[str, str]]) -> bool:
    vertex_list = list(vertices)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        adjacency[a].add(b)
        adjacency[b].add(a)
    start = vertex_list[0]
    stack = [start]
    seen = {start}
    while stack:
        vertex = stack.pop()
        for neighbor in adjacency[vertex]:
            if neighbor not in seen:
                seen.add(neighbor)
                stack.append(neighbor)
    return len(seen) == len(vertex_list)


def enumerate_tight_shapes(
    internal_count: int,
    maximum_internal_degree: int,
    simple_terminal_boundary: bool,
) -> list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]]:
    """Enumerate connected labelled two-terminal bipartite synchronizer shapes."""

    terminals = ("T0", "T1")
    shapes_by_edges: dict[
        frozenset[tuple[str, str]],
        tuple[tuple[str, ...], tuple[tuple[str, str], ...]],
    ] = {}
    for left_count in range(internal_count + 1):
        right_count = internal_count - left_count
        left = [f"L{i}" for i in range(left_count)]
        right = list(terminals) + [f"R{i}" for i in range(right_count)]
        possible = [_ordered_edge(a, b) for a in left for b in right]
        for edge_count in range(2, len(possible) + 1):
            for chosen in itertools.combinations(possible, edge_count):
                degree: dict[str, int] = defaultdict(int)
                for a, b in chosen:
                    degree[a] += 1
                    degree[b] += 1
                vertices = left + right
                if any(degree.get(vertex, 0) == 0 for vertex in vertices):
                    continue
                if any(
                    vertex not in terminals
                    and degree.get(vertex, 0) > maximum_internal_degree
                    for vertex in vertices
                ):
                    continue
                if simple_terminal_boundary and any(
                    degree.get(terminal, 0) != 1 for terminal in terminals
                ):
                    continue
                if not _connected(vertices, chosen):
                    continue
                edge_set = frozenset(chosen)
                shapes_by_edges[edge_set] = (
                    tuple(vertices),
                    tuple(sorted(chosen)),
                )
    return sorted(
        shapes_by_edges.values(),
        key=lambda item: (item[0], item[1]),
    )


def _maximum_internal_degree(
    vertices: tuple[str, ...], edges: tuple[tuple[str, str], ...]
) -> int:
    degree: dict[str, int] = defaultdict(int)
    for a, b in edges:
        degree[a] += 1
        degree[b] += 1
    return max(
        (degree.get(vertex, 0) for vertex in vertices if vertex not in ("T0", "T1")),
        default=0,
    )


def build_synchronizers(
    shapes: list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]],
    signature_time_limit: float,
    maximum_signature_states: int,
    deadline: float,
) -> tuple[list[Synchronizer], dict]:
    synchronizers: list[Synchronizer] = []
    excluded_timeout_or_cap = 0
    started = time.perf_counter()

    for shape_index, (vertices, edges) in enumerate(shapes):
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            excluded_timeout_or_cap += len(shapes) - shape_index
            break

        augmented_vertices = ("X0", "X1") + vertices
        augmented_edges = (
            _ordered_edge("X0", "T0"),
            _ordered_edge("X1", "T1"),
        ) + edges
        augmented = Graph(
            augmented_vertices,
            augmented_edges,
            [
                ["X0", "X1"] + [v for v in vertices if v.startswith("L")],
                [v for v in vertices if not v.startswith("L")],
            ],
        )
        signature = terminal_signatures(
            augmented,
            "T0",
            "T1",
            maximum_signature_states,
            min(signature_time_limit, remaining),
        )
        if signature is None:
            excluded_timeout_or_cap += 1
            continue
        synchronizers.append(
            Synchronizer(
                vertices,
                edges,
                tuple(sorted(signature, key=Signature.sort_key)),
                json.dumps(edges, separators=(",", ":")),
                _maximum_internal_degree(vertices, edges),
            )
        )

    signature_sizes = Counter(len(item.signature) for item in synchronizers)
    stats = {
        "raw_shapes": len(shapes),
        "solved_gadgets": len(synchronizers),
        "excluded_signature_timeout_or_cap": excluded_timeout_or_cap,
        "signature_stage_completed": len(synchronizers) + excluded_timeout_or_cap
        == len(shapes),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "signature_size_counts": {
            str(size): count for size, count in sorted(signature_sizes.items())
        },
    }
    return synchronizers, stats


def _states_at_max_rank0(signature: tuple[Signature, ...]) -> list[Signature]:
    largest = max(item.rank0 for item in signature)
    return [item for item in signature if item.rank0 == largest]


def _states_at_max_rank1(signature: tuple[Signature, ...]) -> list[Signature]:
    largest = max(item.rank1 for item in signature)
    return [item for item in signature if item.rank1 == largest]


def _has_forward_extreme_state(signature: tuple[Signature, ...]) -> bool:
    states = _states_at_max_rank0(signature)
    return any(state.rank1 == 0 for state in states)


def _has_reverse_extreme_state(signature: tuple[Signature, ...]) -> bool:
    states = _states_at_max_rank1(signature)
    return any(state.rank0 == 0 for state in states)


def accepts_asymmetric_boundary(
    signature: tuple[Signature, ...], boundary_mode: str
) -> bool:
    forward = _has_forward_extreme_state(signature)
    reverse = _has_reverse_extreme_state(signature)
    if boundary_mode == "forward_extreme_state_no_reverse":
        return forward and not reverse
    if boundary_mode == "reverse_extreme_state_no_forward":
        return reverse and not forward
    if boundary_mode == "paired_forward_and_reverse_extreme_states":
        return forward and reverse
    raise ValueError(f"unknown boundary mode: {boundary_mode}")


def select_synchronizers(
    synchronizers: list[Synchronizer], boundary_mode: str, maximum_types: int
) -> tuple[list[Synchronizer], dict]:
    selected_all = [
        item
        for item in synchronizers
        if accepts_asymmetric_boundary(item.signature, boundary_mode)
    ]
    selected_all.sort(
        key=lambda item: (
            len(item.edges),
            item.maximum_internal_degree,
            len(item.signature),
            item.key,
        )
    )
    selected = selected_all[:maximum_types]
    stats = {
        "selected_gadgets": len(selected_all),
        "selected_synchronizer_types": len(selected),
        "top_k_selection_applied": len(selected_all) > maximum_types,
        "maximum_edges_among_selected": min(
            (len(item.edges) for item in selected), default=None
        ),
    }
    return selected, stats


def _rename_gadget(
    gadget: Synchronizer, prefix: str
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    mapping = {vertex: f"{prefix}_{vertex}" for vertex in gadget.vertices}
    renamed = [tuple(sorted((mapping[a], mapping[b]))) for a, b in gadget.edges]
    return mapping, renamed


def connector_splits(connector_count: int, mode: str, exact_count: int) -> Iterator[int]:
    if mode == "balanced_exact":
        for positions in itertools.combinations(range(connector_count), exact_count):
            yield sum(1 << position for position in positions)
        return
    if mode == "free":
        yield from range(1 << connector_count)
        return
    raise ValueError(f"unknown connector-split mode: {mode}")


def configured_split_count(connector_count: int, mode: str, exact_count: int) -> int:
    if mode == "balanced_exact":
        return sum(
            1
            for _ in itertools.combinations(range(connector_count), exact_count)
        )
    if mode == "free":
        return 1 << connector_count
    raise ValueError(f"unknown connector-split mode: {mode}")


def build_candidate(
    base: Graph,
    mask: int,
    first: Synchronizer,
    second: Synchronizer,
    boundary_mode: str,
) -> Graph:
    connectors = sorted(base.bipartition[1])
    if len(connectors) != 12:
        raise ValueError("expected the 12 connectors of the hat K_(3,4) seed")
    core = [vertex for vertex in base.bipartition[0] if vertex != "u"]
    first_mapping, first_edges = _rename_gadget(first, "A")
    second_mapping, second_edges = _rename_gadget(second, "B")
    mappings = (first_mapping, second_mapping)

    left = ["U0", "U1"] + core
    right = list(connectors)
    for mapping in mappings:
        left.extend(name for name in mapping.values() if "_L" in name)
        right.extend(name for name in mapping.values() if not "_L" in name)

    edges: list[tuple[str, str]] = []
    for index, connector in enumerate(connectors):
        edges.extend(
            edge
            for edge in base.edges
            if connector in edge and "u" not in edge
        )
        hub = "U0" if mask & (1 << index) else "U1"
        edges.append(_ordered_edge(hub, connector))
    edges.extend(first_edges)
    edges.extend(second_edges)
    edges.extend((_ordered_edge("U0", first_mapping["T0"]), _ordered_edge("U1", first_mapping["T1"])))
    edges.extend((_ordered_edge("U0", second_mapping["T0"]), _ordered_edge("U1", second_mapping["T1"])))

    graph = Graph(left + right, edges, [left, right])
    graph.metadata = {
        "lane": "lane6-terminal-signature-r3",
        "boundary_mode": boundary_mode,
        "synchronizer_keys": [first.key, second.key],
        "synchronizer_edges": [
            [list(edge) for edge in first.edges],
            [list(edge) for edge in second.edges],
        ],
        "mask": format(mask, "012b"),
        "u0_connector_count": sum(bool(mask & (1 << index)) for index in range(12)),
        "u1_connector_count": sum(not bool(mask & (1 << index)) for index in range(12)),
        "u0_degree": sum(bool(mask & (1 << index)) for index in range(12)) + 2,
        "u1_degree": sum(not bool(mask & (1 << index)) for index in range(12)) + 2,
    }
    return graph


def all_spans_before_deadline(
    graph: Graph, per_span_time_limit: float, workers: int, deadline: float
) -> dict:
    results: dict[int, dict] = {}
    overall = "non-colorable"
    started = time.perf_counter()
    for span in range(graph.delta, max(graph.delta, graph.n - 1) + 1):
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            overall = "timeout"
            results[span] = {"status": "NOT_RUN_DEADLINE"}
            continue
        status, coloring = fixed_span_sat_solve(
            graph, span, min(per_span_time_limit, remaining), workers
        )
        results[span] = {
            "status": status,
            "coloring": {
                str(edge): color for edge, color in (coloring or {}).items()
            },
        }
        if status in ("OPTIMAL", "FEASIBLE"):
            overall = "colorable"
            break
        if status == "UNKNOWN":
            overall = "timeout"
    return {
        "encoding": "fixed-span-sat",
        "decision": overall,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "spans": results,
    }


def _result_summary(result) -> dict:
    return {key: value for key, value in result.__dict__.items() if key != "coloring"}


def _side_degrees(graph: Graph) -> dict[str, list[int]]:
    left, right = graph.bipartition
    degrees = graph.degrees
    return {
        "left": sorted(degrees[vertex] for vertex in left),
        "right": sorted(degrees[vertex] for vertex in right),
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def append_negative_event(path: Path, event: dict) -> None:
    if path.exists():
        try:
            events = json.loads(path.read_text()).get("events", [])
        except (OSError, ValueError):
            events = []
    else:
        events = []
    events.append(event)
    write_json(path, {"events": events})


def save_primary_negative(
    graph: Graph,
    candidate_id: str,
    digest: str,
    output_path: Path,
) -> tuple[Path, dict]:
    graph_directory = output_path.parent / "graphs" / "lane6-r3"
    graph_directory.mkdir(parents=True, exist_ok=True)
    graph_path = graph_directory / f"{candidate_id}.graph.json"
    graph.save(graph_path)
    event = {
        "event": "primary_negative_saved",
        "reported_immediately": True,
        "candidate_id": candidate_id,
        "canonical_sha256": digest,
        "graph_json": str(graph_path.resolve()),
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
    }
    append_negative_event(
        output_path.parent / "lane6-signature-r3-negative-events.json", event
    )
    print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)
    return graph_path, event


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--internal-count", type=int, default=5)
    parser.add_argument("--maximum-internal-degree", type=int, default=6)
    parser.add_argument("--simple-terminal-boundary", action="store_true", default=True)
    parser.add_argument("--maximum-signature-states", type=int, default=50000)
    parser.add_argument("--signature-time-limit", type=float, default=10.0)
    parser.add_argument(
        "--boundary-mode",
        choices=(
            "paired_forward_and_reverse_extreme_states",
            "forward_extreme_state_no_reverse",
            "reverse_extreme_state_no_forward",
        ),
        default="paired_forward_and_reverse_extreme_states",
    )
    parser.add_argument("--maximum-synchronizer-types", type=int, default=8)
    parser.add_argument("--minimum-hub-degree", type=int, default=4)
    parser.add_argument("--exact-u0-connectors", type=int, default=6)
    parser.add_argument(
        "--connector-split-mode",
        choices=("balanced_exact", "free"),
        default="balanced_exact",
    )
    parser.add_argument("--maximum-delta", type=int, default=10)
    parser.add_argument("--minimum-graph-degree", type=int, default=2)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--independent-time-limit", type=float, default=15.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-interval", type=int, default=50)
    parser.add_argument("--total-time-limit", type=float, default=TOTAL_TIME_LIMIT_SECONDS)
    parser.add_argument("--output", default="results/lane6-signature-r3.json")
    args = parser.parse_args()

    if args.internal_count != 5:
        parser.error("--internal-count is fixed to 5 in this two-copy family")
    if args.total_time_limit > TOTAL_TIME_LIMIT_SECONDS + 1e-9:
        parser.error("--total-time-limit may not exceed 7200 seconds")

    started = time.perf_counter()
    deadline = started + float(args.total_time_limit)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    negative_event_path = (
        output_path.parent / "lane6-signature-r3-negative-events.json"
    )
    write_json(negative_event_path, {"events": []})

    _, base = seed_graph()
    shapes = enumerate_tight_shapes(
        args.internal_count,
        args.maximum_internal_degree,
        args.simple_terminal_boundary,
    )
    synchronizers, signature_stats = build_synchronizers(
        shapes,
        args.signature_time_limit,
        args.maximum_signature_states,
        deadline,
    )
    selected, selection_stats = select_synchronizers(
        synchronizers, args.boundary_mode, args.maximum_synchronizer_types
    )

    rows: list[dict] = []
    negative_artifacts: list[dict] = []
    seen: set[str] = set()
    skipped_filtered_or_duplicate = 0
    generated = 0
    candidate_stage_completed = True
    all_pairs_processed = True
    configured_splits = configured_split_count(
        12, args.connector_split_mode, args.exact_u0_connectors
    )
    configured_generated = len(selected) * (len(selected) - 1) // 2 * configured_splits

    for first_index, second_index in itertools.combinations(
        range(len(selected)), 2
    ):
        pair_started = time.perf_counter()
        pair_processed = True
        first = selected[first_index]
        second = selected[second_index]
        for mask in connector_splits(
            12, args.connector_split_mode, args.exact_u0_connectors
        ):
            generated += 1
            if time.perf_counter() >= deadline:
                candidate_stage_completed = False
                all_pairs_processed = False
                pair_processed = False
                break

            graph = build_candidate(
                base, mask, first, second, args.boundary_mode
            )
            hub_degrees = (graph.degrees["U0"], graph.degrees["U1"])
            if (
                min(hub_degrees) < args.minimum_hub_degree
                or graph.delta > args.maximum_delta
                or min(graph.degrees.values()) < args.minimum_graph_degree
                or not nx.is_connected(graph._nx)
            ):
                skipped_filtered_or_duplicate += 1
                continue

            digest = nauty_canonical_hash(graph)
            if digest in seen:
                skipped_filtered_or_duplicate += 1
                continue
            seen.add(digest)

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                candidate_stage_completed = False
                all_pairs_processed = False
                pair_processed = False
                break
            result = rank_potential_solve(
                graph, min(args.time_limit, remaining), args.workers
            )
            candidate_id = f"L6R3-{len(rows):04d}"
            row = {
                "candidate_id": candidate_id,
                "canonical_sha256": digest,
                "order": graph.n,
                "size": graph.m,
                "delta": graph.delta,
                "minimum_degree": min(graph.degrees.values()),
                "degree_sequence_by_side": _side_degrees(graph),
                "synchronizer_keys": [first.key, second.key],
                "synchronizer_edge_counts": [len(first.edges), len(second.edges)],
                "boundary_mode": args.boundary_mode,
                "signature_sizes": [len(first.signature), len(second.signature)],
                "metadata": graph.metadata,
                "weighted_hubs_best": weighted_hub_statistics(graph)[:2],
                "primary_result": _result_summary(result),
                "certified_non_colorable": False,
            }

            if result.status == "non-colorable":
                graph_path, negative_event = save_primary_negative(
                    graph, candidate_id, digest, output_path
                )
                row["graph_json"] = str(graph_path)
                row["negative_event"] = negative_event
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    row["independent_spans"] = {
                        "encoding": "fixed-span-sat",
                        "decision": "timeout",
                        "spans": {},
                    }
                else:
                    row["independent_spans"] = all_spans_before_deadline(
                        graph,
                        args.independent_time_limit,
                        args.workers,
                        deadline,
                    )
                row["certified_non_colorable"] = (
                    row["independent_spans"]["decision"] == "non-colorable"
                )
                negative_artifacts.append(
                    {
                        "candidate_id": candidate_id,
                        "canonical_sha256": digest,
                        "graph_json": str(graph_path),
                        "order": graph.n,
                        "size": graph.m,
                        "delta": graph.delta,
                        "minimum_degree": min(graph.degrees.values()),
                        "certified": row["certified_non_colorable"],
                    }
                )

            rows.append(row)
            if (
                args.checkpoint_interval
                and len(rows) % args.checkpoint_interval == 0
            ):
                counts = Counter(
                    row["primary_result"]["status"] for row in rows
                )
                checkpoint = {
                    "checkpoint": True,
                    "generated": generated,
                    "unique": len(rows),
                    "counts": {
                        status: counts.get(status, 0)
                        for status in ("colorable", "non-colorable", "timeout")
                    },
                    "candidate_stage_completed": candidate_stage_completed,
                    "rows": rows,
                }
                write_json(output_path, checkpoint)
                print(
                    json.dumps(
                        {key: value for key, value in checkpoint.items() if key != "rows"},
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )

        elapsed_this_pair = round(time.perf_counter() - pair_started, 3)
        if not pair_processed:
            all_pairs_processed = False
            break
        print(
            json.dumps(
                {
                    "pair_indices": [first_index, second_index],
                    "generated_cumulative": generated,
                    "unique_cumulative": len(rows),
                    "pair_elapsed_seconds": elapsed_this_pair,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )

    counts = Counter(row["primary_result"]["status"] for row in rows)
    runtime_deadline_hit = time.perf_counter() >= deadline
    negative_count = counts.get("non-colorable", 0)
    confirmed_negative_count = sum(
        row.get("certified_non_colorable", False) for row in rows
    )
    conflicting_classifications = sum(
        row.get("independent_spans", {}).get("decision") == "colorable"
        for row in rows
    )
    certification_complete = negative_count == confirmed_negative_count
    family_exhausted = (
        signature_stats["signature_stage_completed"]
        and candidate_stage_completed
        and all_pairs_processed
        and counts.get("timeout", 0) == 0
        and certification_complete
        and conflicting_classifications == 0
    )
    summary = {
        "schema": "lane6-signature-search-round-3",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "total_time_limit_seconds": args.total_time_limit,
        "runtime_deadline_hit": runtime_deadline_hit,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "ortools": __import__("ortools").__version__,
            "networkx": nx.__version__,
        },
        "family_definition": {
            "seed": "hat_K34_prime_Delta11",
            "construction": "two_vertex_disjoint_five_internal_synchronizers",
            "internal_count_per_synchronizer": args.internal_count,
            "maximum_internal_degree": args.maximum_internal_degree,
            "simple_terminal_boundary": args.simple_terminal_boundary,
            "terminal_boundary_condition": args.boundary_mode,
            "maximum_synchronizer_types": args.maximum_synchronizer_types,
            "synchronizer_pair_rule": "distinct unordered types",
            "connector_split_mode": args.connector_split_mode,
            "exact_u0_connectors": args.exact_u0_connectors
            if args.connector_split_mode == "balanced_exact"
            else None,
            "minimum_hub_degree": args.minimum_hub_degree,
            "maximum_delta": args.maximum_delta,
            "minimum_graph_degree": args.minimum_graph_degree,
            "boundary": (
                "connected labelled two-terminal bipartite gadgets with five "
                "internal vertices, one gadget edge at each terminal, internal "
                f"degree at most {args.maximum_internal_degree}, and "
                f"{args.boundary_mode}; classify the top "
                f"{args.maximum_synchronizer_types} types by deterministic "
                "compactness order and all distinct pairs"
            ),
        },
        "gadget_enumeration": {
            **signature_stats,
            **selection_stats,
            "configured_pairs": len(selected) * (len(selected) - 1) // 2,
            "selected_edges": [item.edges for item in selected],
        },
        "composition": {
            "configured_generated_before_filters": configured_generated,
            "generated": generated,
            "skipped_filtered_or_duplicate": skipped_filtered_or_duplicate,
            "unique": len(rows),
            "completed_unique": len(rows),
            "counts": {
                status: counts.get(status, 0)
                for status in ("colorable", "non-colorable", "timeout")
            },
            "candidate_stage_completed": candidate_stage_completed,
            "all_configured_pairs_processed": all_pairs_processed,
            "certification_stage_complete": certification_complete,
            "family_exhausted": family_exhausted,
            "negative_independently_confirmed": confirmed_negative_count,
            "unconfirmed_primary_negatives": negative_count
            - confirmed_negative_count,
            "conflicting_classifications": conflicting_classifications,
            "unresolved_timeout_count": counts.get("timeout", 0)
            + int(not signature_stats["signature_stage_completed"])
            + (negative_count - confirmed_negative_count),
        },
        "negative_artifacts": negative_artifacts,
        "negative_events_path": str(negative_event_path),
        "rows": rows,
    }
    write_json(output_path, summary)
    printable = {key: value for key, value in summary.items() if key != "rows"}
    print(json.dumps(printable, indent=2))


if __name__ == "__main__":
    main()
