#!/usr/bin/env python3
"""Bounded ordinary edge-subdivision search from Delta >= 11 benchmarks."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import networkx as nx
import pynauty

from interval_edge_coloring import (
    Graph,
    all_spans_solve,
    benchmark_graphs,
    rank_potential_solve,
)


@dataclass
class SearchGraph:
    """The Graph fields needed by the exact oracles, without a bipartition."""

    vertices: tuple[str, ...]
    edges: list[tuple[str, str]]
    metadata: dict

    def __post_init__(self) -> None:
        self.vertex_set = set(self.vertices)
        graph = nx.Graph(self.edges)
        graph.add_nodes_from(self.vertices)
        self._nx = graph

    @property
    def n(self) -> int:
        return len(self.vertices)

    @property
    def m(self) -> int:
        return len(self.edges)

    @property
    def degrees(self) -> dict[str, int]:
        return dict(self._nx.degree())

    @property
    def delta(self) -> int:
        return max(self.degrees.values(), default=0)

    def adjacency(self) -> dict[str, list[tuple[str, str]]]:
        result = {vertex: [] for vertex in self.vertices}
        for edge in self.edges:
            u, v = edge
            result[u].append((v, edge))
            result[v].append((u, edge))
        return result

    def labelled_hash(self) -> str:
        payload = json.dumps(
            {
                "vertices": list(self.vertices),
                "edges": [list(edge) for edge in sorted(self.edges)],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def to_json(self, canonical_hash: str) -> dict:
        return {
            "schema_version": 1,
            "vertices": list(self.vertices),
            "edges": [list(edge) for edge in sorted(self.edges)],
            "bipartition": None,
            "metadata": self.metadata,
            "sha256_labelled": self.labelled_hash(),
            "sha256_nauty_uncolored": canonical_hash,
        }

    def save(self, path: str | Path, canonical_hash: str) -> None:
        Path(path).write_text(
            json.dumps(self.to_json(canonical_hash), indent=2) + "\n", encoding="utf-8"
        )


def from_benchmark(graph: Graph) -> SearchGraph:
    metadata = dict(graph.metadata)
    metadata.setdefault("benchmark_name", graph.metadata.get("name"))
    return SearchGraph(
        graph.vertices,
        [(str(u), str(v)) for u, v in graph.edges],
        metadata,
    )


def uncolored_nauty_hash(graph: SearchGraph) -> str:
    index = {vertex: number for number, vertex in enumerate(graph.vertices)}
    adjacency = {
        number: [index[neighbor] for neighbor in graph._nx[vertex]]
        for vertex, number in index.items()
    }
    nauty_graph = pynauty.Graph(
        number_of_vertices=graph.n,
        directed=False,
        adjacency_dict=adjacency,
    )
    digest = hashlib.sha256(pynauty.certificate(nauty_graph)).hexdigest()
    return f"{graph.n}:{digest}"


def subdivide(
    base: SearchGraph, assignment: frozenset[tuple[int, int]]
) -> SearchGraph:
    vertices = list(base.vertices)
    edges: list[tuple[str, str]] = []
    replacements = dict(assignment)
    selected_metadata = []
    for number, (u, v) in enumerate(base.edges):
        if number not in replacements:
            edges.append((u, v))
            continue
        new_vertices = replacements[number]
        if new_vertices < 1:
            raise ValueError("subdivision path must contain at least one new vertex")
        names = [f"SD{number}_{position}" for position in range(new_vertices)]
        vertices.extend(names)
        path = [u, *names, v]
        edges.extend(zip(path, path[1:]))
        selected_metadata.append(
            {
                "index": number,
                "endpoints": [u, v],
                "new_vertices": new_vertices,
                "path_length": new_vertices + 1,
            }
        )
    metadata = {
        **base.metadata,
        "lane": "partial-subdivision-delta10",
        "parent": base.metadata.get("name", base.metadata.get("family")),
        "subdivision_depth": 1,
        "selected_parent_edges": [
            item
            for _, item in sorted(
                (item["index"], item) for item in selected_metadata
            )
        ],
    }
    return SearchGraph(tuple(vertices), edges, metadata)


def reducing_edge_sets(base: SearchGraph, cap: int) -> Iterator[frozenset[int]]:
    """Yield edge sets, by increasing size, that meet reduction demands."""

    demands = {
        vertex: max(0, degree - cap)
        for vertex, degree in base.degrees.items()
        if degree > cap
    }
    minimum = max(demands.values(), default=0)
    for size in range(minimum, len(base.edges) + 1):
        for selected in itertools.combinations(range(len(base.edges)), size):
            residual = dict(demands)
            for number in selected:
                u, v = base.edges[number]
                if u in residual:
                    residual[u] -= 1
                if v in residual:
                    residual[v] -= 1
            if all(value <= 0 for value in residual.values()):
                yield frozenset(selected)


def targeted_configurations(
    base: SearchGraph, cap: int
) -> Iterator[frozenset[tuple[int, int]]]:
    for selected in reducing_edge_sets(base, cap):
        yield frozenset((number, 2) for number in selected)


def satisfies_degree_cap(
    base: SearchGraph,
    assignment: frozenset[tuple[int, int]],
    maximum_final_delta: int,
) -> bool:
    # Ordinary edge subdivision preserves the degree of every original
    # endpoint, so no assignment can repair a parent above the degree cap.
    return all(
        degree <= maximum_final_delta for degree in base.degrees.values()
    )


def _legacy_reduction_check(
    base: SearchGraph,
    assignment: frozenset[tuple[int, int]],
    maximum_final_delta: int,
) -> bool:
    reductions = {vertex: 0 for vertex in base.vertices}
    for number, new_vertices in assignment:
        if new_vertices < 2:
            return False
        u, v = base.edges[number]
        reductions[u] += 1
        reductions[v] += 1
        if u == v:
            raise ValueError("ordinary subdivision requires a simple parent edge")
    return all(
        degree - reductions[vertex] <= maximum_final_delta
        for vertex, degree in base.degrees.items()
    )


def general_configurations(
    base: SearchGraph,
    cap: int,
    maximum_new_vertices_per_edge: int,
    maximum_total_new_vertices: int,
) -> Iterator[frozenset[tuple[int, int]]]:
    """Yield nonminimal path-length assignments within explicit bounds."""

    if maximum_new_vertices_per_edge < 2:
        raise ValueError(
            "path replacements need at least two new vertices"
        )
    lengths = range(2, maximum_new_vertices_per_edge + 1)
    for selected in reducing_edge_sets(base, cap):
        for new_vertex_counts in itertools.product(lengths, repeat=len(selected)):
            if sum(new_vertex_counts) > maximum_total_new_vertices:
                continue
            yield frozenset(zip(sorted(selected), new_vertex_counts))


def independent_confirmation(
    graph: SearchGraph, time_limit: float, workers: int
) -> tuple[bool, dict]:
    result = all_spans_solve(graph, time_limit, workers, stop_on_timeout=False)
    statuses = [span["status"] for span in result["spans"].values()]
    certified = bool(statuses) and all(status == "INFEASIBLE" for status in statuses)
    return certified, result


def classify_benchmark(
    name: str,
    base: SearchGraph,
    output_dir: Path,
    candidate_cap: int,
    maximum_final_delta: int,
    maximum_new_vertices_per_edge: int,
    maximum_total_new_vertices: int,
    time_limit: float,
    workers: int,
) -> tuple[dict, list[dict]]:
    started = time.perf_counter()
    if candidate_cap <= 0:
        raise ValueError("candidate_cap must be positive")

    if base.delta > maximum_final_delta:
        summary = {
            "benchmark": name,
            "parent_order": base.n,
            "parent_size": base.m,
            "parent_delta": base.delta,
            "degree_reduction_required_per_vertex": {},
            "generated": 0,
            "generated_targeted": 0,
            "generated_general": 0,
            "unique": 0,
            "rejected_disconnected": 0,
            "colorable": 0,
            "non_colorable": 0,
            "primary_non_colorable": 0,
            "confirmed_non_colorable": 0,
            "primary_timeout": 0,
            "independent_unresolved": 0,
            "oracle_conflicts": 0,
            "targeted_configurations_complete": True,
            "general_bounded_family_complete": True,
            "generation_complete": True,
            "classification_complete": True,
            "run_complete": True,
            "candidate_cap_reached": False,
            "minimum_required_subdivisions": None,
            "maximum_new_vertices_per_edge": maximum_new_vertices_per_edge,
            "maximum_total_new_vertices": maximum_total_new_vertices,
            "elapsed_seconds": time.perf_counter() - started,
            "negative_events": [],
            "empty_search_space_reason": (
                "ordinary edge subdivision preserves every original endpoint "
                f"degree; parent delta {base.delta} exceeds "
                f"{maximum_final_delta}"
            ),
        }
        return summary, []

    seen: set[str] = set()
    records: list[dict] = []
    negatives: list[dict] = []
    targeted_generated = 0
    targeted_unique = 0
    targeted_complete = True
    general_generated = 0
    general_unique = 0
    general_complete = True
    rejected_disconnected = 0
    negative_sequence = 0
    cap_reached = False

    def process(
        assignment: frozenset[tuple[int, int]],
        phase: str,
        generated_counter: dict[str, int],
    ) -> bool:
        nonlocal negative_sequence, rejected_disconnected
        if not satisfies_degree_cap(base, assignment, maximum_final_delta):
            raise AssertionError(
                f"degree-cap invariant failed for {sorted(assignment)}"
            )
        graph = subdivide(base, assignment)
        if not nx.is_connected(graph._nx):
            rejected_disconnected += 1
            return False
        generated_counter[phase] += 1
        digest = uncolored_nauty_hash(graph)
        if digest in seen:
            return False
        seen.add(digest)
        primary = rank_potential_solve(graph, time_limit, workers)
        row = {
            "phase": phase,
            "selected_parent_edges": sorted(assignment),
            "canonical_sha256": digest,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "primary_status": primary.status,
            "primary_span": primary.span,
            "primary_elapsed_seconds": primary.elapsed_seconds,
        }
        if primary.status == "non-colorable":
            confirmed, independent = independent_confirmation(graph, time_limit, workers)
            independent_statuses = [
                value["status"] for value in independent["spans"].values()
            ]
            row["independent_unresolved"] = "UNKNOWN" in independent_statuses
            row["independent_decision"] = independent["decision"]
            row["independent_spans"] = {
                str(span): value["status"] for span, value in independent["spans"].items()
            }
            row["certified_non_colorable"] = confirmed
            if confirmed:
                negative_sequence += 1
                candidate_id = f"SD10-{name}-{negative_sequence:04d}"
                path = output_dir / f"{candidate_id}.graph.json"
                graph.metadata["candidate_id"] = candidate_id
                graph.save(path, digest)
                event = {
                    "event": "certified_negative",
                    "candidate_id": candidate_id,
                    "path": str(path),
                    "parent": name,
                    "order": graph.n,
                    "size": graph.m,
                    "delta": graph.delta,
                    "canonical_sha256": digest,
                }
                negatives.append(
                    {
                        **event,
                        "selected_parent_edges": row["selected_parent_edges"],
                    }
                )
                print(json.dumps(event, sort_keys=True), flush=True)
        print(
            json.dumps(
                {
                    "event": "candidate_classified",
                    "benchmark": name,
                    "phase": phase,
                    "selected_parent_edges": sorted(assignment),
                    "primary_status": primary.status,
                    "certified_non_colorable": row.get(
                        "certified_non_colorable", False
                    ),
                    "elapsed_seconds": primary.elapsed_seconds,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        records.append(row)
        return True

    def run_bounded_phase(
        configurations: Iterator[frozenset[tuple[int, int]]],
        phase: str,
    ) -> tuple[int, int, bool, bool]:
        generated_count = 0
        unique_count = 0
        pending: frozenset[tuple[int, int]] | None = None
        while True:
            try:
                item = pending if pending is not None else next(configurations)
            except StopIteration:
                return generated_count, unique_count, True, False
            pending = None
            assignment = item
            generated_count += 1
            if process(assignment, phase, generated):
                unique_count += 1
            if unique_count < candidate_cap:
                continue
            try:
                pending = next(configurations)
            except StopIteration:
                return generated_count, unique_count, True, False
            return generated_count, unique_count, False, True

    generated = {"targeted": 0, "general": 0}
    targeted_generated, targeted_unique, targeted_complete, cap_reached = (
        run_bounded_phase(
            targeted_configurations(base, maximum_final_delta), "targeted"
        )
    )
    generated["targeted"] = targeted_generated

    general_generated = general_unique = 0
    general_complete = True
    if not cap_reached:
        (
            general_generated,
            general_unique,
            general_complete,
            cap_reached,
        ) = run_bounded_phase(
            general_configurations(
                base,
                maximum_final_delta,
                maximum_new_vertices_per_edge,
                maximum_total_new_vertices,
            ),
            "general",
        )
        generated["general"] = general_generated

    counts = {
        "colorable": sum(row["primary_status"] == "colorable" for row in records),
        "non_colorable": sum(
            row["primary_status"] == "non-colorable" for row in records
        ),
        "primary_non_colorable": sum(
            row["primary_status"] == "non-colorable" for row in records
        ),
        "confirmed_non_colorable": sum(
            row.get("certified_non_colorable", False) for row in records
        ),
        "primary_timeout": sum(row["primary_status"] == "timeout" for row in records),
        "independent_unresolved": sum(
            row.get("independent_unresolved", False) for row in records
        ),
        "oracle_conflicts": sum(
            row.get("independent_decision") == "colorable"
            or (
                row.get("certified_non_colorable") is not None
                and not row["certified_non_colorable"]
                and not row["independent_unresolved"]
            )
            for row in records
        ),
    }
    summary = {
        "benchmark": name,
        "parent_order": base.n,
        "parent_size": base.m,
        "parent_delta": base.delta,
        "degree_reduction_required_per_vertex": {
            vertex: max(0, degree - maximum_final_delta)
            for vertex, degree in base.degrees.items()
            if degree > maximum_final_delta
        },
        "generated": generated["targeted"] + generated["general"],
        "generated_targeted": generated["targeted"],
        "generated_general": generated["general"],
        "unique": len(seen),
        "rejected_disconnected": rejected_disconnected,
        **counts,
        "targeted_configurations_complete": targeted_complete,
        "general_bounded_family_complete": general_complete,
        "generation_complete": (
            (targeted_complete or cap_reached) and general_complete
        ),
        "classification_complete": (
            counts["primary_timeout"] == 0
            and counts["independent_unresolved"] == 0
            and counts["oracle_conflicts"] == 0
        ),
        "run_complete": (
            (targeted_complete or cap_reached)
            and general_complete
            and counts["primary_timeout"] == 0
            and counts["independent_unresolved"] == 0
            and counts["oracle_conflicts"] == 0
        ),
        "candidate_cap_reached": cap_reached,
        "minimum_required_subdivisions": max(
            (
                max(0, degree - maximum_final_delta)
                for degree in base.degrees.values()
                if degree > maximum_final_delta
            ),
            default=0,
        ),
        "maximum_new_vertices_per_edge": maximum_new_vertices_per_edge,
        "maximum_total_new_vertices": maximum_total_new_vertices,
        "elapsed_seconds": time.perf_counter() - started,
        "negative_events": negatives,
    }
    return summary, records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maximum-final-delta", type=int, default=10)
    parser.add_argument(
        "--maximum-new-vertices-per-edge", type=int, default=6
    )
    parser.add_argument(
        "--maximum-total-new-vertices", type=int, default=24
    )
    parser.add_argument("--candidate-cap", type=int, default=3500)
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", default="results/subdivision-delta10.json")
    args = parser.parse_args()

    if args.maximum_final_delta < 1:
        parser.error("--maximum-final-delta must be positive")
    if args.candidate_cap <= 0:
        parser.error("--candidate-cap must be positive")
    if args.maximum_new_vertices_per_edge < 2:
        parser.error("--maximum-new-vertices-per-edge must be at least 2")
    if args.maximum_total_new_vertices < args.maximum_new_vertices_per_edge:
        parser.error("--maximum-total-new-vertices is too small")
    if args.time_limit <= 0 or args.workers <= 0:
        parser.error("--time-limit and --workers must be positive")

    output_path = Path(args.output)
    graph_dir = output_path.parent / "graphs" / "subdivision_delta10"
    graph_dir.mkdir(parents=True, exist_ok=True)
    run_started = time.perf_counter()
    stale_artifact = Path(
        "results/graphs/subdivision_delta10/SD10-M5_delta_555-0001.graph.json"
    )
    graph_dir_contents_exist = any(graph_dir.glob("*.graph.json"))
    stale_artifact_removed = not (
        stale_artifact.exists() or graph_dir_contents_exist
    )
    if stale_artifact.exists():
        stale_artifact.unlink()
    summaries = []
    all_records = []
    for name, benchmark in benchmark_graphs().items():
        if benchmark.delta <= args.maximum_final_delta:
            continue
        base = from_benchmark(benchmark)
        print(json.dumps({"event": "benchmark_start", "benchmark": name}), flush=True)
        summary, records = classify_benchmark(
            name,
            base,
            graph_dir,
            candidate_cap=args.candidate_cap,
            maximum_final_delta=args.maximum_final_delta,
            maximum_new_vertices_per_edge=args.maximum_new_vertices_per_edge,
            maximum_total_new_vertices=args.maximum_total_new_vertices,
            time_limit=args.time_limit,
            workers=args.workers,
        )
        summaries.append(summary)
        all_records.extend({"benchmark": name, **record} for record in records)
        compact = {key: value for key, value in summary.items() if key != "negative_events"}
        print(json.dumps({"event": "benchmark_complete", **compact}, sort_keys=True), flush=True)
        output_path.write_text(
            json.dumps(
                {
                    "configuration": {
                        "subdivision": (
                            "ordinary edge subdivision by a path; original "
                            "endpoint degrees are invariant, so parents above "
                            "the degree cap have an empty legal search space"
                        ),
                        "subdivision_depth": 1,
                        "maximum_final_delta": args.maximum_final_delta,
                        "maximum_new_vertices_per_edge": (
                            args.maximum_new_vertices_per_edge
                        ),
                        "maximum_total_new_vertices": (
                            args.maximum_total_new_vertices
                        ),
                        "candidate_cap_per_benchmark": args.candidate_cap,
                        "primary": "rank-potential CP-SAT",
                        "independent_negative_confirmation": "fixed-span CP-SAT over all legal spans",
                        "deduplication": "pynauty uncolored canonical certificate",
                    },
                    "audit": {
                        "stale_artifact": str(stale_artifact),
                        "stale_artifact_removed": stale_artifact_removed,
                        "root_cause": (
                            "main passed maximum_final_delta as classify_benchmark's "
                            "candidate_cap and candidate_cap as maximum_final_delta; "
                            "additionally, the prior reduction predicate ignored the "
                            "fact that ordinary edge subdivision preserves endpoint degrees"
                        ),
                    },
                    "completion_flags": {
                        "all_benchmarks_generation_complete": all(
                            item["generation_complete"] for item in summaries
                        ),
                        "all_benchmarks_classification_complete": all(
                            item["classification_complete"] for item in summaries
                        ),
                    },
                    "counts": {
                        "generated": sum(item["generated"] for item in summaries),
                        "unique": sum(item["unique"] for item in summaries),
                        "colorable": sum(item["colorable"] for item in summaries),
                        "non_colorable": sum(
                            item["non_colorable"] for item in summaries
                        ),
                        "timeout": sum(item["primary_timeout"] for item in summaries),
                        "confirmed_non_colorable": sum(
                            item["confirmed_non_colorable"] for item in summaries
                        ),
                    },
                    "complete": all(
                        item.get("run_complete", False) for item in summaries
                    ),
                    "elapsed_seconds": time.perf_counter() - run_started,
                    "summaries": summaries,
                    "records": all_records,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
