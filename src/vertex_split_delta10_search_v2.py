#!/usr/bin/env python3
"""Deeper deterministic same-side vertex-split search bounded at delta 10."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import networkx as nx

from interval_edge_coloring import (
    Graph,
    benchmark_graphs,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)


@dataclass
class Counters:
    constructions_attempted: int = 0
    generated: int = 0
    rejected_disconnected: int = 0
    rejected_low_degree: int = 0
    rejected_degree_cap: int = 0
    duplicate: int = 0
    unique: int = 0
    classified: int = 0
    colorable: int = 0
    non_colorable: int = 0
    timeout: int = 0
    confirmed_non_colorable: int = 0
    primary_negative_candidates: int = 0
    independent_unresolved: int = 0
    unclassified_deadline: int = 0


@dataclass
class RunState:
    global_seen: set[str] = field(default_factory=set)
    deadline_hit: bool = False
    enumeration_complete: bool = True
    classification_complete: bool = True
    independent_confirmation_complete: bool = True
    target_limited: bool = False


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def normalized_degree_variance(graph: Graph) -> float:
    degrees = list(graph.degrees.values())
    mean = sum(degrees) / len(degrees) if degrees else 0.0
    if mean == 0.0:
        return 0.0
    variance = sum((degree - mean) ** 2 for degree in degrees) / len(degrees)
    return variance / (mean * mean)


def ranking_features(graph: Graph) -> dict:
    statistics = weighted_hub_statistics(graph)
    margins = sorted((row["margin"] for row in statistics), reverse=True)
    best_margin = margins[0] if margins else -10**9
    return {
        "hub_best_margin": best_margin,
        "hub_best_margin_tier_at_least_minus_1_5": bool(best_margin >= -1.5),
        "sufficient_obstruction_count": sum(
            row["sufficient_obstruction"] for row in statistics
        ),
        "top_three_hub_margins": margins[:3],
        "hub_margin_sum_top3": sum(margins[:3]),
        "normalized_degree_variance": normalized_degree_variance(graph),
        "weighted_hubs_best": statistics[:5],
    }


def _balanced_remainder(total: int, parts: int) -> tuple[int, ...]:
    base, remainder = divmod(total, parts)
    return tuple(sorted(
        (base + 1 if index < remainder else base for index in range(parts)),
        reverse=True,
    ))


EDGE_PATTERNS = (
    "contiguous-insertion",
    "contiguous-degree-name",
    "round-robin-insertion",
    "round-robin-degree-name",
    "mirrored-blocks",
)


def split_options(degree: int) -> list[tuple[int, ...]]:
    """Deterministic unordered degree partitions used for one replaced hub."""
    if degree < 4:
        return []
    options: set[tuple[int, ...]] = set()
    for lower in range(2, degree // 2 + 1):
        options.add(tuple(sorted((lower, degree - lower), reverse=True)))

    base, remainder = divmod(degree, 3)
    balanced = tuple(sorted((base, base, base + remainder), reverse=True))
    if min(balanced) >= 2:
        options.add(balanced)
    for small_part in (2, 3):
        remaining = degree - small_part
        if remaining < 4:
            continue
        half, odd = divmod(remaining, 2)
        parts = tuple(sorted((small_part, half, half + odd), reverse=True))
        if min(parts) >= 2:
            options.add(parts)
    extreme = tuple(sorted((2, 2, degree - 4), reverse=True))
    if min(extreme) >= 2:
        options.add(extreme)

    balanced_four = _balanced_remainder(degree, 4)
    if min(balanced_four) >= 2:
        options.add(balanced_four)
    extreme_four = _balanced_remainder(degree - 6, 3) + (2, 2, 2)
    if sum(extreme_four) == degree and min(extreme_four) >= 2:
        options.add(tuple(sorted(extreme_four, reverse=True)))
    for small_part in (2, 3):
        parts = _balanced_remainder(degree - small_part, 3) + (small_part,)
        if min(parts) >= 2:
            options.add(tuple(sorted(parts, reverse=True)))
    for leading in (2, 3, degree // 3, (degree + 2) // 3):
        if leading < 2:
            continue
        remaining = degree - leading
        if remaining < 6:
            continue
        parts = (leading,) + _balanced_remainder(remaining, 3)
        if min(parts) >= 2:
            options.add(tuple(sorted(parts, reverse=True)))
    return sorted(options, key=lambda item: (-item[0], item[1:], item))


def assign_incident_edges(
    incident: Sequence[tuple[str, tuple[str, str]]],
    part_sizes: Sequence[int],
    pattern: str,
) -> list[list[tuple[str, str]]]:
    rows = list(incident)
    if pattern.endswith("degree-name"):
        rows = sorted(rows, key=lambda item: (-len(item[1]) - 1, item[0], item[1]))

    assignment: list[list[tuple[str, str]]] = [[] for _ in part_sizes]
    if pattern.startswith("contiguous") or pattern == "mirrored-blocks":
        boundaries = list(itertools.accumulate(part_sizes))
        starts = [0, *boundaries[:-1]]
        block_order = list(range(len(part_sizes)))
        if pattern == "mirrored-blocks":
            ordered: list[int] = []
            low, high = 0, len(part_sizes) - 1
            while low <= high:
                ordered.append(low)
                if low != high:
                    ordered.append(high)
                low, high = low + 1, high - 1
            block_order = ordered
        position_to_block = {
            position: block
            for block, start, stop in zip(block_order, starts, boundaries)
            for position in range(start, stop)
        }
        for position, (_, edge) in enumerate(rows):
            assignment[position_to_block[position]].append(edge)
    elif pattern.startswith("round-robin"):
        remaining = list(part_sizes)
        turn = 0
        for _, edge in rows:
            while not remaining[turn]:
                turn = (turn + 1) % len(part_sizes)
            assignment[turn].append(edge)
            remaining[turn] -= 1
            turn = (turn + 1) % len(part_sizes)
    else:
        raise ValueError(f"unknown edge-distribution pattern: {pattern}")
    return assignment


def connector_pairs(mode: str, replacement_count: int) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    if mode == "none" or replacement_count < 2:
        return pairs
    if mode == "chain":
        pairs.extend((index, index + 1) for index in range(replacement_count - 1))
    elif mode == "bridge":
        pairs.append((0, replacement_count - 1))
    elif mode == "selected-pairs":
        pairs.append((0, 1))
        if replacement_count > 2:
            pairs.append((0, replacement_count - 1))
    else:
        raise ValueError(f"unknown connector mode: {mode}")
    return sorted(set(pairs))


def apply_same_side_split(
    base: Graph,
    hub: str,
    part_sizes: Sequence[int],
    construction_id: int,
    operation_index: int,
    maximum_delta: int,
    pattern: str = "contiguous-insertion",
    connector_mode: str = "none",
) -> tuple[Graph | None, str]:
    """Replace one hub by same-side vertices carrying its incident edges."""
    incident = [
        (next(endpoint for endpoint in edge if endpoint != hub), edge)
        for edge in base.edges
        if hub in edge
    ]
    if sum(part_sizes) != len(incident):
        return None, "partition does not cover incident degree"
    if any(size < 2 for size in part_sizes) or len(part_sizes) < 2:
        return None, "illegal replacement degrees"

    assignment = assign_incident_edges(incident, part_sizes, pattern)

    side_index = 0 if hub in base.bipartition[0] else 1
    prefix = f"V2S{construction_id:06d}O{operation_index:02d}"
    replacements = [f"{prefix}_{part_number:02d}" for part_number in range(len(part_sizes))]
    vertices = [vertex for vertex in base.vertices if vertex != hub]
    vertices.extend(replacements)

    raw_edges: list[tuple[str, str]] = [
        tuple(sorted(edge))
        for edge in base.edges
        if all(endpoint != hub for endpoint in edge)
    ]
    for replacement, edges in zip(replacements, assignment):
        for other, _ in edges:
            raw_edges.append(tuple(sorted((replacement, other))))

    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for edge in raw_edges:
        if edge not in seen:
            seen.add(edge)
            edges.append(edge)

    connector_paths: list[dict] = []
    replacement_degrees = [int(size) for size in part_sizes]
    for first, second in connector_pairs(connector_mode, len(replacements)):
        if (
            replacement_degrees[first] >= maximum_delta
            or replacement_degrees[second] >= maximum_delta
        ):
            continue
        connector = f"{prefix}_P{first:02d}{second:02d}"
        edges.append(tuple(sorted((replacements[first], connector))))
        edges.append(tuple(sorted((replacements[second], connector))))
        replacement_degrees[first] += 1
        replacement_degrees[second] += 1
        connector_paths.append(
            {
                "connector": connector,
                "replacement_endpoints": [
                    replacements[first],
                    replacements[second],
                ],
                "length": 2,
            }
        )
    vertices.extend(sorted(path["connector"] for path in connector_paths))

    sides = [list(side) for side in base.bipartition]
    sides[side_index] = [vertex for vertex in sides[side_index] if vertex != hub]
    sides[side_index].extend(replacements)

    operations = list(base.metadata.get("same_side_vertex_splits", []))
    operations.append(
        {
            "operation_index": operation_index,
            "replaced_vertex": hub,
            "replacement_side": "left" if side_index == 0 else "right",
            "replacement_count": len(replacements),
            "replacement_degrees": [int(size) for size in part_sizes],
            "incident_edges_distributed": len(incident),
            "duplicate_edges_collapsed": len(raw_edges) - len(edges),
            "replacement_vertices": replacements,
            "edge_distribution_pattern": pattern,
            "replacement_degrees_after_connectors": replacement_degrees,
            "connector_paths": connector_paths,
        }
    )
    metadata = {
        **base.metadata,
        "lane": "deeper-same-side-vertex-split-delta10-v2",
        "same_side_vertex_splits": operations,
    }
    try:
        graph = Graph(vertices, edges, [sides[0], sides[1]], metadata)
    except ValueError as exc:
        return None, f"invalid construction: {exc}"

    if graph.delta > maximum_delta:
        return None, "degree cap exceeded"
    if graph.n and min(graph.degrees.values()) < 2:
        return None, "minimum degree below two"
    return graph, "valid"


def validate_structure(graph: Graph, maximum_delta: int) -> tuple[bool, str]:
    if not nx.is_connected(nx.Graph(graph.edges)):
        return False, "disconnected"
    if graph.delta > maximum_delta:
        return False, "degree-cap"
    if min(graph.degrees.values()) < 2:
        return False, "low-degree"
    return True, "valid"


def overcap_vertices(graph: Graph, maximum_delta: int) -> list[str]:
    return sorted(
        (vertex for vertex, degree in graph.degrees.items() if degree > maximum_delta),
        key=lambda vertex: (-graph.degrees[vertex], vertex),
    )


def expand_root(
    root_name: str,
    root_graph: Graph,
    maximum_delta: int,
    construction_offset: int,
    state: RunState,
    counters: Counters,
    deadline: float,
    maximum_constructions: int = 100000,
) -> tuple[list[tuple[str, Graph]], bool]:
    """Compose split choices over every initial over-cap vertex."""
    current: list[tuple[str, Graph]] = [(root_name, root_graph)]
    local_seen: set[str] = set()
    hubs = overcap_vertices(root_graph, maximum_delta)

    for stage, hub in enumerate(hubs):
        following: list[tuple[str, Graph]] = []
        for previous_name, previous_graph in current:
            if time.monotonic() >= deadline - 5.0:
                state.enumeration_complete = False
                state.deadline_hit = True
                return following, False
            for part_sizes in split_options(previous_graph.degrees[hub]):
                for pattern in EDGE_PATTERNS:
                    for connector_mode in ("none", "chain", "bridge", "selected-pairs"):
                        if counters.constructions_attempted >= maximum_constructions:
                            state.enumeration_complete = False
                            return following, False
                        construction_id = (
                            construction_offset + counters.constructions_attempted
                        )
                        counters.constructions_attempted += 1
                        candidate, reason = apply_same_side_split(
                            previous_graph,
                            hub,
                            part_sizes,
                            construction_id,
                            stage,
                            maximum_delta,
                            pattern,
                            connector_mode,
                        )
                        if candidate is None:
                            if reason == "degree cap exceeded":
                                counters.rejected_degree_cap += 1
                            continue
                        valid, validation_reason = validate_structure(
                            candidate,
                            maximum_delta,
                        )
                        if not valid:
                            if validation_reason == "disconnected":
                                counters.rejected_disconnected += 1
                            elif validation_reason == "low-degree":
                                counters.rejected_low_degree += 1
                            elif validation_reason == "degree-cap":
                                counters.rejected_degree_cap += 1
                            continue

                        counters.generated += 1
                        digest = nauty_canonical_hash(candidate)
                        if digest in state.global_seen or digest in local_seen:
                            counters.duplicate += 1
                            continue
                        local_seen.add(digest)
                        state.global_seen.add(digest)
                        counters.unique += 1
                        candidate.metadata["parent_chain"] = [root_name, previous_name]
                        candidate.metadata["source_root"] = root_name
                        candidate.metadata["split_stage"] = stage + 1
                        candidate.metadata["construction_id"] = construction_id
                        following.append((previous_name, candidate))
        current = following
    return current, True


def confirm_negative(
    graph: Graph,
    time_limit: float,
    workers: int,
    deadline: float,
    span_checkpoint=None,
) -> tuple[bool, bool, dict[int, str]]:
    statuses: dict[int, str] = {}
    expected_spans = range(graph.delta, max(graph.delta, graph.n - 1) + 1)
    for span in expected_spans:
        remaining = deadline - time.monotonic()
        if remaining <= 0.25:
            statuses[span] = "UNKNOWN"
            return False, True, statuses
        limit = max(0.01, min(time_limit, remaining - 0.2))
        status_name, coloring = fixed_span_sat_solve(graph, span, limit, workers)
        statuses[span] = status_name
        if span_checkpoint is not None:
            span_checkpoint(dict(statuses))
        if status_name in ("OPTIMAL", "FEASIBLE"):
            if coloring is None:
                raise AssertionError("fixed-span solver returned no certificate")
            return False, False, statuses
    unresolved = any(status == "UNKNOWN" for status in statuses.values())
    confirmed = not unresolved and all(status == "INFEASIBLE" for status in statuses.values())
    return confirmed, unresolved, statuses


def classify_candidate(
    candidate_id: str,
    digest: str,
    graph: Graph,
    features: dict,
    parent_name: str,
    primary_time_limit: float,
    independent_time_limit: float,
    workers: int,
    deadline: float,
    state: RunState,
    counters: Counters,
    output_dir: Path,
    write_row_checkpoint=None,
) -> tuple[dict, dict | None]:
    remaining = deadline - time.monotonic()
    reserve = 12.0
    if remaining <= reserve:
        counters.unclassified_deadline += 1
        state.classification_complete = False
        state.deadline_hit = True
        return {
            "candidate_id": candidate_id,
            "parent": parent_name,
            "canonical_sha256": digest,
            "ranking": features,
            "decision": "not-attempted-deadline",
        }, None

    primary = rank_potential_solve(
        graph,
        max(0.01, min(primary_time_limit, 5.0, remaining - reserve)),
        workers,
    )
    row = {
        "candidate_id": candidate_id,
        "parent": parent_name,
        "canonical_sha256": digest,
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "split_operations": graph.metadata.get("same_side_vertex_splits", []),
        "ranking": features,
        "primary_result": {
            key: value for key, value in primary.__dict__.items() if key != "coloring"
        },
    }
    counters.classified += 1

    def checkpoint_row() -> None:
        if write_row_checkpoint is not None:
            write_row_checkpoint(row)

    if primary.status == "colorable":
        counters.colorable += 1
        row["decision"] = "colorable"
        checkpoint_row()
        return row, None

    if primary.status == "timeout":
        counters.timeout += 1
        row["decision"] = "timeout"
        row["independent_confirmation"] = {
            "required": False,
            "reason": "primary timeout remains unresolved",
        }
        checkpoint_row()
        return row, None

    if primary.status != "non-colorable":
        raise AssertionError(f"unexpected primary status {primary.status}")

    counters.primary_negative_candidates += 1
    row["decision"] = "pending-independent-confirmation"
    row["independent_confirmation"] = {
        "encoding": "fixed-span-cpsat",
        "spans_checked": [],
        "span_statuses": {},
        "confirmed_non_colorable": None,
        "unresolved": None,
    }
    checkpoint_row()

    def span_checkpoint(statuses: dict[int, str]) -> None:
        row["independent_confirmation"]["spans_checked"] = sorted(statuses)
        row["independent_confirmation"]["span_statuses"] = {
            str(span): status for span, status in sorted(statuses.items())
        }
        checkpoint_row()

    confirmed, unresolved, span_statuses = confirm_negative(
        graph,
        max(0.01, min(independent_time_limit, remaining - 1.0)),
        workers,
        deadline - 3.0,
        span_checkpoint,
    )
    row["independent_confirmation"] = {
        "encoding": "fixed-span-cpsat",
        "spans_checked": sorted(span_statuses),
        "span_statuses": {
            str(span): status for span, status in sorted(span_statuses.items())
        },
        "confirmed_non_colorable": confirmed,
        "unresolved": unresolved,
    }
    checkpoint_row()
    if unresolved:
        counters.independent_unresolved += 1
        counters.timeout += 1
        row["decision"] = "timeout"
        state.independent_confirmation_complete = False
        return row, None
    if not confirmed:
        raise AssertionError(
            f"independent encoding contradicted an exact primary negative: {digest}"
        )

    counters.non_colorable += 1
    counters.confirmed_non_colorable += 1
    row["decision"] = "non-colorable"
    graph.metadata["candidate_id"] = candidate_id
    graph.metadata["certification"] = {
        "primary": "rank-potential-cpsat-infeasible",
        "independent": "fixed-span-cpsat-infeasible-all-legal-spans",
        "spans_checked": sorted(span_statuses),
    }
    graph_path = output_dir / f"{candidate_id}.graph.json"
    graph.save(graph_path)
    event = {
        "event": "certified_non_colorable",
        "candidate_id": candidate_id,
        "path": str(graph_path),
        "canonical_sha256": digest,
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
    }
    row["graph_path"] = str(graph_path)
    print(json.dumps(event, sort_keys=True), flush=True)
    return row, event


def search_parent(
    parent_name: str,
    graph: Graph,
    maximum_delta: int,
    candidate_cap: int,
    primary_time_limit: float,
    independent_time_limit: float,
    workers: int,
    deadline: float,
    construction_offset: int,
    output_dir: Path,
    state: RunState,
    output_path: Path,
    configuration: dict,
    seed_resolution: dict,
    run_started: float,
    deadline_seconds: float,
    maximum_constructions: int = 100000,
    completed_summaries: Sequence[dict] | None = None,
) -> tuple[dict, list[dict], list[dict]]:
    started = time.monotonic()
    counters = Counters()
    rows: list[dict] = []
    negative_events: list[dict] = []
    prior_summaries = list(completed_summaries or [])

    expanded, enumeration_finished = expand_root(
        parent_name,
        graph,
        maximum_delta,
        construction_offset,
        state,
        counters,
        deadline,
        maximum_constructions,
    )
    candidates: list[tuple[str, str, Graph, dict]] = []
    for previous_name, candidate in expanded:
        candidates.append(
            (
                nauty_canonical_hash(candidate),
                previous_name,
                candidate,
                ranking_features(candidate),
            )
        )
    candidates.sort(
        key=lambda item: (
            -item[3]["sufficient_obstruction_count"],
            -item[3]["hub_best_margin"],
            -item[3]["hub_margin_sum_top3"],
            -int(item[3]["hub_best_margin_tier_at_least_minus_1_5"]),
            item[3]["normalized_degree_variance"],
            item[0],
        )
    )

    selected = candidates if candidate_cap <= 0 else candidates[:candidate_cap]

    def row_checkpoint(live_row: dict) -> None:
        nonlocal negative_events
        write_checkpoint(
            output_path,
            configuration,
            prior_summaries,
            [*rows, live_row],
            negative_events,
            time.monotonic() - run_started,
            deadline_seconds,
            state,
            seed_resolution,
        )

    for number, (digest, previous_name, candidate, features) in enumerate(
        selected,
        start=1,
    ):
        candidate_id = f"VSD10V2-{number:04d}-{parent_name}"
        row, event = classify_candidate(
            candidate_id,
            digest,
            candidate,
            features,
            previous_name,
            primary_time_limit,
            independent_time_limit,
            workers,
            deadline,
            state,
            counters,
            output_dir,
            row_checkpoint,
        )
        rows.append(row)
        if event is not None:
            negative_events.append(event)

    classification_complete = (
        enumeration_finished
        and counters.unique == counters.classified
        and counters.unclassified_deadline == 0
    )
    summary = {
        "parent": parent_name,
        "parent_order": graph.n,
        "parent_size": graph.m,
        "parent_delta": graph.delta,
        "overcap_vertices": {
            vertex: graph.degrees[vertex]
            for vertex in overcap_vertices(graph, maximum_delta)
        },
        "constructions_attempted": counters.constructions_attempted,
        "generated": counters.generated,
        "unique": counters.unique,
        "classified": counters.classified,
        "colorable": counters.colorable,
        "non-colorable": counters.non_colorable,
        "timeout": counters.timeout,
        "confirmed_non_colorable": counters.confirmed_non_colorable,
        "primary_negative_candidates": counters.primary_negative_candidates,
        "independent_unresolved": counters.independent_unresolved,
        "duplicate": counters.duplicate,
        "rejected_disconnected": counters.rejected_disconnected,
        "rejected_low_degree": counters.rejected_low_degree,
        "rejected_degree_cap": counters.rejected_degree_cap,
        "unclassified_deadline": counters.unclassified_deadline,
        "unique_candidates": counters.unique,
        "selected_for_classification": len(selected),
        "candidate_cap_applied": (
            candidate_cap > 0 and counters.unique > len(selected)
        ),
        "enumeration_complete": enumeration_finished,
        "classification_complete": classification_complete,
        "independent_confirmation_complete": counters.independent_unresolved == 0,
        "complete": classification_complete and counters.independent_unresolved == 0,
        "elapsed_seconds": time.monotonic() - started,
    }
    return summary, rows, negative_events


def totals_from_summaries(summaries: Iterable[dict]) -> dict:
    keys = (
        "constructions_attempted",
        "generated",
        "unique",
        "classified",
        "colorable",
        "non-colorable",
        "timeout",
        "confirmed_non_colorable",
        "primary_negative_candidates",
        "independent_unresolved",
        "duplicate",
        "rejected_disconnected",
        "rejected_low_degree",
        "rejected_degree_cap",
        "unclassified_deadline",
    )
    return {key: sum(int(item.get(key, 0)) for item in summaries) for key in keys}


def make_report(
    configuration: dict,
    summaries: Sequence[dict],
    records: Sequence[dict],
    negative_events: Sequence[dict],
    elapsed_seconds: float,
    deadline_seconds: float,
    state: RunState,
    seed_resolution: dict,
) -> dict:
    totals = totals_from_summaries(summaries)
    counts = {
        "generated": totals["generated"],
        "unique": totals["unique"],
        "classified": totals["classified"],
        "colorable": totals["colorable"],
        "non-colorable": totals["confirmed_non_colorable"],
        "timeout": totals["timeout"],
        "confirmed_non_colorable": totals["confirmed_non_colorable"],
        "independent_unresolved": totals["independent_unresolved"],
    }
    completion = {
        "generation_complete": state.enumeration_complete,
        "all_unique_classified": counts["unique"] == counts["classified"],
        "classification_complete": state.classification_complete,
        "independent_confirmation_complete": state.independent_confirmation_complete,
        "runtime_deadline_hit": state.deadline_hit,
        "complete": (
            state.enumeration_complete
            and state.classification_complete
            and state.independent_confirmation_complete
            and counts["unique"] == counts["classified"]
            and not state.deadline_hit
        ),
    }
    return {
        "schema_version": 1,
        "configuration": configuration,
        "seed_resolution": seed_resolution,
        "operation_definition": {
            "replacement_rule": (
                "delete one over-cap vertex v; insert at least two new vertices in "
                "v's own bipartition side; distribute all former incident edges among "
                "the replacements so each has degree at least two"
            ),
            "invariants": [
                "replacement vertices remain in v's bipartition side",
                "simplicity is enforced and duplicate edges are collapsed",
                "optional replacement-to-replacement paths have length two through a new opposite-side vertex",
                "connectivity is checked after every split",
                "final maximum degree is at most 10",
                "final minimum degree is at least 2",
            ],
            "search_family": (
                "simultaneous composition over all initial over-cap vertices; two-way, "
                "three-way, and selected four-way degree partitions; five deterministic "
                "edge distributions; optional none, chain, bridge, or selected length-two "
                "same-side connecting paths"
            ),
            "deduplication": (
                "Graph.to_json field sha256_bipartition_canonical from "
                "nauty_canonical_hash"
            ),
            "ranking_key": (
                "sufficient weighted-hub obstruction count, hub_best_margin, top-three "
                "margin sum, minus-1.5 tier, normalized degree variance, then canonical hash"
            ),
            "classification": (
                "rank_potential_solve CP-SAT, maximum configured value capped at 5 seconds"
            ),
            "negative_confirmation": (
                "fixed_span_sat_solve independently over every legal span from delta "
                "through n-1; only all-INFEASIBLE evidence counts as non-colorable"
            ),
            "timeout_policy": (
                "a primary or independent UNKNOWN remains unresolved and is counted "
                "as timeout, never as non-colorable"
            ),
        },
        "deadline_seconds": deadline_seconds,
        "elapsed_seconds": elapsed_seconds,
        "counts": counts,
        "completion": completion,
        "complete": completion["complete"],
        "negative_events": list(negative_events),
        "summaries": list(summaries),
        "records": list(records),
    }


def write_checkpoint(
    path: Path,
    configuration: dict,
    completed_summaries: Sequence[dict],
    records: Sequence[dict],
    negatives: Sequence[dict],
    elapsed: float,
    deadline_seconds: float,
    state: RunState,
    seed_resolution: dict,
) -> None:
    report = make_report(
        configuration,
        completed_summaries,
        records,
        negatives,
        elapsed,
        deadline_seconds,
        state,
        seed_resolution,
    )
    report["checkpoint"] = {"partial": True, "written_unix_time": time.time()}
    atomic_write_json(path, report)


def resolve_quotients(requested_dir: Path) -> tuple[list[tuple[str, Graph]], dict]:
    fallback_dir = Path("results/graphs/quotient-r1")
    active_dir = requested_dir if requested_dir.exists() else fallback_dir
    graphs: list[tuple[str, Graph]] = []
    for filename in ("Q1-00012.graph.json", "Q1-00014.graph.json"):
        path = active_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"missing quotient seed: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        metadata = dict(data.get("metadata") or {})
        metadata["source_path"] = str(path)
        graphs.append((filename.removesuffix(".graph.json"), Graph.from_json(data)))
    return graphs, {
        "requested_directory": str(requested_dir),
        "requested_directory_exists": requested_dir.exists(),
        "resolved_directory": str(active_dir),
        "fallback_used": not requested_dir.exists(),
    }


def reconstructed_benchmarks() -> list[tuple[str, Graph]]:
    result: list[tuple[str, Graph]] = []
    for name, benchmark in benchmark_graphs().items():
        metadata = dict(benchmark.metadata)
        metadata["parent_kind"] = "reconstructed_delta_at_least_11_benchmark"
        result.append(
            (
                name,
                Graph(
                    benchmark.vertices,
                    benchmark.edges,
                    benchmark.bipartition,
                    metadata,
                ),
            )
        )
    return result


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quotient-dir", type=Path, default=Path("graphs/Q1/quotient-r1"))
    parser.add_argument(
        "--output", type=Path, default=Path("results/vertex-split-delta10-v2.json")
    )
    parser.add_argument("--maximum-final-delta", type=int, default=10)
    parser.add_argument("--candidate-cap-per-parent", type=int, default=180)
    parser.add_argument(
        "--maximum-constructions-per-parent",
        type=int,
        default=100000,
    )
    parser.add_argument("--primary-time-limit", type=float, default=5.0)
    parser.add_argument("--independent-time-limit", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--deadline-seconds", type=float, default=10800.0)
    parser.add_argument("--smoke", action="store_true")
    return parser


def main() -> None:
    args = argument_parser().parse_args()
    if args.maximum_final_delta != 10:
        raise SystemExit("this deeper lane requires --maximum-final-delta 10")
    if not 1 <= args.workers <= 4:
        raise SystemExit("workers must be between 1 and 4")
    if not 0 < args.primary_time_limit <= 5.0:
        raise SystemExit("primary time limit must be positive and at most 5 seconds")
    if not 0 < args.independent_time_limit <= 5.0:
        raise SystemExit(
            "independent time limit must be positive and at most 5 seconds"
        )
    if args.deadline_seconds <= 0 or args.deadline_seconds > 10800.0:
        raise SystemExit("deadline must be positive and at most 3 hours")

    if args.smoke:
        args.output = Path("results/vertex-split-delta10-v2-smoke.json")
        args.candidate_cap_per_parent = min(args.candidate_cap_per_parent, 2)
        args.maximum_constructions_per_parent = min(
            args.maximum_constructions_per_parent,
            10000,
        )
        args.primary_time_limit = min(args.primary_time_limit, 0.25)
        args.independent_time_limit = min(args.independent_time_limit, 0.25)
        args.deadline_seconds = min(args.deadline_seconds, 120.0)

    run_started_wall = time.time()
    run_started = time.monotonic()
    deadline = run_started + args.deadline_seconds
    output_dir = args.output.parent / "graphs" / "vertex-split-delta10-v2"
    output_dir.mkdir(parents=True, exist_ok=True)

    seeds, seed_resolution = resolve_quotients(args.quotient_dir)
    benchmarks = reconstructed_benchmarks()
    parents = [*seeds, *sorted(benchmarks, key=lambda item: item[0])]
    if args.smoke:
        parents = parents[:1]

    state = RunState()
    summaries: list[dict] = []
    all_records: list[dict] = []
    all_negatives: list[dict] = []
    construction_offset = 0
    stopped_before_next_parent = False

    configuration = {
        "starting_graphs": [name for name, _ in parents],
        "known_quotient_seeds": [name for name, _ in seeds],
        "benchmarks_from_benchmark_graphs": [name for name, _ in benchmarks],
        "maximum_final_delta": args.maximum_final_delta,
        "minimum_final_degree": 2,
        "require_connected_simple_bipartite": True,
        "candidate_cap_unique_per_parent": args.candidate_cap_per_parent,
        "solver_workers": args.workers,
        "primary_time_limit_seconds": args.primary_time_limit,
        "independent_time_limit_seconds": args.independent_time_limit,
        "runtime_hard_deadline_seconds": args.deadline_seconds,
        "edge_distribution_patterns": list(EDGE_PATTERNS),
        "connector_modes": ["none", "chain", "bridge", "selected-pairs"],
        "maximum_constructions_per_parent": args.maximum_constructions_per_parent,
        "atomic_checkpoint_after_every_solve": True,
        "smoke_run": args.smoke,
    }

    for name, graph in parents:
        if deadline - time.monotonic() <= 15.0:
            state.deadline_hit = True
            state.enumeration_complete = False
            state.classification_complete = False
            stopped_before_next_parent = True
            break
        print(
            json.dumps({"event": "parent_start", "parent": name}, sort_keys=True),
            flush=True,
        )
        summary, records, negatives = search_parent(
            name,
            graph,
            args.maximum_final_delta,
            args.candidate_cap_per_parent,
            args.primary_time_limit,
            args.independent_time_limit,
            args.workers,
            deadline,
            construction_offset,
            output_dir,
            state,
            args.output,
            configuration,
            seed_resolution,
            run_started,
            args.deadline_seconds,
            args.maximum_constructions_per_parent,
            summaries,
        )
        summaries.append(summary)
        all_records.extend(records)
        all_negatives.extend(negatives)
        construction_offset += summary["constructions_attempted"]
        write_checkpoint(
            args.output,
            configuration,
            summaries,
            all_records,
            all_negatives,
            time.monotonic() - run_started,
            args.deadline_seconds,
            state,
            seed_resolution,
        )
        compact = {
            key: value for key, value in summary.items() if key != "overcap_vertices"
        }
        print(
            json.dumps({"event": "parent_complete", **compact}, sort_keys=True),
            flush=True,
        )

    report = make_report(
        configuration,
        summaries,
        all_records,
        all_negatives,
        time.monotonic() - run_started,
        args.deadline_seconds,
        state,
        seed_resolution,
    )
    report["environment"] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "started_unix_time": run_started_wall,
        "finished_unix_time": time.time(),
    }
    report["runtime_deadline_stopped_before_next_parent"] = stopped_before_next_parent
    atomic_write_json(args.output, report)
    print(
        json.dumps(
            {
                "event": "run_complete",
                "output": str(args.output),
                "complete": report["complete"],
                "counts": report["counts"],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
