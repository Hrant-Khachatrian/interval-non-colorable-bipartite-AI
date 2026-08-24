#!/usr/bin/env python3
"""Failure-guided same-side hub splits bounded at maximum degree 10.

The known Q1 negatives have one degree-11 hub and a few degree-4 core
vertices.  This search keeps the split copies on the hub's side and attaches
deterministic bipartite repair ears/shared relays to those remaining
high-degree vertices.  Candidates are ranked with features learned from prior
failures and near misses, including an exact CP-SAT probe that asks whether
removing a new gadget edge makes the candidate colorable.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import platform
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import networkx as nx

from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)


MAXIMUM_DELTA = 10
MINIMUM_FINAL_DEGREE = 2
MAX_SOLVER_SECONDS = 5.0
MAX_SOLVER_WORKERS = 4
MAX_RUNTIME_SECONDS = 10800.0
QUOTIENT_SEEDS = ("Q1-00012", "Q1-00014")
DISTRIBUTION_PATTERNS = (
    "contiguous-insertion",
    "contiguous-degree-name",
    "round-robin-insertion",
    "round-robin-degree-name",
)
# Base lengths are adjusted to endpoint parity: even lengths join same-side
# endpoints and odd lengths join opposite-side endpoints.
PATH_LENGTHS = ((2, 3), (3, 4), (4, 5))
ANCHOR_SET_SIZES = (1, 2, 3)


@dataclass
class Counters:
    constructions_attempted: int = 0
    generated: int = 0
    rejected_disconnected: int = 0
    rejected_low_degree: int = 0
    rejected_degree_cap: int = 0
    rejected_invalid: int = 0
    duplicate: int = 0
    unique: int = 0
    feature_probes_attempted: int = 0
    feature_probes_colorable: int = 0
    feature_probes_non_colorable: int = 0
    feature_probes_timeout: int = 0
    classified: int = 0
    colorable: int = 0
    non_colorable: int = 0
    timeout: int = 0
    confirmed_non_colorable: int = 0
    primary_negative_candidates: int = 0
    independent_unresolved: int = 0
    unclassified_deadline: int = 0
    unclassified_candidate_limit: int = 0
    solves_checkpointed: int = 0


@dataclass
class RunState:
    bounded_generation_complete: bool = True
    all_generated_feature_ranked: bool = False
    all_selected_classified: bool = True
    independent_confirmation_complete: bool = True
    runtime_deadline_hit: bool = False
    stop_reason: str | None = None


@dataclass
class Candidate:
    construction_id: int
    parent: str
    graph: Graph
    digest: str
    operation: dict
    static_features: dict = field(default_factory=dict)
    ranking_features: dict | None = None


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def add_edge(edges: set[tuple[str, str]], first: str, second: str) -> tuple[str, str]:
    edge = tuple(sorted((first, second)))
    if edge in edges:
        raise ValueError("repair gadget repeats an edge")
    edges.add(edge)
    return edge


def assign_two_way(
    incident: Sequence[tuple[str, tuple[str, str]]],
    first_size: int,
    pattern: str,
    neighbor_degrees: dict[str, int],
) -> tuple[list[tuple[str, tuple[str, str]]], list[tuple[str, tuple[str, str]]]]:
    rows = list(incident)
    if pattern.endswith("degree-name"):
        rows.sort(
            key=lambda item: (
                -neighbor_degrees[item[0]],
                item[0],
                item[1],
            )
        )
    if pattern.startswith("contiguous"):
        return rows[:first_size], rows[first_size:]
    if not pattern.startswith("round-robin"):
        raise ValueError(f"unknown distribution pattern: {pattern}")
    output: list[list[tuple[str, tuple[str, str]]]] = [[], []]
    remaining = [first_size, len(rows) - first_size]
    turn = 0
    for row in rows:
        while remaining[turn] == 0:
            turn = (turn + 1) % 2
        output[turn].append(row)
        remaining[turn] -= 1
        turn = (turn + 1) % 2
    return output[0], output[1]


def split_options(degree: int) -> list[tuple[int, int]]:
    options: set[tuple[int, int]] = set()
    for lower in range(MINIMUM_FINAL_DEGREE, degree // 2 + 1):
        upper = degree - lower
        if upper >= MINIMUM_FINAL_DEGREE:
            options.add((upper, lower))
    # Unequal splits are deliberately first: balanced loads were the least
    # failure-like family in the earlier synchronized-split run.
    return sorted(options, key=lambda item: (-abs(item[0] - item[1]), item))


def normalized_degree_variance(graph: Graph) -> float:
    degrees = list(graph.degrees.values())
    mean = sum(degrees) / len(degrees) if degrees else 0.0
    variance = sum((degree - mean) ** 2 for degree in degrees) / len(degrees)
    return variance / (mean * mean) if mean else 0.0


def high_degree_threshold(graph: Graph) -> int:
    ordered = sorted(graph.degrees.values(), reverse=True)
    position = max(1, len(ordered) // 4)
    return ordered[position - 1]


def forced_color_features(graph: Graph) -> dict:
    """Count colors forced by local interval incidence at retained hubs.

    At any vertex of degree d, every coloring must use exactly d distinct
    colors.  We therefore count d for each retained vertex at or above the
    top-quartile degree threshold.  This is a lower-bound pressure measure;
    it does not assert global span forcing.
    """
    threshold = high_degree_threshold(graph)
    selected = {
        vertex: degree
        for vertex, degree in sorted(graph.degrees.items())
        if degree >= threshold
    }
    forced_counts = [degree for degree in selected.values()]
    return {
        "high_degree_threshold": threshold,
        "remaining_high_degree_vertices": selected,
        "forced_colors_at_high_degree_vertices": sum(forced_counts),
        "max_forced_colors_at_high_degree_vertex": max(forced_counts, default=0),
        "high_degree_vertex_count": len(selected),
    }


def static_features(graph: Graph) -> dict:
    hubs = weighted_hub_statistics(graph)
    margins = sorted((row["margin"] for row in hubs), reverse=True)
    best_margin = margins[0] if margins else -(10**9)
    tier_rank, tier_name = 2, "below-minus-2.5"
    if best_margin >= -1.5:
        tier_rank, tier_name = 0, "at-least-minus-1.5"
    elif best_margin >= -2.5:
        tier_rank, tier_name = 1, "at-least-minus-2.5"
    return {
        **forced_color_features(graph),
        "hub_best_margin": best_margin,
        "hub_best_margin_tier": tier_name,
        "hub_best_margin_tier_rank": tier_rank,
        "top_three_hub_margins": margins[:3],
        "hub_margin_sum_top3": sum(margins[:3]),
        "sufficient_obstruction_count": sum(row["sufficient_obstruction"] for row in hubs),
        "span_lower_bound": graph.delta,
        "max_hub_forced_width": graph.delta - 1,
        "normalized_degree_variance": normalized_degree_variance(graph),
        "weighted_hubs_best": hubs[:4],
    }


def validate_graph(graph: Graph) -> tuple[bool, str]:
    if not nx.is_connected(graph._nx):
        return False, "disconnected"
    if graph.delta > MAXIMUM_DELTA:
        return False, "degree-cap"
    if min(graph.degrees.values(), default=0) < MINIMUM_FINAL_DEGREE:
        return False, "low-degree"
    return True, "valid"


def construct_candidate(
    base: Graph,
    hub: str,
    part_sizes: tuple[int, int],
    pattern: str,
    anchor_set: tuple[str, ...],
    lengths: tuple[int, int],
    include_shared_pair_relays: bool,
    construction_id: int,
) -> tuple[Graph | None, dict | None, str]:
    neighbor_degrees = {v: base.degrees[v] for v in base.vertices}
    incident = [
        (next(endpoint for endpoint in edge if endpoint != hub), edge)
        for edge in base.edges
        if hub in edge
    ]
    if sum(part_sizes) != len(incident):
        return None, None, "partition-size-mismatch"

    assignments = assign_two_way(incident, part_sizes[0], pattern, neighbor_degrees)
    prefix = f"FGD{construction_id:06d}"
    copies = (f"{prefix}_CA", f"{prefix}_CB")
    vertices = [vertex for vertex in base.vertices if vertex != hub]
    vertices.extend(copies)
    edges: set[tuple[str, str]] = {
        tuple(sorted(edge)) for edge in base.edges if hub not in edge
    }
    for copy, assignment in zip(copies, assignments):
        for neighbor, _ in assignment:
            edges.add(add_edge(edges, copy, neighbor))

    gadget_vertices: list[str] = []
    gadget_edges: list[tuple[str, str]] = []
    ears: list[dict] = []
    pair_relays: list[dict] = []
    copy_ear_load = [0, 0]
    for index, anchor in enumerate(anchor_set):
        anchor_is_left = anchor in base.bipartition[0]
        length = lengths[index % len(lengths)]
        # Adjust the deterministic length ladder to endpoint parity: even
        # lengths preserve side and odd lengths flip it.
        same_side_anchor = anchor_is_left == (hub in base.bipartition[0])
        if (length % 2 == 0) != same_side_anchor:
            length += 1
        # Both replacement copies remain on the hub's side; alternate their
        # repair loads while each path's internal side sequence follows parity.
        copy_index = index % 2
        internal_names = []
        previous = copies[copy_index]
        for position in range(1, length):
            start_side = "L" if hub in base.bipartition[0] else "R"
            current_side = start_side
            for _ in range(position):
                current_side = "R" if current_side == "L" else "L"
            side_name = current_side
            name = f"{prefix}_E{index:02d}V{position:02d}{side_name}"
            internal_names.append(name)
            gadget_vertices.append(name)
            previous, edge = name, add_edge(edges, previous, name)
            gadget_edges.append(edge)
        edge = add_edge(edges, previous, anchor)
        gadget_edges.append(edge)
        copy_ear_load[copy_index] += 1
        ears.append(
            {
                "anchor": anchor,
                "copy": copies[copy_index],
                "path_length": length,
                "internal_vertices": internal_names,
                "edges": [list(edge) for edge in gadget_edges[-length:]],
            }
        )

    if include_shared_pair_relays:
        for left_index, right_index in itertools.combinations(range(len(anchor_set)), 2):
            first_is_left = anchor_set[left_index] in base.bipartition[0]
            second_is_left = anchor_set[right_index] in base.bipartition[0]
            if first_is_left != second_is_left:
                continue
            pair_is_left = (
                anchor_set[left_index] in base.bipartition[0]
                and anchor_set[right_index] in base.bipartition[0]
            )
            relay_side = "R" if pair_is_left else "L"
            name = f"{prefix}_PR{left_index:02d}{right_index:02d}{relay_side}"
            gadget_vertices.append(name)
            first = add_edge(edges, name, anchor_set[left_index])
            second = add_edge(edges, name, anchor_set[right_index])
            gadget_edges.extend((first, second))
            pair_relays.append(
                {
                    "anchors": [anchor_set[left_index], anchor_set[right_index]],
                    "relay": name,
                    "edges": [list(first), list(second)],
            }
        )

    # Gadget relays are new named vertices, not endpoints borrowed from the
    # parent graph.
    vertices.extend(gadget_vertices)
    side_zero = set(base.bipartition[0])
    side_one = set(base.bipartition[1])
    side_zero.remove(hub)
    side_zero.update(copies)
    for vertex in gadget_vertices:
        if vertex.endswith("L"):
            side_zero.add(vertex)
        elif vertex.endswith("R"):
            side_one.add(vertex)
        else:
            return None, None, f"missing-side-marker:{vertex}"

    operation = {
        "operation_family": "failure-guided-anchor-repair",
        "construction_id": construction_id,
        "replaced_hub": hub,
        "replacement_side": "left" if hub in base.bipartition[0] else "right",
        "replacement_copies": list(copies),
        "spoke_loads": list(part_sizes),
        "distribution_pattern": pattern,
        "anchor_set": list(anchor_set),
        "ear_path_lengths_by_anchor": [
            lengths[index % len(lengths)] for index in range(len(anchor_set))
        ],
        "include_shared_pair_relays": include_shared_pair_relays,
        "copy_gadget_degrees": copy_ear_load,
        "gadget_vertices": gadget_vertices,
        "gadget_edges": [list(edge) for edge in gadget_edges],
        "ears": ears,
        "shared_pair_relays": pair_relays,
    }
    metadata = {
        **base.metadata,
        "lane": "failure-guided-delta10",
        "parent": base.metadata.get("candidate_id", base.metadata.get("family")),
        "repair_operation": operation,
    }
    try:
        graph = Graph(vertices, sorted(edges), [sorted(side_zero), sorted(side_one)], metadata)
    except (ValueError, KeyError, IndexError) as exc:
        return None, None, f"invalid-construction: {exc}"
    if [set(graph.bipartition[0]), set(graph.bipartition[1])] != [
        side_zero,
        side_one,
    ]:
        return None, None, "derived-bipartition-mismatch"
    if graph.delta > MAXIMUM_DELTA:
        return None, None, "degree cap exceeded"
    return graph, operation, "valid"


def enumerate_parent_candidates(
    root_name: str,
    root_graph: Graph,
    counters: Counters,
    state: RunState,
    deadline: float,
    global_seen: set[str],
    construction_offset: int,
    maximum_constructions: int,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    overcap = sorted(
        (vertex for vertex, degree in root_graph.degrees.items() if degree > MAXIMUM_DELTA),
        key=lambda vertex: (-root_graph.degrees[vertex], vertex),
    )
    if len(overcap) != 1:
        state.bounded_generation_complete = False
        state.stop_reason = "parent-does-not-have-one-overcap-hub"
        return candidates
    hub = overcap[0]
    threshold = high_degree_threshold(root_graph)
    anchors_all = sorted(
        vertex for vertex, degree in root_graph.degrees.items()
        if vertex != hub and degree >= threshold
    )

    for part_sizes in split_options(root_graph.degrees[hub]):
        for pattern in DISTRIBUTION_PATTERNS:
            for size in ANCHOR_SET_SIZES:
                if size > len(anchors_all):
                    continue
                for anchor_set in itertools.combinations(anchors_all, size):
                    for lengths in PATH_LENGTHS:
                        for shared_relay in (False, True):
                            if time.monotonic() >= deadline - 1.0:
                                state.bounded_generation_complete = False
                                state.runtime_deadline_hit = True
                                state.stop_reason = "generation-deadline"
                                return candidates
                            if counters.constructions_attempted >= maximum_constructions:
                                state.bounded_generation_complete = False
                                state.stop_reason = "construction-limit"
                                return candidates
                            counters.constructions_attempted += 1
                            construction_id = construction_offset + counters.constructions_attempted
                            graph, operation, reason = construct_candidate(
                                root_graph,
                                hub,
                                part_sizes,
                                pattern,
                                anchor_set,
                                lengths,
                                shared_relay,
                                construction_id,
                            )
                            if graph is None or operation is None:
                                if reason == "degree cap exceeded":
                                    counters.rejected_degree_cap += 1
                                elif reason == "disconnected":
                                    counters.rejected_disconnected += 1
                                elif reason == "low-degree":
                                    counters.rejected_low_degree += 1
                                else:
                                    counters.rejected_invalid += 1
                                continue
                            valid, validation_reason = validate_graph(graph)
                            if not valid:
                                setattr(counters, f"rejected_{validation_reason.replace('-', '_')}", getattr(counters, f"rejected_{validation_reason.replace('-', '_')}") + 1)
                                continue
                            counters.generated += 1
                            digest = nauty_canonical_hash(graph)
                            if digest in global_seen:
                                counters.duplicate += 1
                                continue
                            global_seen.add(digest)
                            counters.unique += 1
                            graph.metadata["source_root"] = root_name
                            graph.metadata["construction_id"] = construction_id
                            candidates.append(
                                Candidate(construction_id, root_name, graph, digest, operation)
                            )
    return candidates


def static_rank_key(candidate: Candidate) -> tuple:
    row = candidate.static_features
    return (
        -row["forced_colors_at_high_degree_vertices"],
        -row["hub_best_margin"],
        row["hub_best_margin_tier_rank"],
        row["span_lower_bound"],
        -row["normalized_degree_variance"],
        candidate.digest,
    )


def final_rank_key(candidate: Candidate) -> tuple:
    row = candidate.ranking_features or candidate.static_features
    deletion = row["edge_deletion_restoration"]
    return (
        -int(deletion["any_edge_restores_colorability"]),
        -deletion["restoring_edge_count"],
        -row["forced_colors_at_high_degree_vertices"],
        -row["hub_best_margin"],
        row["hub_best_margin_tier_rank"],
        row["span_lower_bound"],
        -row["normalized_degree_variance"],
        deletion["unresolved_probe_count"],
        candidate.digest,
    )


def probe_new_edge_deletions(
    candidate: Candidate,
    time_limit: float,
    workers: int,
    deadline: float,
    checkpoint: Callable[[], None],
) -> None:
    graph = candidate.graph
    gadget_edges = [tuple(edge) for edge in candidate.operation["gadget_edges"]]
    statuses: dict[str, str] = {}
    restoring: list[list[str]] = []
    unresolved = 0
    serialized_statuses: dict[str, str] = {}
    for edge in gadget_edges:
        if time.monotonic() >= deadline - 0.25:
            unresolved += 1
            statuses[json.dumps(edge, separators=(",", ":"))] = "UNKNOWN-deadline"
            continue
        reduced_edges = [candidate_edge for candidate_edge in graph.edges if candidate_edge != edge]
        try:
            reduced = Graph(graph.vertices, reduced_edges, graph.bipartition, graph.metadata)
        except ValueError:
            statuses[json.dumps(edge, separators=(",", ":"))] = "INVALID"
            continue
        counters_probe = rank_potential_solve(reduced, time_limit, workers)
        status = counters_probe.status
        statuses[json.dumps(edge, separators=(",", ":"))] = status
        serialized_statuses = dict(statuses)
        candidate.ranking_features = {
            **candidate.static_features,
            "edge_deletion_restoration": {
                "encoding": "rank-potential-cpsat",
                "probe_time_limit_seconds": time_limit,
                "probe_workers": workers,
                "new_gadget_edge_count": len(gadget_edges),
                "statuses": serialized_statuses,
                "restoring_edges": [list(item) for item in restoring],
                "restoring_edge_count": len(restoring),
                "any_edge_restores_colorability": bool(restoring),
                "unresolved_probe_count": unresolved,
                "partial": True,
            },
        }
        checkpoint()
        if status == "colorable":
            restoring.append(list(edge))
        elif status == "timeout":
            unresolved += 1
    candidate.ranking_features = {
        **candidate.static_features,
        "edge_deletion_restoration": {
            "encoding": "rank-potential-cpsat",
            "probe_time_limit_seconds": time_limit,
            "probe_workers": workers,
            "new_gadget_edge_count": len(gadget_edges),
            "statuses": statuses,
            "restoring_edges": restoring,
            "restoring_edge_count": len(restoring),
            "any_edge_restores_colorability": bool(restoring),
            "unresolved_probe_count": unresolved,
            "partial": False,
        },
    }


def confirm_negative(
    graph: Graph,
    time_limit: float,
    workers: int,
    deadline: float,
    span_checkpoint: Callable[[dict[int, str]], None],
) -> tuple[bool, bool, dict[int, str]]:
    statuses: dict[int, str] = {}
    legal_spans = range(graph.delta, max(graph.delta, graph.n - 1) + 1)
    for span in legal_spans:
        remaining = deadline - time.monotonic()
        if remaining <= 0.25:
            statuses[span] = "UNKNOWN"
            span_checkpoint(statuses)
            return False, True, statuses
        effective_limit = max(0.01, min(time_limit, MAX_SOLVER_SECONDS, remaining - 0.2))
        status_name, _ = fixed_span_sat_solve(graph, span, effective_limit, workers)
        statuses[span] = status_name
        span_checkpoint(statuses)
        if status_name in ("OPTIMAL", "FEASIBLE"):
            return False, False, statuses
    unresolved = any(status == "UNKNOWN" for status in statuses.values())
    confirmed = not unresolved and all(status == "INFEASIBLE" for status in statuses.values())
    return confirmed, unresolved, statuses


def classify_candidate(
    number: int,
    candidate: Candidate,
    args: argparse.Namespace,
    counters: Counters,
    state: RunState,
    deadline: float,
    checkpoint: Callable[[dict | None], None],
) -> tuple[dict, dict | None]:
    remaining = deadline - time.monotonic()
    reserve = 15.0
    if remaining <= reserve:
        counters.unclassified_deadline += 1
        state.all_selected_classified = False
        state.runtime_deadline_hit = True
        state.stop_reason = "classification-deadline"
        return {"candidate_id": candidate_id(number), "decision": "not-attempted-deadline"}, None

    graph = candidate.graph
    effective_primary = max(
        0.01,
        min(args.primary_time_limit, MAX_SOLVER_SECONDS, remaining - reserve),
    )
    primary = rank_potential_solve(graph, effective_primary, args.workers)
    row = {
        "candidate_id": candidate_id(number),
        "parent": candidate.parent,
        "canonical_sha256": candidate.digest,
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "operation": candidate.operation,
        "ranking_features": candidate.ranking_features,
        "primary_result": {
            key: value for key, value in primary.__dict__.items() if key != "coloring"
        },
    }
    counters.classified += 1
    counters.solves_checkpointed += 1
    checkpoint(row)

    if primary.status == "colorable":
        counters.colorable += 1
        row["decision"] = "colorable"
        return row, None
    if primary.status == "timeout":
        counters.timeout += 1
        row["decision"] = "timeout"
        row["independent_confirmation"] = {
            "required": False,
            "reason": "primary timeout remains unresolved",
        }
        return row, None
    if primary.status != "non-colorable":
        raise AssertionError(f"unexpected solver status: {primary.status}")

    counters.primary_negative_candidates += 1
    row["decision"] = "pending-independent-confirmation"
    row["independent_confirmation"] = {
        "encoding": "fixed-span-cpsat",
        "legal_span_range": [graph.delta, max(graph.delta, graph.n - 1)],
        "spans_checked": [],
        "span_statuses": {},
        "confirmed_non_colorable": None,
        "unresolved": None,
    }

    def span_checkpoint(statuses: dict[int, str]) -> None:
        row["independent_confirmation"]["spans_checked"] = sorted(statuses)
        row["independent_confirmation"]["span_statuses"] = {
            str(span): status for span, status in sorted(statuses.items())
        }
        counters.solves_checkpointed += 1
        checkpoint(row)

    confirmed, unresolved, span_statuses = confirm_negative(
        graph,
        args.independent_time_limit,
        args.workers,
        deadline - 2.0,
        span_checkpoint,
    )
    row["independent_confirmation"].update(
        {
            "spans_checked": sorted(span_statuses),
            "span_statuses": {str(k): v for k, v in sorted(span_statuses.items())},
            "confirmed_non_colorable": confirmed,
            "unresolved": unresolved,
        }
    )
    if unresolved:
        counters.independent_unresolved += 1
        counters.timeout += 1
        row["decision"] = "timeout"
        state.independent_confirmation_complete = False
        return row, None
    if not confirmed:
        raise AssertionError(
            "independent encoding contradicted primary negative: " + candidate.digest
        )

    counters.non_colorable += 1
    counters.confirmed_non_colorable += 1
    row["decision"] = "non-colorable"
    graph.metadata["candidate_id"] = row["candidate_id"]
    graph.metadata["certification"] = {
        "primary": "rank-potential-cpsat-infeasible",
        "independent": "fixed-span-cpsat-infeasible-all-legal-spans",
        "spans_checked": sorted(span_statuses),
    }
    return row, {**row, "graph": graph.to_json()}


def candidate_id(number: int) -> str:
    return f"FGD10-{number:04d}"


def configuration(args: argparse.Namespace) -> dict:
    return {
        "starting_graphs": list(QUOTIENT_SEEDS),
        "maximum_final_delta": MAXIMUM_DELTA,
        "minimum_final_degree": MINIMUM_FINAL_DEGREE,
        "require_connected_simple_bipartite": True,
        "candidate_cap_unique_per_parent": args.candidate_cap_per_parent,
        "feature_shortlist_unique_per_parent": args.feature_shortlist_per_parent,
        "maximum_constructions_per_parent": args.maximum_constructions_per_parent,
        "solver_workers": args.workers,
        "primary_time_limit_seconds": args.primary_time_limit,
        "independent_time_limit_seconds": args.independent_time_limit,
        "edge_deletion_probe_time_limit_seconds": args.edge_deletion_probe_time_limit,
        "runtime_hard_deadline_seconds": args.deadline_seconds,
        "checkpoint_record_window": args.checkpoint_record_window,
        "atomic_checkpoint_after_every_solver_call": True,
        "distribution_patterns": list(DISTRIBUTION_PATTERNS),
        "split_policy": "all unordered two-copy spoke partitions with each load at least 2; unequal loads ranked first",
        "anchor_sets": "all subsets of size 1..3 of non-hub top-quartile/high-degree parent vertices",
        "ear_path_lengths": [list(item) for item in PATH_LENGTHS],
        "shared_pair_relays": [False, True],
    }


def report_base(args: argparse.Namespace, roots: Sequence[tuple[str, Graph]]) -> dict:
    return {
        "schema_version": 1,
        "configuration": configuration(args),
        "environment": {"python": platform.python_version()},
        "roots": [
            {
                "name": name,
                "path": graph.metadata.get("source_path"),
                "order": graph.n,
                "size": graph.m,
                "maximum_degree": graph.delta,
                "minimum_degree": min(graph.degrees.values()),
                "overcap_vertices": {
                    vertex: degree for vertex, degree in graph.degrees.items() if degree > MAXIMUM_DELTA
                },
                "high_degree_vertices": forced_color_features(graph)["remaining_high_degree_vertices"],
            }
            for name, graph in roots
        ],
        "operation_definition": {
            "replacement_rule": (
                "replace each Q1 degree-11 hub by two copies on its own bipartition "
                "side; every former spoke is assigned exactly once and each copy gets "
                "at least two spokes"
            ),
            "repair_rule": (
                "for a deterministic subset of remaining high-degree vertices, attach "
                "an internally disjoint parity-correct bipartite ear from alternating "
                "split copies; optionally add one shared opposite-side relay for each "
                "same-side anchor pair"
            ),
            "learned_failure_basis": (
                "prior Q1 synchronized splits were colorable; obstruction analysis "
                "ranks high best weighted-hub margin, tight span lower bound, and high "
                "degree variance, while advising asymmetric loads and avoiding isolated "
                "symmetric relays"
            ),
            "static_ranking_features": [
                "sum of degree-forced colors at remaining high-degree vertices",
                "best weighted-hub margin",
                "margin tier from learned thresholds -1.5 and -2.5",
                "span lower bound delta",
                "normalized degree variance",
            ],
            "solver_derived_ranking_feature": (
                "bounded rank_potential_solve CP-SAT probes after deleting each new "
                "gadget edge; record which deletions restore colorability"
            ),
            "classification": (
                "exact rank_potential_solve CP-SAT for each selected unique candidate, "
                "limited to at most 5 seconds and 4 workers"
            ),
            "negative_confirmation": (
                "fixed_span_sat_solve independently over every legal span from delta "
                "through n-1; only all-INFEASIBLE evidence counts"
            ),
            "timeout_policy": (
                "a primary or independent UNKNOWN is unresolved, counted once as timeout, "
                "and never counted as non-colorable"
            ),
            "deduplication": "pynauty bipartition-colored certificate from nauty_canonical_hash",
            "graph_serialization_policy": "full graph JSON appears only inside confirmed_negatives",
        },
        "deadline_seconds": args.deadline_seconds,
        "elapsed_seconds": 0.0,
        "counts": {},
        "completion": {},
        "complete": False,
        "negative_events": [],
        "summaries": [],
        "records": [],
        "confirmed_negatives": [],
    }


def make_report(
    args: argparse.Namespace,
    roots: Sequence[tuple[str, Graph]],
    summaries: Sequence[dict],
    records: Iterable[dict],
    confirmed_negatives: Sequence[dict],
    elapsed_seconds: float,
    counters: Counters,
    state: RunState,
    partial: bool,
) -> dict:
    payload = report_base(args, roots)
    counts = {
        "constructions_attempted": counters.constructions_attempted,
        "generated": counters.generated,
        "unique": counters.unique,
        "classified": counters.classified,
        "colorable": counters.colorable,
        "non_colorable": counters.confirmed_non_colorable,
        "timeout": counters.timeout,
        "confirmed_non_colorable": counters.confirmed_non_colorable,
        "primary_negative_candidates": counters.primary_negative_candidates,
        "independent_unresolved": counters.independent_unresolved,
        "duplicate": counters.duplicate,
        "feature_probes_attempted": counters.feature_probes_attempted,
        "feature_probes_colorable": counters.feature_probes_colorable,
        "feature_probes_non_colorable": counters.feature_probes_non_colorable,
        "feature_probes_timeout": counters.feature_probes_timeout,
        "unclassified_deadline": counters.unclassified_deadline,
        "unclassified_candidate_limit": counters.unclassified_candidate_limit,
        "solver_calls_checkpointed": counters.solves_checkpointed,
    }
    completion = {
        "bounded_generation_complete": state.bounded_generation_complete,
        "all_generated_feature_ranked": state.all_generated_feature_ranked,
        "all_selected_classified": state.all_selected_classified,
        "independent_confirmation_complete": state.independent_confirmation_complete,
        "selected_subset_only": args.candidate_cap_per_parent > 0,
        "runtime_deadline_hit": state.runtime_deadline_hit,
        "stop_reason": state.stop_reason,
        "bounded_run_complete": (
            state.bounded_generation_complete
            and state.all_selected_classified
            and state.independent_confirmation_complete
            and not state.runtime_deadline_hit
            and not partial
        ),
        "exhaustive_classification_of_all_unique": (
            state.bounded_generation_complete
            and state.all_generated_feature_ranked
            and counters.unique == counters.classified
            and args.candidate_cap_per_parent <= 0
        ),
    }
    payload.update(
        {
            "elapsed_seconds": elapsed_seconds,
            "counts": counts,
            "completion": completion,
            "complete": completion["bounded_run_complete"],
            "summaries": list(summaries),
            "records": list(records)[-args.checkpoint_record_window :],
            "negative_events": [
                {key: value for key, value in record.items() if key != "graph"}
                for record in confirmed_negatives
            ],
            "confirmed_negatives": list(confirmed_negatives),
        }
    )
    return payload


def write_report(
    output_path: Path,
    args: argparse.Namespace,
    roots: Sequence[tuple[str, Graph]],
    summaries: Sequence[dict],
    records: Sequence[dict],
    negatives: Sequence[dict],
    elapsed_start: float,
    counters: Counters,
    state: RunState,
    partial: bool,
) -> None:
    atomic_write_json(
        output_path,
        make_report(
            args,
            roots,
            summaries,
            records,
            negatives,
            time.monotonic() - elapsed_start,
            counters,
            state,
            partial,
        ),
    )


def resolve_roots(directory: Path) -> tuple[list[tuple[str, Graph]], dict]:
    roots: list[tuple[str, Graph]] = []
    paths: dict[str, str] = {}
    for name in QUOTIENT_SEEDS:
        path = directory / f"{name}.graph.json"
        if not path.exists():
            raise FileNotFoundError(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        metadata = dict(data.get("metadata") or {})
        metadata["root_kind"] = "known_Q1_failure_parent"
        metadata["source_path"] = str(path)
        roots.append((name, Graph.from_json({**data, "metadata": metadata})))
        paths[name] = str(path)
    return roots, {"requested_directory": str(directory), "resolved_paths": paths}


def search_root(
    root_name: str,
    root_graph: Graph,
    args: argparse.Namespace,
    deadline: float,
    global_seen: set[str],
    construction_offset: int,
    completed_summaries: list[dict],
    records: list[dict],
    negatives: list[dict],
    counters: Counters,
    state: RunState,
    roots: Sequence[tuple[str, Graph]],
    output_path: Path,
    started: float,
) -> dict:
    root_started = time.monotonic()
    generated = enumerate_parent_candidates(
        root_name,
        root_graph,
        counters,
        state,
        deadline,
        global_seen,
        construction_offset,
        args.maximum_constructions_per_parent,
    )
    for candidate in generated:
        candidate.static_features = static_features(candidate.graph)
    generated.sort(key=static_rank_key)
    shortlist = (
        generated
        if args.feature_shortlist_per_parent <= 0
        else generated[: args.feature_shortlist_per_parent]
    )

    def no_row_checkpoint() -> None:
        write_report(
            output_path,
            args,
            roots,
            [*completed_summaries, live_summary()],
            records,
            negatives,
            started,
            counters,
            state,
            partial=True,
        )

    def live_summary(elapsed: float | None = None, selected_count: int | None = None) -> dict:
        result = {
            "root": root_name,
            "root_order": root_graph.n,
            "root_size": root_graph.m,
            "generated_in_root": counters.generated,
            "unique_in_root": counters.unique,
            "shortlisted_for_feature_probes": len(shortlist),
            "selected_for_classification": (
                len(shortlist) if selected_count is None else selected_count
            ),
            **vars(counters).copy(),
            "enumeration_complete": state.bounded_generation_complete,
            "classification_complete": state.all_selected_classified,
            "independent_confirmation_complete": state.independent_confirmation_complete,
        }
        if elapsed is not None:
            result["elapsed_seconds"] = elapsed
        return result

    for candidate in shortlist:
        if time.monotonic() >= deadline - 1.0:
            state.all_selected_classified = False
            state.runtime_deadline_hit = True
            state.stop_reason = "feature-ranking-deadline"
            break
        probe_new_edge_deletions(
            candidate,
            args.edge_deletion_probe_time_limit,
            args.workers,
            deadline,
            no_row_checkpoint,
        )
        counters.feature_probes_attempted += len(
            candidate.ranking_features["edge_deletion_restoration"]["statuses"]
        )
        for status in candidate.ranking_features["edge_deletion_restoration"]["statuses"].values():
            if status == "colorable":
                counters.feature_probes_colorable += 1
            elif status == "non-colorable":
                counters.feature_probes_non_colorable += 1
            else:
                counters.feature_probes_timeout += 1
    if state.stop_reason is None:
        state.all_generated_feature_ranked = (
            args.feature_shortlist_per_parent <= 0
            or len(generated) <= len(shortlist)
        )

    selected = (
        shortlist if args.candidate_cap_per_parent <= 0 else shortlist[: args.candidate_cap_per_parent]
    )
    selected.sort(key=final_rank_key)
    if args.candidate_cap_per_parent > 0 and len(generated) > len(selected):
        counters.unclassified_candidate_limit += len(generated) - len(selected)

    def checkpoint(latest_row: dict | None) -> None:
        current_records = [*records, latest_row] if latest_row is not None else records
        write_report(
            output_path,
            args,
            roots,
            [*completed_summaries, live_summary()],
            current_records,
            negatives,
            started,
            counters,
            state,
            partial=True,
        )

    next_number = counters.classified + 1
    for number, candidate in enumerate(selected, start=next_number):
        row, negative_record = classify_candidate(
            number,
            candidate,
            args,
            counters,
            state,
            deadline,
            checkpoint,
        )
        records.append(row)
        if negative_record is not None:
            negatives.append(negative_record)
        checkpoint(None)

    summary = live_summary(time.monotonic() - root_started, len(selected))
    summary["classification_complete"] = (
        state.bounded_generation_complete
        and state.all_selected_classified
        and counters.unclassified_deadline == 0
    )
    summary["complete"] = summary["classification_complete"] and state.independent_confirmation_complete
    return summary


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quotient-dir", type=Path, default=Path("results/graphs/quotient-r1"))
    parser.add_argument("--output", type=Path, default=Path("results/failure-guided-delta10.json"))
    parser.add_argument("--candidate-cap-per-parent", type=int, default=160)
    parser.add_argument("--feature-shortlist-per-parent", type=int, default=512)
    parser.add_argument("--maximum-constructions-per-parent", type=int, default=10000)
    parser.add_argument("--primary-time-limit", type=float, default=5.0)
    parser.add_argument("--independent-time-limit", type=float, default=5.0)
    parser.add_argument("--edge-deletion-probe-time-limit", type=float, default=0.10)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--deadline-seconds", type=float, default=10800.0)
    parser.add_argument("--checkpoint-record-window", type=int, default=256)
    parser.add_argument("--smoke", action="store_true")
    return parser


def smoke_arguments(args: argparse.Namespace) -> argparse.Namespace:
    values = vars(args).copy()
    values.update(
        {
            "output": Path("results/failure-guided-delta10-smoke.json"),
            "candidate_cap_per_parent": 2,
            "feature_shortlist_per_parent": 6,
            "maximum_constructions_per_parent": 3000,
            "primary_time_limit": 1.0,
            "independent_time_limit": 1.0,
            "edge_deletion_probe_time_limit": 0.05,
            "deadline_seconds": min(args.deadline_seconds, 120.0),
        }
    )
    return argparse.Namespace(**values)


def validate_arguments(args: argparse.Namespace) -> None:
    if not 1 <= args.workers <= MAX_SOLVER_WORKERS:
        raise SystemExit(f"workers must be between 1 and {MAX_SOLVER_WORKERS}")
    if not 0 < args.primary_time_limit <= MAX_SOLVER_SECONDS:
        raise SystemExit("primary limit must be positive and at most 5 seconds")
    if not 0 < args.independent_time_limit <= MAX_SOLVER_SECONDS:
        raise SystemExit("independent limit must be positive and at most 5 seconds")
    if not 0 < args.edge_deletion_probe_time_limit <= MAX_SOLVER_SECONDS:
        raise SystemExit("edge deletion probe limit must be positive and at most 5 seconds")
    if not 0 < args.deadline_seconds <= MAX_RUNTIME_SECONDS:
        raise SystemExit("deadline must be positive and at most 10800 seconds")
    if args.candidate_cap_per_parent < 0 or args.feature_shortlist_per_parent < 0:
        raise SystemExit("caps cannot be negative")
    if args.maximum_constructions_per_parent <= 0:
        raise SystemExit("construction limit must be positive")


def main() -> None:
    args = argument_parser().parse_args()
    if args.smoke:
        args = smoke_arguments(args)
    validate_arguments(args)
    roots, resolution = resolve_roots(args.quotient_dir)
    output_path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + args.deadline_seconds
    counters = Counters()
    state = RunState()
    global_seen: set[str] = set()
    records: list[dict] = []
    negatives: list[dict] = []
    completed_summaries: list[dict] = []

    write_report(output_path, args, roots, [], [], [], started, counters, state, True)
    construction_offset = 0
    for root_name, root_graph in roots:
        if time.monotonic() >= deadline - 15.0:
            state.all_selected_classified = False
            state.runtime_deadline_hit = True
            state.stop_reason = "root-start-deadline"
            break
        summary = search_root(
            root_name,
            root_graph,
            args,
            deadline,
            global_seen,
            construction_offset,
            completed_summaries,
            records,
            negatives,
            counters,
            state,
            roots,
            output_path,
            started,
        )
        completed_summaries.append(summary)
        construction_offset = counters.constructions_attempted

    write_report(
        output_path,
        args,
        roots,
        completed_summaries,
        records,
        negatives,
        started,
        counters,
        state,
        False,
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "elapsed_seconds": time.monotonic() - started,
                "counts": asdict(counters),
                "state": asdict(state),
                "seed_resolution": resolution,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
