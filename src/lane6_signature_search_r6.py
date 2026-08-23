#!/usr/bin/env python3
"""Round 6: directed relays around the round-5 span-17 outlier.

Round 5 produced one primary span-17 graph: ring size four, connector loads
(2,1,1,8), and relay pattern (0,0,1,0), with every relay attached in its fixed
forward direction.  The sharp untried hypothesis is therefore local: relay
orientation, not a larger load family, controls whether the strict-extreme
terminal palettes concatenate globally.

This runner keeps the outlier's unequal-load skeleton, allows all ordered
placements of (8,2,1,1), all mixed two-relay patterns, and both orientations at
every ring position.  A base graph is classified first.  Only when its primary
span is at least 16 does it receive the exact terminal-blocker gadgets selected
from the exhaustive <=2-internal-vertex lane-6 signature catalog.  A timeout is
never converted into either a colorable result or a negative.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import platform
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)
from lane6_signature_search import (
    Signature,
    enumerate_gadgets,
    terminal_signatures,
)
from lane1_search import seed_graph


TOTAL_TIME_LIMIT_SECONDS = 2 * 60 * 60
SOURCE_CATALOG = "results/lane6-signature-r2.json"

# These are the two shortest distinct members of the completed strict-extreme
# round-2 catalog.  Keeping the catalogue fixed makes the composition family
# finite and reproducible without re-running the old 4,013-shape enumeration.
SELECTED_RELAY_EDGES = (
    (
        ("L0", "R0"),
        ("L0", "R1"),
        ("L1", "R0"),
        ("L1", "T0"),
        ("L2", "R1"),
        ("L2", "T1"),
    ),
    (
        ("L0", "R0"),
        ("L0", "R1"),
        ("L0", "T0"),
        ("L1", "R0"),
        ("L1", "T1"),
        ("L2", "R1"),
        ("L2", "T1"),
    ),
)

PRIOR_ROUND_DIAGNOSIS = {
    "round5_result": (
        "All 162 unique candidates were exactly colorable.  Primary spans were "
        "10:69, 11:58, 12:18, 13:14, 14:2, and 17:1; no primary negative was "
        "found and no independent certification was required."
    ),
    "sharpest_untried_hypothesis": (
        "The sole span-17 outlier used loads (2,1,1,8) and pattern "
        "(0,0,1,0), but every compact strict-extreme relay was wired in only "
        "one direction.  Mixed per-position relay orientations may align or "
        "deliberately oppose terminal palette concatenation and was untried in "
        "all prior rounds."
    ),
    "blocker_policy": (
        "Do not enlarge every composition.  First classify each directed base "
        "graph; attach exact small same-side terminal blockers only when its "
        "primary span reaches 16."
    ),
}


@dataclass(frozen=True)
class Relay:
    index: int
    vertices: tuple[str, ...]
    left_vertices: tuple[str, ...]
    right_vertices: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    signature: tuple[Signature, ...]
    key: str


@dataclass(frozen=True)
class Plan:
    ring_size: int
    loads: tuple[int, ...]
    pattern: tuple[int, ...]
    orientation: tuple[int, ...]


def _ordered_edge(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _sha256(path: Path):
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def has_forward_strict_endpoint(signature) -> bool:
    """True when maximal rank0 can concatenate onto zero rank1 at offset +1."""

    largest_rank0 = max(state.rank0 for state in signature)
    return any(
        state.rank0 == largest_rank0
        and state.rank1 == 0
        and state.offset == 1
        for state in signature
    )


def has_reverse_strict_endpoint(signature) -> bool:
    """Diagnostic only; the configured ring orientation does not require it."""

    largest_rank1 = max(state.rank1 for state in signature)
    return any(
        state.rank0 == 0
        and state.rank1 == largest_rank1
        and state.offset == -1
        for state in signature
    )


def build_selected_relays(
    signature_time_limit: float,
    maximum_signature_states: int,
    deadline: float,
):
    relays = []
    excluded_timeout_or_cap = 0
    started = time.perf_counter()

    for index, unordered_edges in enumerate(SELECTED_RELAY_EDGES):
        if time.perf_counter() >= deadline:
            excluded_timeout_or_cap += len(SELECTED_RELAY_EDGES) - index
            break

        edges = tuple(sorted(_ordered_edge(a, b) for a, b in unordered_edges))
        vertices = tuple(sorted({vertex for edge in edges for vertex in edge}))
        left_vertices = tuple(v for v in vertices if v.startswith("L"))
        right_vertices = tuple(v for v in vertices if not v.startswith("L"))

        augmented_vertices = ("X0", "X1") + vertices
        augmented_edges = (
            _ordered_edge("X0", "T0"),
            _ordered_edge("X1", "T1"),
        ) + edges
        augmented = Graph(
            augmented_vertices,
            augmented_edges,
            [
                ["X0", "X1"] + list(left_vertices),
                list(right_vertices),
            ],
        )
        remaining = deadline - time.perf_counter()
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

        relay = Relay(
            index=index,
            vertices=vertices,
            left_vertices=left_vertices,
            right_vertices=right_vertices,
            edges=edges,
            signature=tuple(sorted(signature, key=Signature.sort_key)),
            key=json.dumps(edges, separators=(",", ":")),
        )
        if not has_forward_strict_endpoint(relay.signature):
            excluded_timeout_or_cap += 1
            continue
        relays.append(relay)

    signature_sizes = Counter(len(relay.signature) for relay in relays)
    completed = len(relays) + excluded_timeout_or_cap == len(SELECTED_RELAY_EDGES)
    stats = {
        "selected_edge_sets": len(SELECTED_RELAY_EDGES),
        "solved_and_accepted": len(relays),
        "excluded_signature_timeout_or_cap_or_filter": excluded_timeout_or_cap,
        "relay_signature_stage_completed": completed,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "signature_size_counts": {
            str(size): count for size, count in sorted(signature_sizes.items())
        },
        "relays": [
            {
                "index": relay.index,
                "key": relay.key,
                "signature": [state.sort_key() for state in relay.signature],
                "forward_strict_endpoint": has_forward_strict_endpoint(
                    relay.signature
                ),
                "reverse_strict_endpoint": has_reverse_strict_endpoint(
                    relay.signature
                ),
            }
            for relay in relays
        ],
    }
    return relays, stats


def source_catalog_summary():
    path = Path(SOURCE_CATALOG)
    payload = {}
    if path.exists():
        try:
            data = json.loads(path.read_text()).get("gadget_enumeration", {})
            payload = {
                "raw_shapes": data.get("raw_shapes"),
                "solved_gadgets": data.get("solved_gadgets"),
                "excluded_signature_timeout": data.get(
                    "excluded_signature_timeout"
                ),
                "excluded_solution_cap": data.get("excluded_solution_cap"),
                "signature_stage_completed": data.get(
                    "signature_stage_completed"
                ),
                "selected_gadgets": data.get("selected_gadgets"),
            }
        except (OSError, ValueError):
            payload = {"read_error": True}
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "catalog": payload,
        "selection_rule": (
            "distinct strict-extreme relays with fewest edges, then fewest "
            "signature states, then edge-key order"
        ),
    }


def configured_plans():
    """Yield the bounded directed neighborhood of the round-5 outlier."""

    load_multiset = (8, 2, 1, 1)
    for loads in set(itertools.permutations(load_multiset)):
        for pattern in itertools.product(range(2), repeat=4):
            if len(set(pattern)) < 2:
                continue
            for orientation in itertools.product(range(2), repeat=4):
                yield Plan(4, tuple(sorted(loads)), pattern, orientation)


def build_candidate(base, plan, relays):
    if len(relays) != 2:
        raise ValueError("the configured mixed-pattern family needs two relays")
    connectors = sorted(base.bipartition[1])
    if len(connectors) != 12:
        raise ValueError("expected twelve connectors in the hat K_(3,4) seed")

    core = [vertex for vertex in base.bipartition[0] if vertex != "u"]
    hubs = [f"U{index}" for index in range(plan.ring_size)]
    left = hubs + core
    right = list(connectors)
    edges = [edge for edge in base.edges if "u" not in edge]

    connector_index = 0
    for hub_index, load in enumerate(plan.loads):
        for _ in range(load):
            connector = connectors[connector_index]
            edges.append(_ordered_edge(hubs[hub_index], connector))
            connector_index += 1
    if connector_index != len(connectors):
        raise AssertionError("connector load vector did not consume twelve hubs")

    for position, relay_index in enumerate(plan.pattern):
        relay = relays[relay_index]
        next_position = (position + 1) % plan.ring_size
        mapping = {vertex: f"G{position}_{vertex}" for vertex in relay.vertices}
        for vertex in relay.left_vertices:
            mapped = mapping[vertex]
            if mapped not in left:
                left.append(mapped)
        for vertex in relay.right_vertices:
            mapped = mapping[vertex]
            if mapped not in right:
                right.append(mapped)
        for a, b in relay.edges:
            edges.append(_ordered_edge(mapping[a], mapping[b]))
        if plan.orientation[position] == 0:
            first_terminal, second_terminal = "T0", "T1"
        else:
            first_terminal, second_terminal = "T1", "T0"
        edges.append(_ordered_edge(hubs[position], mapping[first_terminal]))
        edges.append(
            _ordered_edge(hubs[next_position], mapping[second_terminal])
        )

    left = list(dict.fromkeys(left))
    right = list(dict.fromkeys(right))
    edges = list(dict.fromkeys(edges))
    graph = Graph(left + right, edges, [left, right])
    graph.metadata = {
        "lane": "lane6-directed-mixed-relay-ring-r6",
        "construction": (
            "four_hub_unequal_load_mixed_relay_ring_with_local_orientation"
        ),
        "ring_size": plan.ring_size,
        "connector_load_vector": list(plan.loads),
        "relay_pattern_by_position": list(plan.pattern),
        "relay_orientation_by_position": [
            "forward" if value == 0 else "reverse"
            for value in plan.orientation
        ],
        "relay_edge_keys": [relays[index].key for index in plan.pattern],
        "forced_degree10_hubs": [
            index for index, load in enumerate(plan.loads) if load == 8
        ],
        "hub_degrees_before_relay_attachment": [
            load + 2 for load in plan.loads
        ],
    }
    return graph


def build_blockers(signature_time_limit, maximum_signature_states, deadline):
    """Select every exact <=2-internal same-side terminal gadget."""

    gadgets, stats = enumerate_gadgets(
        2,
        4,
        maximum_signature_states,
        min(signature_time_limit, max(0.001, deadline - time.perf_counter())),
    )
    completed = (
        stats["excluded_signature_timeout"]
        + stats["excluded_solution_cap"]
        + len(gadgets)
        == stats["candidate_edge_sets"]
    )
    blockers = []
    for index, gadget in enumerate(gadgets):
        blockers.append(
            {
                "index": index,
                "vertices": list(gadget.vertices),
                "left_vertices": [
                    vertex for vertex in gadget.vertices if vertex.startswith("L")
                ],
                "right_vertices": [
                    vertex
                    for vertex in gadget.vertices
                    if not vertex.startswith("L")
                ],
                "edges": [list(edge) for edge in gadget.edges],
                "key": json.dumps(gadget.edges, separators=(",", ":")),
                "signature": [
                    state.sort_key()
                    for state in sorted(gadget.signature, key=Signature.sort_key)
                ],
            }
        )
    stats.update(
        {
            "blocker_stage_completed": completed,
            "selected_exact_terminal_blockers": len(blockers),
            "selection_rule": (
                "every connected two-terminal bipartite gadget with at most "
                "two internal vertices, internal degree at most four, and a "
                "complete timeout-free exact signature"
            ),
            "blockers": blockers,
        }
    )
    return gadgets, stats


def attach_terminal_blocker(base_graph, blocker, hub):
    """Attach one blocker's T0/T1 to the same existing left-side hub."""

    left, right = base_graph.bipartition
    if hub not in left:
        raise ValueError("terminal blocker attachment must use a left-side hub")
    prefix = f"B_{hub}_"
    mapping = {vertex: prefix + vertex for vertex in blocker.vertices}
    left_vertices = [v for v in blocker.vertices if v.startswith("L")]
    right_vertices = [v for v in blocker.vertices if not v.startswith("L")]
    mapped_left = [mapping[v] for v in left_vertices]
    mapped_right = [mapping[v] for v in right_vertices]
    overlap = (set(mapped_left) | set(mapped_right)) & set(base_graph.vertices)
    if overlap:
        raise AssertionError(f"blocker vertex collision: {sorted(overlap)}")
    new_left = list(left) + mapped_left
    new_right = list(right) + mapped_right
    edges = list(base_graph.edges)
    for a, b in blocker.edges:
        edges.append(_ordered_edge(mapping[a], mapping[b]))
    edges.extend(
        (
            _ordered_edge(hub, mapping["T0"]),
            _ordered_edge(hub, mapping["T1"]),
        )
    )
    graph = Graph(new_left + new_right, edges, [new_left, new_right])
    graph.metadata = {
        **base_graph.metadata,
        "terminal_blocker_attached": True,
        "terminal_blocker_hub": hub,
        "terminal_blocker_edges": [list(edge) for edge in blocker.edges],
        "terminal_blocker_key": json.dumps(
            blocker.edges, separators=(",", ":")
        ),
    }
    return graph


def eligible_blocker_hubs(graph):
    return [
        vertex
        for vertex in sorted(graph.bipartition[0])
        if graph.degrees[vertex] + 2 <= graph.delta
    ]


def configured_blocker_variants(blockers, graph, maximum_per_base=3):
    """Return a finite lexicographic prefix of exact blocker placements."""

    variants = []
    for blocker in blockers:
        for hub in eligible_blocker_hubs(graph):
            variants.append((blocker, hub))
            if len(variants) >= maximum_per_base:
                return variants
    return variants


def all_fixed_spans_before_deadline(
    graph,
    per_span_time_limit,
    workers,
    deadline,
):
    """Independent fixed-span certification under the global hard deadline."""

    results = {}
    overall = "non-colorable"
    started = time.perf_counter()
    for span in range(graph.delta, max(graph.delta, graph.n - 1) + 1):
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            overall = "timeout"
            results[span] = {"status": "NOT_RUN_DEADLINE", "coloring": {}}
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
        "completed_before_deadline": overall != "timeout",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "spans": results,
    }


def result_summary(result):
    return {
        key: value for key, value in result.__dict__.items() if key != "coloring"
    }


def exact_primary_summary(result):
    """Do not report a feasible upper bound as an exact primary span."""

    summary = result_summary(result)
    if summary["status"] == "colorable" and summary.get("solver_status") != "OPTIMAL":
        summary["status"] = "timeout"
        summary["exact_classification"] = False
        summary["observed_span_upper_bound"] = summary.get("span")
        summary["span"] = None
    return summary


def side_degrees(graph):
    left, right = graph.bipartition
    degrees = graph.degrees
    return {
        "left": sorted(degrees[vertex] for vertex in left),
        "right": sorted(degrees[vertex] for vertex in right),
    }


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def append_negative_event(path, event):
    if path.exists():
        try:
            events = json.loads(path.read_text()).get("events", [])
        except (OSError, ValueError):
            events = []
    else:
        events = []
    events.append(event)
    write_json(path, {"events": events})


def save_primary_negative(graph, candidate_id, digest, output_path):
    graph_directory = output_path.parent / "graphs" / "lane6-r6"
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
        output_path.parent / "lane6-signature-r6-negative-events.json",
        event,
    )
    print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)
    return graph_path, event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--maximum-delta", type=int, choices=(10,), default=10)
    parser.add_argument("--minimum-graph-degree", type=int, default=2)
    parser.add_argument("--maximum-signature-states", type=int, default=500000)
    parser.add_argument("--signature-time-limit", type=float, default=10.0)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--independent-time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-interval", type=int, default=25)
    parser.add_argument(
        "--total-time-limit",
        type=float,
        default=TOTAL_TIME_LIMIT_SECONDS,
    )
    parser.add_argument("--output", default="results/lane6-signature-r6.json")
    args = parser.parse_args()

    if args.total_time_limit > TOTAL_TIME_LIMIT_SECONDS + 1e-9:
        parser.error("--total-time-limit may not exceed 7200 seconds")
    if args.workers < 1:
        parser.error("--workers must be positive")

    started = time.perf_counter()
    deadline = started + float(args.total_time_limit)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    negative_event_path = (
        output_path.parent / "lane6-signature-r6-negative-events.json"
    )
    write_json(negative_event_path, {"events": []})

    _, base = seed_graph()
    relays, relay_stats = build_selected_relays(
        args.signature_time_limit,
        args.maximum_signature_states,
        deadline,
    )
    relay_stage_completed = relay_stats["relay_signature_stage_completed"]
    blockers, blocker_stats = (
        build_blockers(
            args.signature_time_limit,
            args.maximum_signature_states,
            deadline,
        )
        if relay_stage_completed
        else ([], {"blocker_stage_completed": False})
    )
    blocker_stage_completed = blocker_stats["blocker_stage_completed"]

    plans = list(configured_plans()) if relay_stage_completed else []

    configured_compositions_before_filters = (
        len(plans) * 4
        if blocker_stage_completed
        else len(plans)
    )

    rows = []
    negative_artifacts = []
    seen = set()
    generated = 0
    base_generated = 0
    blocker_eligible_base_count = 0
    blocker_augmented_generated = 0
    blocker_augmented_unique = 0
    base_rows = []
    augmented_rows_by_base: dict[str, list[dict]] = {}
    skipped_filtered_or_duplicate = 0
    candidate_stage_completed = True
    all_configured_candidates_processed = True

    for plan_index, plan in enumerate(plans):
        if time.perf_counter() >= deadline:
            candidate_stage_completed = False
            all_configured_candidates_processed = False
            break

        graph = build_candidate(base, plan, relays)
        generated += 1
        base_generated += 1
        structural_filter_passed = (
            graph.delta <= args.maximum_delta
            and min(graph.degrees.values()) >= args.minimum_graph_degree
            and nx.is_connected(graph._nx)
        )
        if not structural_filter_passed:
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
            all_configured_candidates_processed = False
            break
        result = rank_potential_solve(
            graph,
            min(args.time_limit, remaining),
            args.workers,
        )
        candidate_id = f"L6R6B-{len(base_rows):04d}"
        row = {
            "candidate_id": candidate_id,
            "canonical_sha256": digest,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "minimum_degree": min(graph.degrees.values()),
            "degree_sequence_by_side": side_degrees(graph),
            "plan_index": plan_index,
            "plan": {
                "ring_size": plan.ring_size,
                "loads": list(plan.loads),
                "pattern": list(plan.pattern),
                "orientation": list(plan.orientation),
            },
            "metadata": graph.metadata,
            "weighted_hubs_best": weighted_hub_statistics(graph)[:2],
            "primary_result": exact_primary_summary(result),
            "certified_non_colorable": False,
            "blocker_stage_triggered": False,
            "blocker_augmented_candidates": [],
        }
        augmented_rows_by_base[candidate_id] = []

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
                    "completed_before_deadline": False,
                    "spans": {},
                }
            else:
                row["independent_spans"] = all_fixed_spans_before_deadline(
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
                    "independently_certified": row["certified_non_colorable"],
                }
            )
            print(
                json.dumps(negative_artifacts[-1], sort_keys=True),
                file=sys.stderr,
                flush=True,
            )

        if result.status == "colorable" and result.span is not None and result.span >= 16:
            blocker_eligible_base_count += 1
            row["blocker_stage_triggered"] = True
            blocker_variants = configured_blocker_variants(blockers, graph)
            for blocker, hub in blocker_variants:
                if time.perf_counter() >= deadline:
                    candidate_stage_completed = False
                    all_configured_candidates_processed = False
                    break
                augmented_graph = attach_terminal_blocker(graph, blocker, hub)
                blocker_augmented_generated += 1
                structural_filter_passed = (
                    augmented_graph.delta <= args.maximum_delta
                    and min(augmented_graph.degrees.values())
                    >= args.minimum_graph_degree
                    and nx.is_connected(augmented_graph._nx)
                )
                if not structural_filter_passed:
                    skipped_filtered_or_duplicate += 1
                    continue
                augmented_digest = nauty_canonical_hash(augmented_graph)
                if augmented_digest in seen:
                    skipped_filtered_or_duplicate += 1
                    continue
                seen.add(augmented_digest)
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    candidate_stage_completed = False
                    all_configured_candidates_processed = False
                    break
                augmented_result = rank_potential_solve(
                    augmented_graph,
                    min(args.time_limit, remaining),
                    args.workers,
                )
                blocker_augmented_unique += 1
                augmented_id = f"L6R6X{len(rows):04d}-B{blocker['index']:02d}-{hub}"
                augmented_row = {
                    "candidate_id": augmented_id,
                    "canonical_sha256": augmented_digest,
                    "base_candidate_id": candidate_id,
                    "order": augmented_graph.n,
                    "size": augmented_graph.m,
                    "delta": augmented_graph.delta,
                    "minimum_degree": min(
                        augmented_graph.degrees.values()
                    ),
                    "degree_sequence_by_side": side_degrees(
                        augmented_graph
                    ),
                    "plan_index": plan_index,
                    "blocker": {
                        "index": blocker["index"],
                        "key": blocker["key"],
                        "signature_states": len(blocker["signature"]),
                        "hub": hub,
                    },
                    "metadata": augmented_graph.metadata,
                    "weighted_hubs_best": weighted_hub_statistics(
                        augmented_graph
                    )[:2],
                    "primary_result": exact_primary_summary(augmented_result),
                    "certified_non_colorable": False,
                }
                if augmented_result.status == "non-colorable":
                    augmented_graph_path, negative_event = save_primary_negative(
                        augmented_graph,
                        augmented_id.replace("-", "_"),
                        augmented_digest,
                        output_path,
                    )
                    augmented_row["graph_json"] = str(augmented_graph_path)
                    augmented_row["negative_event"] = negative_event
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        augmented_row["independent_spans"] = {
                            "encoding": "fixed-span-sat",
                            "decision": "timeout",
                            "completed_before_deadline": False,
                            "spans": {},
                        }
                    else:
                        augmented_row["independent_spans"] = (
                            all_fixed_spans_before_deadline(
                                augmented_graph,
                                args.independent_time_limit,
                                args.workers,
                                deadline,
                            )
                        )
                    augmented_row["certified_non_colorable"] = (
                        augmented_row["independent_spans"]["decision"]
                        == "non-colorable"
                    )
                    negative_artifacts.append(
                        {
                            "candidate_id": augmented_id,
                            "canonical_sha256": augmented_digest,
                            "graph_json": str(augmented_graph_path),
                            "order": augmented_graph.n,
                            "size": augmented_graph.m,
                            "delta": augmented_graph.delta,
                            "minimum_degree": min(
                                augmented_graph.degrees.values()
                            ),
                            "independently_certified": augmented_row[
                                "certified_non_colorable"
                            ],
                        }
                    )
                    print(
                        json.dumps(negative_artifacts[-1], sort_keys=True),
                        file=sys.stderr,
                        flush=True,
                    )
                augmented_rows_by_base[candidate_id].append(augmented_row)
                rows.append(augmented_row)
            if len(blocker_variants) < 3:
                row["blocker_variant_note"] = (
                    f"only {len(blocker_variants)} capacity-feasible "
                    "configured placements"
                )
        elif result.status == "timeout":
            row["blocker_stage_skipped_reason"] = "primary_timeout"
        row["blocker_augmented_candidate_ids"] = [
            item["candidate_id"] for item in augmented_rows_by_base[candidate_id]
        ]
        base_rows.append(row)
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

    base_stage_completed = candidate_stage_completed
    blocker_augmentation_completed = candidate_stage_completed
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
    unfinished_independent_certifications = sum(
        row.get("independent_spans", {}).get("decision") == "timeout"
        for row in rows
    )
    certification_stage_complete = (
        non_colorable_count
        == confirmed_negative_count + conflicting_classifications
        and unfinished_independent_certifications == 0
    )
    runtime_deadline_hit = time.perf_counter() >= deadline
    candidate_stage_completed = (
        candidate_stage_completed
        and base_stage_completed
        and blocker_augmentation_completed
    )
    all_configured_candidates_processed = (
        all_configured_candidates_processed
        and base_stage_completed
        and blocker_augmentation_completed
    )
    nauty_deduplication_complete = candidate_stage_completed
    exact_classification_complete = candidate_stage_completed and timeout_count == 0
    family_exhausted = (
        relay_stage_completed
        and blocker_stage_completed
        and candidate_stage_completed
        and all_configured_candidates_processed
        and nauty_deduplication_complete
        and exact_classification_complete
        and certification_stage_complete
        and conflicting_classifications == 0
    )
    unresolved_timeout_count = (
        timeout_count
        + int(not relay_stage_completed)
        + int(not blocker_stage_completed)
        + int(not candidate_stage_completed)
        + unfinished_independent_certifications
        + conflicting_classifications
    )

    spans = [
        row["primary_result"]["span"]
        for row in rows
        if row["primary_result"].get("span") is not None
    ]
    summary = {
        "schema": "lane6-signature-search-round-6",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "total_time_limit_seconds": args.total_time_limit,
        "runtime_deadline_hit": runtime_deadline_hit,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "ortools": __import__("ortools").__version__,
            "networkx": nx.__version__,
        },
        "analysis_inputs": {
            "completed_results": [
                "results/lane6-signature-r2.json",
                "results/lane6-signature-r3.json",
                "results/lane6-signature-r4.json",
                "results/lane6-split-hub-delta10.json",
                "results/lane6-chained-sync-corrected.json",
            ],
            "theory": "interval-edge-coloring-research-plan.md",
            "source_catalog_sha256": _sha256(Path(SOURCE_CATALOG)),
            "prior_round_result": "results/lane6-signature-r5.json",
        },
        "prior_round_diagnosis": PRIOR_ROUND_DIAGNOSIS,
        "family_definition": {
            "seed": "hat_K34_prime_Delta11",
            "construction": (
                "four-hub unequal-load (8,2,1,1) mixed relay ring with an "
                "independent forward/reverse orientation choice at each ring "
                "position; exact <=2-internal terminal blockers are attached "
                "only after a colorable base reaches primary span >=16"
            ),
            "relay_source_catalog": SOURCE_CATALOG,
            "relay_internal_vertex_bound": 5,
            "relay_selection": (
                "two shortest distinct forward strict-endpoint relays"
            ),
            "ring_sizes": [4],
            "connector_load_rule": (
                "all ordered permutations of (8,2,1,1), preserving the "
                "outlier's degree-10 relay and unequal loads"
            ),
            "pattern_rule": (
                "all length-four binary patterns using both selected relays"
            ),
            "orientation_rule": (
                "both choices independently at every ring position; forward "
                "is hub(position)-T0 and hub(next)-T1, reverse swaps T0/T1"
            ),
            "terminal_blocker_family": (
                "every exhaustive <=2-internal connected bipartite same-side "
                "two-terminal gadget with complete exact signature; attach "
                "T0/T1 to the same eligible left-side hub"
            ),
            "blocker_trigger_rule": (
                "only when the unblocked graph is exactly colorable with "
                "primary span at least 16"
            ),
            "blocker_hub_capacity_rule": "existing hub degree plus two <= Delta",
            "minimum_graph_degree": args.minimum_graph_degree,
            "maximum_delta": args.maximum_delta,
        },
        "completion_flags": {
            "relay_signature_stage_completed": relay_stage_completed,
            "exact_terminal_blocker_stage_completed": blocker_stage_completed,
            "candidate_stage_completed": candidate_stage_completed,
            "all_configured_candidates_processed": (
                all_configured_candidates_processed
            ),
            "nauty_deduplication_complete": nauty_deduplication_complete,
            "exact_rank_potential_classification_complete": (
                exact_classification_complete
            ),
            "independent_certification_stage_complete": (
                certification_stage_complete
            ),
            "configured_family_exhausted": family_exhausted,
            "family_exhausted": family_exhausted,
            "no_timeout_treated_as_negative": True,
        },
        "relay_enumeration": {
            "source_catalog": source_catalog_summary(),
            **relay_stats,
        },
        "terminal_blocker_enumeration": blocker_stats,
        "composition": {
            "configured_compositions_before_filters": (
                configured_compositions_before_filters
            ),
            "configured_base_plans_before_filters": len(plans),
            "maximum_configured_blockers_per_eligible_base": 3,
            "base_generated": base_generated,
            "base_unique": len(base_rows),
            "blocker_eligible_base_count": blocker_eligible_base_count,
            "blocker_augmented_generated": blocker_augmented_generated,
            "blocker_augmented_unique": blocker_augmented_unique,
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
            "primary_encoding": "exact-rank-potential-cpsat",
            "deduplication": "pynauty bipartition-colored certificate sha256",
            "minimum_primary_span": min(spans) if spans else None,
            "maximum_primary_span": max(spans) if spans else None,
        },
        "certification": {
            "policy": (
                "a primary negative is saved immediately but is called "
                "certified only when independent fixed-span SAT proves "
                "INFEASIBLE for every span delta through n-1"
            ),
            "negative_independently_confirmed": confirmed_negative_count,
            "unconfirmed_primary_negatives": non_colorable_count
            - confirmed_negative_count
            - conflicting_classifications,
            "conflicting_classifications": conflicting_classifications,
            "unfinished_independent_certifications": (
                unfinished_independent_certifications
            ),
            "unresolved_timeout_count": unresolved_timeout_count,
        },
        "negative_artifacts": negative_artifacts,
        "negative_events_path": str(negative_event_path.resolve()),
        "rows": rows,
    }
    write_json(output_path, summary)
    printable = {key: value for key, value in summary.items() if key != "rows"}
    print(json.dumps(printable, indent=2))


if __name__ == "__main__":
    main()
