#!/usr/bin/env python3
"""Round 4: asymmetric independent synchronizer roles.

The family is not a larger copy of round 3.  It pairs synchronizers from two
different internal-vertex budgets and complementary terminal-degree tables.
Both members must have the strict paired endpoint states (maximal rank at one
terminal with zero rank at the other, in both orientations).  Their hub
attachments are deliberately opposite.
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
    terminal_degree_pair: tuple[int, int]


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


def _parse_terminal_degree_pair(text: str) -> tuple[int, int]:
    pieces = text.split(",")
    if len(pieces) != 2:
        raise ValueError("terminal degrees must have the form T0,T1")
    values = tuple(int(piece.strip()) for piece in pieces)
    if any(value < 1 or value > 4 for value in values):
        raise ValueError("each terminal degree must be between 1 and 4")
    return values  # type: ignore[return-value]


def enumerate_imbalanced_shapes(
    internal_count: int,
    maximum_internal_degree: int,
    terminal_degree_pair: tuple[int, int],
    minimum_internal_degree: int = 2,
) -> list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]]:
    """Enumerate connected bipartite shapes with exact terminal degrees."""

    terminal_degrees = {
        "T0": terminal_degree_pair[0],
        "T1": terminal_degree_pair[1],
    }
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
                    degree.get(terminal, 0) != wanted
                    for terminal, wanted in terminal_degrees.items()
                ):
                    continue
                if any(
                    vertex not in terminals
                    and degree.get(vertex, 0) > maximum_internal_degree
                    for vertex in vertices
                ):
                    continue
                if any(
                    vertex not in terminals
                    and degree.get(vertex, 0) < minimum_internal_degree
                    for vertex in vertices
                ):
                    continue
                if not _connected(vertices, chosen):
                    continue
                edge_set = frozenset(chosen)
                shapes_by_edges[edge_set] = (
                    tuple(vertices),
                    tuple(sorted(chosen)),
                )
    return sorted(shapes_by_edges.values(), key=lambda item: (item[0], item[1]))


def _maximum_internal_degree(
    vertices: tuple[str, ...],
    edges: tuple[tuple[str, str], ...],
) -> int:
    degree: dict[str, int] = defaultdict(int)
    for a, b in edges:
        degree[a] += 1
        degree[b] += 1
    return max(
        (degree[vertex] for vertex in vertices if vertex not in ("T0", "T1")),
        default=0,
    )


def build_synchronizers(
    shapes: list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]],
    terminal_degree_pair: tuple[int, int],
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
                terminal_degree_pair,
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


def states_at_max_rank(
    signature: tuple[Signature, ...],
    attribute: str,
) -> list[Signature]:
    largest = max(getattr(item, attribute) for item in signature)
    return [item for item in signature if getattr(item, attribute) == largest]


def has_forward_extreme_state(signature: tuple[Signature, ...]) -> bool:
    return any(
        state.rank1 == 0
        for state in states_at_max_rank(signature, "rank0")
    )


def has_reverse_extreme_state(signature: tuple[Signature, ...]) -> bool:
    return any(
        state.rank0 == 0
        for state in states_at_max_rank(signature, "rank1")
    )


def select_role_synchronizers(
    synchronizers: list[Synchronizer],
    role: str,
    maximum_types: int,
) -> tuple[list[Synchronizer], int]:
    selected_all: list[Synchronizer] = []
    for item in synchronizers:
        forward = has_forward_extreme_state(item.signature)
        reverse = has_reverse_extreme_state(item.signature)
        if role in ("forward", "reverse") and forward and reverse:
            selected_all.append(item)
    selected_all.sort(
        key=lambda item: (
            len(item.edges),
            item.maximum_internal_degree,
            len(item.signature),
            item.key,
        )
    )
    return selected_all[:maximum_types], len(selected_all)


def connector_splits(connector_count: int, exact_count: int) -> Iterator[int]:
    for positions in itertools.combinations(range(connector_count), exact_count):
        yield sum(1 << position for position in positions)


def configured_split_count(connector_count: int, exact_count: int) -> int:
    return sum(1 for _ in itertools.combinations(range(connector_count), exact_count))


def rename_gadget(
    gadget: Synchronizer,
    prefix: str,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    mapping = {vertex: f"{prefix}_{vertex}" for vertex in gadget.vertices}
    renamed = [tuple(sorted((mapping[a], mapping[b]))) for a, b in gadget.edges]
    return mapping, renamed


def build_candidate(
    base: Graph,
    mask: int,
    forward: Synchronizer,
    reverse: Synchronizer,
) -> Graph:
    connectors = sorted(base.bipartition[1])
    if len(connectors) != 12:
        raise ValueError("expected the 12 connectors of the hat K_(3,4) seed")
    core = [vertex for vertex in base.bipartition[0] if vertex != "u"]
    forward_mapping, forward_edges = rename_gadget(forward, "F")
    reverse_mapping, reverse_edges = rename_gadget(reverse, "R")

    left = ["U0", "U1"] + core
    right = list(connectors)
    for mapping in (forward_mapping, reverse_mapping):
        left.extend(name for name in mapping.values() if "_L" in name)
        right.extend(name for name in mapping.values() if "_L" not in name)

    edges: list[tuple[str, str]] = []
    for index, connector in enumerate(connectors):
        edges.extend(
            edge
            for edge in base.edges
            if connector in edge and "u" not in edge
        )
        hub = "U0" if mask & (1 << index) else "U1"
        edges.append(_ordered_edge(hub, connector))
    edges.extend(forward_edges)
    edges.extend(reverse_edges)
    # The reverse gadget deliberately receives opposite terminal hub roles.
    edges.extend(
        (
            _ordered_edge("U0", forward_mapping["T0"]),
            _ordered_edge("U1", forward_mapping["T1"]),
            _ordered_edge("U1", reverse_mapping["T0"]),
            _ordered_edge("U0", reverse_mapping["T1"]),
        )
    )

    graph = Graph(left + right, edges, [left, right])
    graph.metadata = {
        "lane": "lane6-asymmetric-terminal-signature-r4",
        "construction": "forward_only_plus_mirrored_reverse_only",
        "forward_key": forward.key,
        "reverse_key": reverse.key,
        "forward_edges": [list(edge) for edge in forward.edges],
        "reverse_edges": [list(edge) for edge in reverse.edges],
        "forward_terminal_degrees_before_hub_attachment": list(
            forward.terminal_degree_pair
        ),
        "reverse_terminal_degrees_before_hub_attachment": list(
            reverse.terminal_degree_pair
        ),
        "mask": format(mask, "012b"),
        "u0_connector_count": sum(bool(mask & (1 << index)) for index in range(12)),
        "u1_connector_count": sum(not bool(mask & (1 << index)) for index in range(12)),
        "u0_degree": sum(bool(mask & (1 << index)) for index in range(12)) + 2,
        "u1_degree": sum(not bool(mask & (1 << index)) for index in range(12)) + 2,
    }
    return graph


def all_spans_before_deadline(
    graph: Graph,
    per_span_time_limit: float,
    workers: int,
    deadline: float,
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
            graph,
            span,
            min(per_span_time_limit, remaining),
            workers,
        )
        results[span] = {
            "status": status,
            "coloring": {
                str(edge): color
                for edge, color in (coloring or {}).items()
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


def result_summary(result) -> dict:
    return {
        key: value
        for key, value in result.__dict__.items()
        if key != "coloring"
    }


def side_degrees(graph: Graph) -> dict[str, list[int]]:
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
    graph_directory = output_path.parent / "graphs" / "lane6-r4"
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
        output_path.parent / "lane6-signature-r4-negative-events.json",
        event,
    )
    print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)
    return graph_path, event


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-internal-count", type=int, default=3)
    parser.add_argument("--reverse-internal-count", type=int, default=5)
    parser.add_argument("--maximum-internal-degree", type=int, default=5)
    parser.add_argument("--minimum-internal-degree", type=int, default=2)
    parser.add_argument("--forward-terminal-degrees", default="1,2")
    parser.add_argument("--reverse-terminal-degrees", default="2,1")
    parser.add_argument("--maximum-signature-states", type=int, default=500000)
    parser.add_argument("--signature-time-limit", type=float, default=10.0)
    parser.add_argument("--maximum-types-per-role", type=int, default=2)
    parser.add_argument("--minimum-hub-degree", type=int, default=4)
    parser.add_argument("--exact-u0-connectors", type=int, default=6)
    parser.add_argument("--maximum-delta", type=int, default=10)
    parser.add_argument("--minimum-graph-degree", type=int, default=2)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--independent-time-limit", type=float, default=15.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-interval", type=int, default=50)
    parser.add_argument(
        "--total-time-limit",
        type=float,
        default=TOTAL_TIME_LIMIT_SECONDS,
    )
    parser.add_argument("--output", default="results/lane6-signature-r4.json")
    args = parser.parse_args()

    if args.forward_internal_count < 1 or args.reverse_internal_count < 1:
        parser.error("both synchronizer roles need at least one internal vertex")
    if args.maximum_types_per_role < 1:
        parser.error("--maximum-types-per-role must be positive")
    if args.maximum_internal_degree < args.minimum_internal_degree:
        parser.error("maximum internal degree must bound minimum internal degree")
    if args.maximum_delta > 10:
        parser.error("--maximum-delta is capped at 10")
    if args.total_time_limit > TOTAL_TIME_LIMIT_SECONDS + 1e-9:
        parser.error("--total-time-limit may not exceed 7200 seconds")

    try:
        forward_terminal_degrees = _parse_terminal_degree_pair(
            args.forward_terminal_degrees
        )
        reverse_terminal_degrees = _parse_terminal_degree_pair(
            args.reverse_terminal_degrees
        )
    except ValueError as exc:
        parser.error(str(exc))
        raise AssertionError

    started = time.perf_counter()
    deadline = started + float(args.total_time_limit)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    negative_event_path = output_path.parent / "lane6-signature-r4-negative-events.json"
    write_json(negative_event_path, {"events": []})

    _, base = seed_graph()
    forward_shapes = enumerate_imbalanced_shapes(
        args.forward_internal_count,
        args.maximum_internal_degree,
        forward_terminal_degrees,
        args.minimum_internal_degree,
    )
    reverse_shapes = enumerate_imbalanced_shapes(
        args.reverse_internal_count,
        args.maximum_internal_degree,
        reverse_terminal_degrees,
        args.minimum_internal_degree,
    )
    raw_shapes = len(forward_shapes) + len(reverse_shapes)

    forward_all, forward_stats = build_synchronizers(
        forward_shapes,
        forward_terminal_degrees,
        args.signature_time_limit,
        args.maximum_signature_states,
        deadline,
    )
    signature_stage_completed = forward_stats["signature_stage_completed"]
    if signature_stage_completed and time.perf_counter() < deadline:
        reverse_all, reverse_stats = build_synchronizers(
            reverse_shapes,
            reverse_terminal_degrees,
            args.signature_time_limit,
            args.maximum_signature_states,
            deadline,
        )
        signature_stage_completed = reverse_stats["signature_stage_completed"]
    else:
        reverse_all = []
        reverse_stats = {
            "raw_shapes": len(reverse_shapes),
            "solved_gadgets": 0,
            "excluded_signature_timeout_or_cap": (
                len(reverse_shapes) if not signature_stage_completed else 0
            ),
            "signature_stage_completed": False,
            "elapsed_seconds": 0.0,
            "signature_size_counts": {},
        }

    selected_forward, forward_available = select_role_synchronizers(
        forward_all,
        "forward",
        args.maximum_types_per_role,
    )
    selected_reverse, reverse_available = select_role_synchronizers(
        reverse_all,
        "reverse",
        args.maximum_types_per_role,
    )

    rows: list[dict] = []
    negative_artifacts: list[dict] = []
    seen: set[str] = set()
    skipped_filtered_or_duplicate = 0
    generated = 0
    candidate_stage_completed = True
    all_configured_pairs_processed = True
    configured_pairs = len(selected_forward) * len(selected_reverse)
    configured_splits = configured_split_count(12, args.exact_u0_connectors)
    configured_generated_before_filters = configured_pairs * configured_splits

    for forward_index, forward_gadget in enumerate(selected_forward):
        pair_processed = True
        for reverse_index, reverse_gadget in enumerate(selected_reverse):
            for mask in connector_splits(12, args.exact_u0_connectors):
                generated += 1
                if time.perf_counter() >= deadline:
                    candidate_stage_completed = False
                    all_configured_pairs_processed = False
                    pair_processed = False
                    break

                graph = build_candidate(
                    base,
                    mask,
                    forward_gadget,
                    reverse_gadget,
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
                    all_configured_pairs_processed = False
                    pair_processed = False
                    break
                result = rank_potential_solve(
                    graph,
                    min(args.time_limit, remaining),
                    args.workers,
                )
                candidate_id = f"L6R4-{len(rows):04d}"
                row = {
                    "candidate_id": candidate_id,
                    "canonical_sha256": digest,
                    "order": graph.n,
                    "size": graph.m,
                    "delta": graph.delta,
                    "minimum_degree": min(graph.degrees.values()),
                    "degree_sequence_by_side": side_degrees(graph),
                    "forward_key": forward_gadget.key,
                    "reverse_key": reverse_gadget.key,
                    "forward_index": forward_index,
                    "reverse_index": reverse_index,
                    "gadget_edge_counts": [
                        len(forward_gadget.edges),
                        len(reverse_gadget.edges),
                    ],
                    "metadata": graph.metadata,
                    "weighted_hubs_best": weighted_hub_statistics(graph)[:2],
                    "primary_result": result_summary(result),
                    "certified_non_colorable": False,
                }

                if result.status == "non-colorable":
                    graph_path, negative_event = save_primary_negative(
                        graph,
                        candidate_id,
                        digest,
                        output_path,
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
                    checkpoint_counts = Counter(
                        row["primary_result"]["status"] for row in rows
                    )
                    checkpoint = {
                        "checkpoint": True,
                        "generated": generated,
                        "unique": len(rows),
                        "counts": {
                            status: checkpoint_counts.get(status, 0)
                            for status in ("colorable", "non-colorable", "timeout")
                        },
                        "candidate_stage_completed": candidate_stage_completed,
                        "rows": rows,
                    }
                    write_json(output_path, checkpoint)
                    print(
                        json.dumps(
                            {
                                key: value
                                for key, value in checkpoint.items()
                                if key != "rows"
                            },
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
            if not pair_processed:
                break
        if not pair_processed:
            all_configured_pairs_processed = False
            break
        print(
            json.dumps(
                {
                    "forward_index": forward_index,
                    "generated_cumulative": generated,
                    "unique_cumulative": len(rows),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )

    counts = Counter(row["primary_result"]["status"] for row in rows)
    colorable_count = counts.get("colorable", 0)
    non_colorable_count = counts.get("non-colorable", 0)
    timeout_count = counts.get("timeout", 0)
    confirmed_negative_count = sum(
        row.get("certified_non_colorable", False) for row in rows
    )
    conflicting_classifications = sum(
        row.get("independent_spans", {}).get("decision") == "colorable"
        for row in rows
    )
    certification_complete = non_colorable_count == confirmed_negative_count
    runtime_deadline_hit = time.perf_counter() >= deadline
    family_exhausted = (
        signature_stage_completed
        and candidate_stage_completed
        and all_configured_pairs_processed
        and timeout_count == 0
        and certification_complete
        and conflicting_classifications == 0
    )
    unresolved_timeout_count = (
        timeout_count
        + int(not signature_stage_completed)
        + (non_colorable_count - confirmed_negative_count)
        + conflicting_classifications
    )
    summary = {
        "schema": "lane6-signature-search-round-4",
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
            "construction": (
                "one compact paired-endpoint synchronizer paired with one "
                "larger synchronizer under asymmetric internal budgets, "
                "complementary terminal-degree imbalance, and reversed "
                "hub attachments"
            ),
            "forward_internal_count": args.forward_internal_count,
            "reverse_internal_count": args.reverse_internal_count,
            "maximum_internal_degree": args.maximum_internal_degree,
            "minimum_internal_degree": args.minimum_internal_degree,
            "forward_terminal_degrees_before_attachment": list(
                forward_terminal_degrees
            ),
            "reverse_terminal_degrees_before_attachment": list(
                reverse_terminal_degrees
            ),
            "role_boundary_rule": (
                "each role contains a maximal-rank0/rank1-zero state and a "
                "maximal-rank1/rank0-zero state; roles differ in budget, "
                "terminal degrees, and attachment direction"
            ),
            "maximum_types_per_role": args.maximum_types_per_role,
            "connector_split_mode": "balanced_exact",
            "exact_u0_connectors": args.exact_u0_connectors,
            "minimum_hub_degree": args.minimum_hub_degree,
            "maximum_delta": args.maximum_delta,
            "minimum_graph_degree": args.minimum_graph_degree,
        },
        "gadget_enumeration": {
            "raw_shapes": raw_shapes,
            "solved_gadgets": len(forward_all) + len(reverse_all),
            "excluded_signature_timeout_or_cap": (
                forward_stats["excluded_signature_timeout_or_cap"]
                + reverse_stats["excluded_signature_timeout_or_cap"]
            ),
            "signature_stage_completed": signature_stage_completed,
            "available_paired_endpoint_forward_types": forward_available,
            "available_paired_endpoint_reverse_types": reverse_available,
            "selected_forward_types": len(selected_forward),
            "selected_reverse_types": len(selected_reverse),
            "configured_pairs": configured_pairs,
            "configured_connector_splits": configured_splits,
            "forward_stats": forward_stats,
            "reverse_stats": reverse_stats,
            "selected_forward_edges": [
                [list(edge) for edge in item.edges]
                for item in selected_forward
            ],
            "selected_reverse_edges": [
                [list(edge) for edge in item.edges]
                for item in selected_reverse
            ],
        },
        "composition": {
            "raw_shapes": raw_shapes,
            "solved_gadgets": len(forward_all) + len(reverse_all),
            "selected_gadgets": len(selected_forward) * len(selected_reverse),
            "generated_before_filters": configured_generated_before_filters,
            "generated": generated,
            "skipped_filtered_or_duplicate": skipped_filtered_or_duplicate,
            "unique": len(rows),
            "completed_unique": len(rows),
            "colorable": colorable_count,
            "non_colorable": non_colorable_count,
            "timeout": timeout_count,
            "counts": {
                status: counts.get(status, 0)
                for status in ("colorable", "non-colorable", "timeout")
            },
            "candidate_stage_completed": candidate_stage_completed,
            "all_configured_pairs_processed": all_configured_pairs_processed,
            "certification_complete": certification_complete,
            "family_exhausted": family_exhausted,
            "configured_family_exhausted": family_exhausted,
            "negative_independently_confirmed": confirmed_negative_count,
            "unconfirmed_primary_negatives": non_colorable_count
            - confirmed_negative_count,
            "conflicting_classifications": conflicting_classifications,
            "unresolved_timeout_count": unresolved_timeout_count,
        },
        "negative_artifacts": negative_artifacts,
        "negative_events_path": str(negative_event_path.resolve()),
        "rows": rows,
    }
    write_json(output_path, summary)
    printable = {
        key: value for key, value in summary.items() if key != "rows"
    }
    print(json.dumps(printable, indent=2))


if __name__ == "__main__":
    main()
