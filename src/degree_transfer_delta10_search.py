#!/usr/bin/env python3
"""Bounded terminal-gadget transfer search for bipartite Delta<=10 graphs."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import signal
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

import networkx as nx

from interval_edge_coloring import (
    Graph,
    benchmark_graphs,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)


@dataclass(frozen=True)
class RootedMotif:
    """A rooted bipartite auxiliary gadget whose non-root vertices have degree >= 2."""

    motif_id: int
    name: str
    internal_labels: tuple[str, ...]
    internal_sides: tuple[int, ...]
    edges: tuple[tuple[str, str], ...]

    @property
    def internal_vertex_count(self) -> int:
        return len(self.internal_sides)

    @property
    def root_degree(self) -> int:
        return sum("R" in edge for edge in self.edges)


def _cycle(root: str, labels: Sequence[str]) -> list[tuple[str, str]]:
    chain = [root, *labels, root]
    return [tuple(sorted((chain[i], chain[i + 1]))) for i in range(len(chain) - 1)]


def _motifs() -> tuple[RootedMotif, ...]:
    specs: list[tuple[int, str, tuple[str, ...], tuple[tuple[str, str], ...]]] = [
        (0, "rooted_C4", ("a", "b", "c"), _cycle("R", ["a", "b", "c"])),
        (
            1,
            "rooted_C6",
            ("a", "b", "c", "d", "e"),
            _cycle("R", ["a", "b", "c", "d", "e"]),
        ),
        (
            2,
            "rooted_K_2_3",
            ("A", "p", "q", "r"),
            [
                ("R", "p"), ("R", "q"), ("R", "r"),
                ("A", "p"), ("A", "q"), ("A", "r"),
            ],
        ),
        (
            3,
            "two_rooted_C4_blocks",
            ("a", "b", "c", "d", "e", "f"),
            _cycle("R", ["a", "b", "c"]) + _cycle("R", ["d", "e", "f"]),
        ),
        (
            4,
            "rooted_C8",
            tuple("abcdefg"),
            _cycle("R", list("abcdefg")),
        ),
        (
            5,
            "rooted_K_2_4",
            ("A", "p", "q", "r", "s"),
            [
                ("R", side) for side in ("p", "q", "r", "s")
            ] + [
                ("A", side) for side in ("p", "q", "r", "s")
            ],
        ),
        (
            6,
            "rooted_K_3_3",
            ("A", "B", "p", "q", "r"),
            [
                ("R", side) for side in ("p", "q", "r")
            ] + [
                ("A", side) for side in ("p", "q", "r")
            ] + [
                ("B", side) for side in ("p", "q", "r")
            ],
        ),
        (
            7,
            "two_rooted_C6_blocks",
            tuple("abcdefghij"),
            _cycle("R", list("abcde")) + _cycle("R", list("fghij")),
        ),
        (
            8,
            "rooted_K_3_4",
            ("A", "B", "p", "q", "r", "s"),
            [
                (root_side, side)
                for root_side in ("R", "A", "B")
                for side in ("p", "q", "r", "s")
            ],
        ),
        (
            9,
            "rooted_C10",
            tuple("abcdefghi"),
            _cycle("R", list("abcdefghi")),
        ),
    ]
    def template_sides(labels: Sequence[str], edges: Sequence[tuple[str, str]]) -> tuple[int, ...]:
        neighbors: dict[str, list[str]] = {label: [] for label in ("R", *labels)}
        for u, v in edges:
            neighbors[u].append(v)
            neighbors[v].append(u)
        distance = {"R": 0}
        queue = ["R"]
        while queue:
            vertex = queue.pop(0)
            for neighbor in neighbors[vertex]:
                if neighbor not in distance:
                    distance[neighbor] = distance[vertex] + 1
                    queue.append(neighbor)
        if set(distance) != set(("R", *labels)):
            raise ValueError("rooted gadget is disconnected")
        return tuple(distance[label] % 2 for label in labels)

    result: list[RootedMotif] = []
    for motif_id, name, labels, raw_edges in specs:
        edges = {tuple(sorted(edge)) for edge in raw_edges}
        sides = template_sides(labels, edges)
        result.append(RootedMotif(motif_id, name, tuple(labels), sides, tuple(sorted(edges))))
    return tuple(result)


MOTIFS = _motifs()


class SearchStopped(Exception):
    """Raised internally when the bounded family cannot continue safely."""


@dataclass
class Counters:
    constructed: int = 0
    accepted: int = 0
    rejected_disconnected: int = 0
    rejected_low_degree: int = 0
    duplicate: int = 0
    colorable: int = 0
    non_colorable: int = 0
    primary_negative: int = 0
    confirmed_non_colorable: int = 0
    timeout: int = 0
    independent_unresolved: int = 0


def known_counterexamples(graph_dir: Path) -> list[Graph]:
    graphs: list[Graph] = []
    directory = graph_dir.parent.parent / "graphs" / "quotient-r1"
    for name in ("Q1-00012.graph.json", "Q1-00014.graph.json"):
        path = directory / name
        data = json.loads(path.read_text(encoding="utf-8"))
        metadata = dict(data.get("metadata") or {})
        metadata["parent_name"] = name.removesuffix(".graph.json")
        metadata["parent_kind"] = "verified_19_vertex_counterexample"
        graphs.append(Graph.from_json({**data, "metadata": metadata}))
    return graphs


def reconstructed_benchmarks() -> list[tuple[str, Graph]]:
    result = []
    for name, graph in benchmark_graphs().items():
        metadata = dict(graph.metadata)
        metadata["parent_name"] = name
        metadata["parent_kind"] = "reconstructed_delta_at_least_11_benchmark"
        replacement = Graph(
            graph.vertices,
            graph.edges,
            graph.bipartition,
            metadata,
        )
        result.append((name, replacement))
    return result


def parent_graphs(graph_dir: Path) -> list[tuple[str, Graph, str]]:
    parents: list[tuple[str, Graph, str]] = [
        (graph.metadata["parent_name"], graph, "known_negative") for graph in known_counterexamples(graph_dir)
    ]
    parents.extend(
        (name, graph, "reconstructed_benchmark") for name, graph in reconstructed_benchmarks()
    )
    workload = {
        name: (
            sum(degree > 10 for degree in graph.degrees.values()),
            sum(any(vertex in edge for vertex in graph.degrees if graph.degrees[vertex] > 10) for edge in graph.edges),
            graph.n * graph.m,
        )
        for name, graph, _ in parents
    }
    known_first = sorted(
        [row for row in parents if row[2] == "known_negative"],
        key=lambda row: workload[row[0]],
    )
    benchmarks = sorted(
        [row for row in parents if row[2] == "reconstructed_benchmark"],
        key=lambda row: workload[row[0]],
    )
    return known_first + benchmarks


def demand_edges(base: Graph, maximum_delta: int) -> list[int]:
    demanding = {v for v, degree in base.degrees.items() if degree > maximum_delta}
    # The bounded transfer model rewires through the unique non-over-cap
    # endpoint. Edges with two over-cap endpoints belong to a different lane.
    return [
        number
        for number, edge in enumerate(base.edges)
        if len(demanding.intersection(edge)) == 1
    ]


def covering_selections(
    base: Graph,
    candidate_edges: Sequence[int],
    maximum_delta: int,
    maximum_replacements: int,
    deadline: float | None,
) -> Iterator[tuple[int, ...]]:
    demands = {
        vertex: max(0, degree - maximum_delta)
        for vertex, degree in base.degrees.items()
        if degree > maximum_delta
    }
    residual = dict(demands)

    def can_finish(position: int, current: dict[str, int], slots: int) -> bool:
        for vertex, need in current.items():
            available = sum(
                vertex in base.edges[edge_number]
                for edge_number in candidate_edges
                if edge_number >= position
            )
            if need > min(available, slots):
                return False
        return True

    def visit(position: int, slots: int) -> Iterator[tuple[int, ...]]:
        if deadline is not None and time.monotonic() >= deadline - 5.0:
            raise SearchStopped("deadline reserve reached")
        if not any(residual.values()):
            yield tuple(chosen)
        if slots == 0 or position == len(candidate_edges):
            return
        if not can_finish(position, residual, slots):
            return
        edge_number = candidate_edges[position]
        affected = [vertex for vertex in residual if vertex in base.edges[edge_number]]
        yield from visit(position + 1, slots)
        chosen.append(edge_number)
        for vertex in affected:
            residual[vertex] -= 1
        try:
            yield from visit(position + 1, slots - 1)
        finally:
            for vertex in affected:
                residual[vertex] += 1
            chosen.pop()

    chosen: list[int] = []
    yield from visit(0, maximum_replacements)


def apply_terminal_gadget(
    base: Graph,
    selected_edges: Sequence[int],
    motif_ids: Sequence[int],
    construction_id: int,
) -> tuple[Graph, list[dict]]:
    if len(selected_edges) != len(motif_ids):
        raise AssertionError("replacement/motif count mismatch")
    left = set(base.bipartition[0])
    overcap = {v for v, degree in base.degrees.items() if degree > 10}
    vertices = list(base.vertices)
    gadget_edges: list[tuple[str, str]] = []
    replacement_records: list[dict] = []

    for sequence, (edge_number, motif_id) in enumerate(zip(selected_edges, motif_ids)):
        motif = MOTIFS[motif_id]
        old_edge = tuple(sorted(base.edges[edge_number]))
        u, v = old_edge
        roots = [endpoint for endpoint in (u, v) if endpoint not in overcap]
        if len(roots) != 1:
            raise AssertionError("replacement edge must have one transfer root")
        root = roots[0]
        mapping = {"R": root}
        for internal_number, raw_label in enumerate(motif.internal_labels):
            new_name = f"G{construction_id:05d}_{sequence:02d}_{internal_number:02d}"
            mapping[raw_label] = new_name
            vertices.append(new_name)

        for raw_u, raw_v in motif.edges:
            gadget_edges.append((mapping[raw_u], mapping[raw_v]))

        replacement_records.append(
            {
                "parent_edge_index": edge_number,
                "parent_endpoints": list(old_edge),
                "transfer_root": root,
                "motif_id": motif_id,
                "motif_name": motif.name,
                "root_degree": motif.root_degree,
                "new_vertices": motif.internal_vertex_count,
            }
        )

    selected_names = {tuple(sorted(base.edges[number])) for number in selected_edges}
    normalized_edges: list[tuple[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for edge in base.edges:
        key = tuple(sorted(edge))
        if key not in selected_names:
            normalized_edges.append(key)
            seen_edges.add(key)
    for raw_edge in gadget_edges:
        key = tuple(sorted(raw_edge))
        if key not in seen_edges:
            normalized_edges.append(key)
            seen_edges.add(key)

    new_left = set(base.bipartition[0])
    new_right = set(base.bipartition[1])
    for sequence, (edge_number, motif_id) in enumerate(zip(selected_edges, motif_ids)):
        motif = MOTIFS[motif_id]
        root = replacement_records[sequence]["transfer_root"]
        template_zero_is_left = root in left
        for internal_number, (raw_label, template_side) in enumerate(
            zip(motif.internal_labels, motif.internal_sides)
        ):
            new_name = f"G{construction_id:05d}_{sequence:02d}_{internal_number:02d}"
            belongs_left = (template_side == 0) == template_zero_is_left
            if new_name in new_left or new_name in new_right:
                raise AssertionError(f"bipartition collision for {new_name}")
            (new_left if belongs_left else new_right).add(new_name)

    if set(vertices) != new_left | new_right or new_left & new_right:
        raise AssertionError("incomplete replacement bipartition")
    metadata = {
        **base.metadata,
        "lane": "degree-transfer-rooted-auxiliary-gadget",
        "construction_id": construction_id,
        "selected_parent_edges": sorted(selected_edges),
        "replacement_motifs": [record["motif_id"] for record in replacement_records],
    }
    graph = Graph(vertices, normalized_edges, [sorted(new_left), sorted(new_right)], metadata)
    return graph, replacement_records


def degree_cap_holds(
    base: Graph,
    selected: Sequence[int],
    motif_ids: Sequence[int],
    maximum_delta: int,
) -> bool:
    changes = {vertex: 0 for vertex in base.vertices}
    overcap = {vertex for vertex, degree in base.degrees.items() if degree > maximum_delta}
    for edge_number, motif_id in zip(selected, motif_ids):
        endpoints = tuple(sorted(base.edges[edge_number]))
        hubs = [endpoint for endpoint in endpoints if endpoint in overcap]
        if len(hubs) != 1:
            return False
        hub = hubs[0]
        root = next(endpoint for endpoint in endpoints if endpoint != hub)
        changes[hub] -= 1
        changes[root] += MOTIFS[motif_id].root_degree - 1
    return all(
        degree + changes[vertex] <= maximum_delta
        for vertex, degree in base.degrees.items()
    )
    return all(
        degree - reductions[vertex] <= maximum_delta
        for vertex, degree in base.degrees.items()
    )


def near_miss_rows(rows: Iterable[dict], limit: int = 8) -> list[dict]:
    def key(row: dict) -> tuple[int, int, int]:
        hubs = row.get("weighted_hubs_best") or []
        margin = min((item["margin"] for item in hubs), default=999)
        span_penalty = -(row.get("primary_span") or 0)
        timeout_rank = 0 if row.get("primary_status") == "timeout" else 1
        return timeout_rank, span_penalty, margin

    compact = []
    for row in rows:
        compact.append(
            {
                "canonical_sha256": row["canonical_sha256"],
                "order": row["order"],
                "size": row["size"],
                "delta": row["delta"],
                "minimum_degree": row["minimum_degree"],
                "selected_parent_edges": row["selected_parent_edges"],
                "replacement_motifs": row["replacement_motifs"],
                "primary_status": row["primary_status"],
                "primary_span": row.get("primary_span"),
                "weighted_hubs_best": row.get("weighted_hubs_best"),
            }
        )
    return sorted(compact, key=key)[:limit]


class _ConfirmationDeadline(Exception):
    pass


def independent_confirmation(
    graph: Graph,
    time_limit: float,
    workers: int,
    deadline: float,
) -> tuple[bool, bool, dict[int, str]]:
    """Classify every legal span with the independent fixed-span encoding."""

    statuses: dict[int, str] = {}
    saw_solution = False
    wall_timeout = False
    current_span: int | None = None

    def raise_deadline(signum: int, frame: object) -> None:
        del signum, frame
        raise _ConfirmationDeadline()

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_deadline)
    signal.setitimer(signal.ITIMER_REAL, max(0.0, deadline - time.monotonic()))
    try:
        for span in range(graph.delta, max(graph.delta, graph.n - 1) + 1):
            current_span = span
            remaining = deadline - time.monotonic()
            if remaining <= 0.25:
                statuses[span] = "UNKNOWN"
                wall_timeout = True
                break
            solve_limit = max(0.01, min(time_limit, remaining - 0.2))
            status_name, coloring = fixed_span_sat_solve(
                graph, span, solve_limit, workers
            )
            statuses[span] = status_name
            if status_name in ("OPTIMAL", "FEASIBLE"):
                if coloring is None:
                    raise AssertionError("fixed-span solver returned no coloring")
                saw_solution = True
                break
    except _ConfirmationDeadline:
        wall_timeout = True
        if current_span is not None:
            statuses[current_span] = "UNKNOWN"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)

    expected_spans = set(range(graph.delta, max(graph.delta, graph.n - 1) + 1))
    unresolved = wall_timeout or set(statuses) != expected_spans or any(
        status == "UNKNOWN" for status in statuses.values()
    )
    confirmed = not saw_solution and not unresolved and all(
        statuses[span] == "INFEASIBLE" for span in expected_spans
    )
    return confirmed, unresolved, statuses


def search_parent(
    parent_name: str,
    base: Graph,
    parent_kind: str,
    maximum_delta: int,
    maximum_replacements: int,
    minimum_degree: int,
    time_limit: float,
    workers: int,
    global_seen: set[str],
    global_unique_count: int,
    candidate_cap: int,
    deadline: float,
    output_dir: Path,
    checkpoint_callback: Callable[[dict, list[dict], list[dict], bool], None],
) -> tuple[dict, list[dict], list[dict], bool, bool]:
    started = time.monotonic()
    counters = Counters()
    rows: list[dict] = []
    negative_events: list[dict] = []
    stopped_for_deadline = False
    cap_reached = False
    enumeration_finished = True
    construction_number = 0
    local_seen: set[str] = set()
    last_checkpoint = started
    candidate_edges = demand_edges(base, maximum_delta)

    def partial_summary(stopped: bool) -> dict:
        return {
            "parent": parent_name,
            "parent_kind": parent_kind,
            "parent_order": base.n,
            "parent_size": base.m,
            "parent_delta": base.delta,
            "overcap_vertices": {
                vertex: degree - maximum_delta
                for vertex, degree in base.degrees.items()
                if degree > maximum_delta
            },
            "candidate_parent_edges": len(candidate_edges),
            "replacements_generated": counters.constructed,
            "generated": counters.accepted,
            "unique": len(local_seen),
            "rejected_disconnected": counters.rejected_disconnected,
            "rejected_low_degree": counters.rejected_low_degree,
            "duplicates": counters.duplicate,
            "colorable": counters.colorable,
            "non_colorable": counters.confirmed_non_colorable,
            "primary_negative_candidates": counters.primary_negative,
            "confirmed_non_colorable": counters.confirmed_non_colorable,
            "timeout": counters.timeout,
            "independent_unresolved": counters.independent_unresolved,
            "best_near_miss_diagnostics": near_miss_rows(rows),
            "replacement_family_complete": False,
            "classification_complete": False,
            "independent_confirmation_complete": counters.independent_unresolved == 0,
            "stopped": stopped,
        }

    def checkpoint_periodically(stopped: bool) -> None:
        nonlocal last_checkpoint
        now = time.monotonic()
        if now - last_checkpoint >= 30.0:
            checkpoint_callback(partial_summary(stopped), rows, negative_events, stopped)
            last_checkpoint = now

    try:
        selections = covering_selections(
            base, candidate_edges, maximum_delta, maximum_replacements, deadline
        )
        for selected in selections:
            if global_unique_count + counters.accepted - counters.duplicate >= candidate_cap:
                cap_reached = True
                enumeration_finished = False
                break
            for motif_ids in itertools.product(range(len(MOTIFS)), repeat=len(selected)):
                remaining = deadline - time.monotonic()
                if remaining <= 30.0:
                    stopped_for_deadline = True
                    enumeration_finished = False
                    break
                if not degree_cap_holds(
                    base, selected, motif_ids, maximum_delta
                ):
                    raise AssertionError("degree-cap invariant failed")
                graph, replacements = apply_terminal_gadget(
                    base, selected, motif_ids, construction_number
                )
                construction_number += 1
                counters.constructed += 1
                nx_graph = nx.Graph(graph.edges)
                nx_graph.add_nodes_from(graph.vertices)
                if not nx.is_connected(nx_graph):
                    counters.rejected_disconnected += 1
                    continue
                if min(nx_graph.degree(vertex) for vertex in graph.vertices) < minimum_degree:
                    counters.rejected_low_degree += 1
                    continue
                counters.accepted += 1
                digest = nauty_canonical_hash(graph)
                if digest in global_seen or digest in local_seen:
                    counters.duplicate += 1
                    continue
                local_seen.add(digest)
                global_seen.add(digest)

                solve_limit = max(0.05, min(time_limit, remaining - 25.0))
                primary = rank_potential_solve(graph, solve_limit, workers)
                row = {
                    "parent": parent_name,
                    "parent_kind": parent_kind,
                    "canonical_sha256": digest,
                    "order": graph.n,
                    "size": graph.m,
                    "delta": graph.delta,
                    "minimum_degree": min(graph.degrees.values()),
                    "connected": True,
                    "selected_parent_edges": sorted(selected),
                    "replacement_motifs": list(motif_ids),
                    "replacement_details": replacements,
                    "primary_status": primary.status,
                    "primary_span": primary.span,
                    "primary_elapsed_seconds": primary.elapsed_seconds,
                    "weighted_hubs_best": weighted_hub_statistics(graph)[:2],
                }
                if primary.status == "non-colorable":
                    counters.primary_negative += 1
                    confirmed, unresolved, span_statuses = independent_confirmation(
                        graph,
                        time_limit,
                        workers,
                        deadline - 3.0,
                    )
                    row["independent_decision"] = (
                        "non-colorable"
                        if confirmed
                        else ("timeout" if unresolved else "colorable")
                    )
                    row["independent_spans"] = {
                        str(span): status for span, status in sorted(span_statuses.items())
                    }
                    row["independent_unresolved"] = unresolved
                    if not unresolved and not confirmed:
                        raise AssertionError(
                            f"primary/independent classification conflict for {digest}"
                        )
                    if confirmed:
                        counters.confirmed_non_colorable += 1
                        counters.non_colorable += 1
                        candidate_id = f"DTR-{len(negative_events) + 1:04d}"
                        graph.metadata["candidate_id"] = candidate_id
                        graph.metadata["certification"] = {
                            "primary": "rank-potential-cpsat-infeasible",
                            "independent": "fixed-span-cpsat-infeasible-all-legal-spans",
                            "spans_checked": sorted(span_statuses),
                        }
                        path = output_dir / f"{candidate_id}.graph.json"
                        graph.save(path)
                        event = {
                            "event": "certified_non_colorable",
                            "candidate_id": candidate_id,
                            "path": str(path),
                            "parent": parent_name,
                            "canonical_sha256": digest,
                            "order": graph.n,
                            "size": graph.m,
                            "delta": graph.delta,
                        }
                        negative_events.append(event)
                        print(json.dumps(event, sort_keys=True), flush=True)
                    else:
                        counters.independent_unresolved += 1
                        if time.monotonic() >= deadline - 10.0:
                            stopped_for_deadline = True
                            enumeration_finished = False
                elif primary.status == "timeout":
                    counters.timeout += 1
                else:
                    counters.colorable += 1
                rows.append(row)
                now = time.monotonic()
                if now - last_checkpoint >= 30.0:
                    checkpoint_callback(partial_summary(stopped_for_deadline or cap_reached), rows, negative_events, stopped_for_deadline or cap_reached)
                    last_checkpoint = now
                if global_unique_count + len(local_seen) >= candidate_cap:
                    cap_reached = True
                    enumeration_finished = False
                    break
            if stopped_for_deadline or cap_reached:
                break
    except SearchStopped:
        stopped_for_deadline = True
        enumeration_finished = False

    classification_complete = enumeration_finished and not stopped_for_deadline and not cap_reached
    summary = partial_summary(stopped_for_deadline or cap_reached)
    summary.update(
        {
            "replacement_motif_catalog_size": len(MOTIFS),
            "maximum_replaced_edges_per_parent": maximum_replacements,
            "replacement_family_complete": enumeration_finished,
            "classification_complete": classification_complete,
            "independent_confirmation_complete": counters.independent_unresolved == 0,
            "parent_processing_complete": classification_complete and counters.independent_unresolved == 0,
            "candidate_cap_reached": cap_reached,
            "deadline_stop": stopped_for_deadline,
            "elapsed_seconds": time.monotonic() - started,
        }
    )
    return summary, rows, negative_events, stopped_for_deadline, cap_reached


def totals_from_summaries(summaries: Sequence[dict]) -> dict:
    fields = (
        "replacements_generated",
        "generated",
        "unique",
        "colorable",
        "non_colorable",
        "confirmed_non_colorable",
        "primary_negative_candidates",
        "timeout",
        "independent_unresolved",
        "duplicates",
        "rejected_disconnected",
        "rejected_low_degree",
    )
    totals = {field: sum(int(item.get(field, 0)) for item in summaries) for field in fields}
    totals["parents_completed"] = sum(bool(item.get("parent_processing_complete")) for item in summaries)
    totals["parents_started"] = len(summaries)
    return totals


def make_report(
    configuration: dict,
    summaries: Sequence[dict],
    records: Sequence[dict],
    negative_events: Sequence[dict],
    elapsed_seconds: float,
    deadline_seconds: float,
    deadline_hit: bool,
) -> dict:
    totals = totals_from_summaries(summaries)
    complete = bool(summaries) and all(item.get("parent_processing_complete") for item in summaries)
    independent_complete = all(item.get("independent_confirmation_complete") for item in summaries)
    best = {
        parent: near_miss_rows([row for row in records if row["parent"] == parent], 3)
        for parent in {row["parent"] for row in records}
    }
    return {
        "schema_version": 1,
        "configuration": configuration,
        "complete": complete,
        "completion_flags": {
            "family_exhausted_within_bounds": complete,
            "all_parents_fully_classified": complete,
            "independent_confirmation_complete_without_timeout": independent_complete,
            "runtime_deadline_hit": deadline_hit,
        },
        "runtime_deadline_hit": deadline_hit,
        "deadline_seconds": deadline_seconds,
        "elapsed_seconds": elapsed_seconds,
        "totals": totals,
        "counts": {
            "replacement": totals["replacements_generated"],
            "generated": totals["generated"],
            "unique": totals["unique"],
            "colorable": totals["colorable"],
            "non_colorable": totals["non_colorable"],
            "timeout": totals["timeout"],
        },
        "best_near_miss_diagnostics": {
            "by_parent": best,
            "global_top": near_miss_rows(records, 10),
        },
        "negative_events": list(negative_events),
        "summaries": list(summaries),
        "records": list(records),
    }


def atomic_write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maximum-final-delta", type=int, default=10)
    parser.add_argument("--max-replaced-edges", type=int, default=6)
    parser.add_argument("--minimum-degree", type=int, default=2)
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--candidate-cap-per-parent", type=int, default=1500)
    parser.add_argument("--deadline-seconds", type=float, default=7200.0)
    parser.add_argument("--output", default="results/degree-transfer-delta10.json")
    args = parser.parse_args()

    run_started_wall = time.time()
    run_started = time.monotonic()
    deadline = run_started + args.deadline_seconds
    output_path = Path(args.output)
    graph_dir = output_path.parent / "graphs" / "degree-transfer-delta10"
    graph_dir.mkdir(parents=True, exist_ok=True)
    parents = parent_graphs(graph_dir)
    global_seen: set[str] = set()
    summaries: list[dict] = []
    all_records: list[dict] = []
    all_negatives: list[dict] = []
    global_cap_reached = False
    runtime_deadline_hit = False
    prior_unique = 0

    configuration = {
        "search_lane": "local terminal-gadget transfer of incident over-cap edges",
        "starting_graphs": {
            "known_counterexamples": [name for name, _, kind in parents if kind == "known_negative"],
            "reconstructed_delta_at_least_11": [
                name for name, _, kind in parents if kind == "reconstructed_benchmark"
            ],
        },
        "replacement_model": (
            "each selected over-cap/non-over-cap edge is deleted at its over-cap "
            "endpoint and rewired into a rooted bipartite auxiliary gadget at the "
            "non-over-cap endpoint; all final degrees are capped"
        ),
        "terminal_gadgets": [
            {"id": motif.motif_id, "name": motif.name, "internal_vertices": motif.internal_vertex_count}
            for motif in MOTIFS
        ],
        "maximum_final_delta": args.maximum_final_delta,
        "maximum_replaced_edges_per_parent": args.max_replaced_edges,
        "minimum_graph_degree": args.minimum_degree,
        "require_connected": True,
        "deduplication": "bipartition-colored Nauty certificate SHA-256",
        "primary_classification": "rank-potential CP-SAT",
        "negative_confirmation": "fixed-span CP-SAT independently over every legal span",
        "timeout_policy": "timeout is unresolved and never counted as non-colorable",
        "candidate_cap_unique_per_parent": args.candidate_cap_per_parent,
        "solver_time_limit_seconds": args.time_limit,
        "workers": args.workers,
    }

    def write_checkpoint(current: dict, records: list[dict], negatives: list[dict], stopped: bool) -> None:
        combined_records = [*all_records, *records]
        combined_negatives = [*all_negatives, *negatives]
        report = make_report(
            configuration,
            [*summaries, current],
            combined_records,
            combined_negatives,
            time.monotonic() - run_started,
            args.deadline_seconds,
            stopped or time.monotonic() >= deadline,
        )
        report["checkpoint"] = {
            "written_unix_time": time.time(),
            "partial": True,
            "current_parent": current.get("parent"),
        }
        atomic_write_json(output_path, report)

    for name, base, kind in parents:
        remaining = deadline - time.monotonic()
        if remaining <= 30.0:
            runtime_deadline_hit = True
            break
        start_event = {
            "event": "parent_start",
            "parent": name,
            "kind": kind,
            "order": base.n,
            "size": base.m,
            "delta": base.delta,
        }
        print(json.dumps(start_event, sort_keys=True), flush=True)
        summary, records, negatives, deadline_stop, cap_reached = search_parent(
            name,
            base,
            kind,
            args.maximum_final_delta,
            args.max_replaced_edges,
            args.minimum_degree,
            args.time_limit,
            args.workers,
            global_seen,
            0,
            args.candidate_cap_per_parent,
            deadline,
            graph_dir,
            write_checkpoint,
        )
        summaries.append(summary)
        all_records.extend({"parent": name, **row} for row in records)
        all_negatives.extend(negatives)
        prior_unique += summary["unique"]
        global_cap_reached = global_cap_reached or cap_reached
        runtime_deadline_hit = runtime_deadline_hit or deadline_stop
        compact = {key: value for key, value in summary.items() if key != "best_near_miss_diagnostics"}
        print(json.dumps({"event": "parent_complete", **compact}, sort_keys=True), flush=True)
        atomic_write_json(
            output_path,
            make_report(
                configuration,
                summaries,
                all_records,
                all_negatives,
                time.monotonic() - run_started,
                args.deadline_seconds,
                runtime_deadline_hit or time.monotonic() >= deadline,
            ),
        )
        # A per-parent cap intentionally does not prevent later parents from
        # receiving the same bounded search budget.

    final_report = make_report(
        configuration,
        summaries,
        all_records,
        all_negatives,
        time.monotonic() - run_started,
        args.deadline_seconds,
        runtime_deadline_hit or time.monotonic() >= deadline,
    )
    final_report["environment"] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "started_unix_time": run_started_wall,
        "finished_unix_time": time.time(),
    }
    atomic_write_json(output_path, final_report)
    print(
        json.dumps(
            {
                "event": "run_complete",
                "complete": final_report["complete"],
                "runtime_deadline_hit": runtime_deadline_hit,
                "counts": final_report["counts"],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
