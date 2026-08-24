#!/usr/bin/env python3
"""Synchronized two-way hub splitting bounded at final maximum degree 10.

Each initial over-cap hub is replaced by two same-side copies.  The copies are
then coupled by one small bipartite synchronization gadget and optional
deterministic anchored four-cycles.  The catalog deliberately includes equal
and unequal path lengths, shared cores, separate links, and cross-rungs.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import platform
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


MAXIMUM_DELTA = 10
MINIMUM_DEGREE = 2
MAX_RUNTIME_SECONDS = 10800.0
MAX_SOLVER_SECONDS = 5.0
MAX_SOLVER_WORKERS = 4
QUOTIENT_SEEDS = ("Q1-00012", "Q1-00014")
EDGE_PATTERNS = (
    "contiguous-insertion",
    "contiguous-degree-name",
    "round-robin-insertion",
    "round-robin-degree-name",
)
THETA_PATH_LENGTHS = ((2, 2), (2, 4), (2, 6), (4, 4), (4, 6), (6, 6))
SHORT_SHARED_RUNGS = (4, 6)
CROSS_RUNG_PATHS = ((4, 4), (4, 6), (6, 6))
ATTACHMENT_MODES = ("none", "first-cycle", "second-cycle", "both-cycles")


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
    enumeration_complete: bool = True
    classification_complete: bool = True
    independent_confirmation_complete: bool = True
    runtime_deadline_hit: bool = False
    stop_reason: str | None = None


@dataclass(frozen=True)
class Candidate:
    construction_id: int
    parent_chain: tuple[str, ...]
    graph: Graph
    digest: str
    features: dict


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
        "weighted_hubs_best": statistics[:4],
    }


def assign_two_way(
    incident: Sequence[tuple[str, tuple[str, str]]],
    first_size: int,
    pattern: str,
) -> tuple[list[tuple[str, tuple[str, str]]], list[tuple[str, tuple[str, str]]]]:
    rows = list(incident)
    if pattern.endswith("degree-name"):
        rows.sort(key=lambda item: (-graph_neighbor_degree(item), item[0], item[1]))
    if pattern.startswith("contiguous"):
        return rows[:first_size], rows[first_size:]
    if not pattern.startswith("round-robin"):
        raise ValueError(f"unknown edge pattern: {pattern}")
    output: list[list[tuple[str, tuple[str, str]]]] = [[], []]
    turn = 0
    remaining = [first_size, len(rows) - first_size]
    for row in rows:
        while remaining[turn] == 0:
            turn = (turn + 1) % 2
        output[turn].append(row)
        remaining[turn] -= 1
        turn = (turn + 1) % 2
    return output[0], output[1]


_NEIGHBOR_DEGREE_CONTEXT: dict[str, int] = {}


def graph_neighbor_degree(item: tuple[str, tuple[str, str]]) -> int:
    neighbor, _ = item
    return _NEIGHBOR_DEGREE_CONTEXT.get(neighbor, 0)


def even_path(
    prefix: str,
    gadget_index: int,
    path_index: int,
    first: str,
    second: str,
    length: int,
    first_side_index: int,
) -> tuple[list[str], list[tuple[str, str]]]:
    if length < 2 or length % 2:
        raise ValueError("same-side synchronization paths must have positive even length")
    internal = []
    for position in range(1, length):
        side = (position % 2 + first_side_index) % 2
        side_name = "L" if side == 0 else "R"
        internal.append(
            f"{prefix}_G{gadget_index:02d}P{path_index:02d}"
            f"V{position:02d}{side_name}"
        )
    chain = [first, *internal, second]
    edges = list(zip(chain, chain[1:]))
    return internal, [tuple(sorted(edge)) for edge in edges]


def add_edge(edges: set[tuple[str, str]], first: str, second: str) -> bool:
    edge = tuple(sorted((first, second)))
    if edge in edges:
        return False
    edges.add(edge)
    return True


def build_gadget(
    kind: str,
    prefix: str,
    gadget_index: int,
    copies: Sequence[str],
    side_index: int,
) -> tuple[list[str], set[tuple[str, str]], dict]:
    first, second = copies
    opposite_name = "R" if side_index == 0 else "L"
    same_name = "L" if side_index == 0 else "R"
    vertices: list[str] = []
    edges: set[tuple[str, str]] = set()
    details: dict = {"kind": kind, "gadget_index": gadget_index}

    if kind.startswith("theta-"):
        left_text, right_text = kind.removeprefix("theta-").split("-", 1)
        if right_text == "short-shared-rungs":
            lengths = (2, int(left_text))
            details["path_lengths"] = list(lengths)
            details["shared_intermediates"] = ["short-path relay"]
            details["deterministic_attachments"] = ["rungs from long-path same-side vertices"]
            paths = []
            for path_index, length in enumerate(lengths):
                internal, path_edges = even_path(
                    prefix,
                    gadget_index,
                    path_index,
                    first,
                    second,
                    length,
                    side_index,
                )
                vertices.extend(internal)
                edges.update(path_edges)
                paths.append(internal)
            short_relay = paths[0][0]
            long_path = paths[1]
            for position, vertex in enumerate(long_path, start=1):
                if position % 2 == 0 and position < lengths[1]:
                    if not add_edge(edges, short_relay, vertex):
                        raise ValueError("non-simple deterministic attachment")
        elif right_text.endswith("-cross-rungs"):
            lengths = (int(left_text), int(right_text.removesuffix("-cross-rungs")))
            details["path_lengths"] = list(lengths)
            details["shared_intermediates"] = []
            details["deterministic_attachments"] = ["paired cross-rungs"]
            paths = []
            for path_index, length in enumerate(lengths):
                internal, path_edges = even_path(
                    prefix,
                    gadget_index,
                    path_index,
                    first,
                    second,
                    length,
                    side_index,
                )
                vertices.extend(internal)
                edges.update(path_edges)
                paths.append(internal)
            same_side_first = [
                vertex
                for position, vertex in enumerate(paths[0], start=1)
                if position % 2 == 0
            ]
            opposite_side_second = [
                vertex
                for position, vertex in enumerate(paths[1], start=1)
                if position % 2 == 1
            ]
            for left_vertex, right_vertex in zip(same_side_first, opposite_side_second):
                if not add_edge(edges, left_vertex, right_vertex):
                    raise ValueError("non-simple cross-rung")
        else:
            lengths = tuple(map(int, kind.removeprefix("theta-").split("-")))
            details["path_lengths"] = list(lengths)
            details["shared_intermediates"] = []
            details["deterministic_attachments"] = []
            for path_index, length in enumerate(lengths):
                internal, path_edges = even_path(
                    prefix,
                    gadget_index,
                    path_index,
                    first,
                    second,
                    length,
                    side_index,
                )
                vertices.extend(internal)
                edges.update(path_edges)
        return vertices, edges, details

    if kind == "separate-three-links":
        count = 3
        details.update(
            path_lengths=[2, 2, 2],
            shared_intermediates=[],
            deterministic_attachments=[],
        )
        for path_index in range(count):
            internal, path_edges = even_path(
                prefix,
                gadget_index,
                path_index,
                first,
                second,
                2,
                side_index,
            )
            vertices.extend(internal)
            edges.update(path_edges)
        return vertices, edges, details

    if kind in ("shared-core-K3-2", "shared-core-K3-3", "shared-core-K2-3"):
        if kind == "shared-core-K3-2":
            opposite_count = 2
            include_same_relay = True
        elif kind == "shared-core-K3-3":
            opposite_count = 3
            include_same_relay = True
        else:
            opposite_count = 3
            include_same_relay = False
        relays = []
        if include_same_relay:
            relay = f"{prefix}_G{gadget_index:02d}SR{same_name}"
            relays.append(relay)
            vertices.append(relay)
        shared = []
        for number in range(opposite_count):
            vertex = (
                f"{prefix}_G{gadget_index:02d}SI{number:02d}{opposite_name}"
            )
            shared.append(vertex)
            vertices.append(vertex)
        left = [*copies, *relays]
        for left_vertex in left:
            for right_vertex in shared:
                add_edge(edges, left_vertex, right_vertex)
        details.update(
            path_lengths=None,
            shared_intermediates=shared,
            deterministic_attachments=(
                ["one extra same-side relay joined to every shared intermediate"]
                if include_same_relay
                else []
            ),
        )
        return vertices, edges, details

    raise ValueError(f"unknown synchronization gadget: {kind}")


def endpoint_increment(kind: str) -> tuple[int, int]:
    if kind.startswith("theta-"):
        text = kind.removeprefix("theta-")
        if text.endswith("-short-shared-rungs") or text.endswith("-cross-rungs"):
            return 2, 2
        lengths = tuple(map(int, text.split("-")))
        return len(lengths), len(lengths)
    if kind == "separate-three-links":
        return 3, 3
    if kind in ("shared-core-K3-2", "shared-core-K2-3"):
        return 2, 2
    if kind == "shared-core-K3-3":
        return 3, 3
    raise ValueError(f"unknown gadget: {kind}")


def attachment_increment(mode: str) -> tuple[int, int]:
    values = {"none": (0, 0), "first-cycle": (2, 0), "second-cycle": (0, 2)}
    values["both-cycles"] = (2, 2)
    return values[mode]


def add_anchored_cycle(
    vertices: list[str],
    edges: set[tuple[str, str]],
    prefix: str,
    copy_number: int,
    endpoint: str,
    side_index: int,
) -> None:
    opposite_name = "R" if side_index == 0 else "L"
    same_name = "L" if side_index == 0 else "R"
    near = f"{prefix}_A{copy_number:02d}N{opposite_name}"
    anchor = f"{prefix}_A{copy_number:02d}B{same_name}"
    far = f"{prefix}_A{copy_number:02d}F{opposite_name}"
    vertices.extend((near, anchor, far))
    for first, second in (
        (endpoint, near),
        (near, anchor),
        (anchor, far),
        (far, endpoint),
    ):
        if not add_edge(edges, first, second):
            raise ValueError("anchored cycle is not simple")


def split_options(degree: int) -> list[tuple[int, int]]:
    options: set[tuple[int, int]] = set()
    for lower in range(MINIMUM_DEGREE, degree // 2 + 1):
        upper = degree - lower
        if upper >= MINIMUM_DEGREE:
            options.add((upper, lower))
    # Prefer balanced copies first.  This is also the useful prefix under the
    # smoke and deadline construction limits.
    return sorted(options, key=lambda item: (abs(item[0] - item[1]), -min(item), item))


def validate_graph(graph: Graph) -> tuple[bool, str]:
    if graph.n and not nx.is_connected(nx.Graph(graph.edges)):
        return False, "disconnected"
    if graph.delta > MAXIMUM_DELTA:
        return False, "degree-cap"
    if min(graph.degrees.values(), default=0) < MINIMUM_DEGREE:
        return False, "low-degree"
    return True, "valid"


def construct_candidate(
    base: Graph,
    hub: str,
    part_sizes: tuple[int, int],
    pattern: str,
    gadget_kind: str,
    attachment_mode: str,
    construction_id: int,
    stage: int,
) -> tuple[Graph | None, str]:
    incident = [
        (next(endpoint for endpoint in edge if endpoint != hub), edge)
        for edge in base.edges
        if hub in edge
    ]
    if sum(part_sizes) != len(incident):
        return None, "partition-size-mismatch"

    _NEIGHBOR_DEGREE_CONTEXT.clear()
    for neighbor, _ in incident:
        _NEIGHBOR_DEGREE_CONTEXT[neighbor] = base.degrees[neighbor]
    assignments = assign_two_way(incident, part_sizes[0], pattern)

    side_index = 0 if hub in base.bipartition[0] else 1
    prefix = f"MHS{construction_id:06d}S{stage:02d}"
    copies = [f"{prefix}_C00", f"{prefix}_C01"]
    vertices = [vertex for vertex in base.vertices if vertex != hub]
    vertices.extend(copies)
    raw_edges: set[tuple[str, str]] = {
        tuple(sorted(edge))
        for edge in base.edges
        if all(endpoint != hub for endpoint in edge)
    }
    for copy, assignment in zip(copies, assignments):
        for neighbor, _ in assignment:
            add_edge(raw_edges, copy, neighbor)

    gadget_vertices, gadget_edges, gadget_details = build_gadget(
        gadget_kind,
        prefix,
        stage,
        copies,
        side_index,
    )
    vertices.extend(gadget_vertices)
    all_edges = set(raw_edges) | gadget_edges

    attachment_records: list[dict] = []
    for copy_number, (copy, mode_value) in enumerate(zip(copies, attachment_increment(attachment_mode))):
        if mode_value == 2:
            add_anchored_cycle(
                vertices,
                all_edges,
                prefix,
                copy_number,
                copy,
                side_index,
            )
            attachment_records.append({"copy": copy, "kind": "anchored-C4"})

    sides = [list(side) for side in base.bipartition]
    sides[side_index].remove(hub)
    sides[side_index].extend(copies)
    for vertex in vertices:
        if vertex in sides[0] or vertex in sides[1]:
            continue
        if vertex.endswith("L"):
            sides[0].append(vertex)
        elif vertex.endswith("R"):
            sides[1].append(vertex)
        else:
            return None, f"missing-side-marker: {vertex}"

    operation = {
        "operation_index": stage,
        "replaced_vertex": hub,
        "replacement_side": "left" if side_index == 0 else "right",
        "replacement_count": 2,
        "replacement_degrees_from_spokes": list(part_sizes),
        "synchronization_gadget": gadget_details,
        "endpoint_increments_from_gadget": list(endpoint_increment(gadget_kind)),
        "attachment_mode": attachment_mode,
        "attached_cycles": attachment_records,
        "edge_distribution_pattern": pattern,
    }
    prior_operations = list(base.metadata.get("hub_synchronizations", []))
    metadata = {
        **base.metadata,
        "lane": "synchronized-multi-hub-split-delta10",
        "hub_synchronizations": [*prior_operations, operation],
    }
    try:
        graph = Graph(vertices, sorted(all_edges), [sides[0], sides[1]], metadata)
    except ValueError as exc:
        return None, f"invalid-construction: {exc}"
    if graph.delta > MAXIMUM_DELTA:
        return None, "degree cap exceeded"
    return graph, "valid"


def overcap_vertices(graph: Graph) -> list[str]:
    return sorted(
        (vertex for vertex, degree in graph.degrees.items() if degree > MAXIMUM_DELTA),
        key=lambda vertex: (-graph.degrees[vertex], vertex),
    )


def expand_root(
    root_name: str,
    root_graph: Graph,
    state: RunState,
    counters: Counters,
    deadline: float,
    global_seen: set[str],
    construction_offset: int,
    maximum_constructions: int,
) -> list[Candidate]:
    current: list[tuple[tuple[str, ...], Graph]] = [((root_name,), root_graph)]
    local_seen: set[str] = set()
    candidates: list[Candidate] = []
    hubs = overcap_vertices(root_graph)
    for stage, hub in enumerate(hubs):
        following: list[tuple[tuple[str, ...], Graph]] = []
        for parent_chain, previous_graph in current:
            for part_sizes in split_options(previous_graph.degrees[hub]):
                for pattern in EDGE_PATTERNS:
                    gadget_kinds = [
                        "theta-2-2",
                        "shared-core-K3-2",
                        "theta-2-4",
                        "theta-4-short-shared-rungs",
                        "theta-4-4",
                        "theta-4-4-cross-rungs",
                        "theta-2-6",
                        "theta-6-short-shared-rungs",
                        "theta-4-6",
                        "theta-4-6-cross-rungs",
                        "separate-three-links",
                        "shared-core-K3-3",
                        "shared-core-K2-3",
                        "theta-6-6",
                        "theta-6-6-cross-rungs",
                    ]
                    for gadget_kind in gadget_kinds:
                        for attachment_mode in ATTACHMENT_MODES:
                            if time.monotonic() >= deadline - 2.0:
                                state.enumeration_complete = False
                                state.runtime_deadline_hit = True
                                state.stop_reason = "generation-deadline"
                                return candidates
                            if counters.constructions_attempted >= maximum_constructions:
                                state.enumeration_complete = False
                                state.stop_reason = "construction-limit"
                                return candidates
                            counters.constructions_attempted += 1
                            construction_id = construction_offset + counters.constructions_attempted
                            candidate_graph, reason = construct_candidate(
                                previous_graph,
                                hub,
                                part_sizes,
                                pattern,
                                gadget_kind,
                                attachment_mode,
                                construction_id,
                                stage,
                            )
                            if candidate_graph is None:
                                if reason == "degree cap exceeded":
                                    counters.rejected_degree_cap += 1
                                else:
                                    counters.rejected_invalid += 1
                                continue
                            valid, validation_reason = validate_graph(candidate_graph)
                            if not valid:
                                if validation_reason == "disconnected":
                                    counters.rejected_disconnected += 1
                                elif validation_reason == "low-degree":
                                    counters.rejected_low_degree += 1
                                elif validation_reason == "degree-cap":
                                    counters.rejected_degree_cap += 1
                                else:
                                    counters.rejected_invalid += 1
                                continue
                            counters.generated += 1
                            digest = nauty_canonical_hash(candidate_graph)
                            if digest in global_seen or digest in local_seen:
                                counters.duplicate += 1
                                continue
                            local_seen.add(digest)
                            global_seen.add(digest)
                            counters.unique += 1
                            candidate_graph.metadata["source_root"] = root_name
                            candidate_graph.metadata["parent_chain"] = list(parent_chain)
                            candidate_graph.metadata["construction_id"] = construction_id
                            candidates.append(
                                Candidate(
                                    construction_id=construction_id,
                                    parent_chain=parent_chain,
                                    graph=candidate_graph,
                                    digest=digest,
                                    features=ranking_features(candidate_graph),
                                )
                            )
                            following.append(((*parent_chain, hub), candidate_graph))
        current = following
    return candidates


def confirm_negative(
    graph: Graph,
    time_limit: float,
    workers: int,
    deadline: float,
    span_checkpoint=None,
) -> tuple[bool, bool, dict[int, str]]:
    statuses: dict[int, str] = {}
    legal_spans = range(graph.delta, max(graph.delta, graph.n - 1) + 1)
    for span in legal_spans:
        remaining = deadline - time.monotonic()
        if remaining <= 0.25:
            statuses[span] = "UNKNOWN"
            return False, True, statuses
        effective_limit = max(0.01, min(time_limit, MAX_SOLVER_SECONDS, remaining - 0.2))
        status_name, coloring = fixed_span_sat_solve(graph, span, effective_limit, workers)
        statuses[span] = status_name
        if span_checkpoint is not None:
            span_checkpoint(dict(statuses))
        if status_name in ("OPTIMAL", "FEASIBLE"):
            if coloring is None:
                raise AssertionError("fixed-span solver returned no coloring")
            return False, False, statuses
    unresolved = any(status == "UNKNOWN" for status in statuses.values())
    confirmed = not unresolved and all(status == "INFEASIBLE" for status in statuses.values())
    return confirmed, unresolved, statuses


def classify_candidate(
    number: int,
    candidate: Candidate,
    primary_time_limit: float,
    independent_time_limit: float,
    workers: int,
    deadline: float,
    counters: Counters,
    state: RunState,
    solve_checkpoint=None,
) -> tuple[dict, dict | None]:
    remaining = deadline - time.monotonic()
    reserve = 15.0
    if remaining <= reserve:
        counters.unclassified_deadline += 1
        state.classification_complete = False
        state.runtime_deadline_hit = True
        state.stop_reason = "classification-deadline"
        row = {
            "candidate_id": f"MHSD10-{number:04d}",
            "parent_chain": list(candidate.parent_chain),
            "canonical_sha256": candidate.digest,
            "decision": "not-attempted-deadline",
        }
        return row, None

    graph = candidate.graph
    primary = rank_potential_solve(
        graph,
        max(0.01, min(primary_time_limit, MAX_SOLVER_SECONDS, remaining - reserve)),
        workers,
    )
    row = {
        "candidate_id": f"MHSD10-{number:04d}",
        "parent_chain": list(candidate.parent_chain),
        "canonical_sha256": candidate.digest,
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "hub_synchronizations": graph.metadata.get("hub_synchronizations", []),
        "ranking": candidate.features,
        "primary_result": {
            key: value for key, value in primary.__dict__.items() if key != "coloring"
        },
    }
    counters.classified += 1
    if solve_checkpoint is not None:
        solve_checkpoint(row)

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
        raise AssertionError(f"unexpected primary solver status: {primary.status}")

    counters.primary_negative_candidates += 1
    row["decision"] = "pending-independent-confirmation"
    row["independent_confirmation"] = {
        "encoding": "fixed-span-cpsat",
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
        if solve_checkpoint is not None:
            solve_checkpoint(row)

    confirmed, unresolved, span_statuses = confirm_negative(
        graph,
        max(0.01, min(independent_time_limit, remaining - 1.0)),
        workers,
        deadline - 3.0,
        span_checkpoint,
    )
    row["independent_confirmation"].update(
        {
            "spans_checked": sorted(span_statuses),
            "span_statuses": {
                str(span): status for span, status in sorted(span_statuses.items())
            },
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
            "independent encoding contradicted a primary negative: "
            f"{candidate.digest}"
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
    negative_record = {
        **row,
        "graph": graph.to_json(),
    }
    event = {
        "event": "confirmed_non_colorable",
        "candidate_id": row["candidate_id"],
        "canonical_sha256": candidate.digest,
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
    }
    print(json.dumps(event, sort_keys=True), flush=True)
    return row, negative_record


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
        "rejected_invalid",
        "unclassified_deadline",
    )
    return {key: sum(int(item.get(key, 0)) for item in summaries) for key in keys}


def make_report(
    configuration: dict,
    roots: Sequence[tuple[str, Graph]],
    summaries: Sequence[dict],
    records: Sequence[dict],
    confirmed_negatives: Sequence[dict],
    elapsed_seconds: float,
    state: RunState,
) -> dict:
    totals = totals_from_summaries(summaries)
    counts = {
        "generated": totals["generated"],
        "unique": totals["unique"],
        "classified": totals["classified"],
        "colorable": totals["colorable"],
        "non_colorable": totals["confirmed_non_colorable"],
        "timeout": totals["timeout"],
        "confirmed_non_colorable": totals["confirmed_non_colorable"],
        "primary_negative_candidates": totals["primary_negative_candidates"],
        "independent_unresolved": totals["independent_unresolved"],
        "duplicate": totals["duplicate"],
        "unclassified_deadline": totals["unclassified_deadline"],
    }
    selected_all_generated = totals["unique"] == totals["classified"]
    completion = {
        "bounded_generation_complete": state.enumeration_complete,
        "all_generated_unique_classified": selected_all_generated,
        "classification_complete": state.classification_complete,
        "independent_confirmation_complete": state.independent_confirmation_complete,
        "runtime_deadline_hit": state.runtime_deadline_hit,
        "stop_reason": state.stop_reason,
        "complete": (
            state.enumeration_complete
            and state.classification_complete
            and state.independent_confirmation_complete
            and selected_all_generated
            and not state.runtime_deadline_hit
        ),
    }
    return {
        "schema_version": 1,
        "configuration": configuration,
        "roots": [
            {
                "name": name,
                "order": graph.n,
                "size": graph.m,
                "maximum_degree": graph.delta,
                "minimum_degree": min(graph.degrees.values()),
                "overcap_vertices": {
                    vertex: graph.degrees[vertex]
                    for vertex in overcap_vertices(graph)
                },
            }
            for name, graph in roots
        ],
        "operation_definition": {
            "replacement_rule": (
                "replace every initial over-cap hub by exactly two copies in the "
                "hub's own bipartition side; distribute all former spokes between "
                "the copies deterministically with each copy receiving at least two"
            ),
            "synchronization_rule": (
                "connect the two copies through one bounded bipartite gadget; all "
                "new internal vertices have degree at least two after construction"
            ),
            "search_family": (
                "four spoke distributions; six theta pairs including asymmetric "
                "(2,4), (2,6), (4,6), and (6,6); two short-shared-rung variants; "
                "three cross-rung variants; separate three-link, K3-2/K3-3 shared "
                "cores, and a bipartition-colored K2-3 control; optional anchored "
                "C4 attachments at either or both copies"
            ),
            "invariants": [
                "simple connected bipartite graph with an explicit bipartition",
                "final maximum degree at most 10",
                "final minimum degree at least 2",
                "two copies remain connected only through the synchronized structure "
                "after deletion of the original hub",
            ],
            "deduplication": (
                "sha256 of the pynauty bipartition-colored certificate as returned by "
                "nauty_canonical_hash"
            ),
            "ranking_key": (
                "weighted-hub sufficient obstruction count, best margin, top-three "
                "margin sum, margin tier, normalized degree variance, canonical hash"
            ),
            "classification": (
                "rank_potential_solve CP-SAT with each candidate limited to at most "
                "5 seconds and 4 workers"
            ),
            "negative_confirmation": (
                "fixed_span_sat_solve independently over every legal span from delta "
                "through n-1; only all-INFEASIBLE evidence counts as non-colorable"
            ),
            "timeout_policy": (
                "primary or independent UNKNOWN remains unresolved, is counted once "
                "as timeout, and never as non-colorable"
            ),
            "graph_serialization_policy": (
                "full graph JSON appears only inside confirmed_negatives"
            ),
        },
        "deadline_seconds": configuration["runtime_hard_deadline_seconds"],
        "elapsed_seconds": elapsed_seconds,
        "counts": counts,
        "completion": completion,
        "complete": completion["complete"],
        "negative_events": [
            {key: value for key, value in record.items() if key != "graph"}
            for record in confirmed_negatives
        ],
        "summaries": list(summaries),
        "records": list(records),
        "confirmed_negatives": list(confirmed_negatives),
    }


def write_checkpoint(
    output_path: Path,
    configuration: dict,
    completed_summaries: Sequence[dict],
    live_summary: dict | None,
    recent_records: Sequence[dict],
    confirmed_negatives: Sequence[dict],
    running_counters: Counters,
    state: RunState,
    started: float,
) -> None:
    summaries = [*completed_summaries, *( [live_summary] if live_summary else [] )]
    report = make_report(
        configuration,
        [],
        summaries,
        list(recent_records),
        confirmed_negatives,
        time.monotonic() - started,
        state,
    )
    # Checkpoints retain a bounded record window; the final file retains all.
    report["checkpoint"] = {
        "partial": True,
        "record_window_size": configuration["checkpoint_record_window"],
        "written_unix_time": time.time(),
        "running_counts": {
            "generated": running_counters.generated,
            "unique": running_counters.unique,
            "classified": running_counters.classified,
            "colorable": running_counters.colorable,
            "non_colorable": running_counters.confirmed_non_colorable,
            "timeout": running_counters.timeout,
        },
    }
    atomic_write_json(output_path, report)


def resolve_roots(quotient_directory: Path) -> tuple[list[tuple[str, Graph]], dict]:
    roots: list[tuple[str, Graph]] = []
    resolved: list[str] = []
    fallback_used = False
    active_directory = quotient_directory
    if not active_directory.exists():
        active_directory = Path("results/graphs/quotient-r1")
        fallback_used = True
    for name in QUOTIENT_SEEDS:
        path = active_directory / f"{name}.graph.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        metadata = dict(data.get("metadata") or {})
        metadata["root_kind"] = "known_delta11_quotient_seed"
        metadata["source_path"] = str(path)
        roots.append((name, Graph.from_json({**data, "metadata": metadata})))
        resolved.append(name)
    for name, benchmark in benchmark_graphs().items():
        metadata = dict(benchmark.metadata)
        metadata["root_kind"] = "delta_above_10_benchmark_control"
        roots.append((name, Graph(benchmark.vertices, benchmark.edges, benchmark.bipartition, metadata)))
        resolved.append(name)
    resolution = {
        "requested_quotient_directory": str(quotient_directory),
        "resolved_quotient_directory": str(active_directory),
        "fallback_used": fallback_used,
        "resolved_names": resolved,
    }
    return roots, resolution


def search_root(
    root_name: str,
    root_graph: Graph,
    args,
    deadline: float,
    global_seen: set[str],
    construction_offset: int,
    completed_summaries: list[dict],
    all_records: list[dict],
    confirmed_negatives: list[dict],
    state: RunState,
    output_path: Path,
    started: float,
) -> dict:
    started_root = time.monotonic()
    counters = Counters()
    generated = expand_root(
        root_name,
        root_graph,
        state,
        counters,
        deadline,
        global_seen,
        construction_offset,
        args.maximum_constructions_per_parent,
    )
    generated.sort(
        key=lambda candidate: (
            -candidate.features["sufficient_obstruction_count"],
            -candidate.features["hub_best_margin"],
            -candidate.features["hub_margin_sum_top3"],
            -int(candidate.features["hub_best_margin_tier_at_least_minus_1_5"]),
            candidate.features["normalized_degree_variance"],
            candidate.digest,
        )
    )
    selected = generated if args.candidate_cap_per_parent <= 0 else generated[:args.candidate_cap_per_parent]
    if args.candidate_cap_per_parent > 0 and len(generated) > len(selected):
        state.enumeration_complete = False
        state.stop_reason = "candidate-limit"

    def summary(
        elapsed_seconds: float | None = None,
        complete: bool = True,
        selected_count: int | None = None,
    ) -> dict:
        candidate_cap_applied = (
            args.candidate_cap_per_parent > 0
            and counters.unique > (selected_count or 0)
        )
        payload = {
            "root": root_name,
            "root_order": root_graph.n,
            "root_size": root_graph.m,
            "root_maximum_degree": root_graph.delta,
            "overcap_vertices": {
                vertex: root_graph.degrees[vertex]
                for vertex in overcap_vertices(root_graph)
            },
            **vars(counters).copy(),
            "selected_for_classification": selected_count if selected_count is not None else counters.unique,
            "candidate_cap_applied": candidate_cap_applied,
            "enumeration_complete": state.enumeration_complete and not candidate_cap_applied,
            "classification_complete": complete,
            "independent_confirmation_complete": counters.independent_unresolved == 0,
            "complete": complete and counters.independent_unresolved == 0,
        }
        if elapsed_seconds is not None:
            payload["elapsed_seconds"] = elapsed_seconds
        return payload

    def checkpoint(latest_row: dict | None = None) -> None:
        live_summary = summary(complete=False)
        window = max(0, args.checkpoint_record_window)
        recent = ([*all_records, latest_row] if latest_row is not None else all_records)[-window:]
        write_checkpoint(
            output_path,
            configuration(args),
            completed_summaries,
            live_summary,
            recent,
            confirmed_negatives,
            counters,
            state,
            started,
        )

    for number, candidate in enumerate(selected, start=1):
        remaining = deadline - time.monotonic()
        if remaining <= 15.0:
            counters.unclassified_deadline += 1
            state.classification_complete = False
            state.runtime_deadline_hit = True
            state.stop_reason = "classification-deadline"
            break
        row, negative_record = classify_candidate(
            number,
            candidate,
            args.primary_time_limit,
            args.independent_time_limit,
            args.workers,
            deadline,
            counters,
            state,
            checkpoint,
        )
        all_records.append(row)
        if negative_record is not None:
            confirmed_negatives.append(negative_record)
        checkpoint(row)

    result_summary = summary(time.monotonic() - started_root, selected_count=len(selected))
    classification_complete = (
        state.enumeration_complete
        and counters.unique == counters.classified
        and counters.unclassified_deadline == 0
    )
    result_summary["classification_complete"] = classification_complete
    result_summary["complete"] = classification_complete and counters.independent_unresolved == 0
    return result_summary


def configuration(args) -> dict:
    return {
        "starting_graphs": [
            *QUOTIENT_SEEDS,
            "M5_delta_555",
            "Erd_Fano_2222221",
            "hat_K34",
            "hat_K34_prime_Delta11",
            "hat_K222",
        ],
        "maximum_final_delta": MAXIMUM_DELTA,
        "minimum_final_degree": MINIMUM_DEGREE,
        "require_connected_simple_bipartite": True,
        "candidate_cap_unique_per_parent": args.candidate_cap_per_parent,
        "maximum_constructions_per_parent": args.maximum_constructions_per_parent,
        "solver_workers": args.workers,
        "primary_time_limit_seconds": args.primary_time_limit,
        "independent_time_limit_seconds": args.independent_time_limit,
        "runtime_hard_deadline_seconds": args.deadline_seconds,
        "checkpoint_record_window": args.checkpoint_record_window,
        "atomic_checkpoint_after_every_solve": True,
        "edge_distribution_patterns": list(EDGE_PATTERNS),
        "theta_path_lengths": [list(pair) for pair in THETA_PATH_LENGTHS],
        "short_shared_rung_long_paths": list(SHORT_SHARED_RUNGS),
        "cross_rung_path_pairs": [list(pair) for pair in CROSS_RUNG_PATHS],
        "shared_cores": ["K3-2", "K3-3", "bipartition-colored K2-3 control"],
        "attachment_modes": list(ATTACHMENT_MODES),
        "smoke_run": args.smoke,
    }


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quotient-dir",
        type=Path,
        default=Path("results/graphs/quotient-r1"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/multihub-sync-delta10.json"),
    )
    parser.add_argument("--candidate-cap-per-parent", type=int, default=0)
    parser.add_argument("--maximum-constructions-per-parent", type=int, default=100000)
    parser.add_argument("--primary-time-limit", type=float, default=5.0)
    parser.add_argument("--independent-time-limit", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--deadline-seconds", type=float, default=10800.0)
    parser.add_argument("--checkpoint-record-window", type=int, default=256)
    parser.add_argument("--smoke", action="store_true")
    return parser


def validate_arguments(args) -> None:
    if not 1 <= args.workers <= MAX_SOLVER_WORKERS:
        raise SystemExit(f"workers must be between 1 and {MAX_SOLVER_WORKERS}")
    if not 0 < args.primary_time_limit <= MAX_SOLVER_SECONDS:
        raise SystemExit("primary time limit must be positive and at most 5 seconds")
    if not 0 < args.independent_time_limit <= MAX_SOLVER_SECONDS:
        raise SystemExit("independent time limit must be positive and at most 5 seconds")
    if args.deadline_seconds <= 0 or args.deadline_seconds > MAX_RUNTIME_SECONDS:
        raise SystemExit("deadline must be positive and at most 3 hours")
    if args.candidate_cap_per_parent < 0:
        raise SystemExit("candidate cap cannot be negative")
    if args.maximum_constructions_per_parent <= 0:
        raise SystemExit("construction limit must be positive")
    if args.checkpoint_record_window < 0:
        raise SystemExit("checkpoint record window cannot be negative")


def main() -> None:
    args = argument_parser().parse_args()
    validate_arguments(args)
    if args.smoke:
        args.output = Path("results/multihub-sync-delta10-smoke.json")
        args.candidate_cap_per_parent = min(args.candidate_cap_per_parent, 1)
        args.maximum_constructions_per_parent = min(
            args.maximum_constructions_per_parent,
            80,
        )
        args.primary_time_limit = min(args.primary_time_limit, 0.25)
        args.independent_time_limit = min(args.independent_time_limit, 0.25)
        args.workers = min(args.workers, 2)
        args.deadline_seconds = min(args.deadline_seconds, 120.0)
        args.checkpoint_record_window = min(args.checkpoint_record_window, 16)

    run_started = time.monotonic()
    deadline = run_started + args.deadline_seconds
    roots, seed_resolution = resolve_roots(args.quotient_dir)
    state = RunState()
    global_seen: set[str] = set()
    summaries: list[dict] = []
    all_records: list[dict] = []
    confirmed_negatives: list[dict] = []
    construction_offset = 0

    active_configuration = configuration(args)
    active_configuration["seed_resolution"] = seed_resolution
    for root_number, (root_name, root_graph) in enumerate(roots):
        if time.monotonic() >= deadline - 15.0:
            state.enumeration_complete = False
            state.classification_complete = False
            state.runtime_deadline_hit = True
            state.stop_reason = "root-deadline"
            break
        summary = search_root(
            root_name,
            root_graph,
            args,
            deadline,
            global_seen,
            construction_offset,
            summaries,
            all_records,
            confirmed_negatives,
            state,
            args.output,
            run_started,
        )
        summaries.append(summary)
        construction_offset += summary["constructions_attempted"]
        print(
            json.dumps(
                {
                    "event": "root_complete",
                    "root": root_name,
                    "generated": summary["generated"],
                    "unique": summary["unique"],
                    "classified": summary["classified"],
                    "timeout": summary["timeout"],
                    "confirmed_non_colorable": summary["confirmed_non_colorable"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

    report = make_report(
        active_configuration,
        roots,
        summaries,
        all_records,
        confirmed_negatives,
        time.monotonic() - run_started,
        state,
    )
    atomic_write_json(args.output, report)
    print(
        json.dumps(
            {
                "event": "search_complete",
                "output": str(args.output),
                "counts": report["counts"],
                "completion": report["completion"],
            },
            sort_keys=True,
        ),
        flush=True,
    )

if __name__ == "__main__":
    main()
