#!/usr/bin/env python3
"""Edge-minimal obstruction grafting around a degree-11 hub.

Remove one or two hub-adjacent connectors as a unit.  The retained hub and all
surviving core endpoints become terminals of one deterministic bipartite
replacement component.  Generate every rooted replacement tree of height at
most three, then allow at most one non-tree chord (a multitree).  The whole
graft therefore has diameter at most six and at most twelve new vertices.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from interval_edge_coloring import (
    Graph,
    benchmark_graphs,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)


MAXIMUM_DELTA = 10
MINIMUM_FINAL_DEGREE = 2
MAX_NEW_VERTICES = 12
MAXIMUM_GRAFT_DIAMETER = 6
MAX_SOLVER_SECONDS = 5.0
MAX_SOLVER_WORKERS = 4
MAX_RUNTIME_SECONDS = 10800.0
QUOTIENT_SEEDS = ("Q1-00012", "Q1-00014")
BENCHMARK_NAMES = (
    "M5_delta_555",
    "Erd_Fano_2222221",
    "hat_K34",
    "hat_K34_prime_Delta11",
    "hat_K222",
)


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
    high_margin_unique: int = 0
    classified: int = 0
    colorable: int = 0
    non_colorable: int = 0
    timeout: int = 0
    primary_negative_candidates: int = 0
    confirmed_non_colorable: int = 0
    independent_unresolved: int = 0
    unclassified_deadline: int = 0
    selected_for_classification: int = 0


@dataclass
class RunState:
    bounded_generation_complete: bool = True
    all_selected_classified: bool = True
    independent_confirmation_complete: bool = True
    runtime_deadline_hit: bool = False
    stop_reason: str | None = None


@dataclass(frozen=True)
class Candidate:
    construction_id: int
    parent: str
    graph: Graph
    digest: str
    features: dict
    operation: dict


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def set_partitions(items: tuple[int, ...]) -> Iterable[tuple[tuple[int, ...], ...]]:
    if not items:
        yield ()
        return
    first, rest = items[0], items[1:]
    for previous in set_partitions(rest):
        blocks = [list(block) for block in previous]
        for index in range(len(blocks)):
            blocks[index].append(first)
            yield tuple(sorted(tuple(block) for block in blocks))
        yield tuple(sorted(((first,), *previous)))


def normalized_degree_variance(graph: Graph) -> float:
    degrees = list(graph.degrees.values())
    mean = sum(degrees) / len(degrees) if degrees else 0.0
    variance = sum((degree - mean) ** 2 for degree in degrees) / len(degrees)
    return variance / (mean * mean) if mean else 0.0


def ranking_features(graph: Graph) -> dict:
    hubs = weighted_hub_statistics(graph)
    margins = sorted((row["margin"] for row in hubs), reverse=True)
    best = margins[0] if margins else -10**9
    tier_rank, tier_name = 2, "below-high-margin"
    if best >= -1.5:
        tier_rank, tier_name = 0, "margin-at-least-minus-1.5"
    elif best >= -2.5:
        tier_rank, tier_name = 1, "margin-at-least-minus-2.5"
    return {
        "hub_best_margin": best,
        "hub_best_margin_tier": tier_name,
        "hub_best_margin_tier_rank": tier_rank,
        "high_margin": best >= -2.5,
        "sufficient_obstruction_count": sum(
            row["sufficient_obstruction"] for row in hubs
        ),
        "top_three_hub_margins": margins[:3],
        "hub_margin_sum_top3": sum(margins[:3]),
        "normalized_degree_variance": normalized_degree_variance(graph),
        "weighted_hubs_best": hubs[:4],
    }


def ranking_key(candidate: Candidate) -> tuple:
    features = candidate.features
    return (
        -features["hub_best_margin"],
        features["hub_best_margin_tier_rank"],
        features["normalized_degree_variance"],
        -features["sufficient_obstruction_count"],
        -features["hub_margin_sum_top3"],
        candidate.digest,
    )


def render_group(group: tuple[int, ...]) -> list[dict]:
    group_set = frozenset(group)
    options: list[dict] = []
    for mask in range(1 << len(group)):
        direct = tuple(
            group[index]
            for index in range(len(group))
            if mask & (1 << index)
        )
        remaining = tuple(sorted(group_set.difference(direct)))
        for blocks in set_partitions(remaining):
            new_count = 1 + len(blocks) + sum(map(len, blocks))
            if new_count <= MAX_NEW_VERTICES:
                options.append({"direct": direct, "blocks": blocks})
    return options


def replacement_trees(core_terminal_count: int) -> Iterable[dict]:
    terms = tuple(range(core_terminal_count))
    for root_blocks in set_partitions(terms):
        rendered_options = [render_group(block) for block in root_blocks]
        if any(not choices for choices in rendered_options):
            continue

        def visit(index: int, new_used: int, chosen: list[dict]) -> Iterable[dict]:
            if index == len(rendered_options):
                nodes = [{"name": "HUB", "side": "L", "kind": "retained-hub"}]
                edges: list[tuple[str, str]] = []
                serial = 0
                for option in chosen:
                    root_child = f"N{serial:02d}_R"
                    serial += 1
                    nodes.append({"name": root_child, "side": "R", "kind": "new"})
                    edges.append(("HUB", root_child))
                    for terminal in option["direct"]:
                        edges.append((root_child, f"T{terminal:02d}"))
                    for terminal_block in option["blocks"]:
                        left_vertex = f"N{serial:02d}_L"
                        serial += 1
                        nodes.append({"name": left_vertex, "side": "L", "kind": "new"})
                        edges.append((root_child, left_vertex))
                        for terminal in terminal_block:
                            right_vertex = f"N{serial:02d}_R"
                            serial += 1
                            nodes.append({"name": right_vertex, "side": "R", "kind": "new"})
                            edges.append((left_vertex, right_vertex))
                            edges.append((right_vertex, f"T{terminal:02d}"))
                yield {
                    "rule": "rooted-height-3-bipartite-tree",
                    "root_children": len(root_blocks),
                    "core_terminal_count": core_terminal_count,
                    "tree_new_vertices": new_used,
                    "nodes": nodes,
                    "edges": sorted(tuple(sorted(edge)) for edge in edges),
                    "non_tree_chords": [],
                }
                return

            remaining_new = MAX_NEW_VERTICES - new_used
            for option in rendered_options[index]:
                option_new = (
                    1 + len(option["blocks"]) + sum(map(len, option["blocks"]))
                )
                if option_new <= remaining_new:
                    yield from visit(index + 1, new_used + option_new, [*chosen, option])

        yield from visit(0, 0, [])


def chord_options(tree: dict) -> list[tuple[str, str]]:
    sides = {node["name"]: node["side"] for node in tree["nodes"]}
    tree_edges = set(tree["edges"])
    names = list(sides)
    pairs: list[tuple[str, str]] = []
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            if sides[left] == sides[right]:
                continue
            pair = tuple(sorted((left, right)))
            if pair not in tree_edges:
                pairs.append(pair)
    return sorted(pairs)


def graph_diameter(names: Sequence[str], edges: Sequence[tuple[str, str]]) -> int | None:
    adjacency = {name: [] for name in names}
    external: set[str] = set()
    for left, right in edges:
        for endpoint, other in ((left, right), (right, left)):
            if endpoint in adjacency:
                adjacency[endpoint].append(other)
            else:
                external.add(endpoint)
    best = 0
    for source in names:
        distances = {source: 0}
        frontier = [source]
        while frontier:
            next_frontier = []
            for vertex in frontier:
                for neighbor in adjacency.get(vertex, []):
                    if neighbor not in distances:
                        distances[neighbor] = distances[vertex] + 1
                        next_frontier.append(neighbor)
            frontier = next_frontier
        if source == names[0] and not external <= set(distances):
            return None
        best = max(best, max(distances.values()))
    return best


def boundary_ports(seed: Graph, hub: str, neighbors: Sequence[str]) -> dict[str, list[str]]:
    ports: dict[str, set[str]] = {neighbor: set() for neighbor in neighbors}
    selected = set(neighbors)
    for left, right in seed.edges:
        if right == hub and left in selected:
            continue
        if left == hub and right in selected:
            continue
        if left in selected:
            ports[left].add(right)
        elif right in selected:
            ports[right].add(left)
    return {neighbor: sorted(values) for neighbor, values in ports.items()}


def graft_graph(
    seed: Graph,
    hub: str,
    selected_neighbors: Sequence[str],
    template: dict,
) -> Graph | None:
    ports_by_neighbor = boundary_ports(seed, hub, selected_neighbors)
    if any(not values for values in ports_by_neighbor.values()):
        return None
    flat_ports: list[str] = []
    for neighbor in selected_neighbors:
        flat_ports.extend(ports_by_neighbor[neighbor])
    if len(flat_ports) != template["core_terminal_count"]:
        return None

    vertices = [
        vertex for vertex in seed.vertices if vertex not in selected_neighbors
    ]
    selected = set(selected_neighbors)
    removed_edges = {
        edge for edge in seed.edges if edge[0] in selected or edge[1] in selected
    }
    edges = [edge for edge in seed.edges if edge not in removed_edges]
    side_zero = set(seed.bipartition[0])
    side_one = set(seed.bipartition[1])
    for vertex in [*side_zero, *side_one]:
        if vertex in selected_neighbors:
            side_zero.discard(vertex)
            side_one.discard(vertex)

    new_names: set[str] = set()
    for node in template["nodes"]:
        if node["kind"] == "retained-hub":
            if node["name"] != "HUB" or node["side"] != "L":
                return None
            if hub not in side_zero:
                return None
        else:
            name = node["name"]
            if name.startswith("T"):
                return None
            if name in new_names or name in side_zero or name in side_one:
                return None
            new_names.add(name)
            vertices.append(name)
            if node["side"] == "L":
                side_zero.add(name)
            elif node["side"] == "R":
                side_one.add(name)
            else:
                return None

    translated: set[tuple[str, str]] = set()

    def translate_endpoint(endpoint: str) -> str | None:
        if endpoint != "HUB" and not endpoint.startswith("T"):
            return endpoint
        if endpoint == "HUB":
            return hub
        terminal_number = int(endpoint[1:])
        if not 0 <= terminal_number < len(flat_ports):
            return None
        return flat_ports[terminal_number]

    all_graft_edges = [*template["edges"], *template["non_tree_chords"]]
    for left, right in all_graft_edges:
        mapped_left = translate_endpoint(left)
        mapped_right = translate_endpoint(right)
        if mapped_left is None or mapped_right is None or mapped_left == mapped_right:
            return None
        edge = tuple(sorted((mapped_left, mapped_right)))
        if edge in translated:
            return None
        translated.add(edge)
        edges.append(edge)

    replacement_names = ["HUB"] + list(new_names)
    diameter = graph_diameter(replacement_names, all_graft_edges)
    if diameter is None or diameter > MAXIMUM_GRAFT_DIAMETER:
        return None
    metadata = {
        **seed.metadata,
        "lane": "edge-minimal-neighborhood-graft-delta10",
        "parent": seed.metadata.get("candidate_id", seed.metadata.get("family")),
        "graft_operation": template,
    }
    try:
        return Graph(
            vertices,
            edges,
            [sorted(side_zero), sorted(side_one)],
            metadata,
        )
    except (ValueError, KeyError, IndexError):
        return None


def resolve_roots() -> tuple[list[tuple[str, Graph]], dict]:
    roots: list[tuple[str, Graph]] = []
    for name in QUOTIENT_SEEDS:
        path = Path("results/graphs/quotient-r1") / f"{name}.graph.json"
        fallback = Path("results/candidates") / name / f"{name}.graph.json"
        active = path if path.exists() else fallback
        data = json.loads(active.read_text(encoding="utf-8"))
        metadata = dict(data.get("metadata") or {})
        metadata["root_kind"] = "known_delta11_quotient_seed"
        metadata["source_path"] = str(active)
        roots.append((name, Graph.from_json({**data, "metadata": metadata})))

    reconstructed = benchmark_graphs()
    benchmark_count = 0
    for name in BENCHMARK_NAMES:
        graph = reconstructed[name]
        metadata = dict(graph.metadata)
        metadata["root_kind"] = "reconstructed_benchmark"
        roots.append(
            (name, Graph(graph.vertices, graph.edges, graph.bipartition, metadata))
        )
        benchmark_count += 1
    return roots, {
        "quotient_seeds": list(QUOTIENT_SEEDS),
        "reconstructed_benchmarks": list(BENCHMARK_NAMES),
        "resolved_benchmark_count": benchmark_count,
    }


def applicable_hub(seed: Graph) -> str | None:
    degree_eleven = [vertex for vertex, degree in seed.degrees.items() if degree == 11]
    if len(degree_eleven) != 1:
        return None
    return degree_eleven[0]


def enumerate_root_candidates(
    root_name: str,
    seed: Graph,
) -> Iterable[Candidate]:
    hub = applicable_hub(seed)
    if hub is None:
        return
    neighbors = sorted(next_vertex for next_vertex, _ in seed.adjacency()[hub])
    serial = 0
    for selected_count in (1, 2):
        for selected_neighbors in itertools.combinations(neighbors, selected_count):
            ports = boundary_ports(seed, hub, selected_neighbors)
            terminal_count = sum(len(values) for values in ports.values())
            for base_tree in replacement_trees(terminal_count):
                templates = [base_tree]
                tree_edge_set = set(base_tree["edges"])
                for chord in chord_options(base_tree):
                    if chord in tree_edge_set:
                        continue
                    multitree = {
                        **base_tree,
                        "rule": "rooted-height-3-bipartite-multitree",
                        "non_tree_chords": [chord],
                    }
                    templates.append(multitree)
                for template in templates:
                    serial += 1
                    graph = graft_graph(seed, hub, selected_neighbors, template)
                    if graph is None:
                        continue
                    operation = {
                        "hub": hub,
                        "removed_connectors": list(selected_neighbors),
                        "boundary_ports": ports,
                        **template,
                    }
                    yield Candidate(
                        f"{root_name}-G{serial:06d}",
                        root_name,
                        graph,
                        "",
                        {},
                        operation,
                    )


def operation_summary(operation: dict) -> dict:
    return {
        "rule": operation["rule"],
        "hub": operation["hub"],
        "removed_connectors": operation["removed_connectors"],
        "boundary_port_count": len(operation["boundary_ports"]),
        "core_terminal_count": operation["core_terminal_count"],
        "tree_new_vertices": operation["tree_new_vertices"],
        "non_tree_chords": operation["non_tree_chords"],
    }


def confirm_negative(
    graph: Graph,
    time_limit: float,
    workers: int,
) -> tuple[bool, bool, dict[int, str]]:
    statuses: dict[int, str] = {}
    for span in range(graph.delta, max(graph.delta, graph.n - 1) + 1):
        status, _ = fixed_span_sat_solve(graph, span, time_limit, workers)
        statuses[span] = status
        if status in ("OPTIMAL", "FEASIBLE"):
            return False, False, statuses
    confirmed = all(status == "INFEASIBLE" for status in statuses.values())
    unresolved = any(status == "UNKNOWN" for status in statuses.values())
    return confirmed and not unresolved, unresolved, statuses


def make_report(
    configuration: dict,
    roots: Sequence[tuple[str, Graph]],
    summaries: Sequence[dict],
    records: Sequence[dict],
    negatives: Sequence[dict],
    elapsed_seconds: float,
    state: RunState,
) -> dict:
    totals = {key: sum(int(item.get(key, 0)) for item in summaries) for key in (
        "constructions_attempted",
        "generated",
        "rejected_disconnected",
        "rejected_low_degree",
        "rejected_degree_cap",
        "rejected_invalid",
        "duplicate",
        "unique",
        "high_margin_unique",
        "classified",
        "colorable",
        "non_colorable",
        "timeout",
        "primary_negative_candidates",
        "confirmed_non_colorable",
        "independent_unresolved",
        "unclassified_deadline",
        "selected_for_classification",
    )}
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
        "high_margin_unique": totals["high_margin_unique"],
        "selected_for_classification": totals["selected_for_classification"],
        "unclassified_deadline": totals["unclassified_deadline"],
    }
    completion = {
        "bounded_generation_complete": state.bounded_generation_complete,
        "all_selected_unique_classified": totals["classified"] == totals["selected_for_classification"] and not totals["unclassified_deadline"],
        "classification_complete": totals["classified"] == totals["unique"],
        "independent_confirmation_complete": state.independent_confirmation_complete,
        "runtime_deadline_hit": state.runtime_deadline_hit,
        "stop_reason": state.stop_reason,
        "complete": (
            state.bounded_generation_complete
            and totals["classified"] == totals["selected_for_classification"]
            and totals["unclassified_deadline"] == 0
            and state.independent_confirmation_complete
            and not state.runtime_deadline_hit
        ),
    }
    return {
        "schema_version": 1,
        "configuration": configuration,
        "operation_definition": {
            "neighborhood_rule": (
                "remove one or two vertices adjacent to the unique degree-11 hub; "
                "the retained hub plus every surviving core endpoint of those removed "
                "connectors forms the replacement boundary"
            ),
            "replacement_rule": (
                "graft one deterministic connected bipartite replacement spanning the "
                "boundary; enumerate every rooted tree of height at most three, then add "
                "at most one non-tree chord to form a multitree"
            ),
            "bounds": {
                "replacement_diameter_at_most": MAXIMUM_GRAFT_DIAMETER,
                "new_vertices_at_most": MAX_NEW_VERTICES,
                "final_delta_at_most": MAXIMUM_DELTA,
                "minimum_final_degree": MINIMUM_FINAL_DEGREE,
                "replacement_endpoint_minimum_degree_after_graft": MINIMUM_FINAL_DEGREE,
            },
            "deduplication": "bipartition-colored Nauty certificate SHA-256 via nauty_canonical_hash",
            "ranking_key": (
                "-hub_best_margin, -1.5/-2.5 margin tiers, normalized degree variance, "
                "-sufficient-obstruction count, -top-three margin sum, canonical hash"
            ),
            "classification": (
                "rank_potential_solve CP-SAT only for unique candidates with hub_best_margin "
                ">= -2.5; at most 5 seconds and 4 workers per solve"
            ),
            "negative_confirmation": (
                "fixed_span_sat_solve CP-SAT over every legal span delta through n-1; only "
                "all-INFEASIBLE evidence is non-colorable"
            ),
            "timeout_policy": (
                "primary or independent UNKNOWN remains unresolved, counts once as timeout, "
                "and never counts as non-colorable"
            ),
            "graph_serialization_policy": (
                "full graph JSON appears only inside confirmed_negatives"
            ),
        },
        "roots": [
            {
                "name": name,
                "order": graph.n,
                "size": graph.m,
                "maximum_degree": graph.delta,
                "degree_11_hub": applicable_hub(graph),
            }
            for name, graph in roots
        ],
        "deadline_seconds": configuration["runtime_hard_deadline_seconds"],
        "elapsed_seconds": elapsed_seconds,
        "counts": counts,
        "completion": completion,
        "complete": completion["complete"],
        "negative_events": [
            {key: value for key, value in record.items() if key != "graph"}
            for record in negatives
        ],
        "summaries": list(summaries),
        "records": list(records),
        "confirmed_negatives": list(negatives),
    }


def search_root(
    root_name: str,
    root_graph: Graph,
    args,
    deadline: float,
    global_seen: set[str],
    construction_offset: int,
    completed_summaries: list[dict],
    records: list[dict],
    negatives: list[dict],
    state: RunState,
    output_path: Path,
    started: float,
) -> dict:
    counters = Counters()
    candidates: list[Candidate] = []
    if any(degree > MAXIMUM_DELTA and degree != 11 for degree in root_graph.degrees.values()):
        state.stop_reason = "unsupported-parent-overcap-vertex"
        return {
            "root": root_name,
            "constructions_attempted": 0,
            "generated": 0,
            "unique": 0,
            "classified": 0,
            "selected_for_classification": 0,
            "unclassified_deadline": 0,
            "classification_complete": True,
            "independent_confirmation_complete": True,
            "complete": False,
            "elapsed_seconds": 0.0,
        }
    for candidate in enumerate_root_candidates(root_name, root_graph):
        counters.constructions_attempted += 1
        graph = candidate.graph
        if graph is None:
            continue
        counters.generated += 1
        if graph.delta > MAXIMUM_DELTA:
            counters.rejected_degree_cap += 1
            continue
        if min(graph.degrees.values()) < MINIMUM_FINAL_DEGREE:
            counters.rejected_low_degree += 1
            continue
        digest = nauty_canonical_hash(graph)
        if digest in global_seen:
            counters.duplicate += 1
            continue
        global_seen.add(digest)
        counters.unique += 1
        features = ranking_features(graph)
        candidates.append(
            Candidate(
                f"NGD10-{construction_offset + counters.unique:05d}",
                root_name,
                graph,
                digest,
                features,
                candidate.operation,
            )
        )

    # The focused run classifies every accepted unique graph.  The high-margin
    # count remains as the stricter structural-screen diagnostic.
    selected = list(candidates)
    counters.high_margin_unique = len(selected)
    selected.sort(key=ranking_key)
    counters.selected_for_classification = len(selected)

    def checkpoint(latest_record: dict | None = None) -> None:
        summary = {
            **vars(counters).copy(),
            "root": root_name,
            "classification_complete": False,
            "independent_confirmation_complete": state.independent_confirmation_complete,
        }
        report = make_report(
            configuration(args),
            [],
            [*completed_summaries, summary],
            ([*records, latest_record] if latest_record else records)[-max(0, args.checkpoint_record_window):],
            negatives,
            time.monotonic() - started,
            state,
        )
        report["checkpoint"] = {"partial": True}
        atomic_write_json(output_path, report)

    for number, candidate in enumerate(selected, start=1):
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            counters.unclassified_deadline += len(selected) - number + 1
            state.runtime_deadline_hit = True
            state.stop_reason = "classification-deadline"
            break
        primary = rank_potential_solve(
            candidate.graph,
            max(0.01, min(args.primary_time_limit, remaining)),
            args.workers,
        )
        record = {
            "candidate_id": candidate.construction_id,
            "parent": candidate.parent,
            "canonical_sha256": candidate.digest,
            "order": candidate.graph.n,
            "size": candidate.graph.m,
            "delta": candidate.graph.delta,
            "minimum_degree": min(candidate.graph.degrees.values()),
            "operation": operation_summary(candidate.operation),
            "ranking": candidate.features,
            "primary_result": {
                key: value for key, value in primary.__dict__.items() if key != "coloring"
            },
        }
        counters.classified += 1
        if primary.status == "colorable":
            counters.colorable += 1
            record["decision"] = "colorable"
        elif primary.status == "timeout":
            counters.timeout += 1
            record["decision"] = "timeout"
            record["independent_confirmation"] = {
                "required": False,
                "reason": "primary timeout remains unresolved",
            }
        elif primary.status == "non-colorable":
            counters.primary_negative_candidates += 1
            confirmed, unresolved, statuses = confirm_negative(
                candidate.graph,
                max(0.01, min(args.independent_time_limit, remaining)),
                args.workers,
            )
            record["independent_confirmation"] = {
                "encoding": "fixed-span-cpsat",
                "spans_checked": sorted(statuses),
                "span_statuses": {str(span): status for span, status in sorted(statuses.items())},
                "confirmed_non_colorable": confirmed,
                "unresolved": unresolved,
            }
            if unresolved:
                counters.independent_unresolved += 1
                counters.timeout += 1
                record["decision"] = "timeout"
                state.independent_confirmation_complete = False
            elif not confirmed:
                raise AssertionError("primary negative contradicted by independent encoding")
            else:
                counters.non_colorable += 1
                counters.confirmed_non_colorable += 1
                record["decision"] = "non-colorable"
                candidate.graph.metadata["certification"] = record["independent_confirmation"]
                negatives.append({**record, "graph": candidate.graph.to_json()})
        else:
            raise AssertionError(f"unexpected primary status {primary.status}")
        records.append(record)
        checkpoint(record)

    classification_complete = (
        counters.classified == len(selected) and counters.unclassified_deadline == 0
    )
    return {
        **vars(counters).copy(),
        "root": root_name,
        "classification_complete": classification_complete,
        "independent_confirmation_complete": state.independent_confirmation_complete,
        "complete": classification_complete and state.independent_confirmation_complete,
        "elapsed_seconds": time.monotonic() - (deadline - args.deadline_seconds),
    }


def configuration(args) -> dict:
    return {
        "starting_graphs": [*QUOTIENT_SEEDS, *BENCHMARK_NAMES],
        "maximum_final_delta": MAXIMUM_DELTA,
        "minimum_final_degree": MINIMUM_FINAL_DEGREE,
        "replacement_diameter_at_most": MAXIMUM_GRAFT_DIAMETER,
        "new_vertices_at_most": MAX_NEW_VERTICES,
        "solver_workers": args.workers,
        "primary_time_limit_seconds": args.primary_time_limit,
        "independent_time_limit_seconds": args.independent_time_limit,
        "runtime_hard_deadline_seconds": args.deadline_seconds,
        "checkpoint_record_window": args.checkpoint_record_window,
        "atomic_checkpoint_after_every_solve": True,
        "smoke_run": args.smoke,
    }


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-time-limit", type=float, default=5.0)
    parser.add_argument("--independent-time-limit", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--deadline-seconds", type=float, default=10800.0)
    parser.add_argument("--checkpoint-record-window", type=int, default=256)
    parser.add_argument("--output", type=Path, default=Path("results/neighborhood-graft-delta10.json"))
    parser.add_argument("--smoke", action="store_true")
    return parser


def validate_arguments(args) -> None:
    if not 1 <= args.workers <= MAX_SOLVER_WORKERS:
        raise SystemExit(f"workers must be between 1 and {MAX_SOLVER_WORKERS}")
    if not 0 < args.primary_time_limit <= MAX_SOLVER_SECONDS:
        raise SystemExit("primary time limit must be in (0, 5]")
    if not 0 < args.independent_time_limit <= MAX_SOLVER_SECONDS:
        raise SystemExit("independent time limit must be in (0, 5]")
    if not 0 < args.deadline_seconds <= MAX_RUNTIME_SECONDS:
        raise SystemExit("deadline seconds must be in (0, 10800]")


def main() -> None:
    args = argument_parser().parse_args()
    validate_arguments(args)
    if args.smoke:
        args.output = Path("results/neighborhood-graft-delta10-smoke.json")
        args.primary_time_limit = min(args.primary_time_limit, 0.25)
        args.independent_time_limit = min(args.independent_time_limit, 0.25)
        args.workers = min(args.workers, 2)
        args.deadline_seconds = min(args.deadline_seconds, 180.0)
        args.checkpoint_record_window = min(args.checkpoint_record_window, 16)

    started = time.monotonic()
    deadline = started + args.deadline_seconds
    roots, seed_resolution = resolve_roots()
    active_configuration = configuration(args)
    active_configuration["seed_resolution"] = seed_resolution
    global_seen: set[str] = set()
    summaries: list[dict] = []
    records: list[dict] = []
    negatives: list[dict] = []
    state = RunState()
    construction_offset = 0

    for root_name, root_graph in roots:
        if time.monotonic() >= deadline - 1.0:
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
            records,
            negatives,
            state,
            args.output,
            started,
        )
        summaries.append(summary)
        construction_offset += summary["constructions_attempted"]

    report = make_report(
        active_configuration,
        roots,
        summaries,
        records[-args.checkpoint_record_window:] if args.checkpoint_record_window else records,
        negatives,
        time.monotonic() - started,
        state,
    )
    atomic_write_json(args.output, report)


if __name__ == "__main__":
    main()
