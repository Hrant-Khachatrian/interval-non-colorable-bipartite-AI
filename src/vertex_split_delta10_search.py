#!/usr/bin/env python3
"""Corrected same-side vertex-splitting search bounded at maximum degree 10."""

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
    best_margin = max((row["margin"] for row in statistics), default=-10**9)
    return {
        "hub_best_margin": best_margin,
        "hub_best_margin_tier_at_least_minus_1_5": bool(best_margin >= -1.5),
        "normalized_degree_variance": normalized_degree_variance(graph),
        "weighted_hubs_best": statistics[:3],
    }


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
    return sorted(options, key=lambda item: (-item[0], item[1:], item))


def apply_same_side_split(
    base: Graph,
    hub: str,
    part_sizes: Sequence[int],
    construction_id: int,
    operation_index: int,
    maximum_delta: int,
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

    boundaries = list(itertools.accumulate(part_sizes))
    assignment: list[list[tuple[str, str]]] = [[] for _ in part_sizes]
    for position, (_, edge) in enumerate(incident):
        part_number = next(
            index for index, boundary in enumerate(boundaries) if position < boundary
        )
        assignment[part_number].append(edge)

    side_index = 0 if hub in base.bipartition[0] else 1
    prefix = f"VS{construction_id:05d}O{operation_index:02d}"
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
        }
    )
    metadata = {
        **base.metadata,
        "lane": "corrected-same-side-vertex-split-delta10",
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
) -> tuple[list[tuple[str, Graph]], bool]:
    """Apply bounded split choices to each initial over-cap vertex in order."""
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
                construction_id = construction_offset + counters.constructions_attempted
                counters.constructions_attempted += 1
                candidate, reason = apply_same_side_split(
                    previous_graph,
                    hub,
                    part_sizes,
                    construction_id,
                    stage,
                    maximum_delta,
                )
                if candidate is None:
                    if reason == "degree cap exceeded":
                        counters.rejected_degree_cap += 1
                    continue
                valid, validation_reason = validate_structure(candidate, maximum_delta)
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
                following.append((previous_name, candidate))
        current = following
    return current, True


def confirm_negative(
    graph: Graph,
    time_limit: float,
    workers: int,
    deadline: float,
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
        max(0.01, min(primary_time_limit, 8.0, remaining - reserve)),
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
        raise AssertionError(f"unexpected primary status {primary.status}")

    counters.primary_negative_candidates += 1
    confirmed, unresolved, span_statuses = confirm_negative(
        graph,
        max(0.01, min(independent_time_limit, remaining - 1.0)),
        workers,
        deadline - 3.0,
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
) -> tuple[dict, list[dict], list[dict]]:
    started = time.monotonic()
    counters = Counters()
    rows: list[dict] = []
    negative_events: list[dict] = []

    expanded, enumeration_finished = expand_root(
        parent_name,
        graph,
        maximum_delta,
        construction_offset,
        state,
        counters,
        deadline,
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
            -item[3]["hub_best_margin"],
            -int(item[3]["hub_best_margin_tier_at_least_minus_1_5"]),
            item[3]["normalized_degree_variance"],
            item[0],
        )
    )

    selected = candidates if candidate_cap <= 0 else candidates[:candidate_cap]
    if candidate_cap > 0 and len(candidates) > candidate_cap:
        state.classification_complete = False
    for number, (digest, previous_name, candidate, features) in enumerate(selected, start=1):
        candidate_id = f"VSD10-{number:04d}-{parent_name}"
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
                "connectivity is checked after every split",
                "final maximum degree is at most 10",
                "final minimum degree is at least 2",
            ],
            "search_family": (
                "all unordered balanced and unbalanced two-way degree partitions; "
                "selected deterministic balanced, small-part, and extreme three-way "
                "partitions; over-cap vertices proceed by descending degree then name"
            ),
            "deduplication": (
                "Graph.to_json field sha256_bipartition_canonical from "
                "nauty_canonical_hash"
            ),
            "ranking_key": (
                "hub_best_margin descending, hub_best_margin >= -1.5 tier descending, "
                "normalized degree variance ascending, canonical hash final tie-break"
            ),
            "classification": (
                "rank_potential_solve CP-SAT, maximum 8 seconds and 8 workers"
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
        "--output", type=Path, default=Path("results/vertex-split-delta10.json")
    )
    parser.add_argument("--maximum-final-delta", type=int, default=10)
    parser.add_argument("--candidate-cap-per-parent", type=int, default=48)
    parser.add_argument("--primary-time-limit", type=float, default=8.0)
    parser.add_argument("--independent-time-limit", type=float, default=8.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--deadline-seconds", type=float, default=3600.0)
    parser.add_argument("--smoke", action="store_true")
    return parser


def main() -> None:
    args = argument_parser().parse_args()
    if args.maximum_final_delta != 10:
        raise SystemExit("this corrected lane requires --maximum-final-delta 10")
    if not 1 <= args.workers <= 8:
        raise SystemExit("workers must be between 1 and 8")
    if not 0 < args.primary_time_limit <= 8.0:
        raise SystemExit("primary time limit must be positive and at most 8 seconds")
    if args.deadline_seconds <= 0 or args.deadline_seconds > 3600.0:
        raise SystemExit("deadline must be positive and at most 3600 seconds")

    if args.smoke:
        args.output = Path("results/vertex-split-delta10-smoke.json")
        args.candidate_cap_per_parent = min(args.candidate_cap_per_parent, 2)
        args.primary_time_limit = min(args.primary_time_limit, 0.5)
        args.independent_time_limit = min(args.independent_time_limit, 0.5)
        args.deadline_seconds = min(args.deadline_seconds, 120.0)

    run_started_wall = time.time()
    run_started = time.monotonic()
    deadline = run_started + args.deadline_seconds
    output_dir = args.output.parent / "graphs" / "vertex-split-delta10"
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
