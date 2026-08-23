#!/usr/bin/env python3
"""Round 2: five-internal-vertex strict terminal-signature search.

The round-1 search exhausted all two-terminal gadgets with at most four
internal vertices.  This runner extends the labelled gadget shape space to
five, keeps gadgets exhibiting an extreme-palette concatenation state, and
exactly classifies every degree-cap-feasible split of the 12 seed-hub
neighbors.  A timeout is recorded only as ``timeout``.
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

import networkx as nx

from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)
from lane6_signature_search import Signature, is_concatenator, terminal_signatures
from lane1_search import seed_graph


TOTAL_TIME_LIMIT_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class Gadget:
    vertices: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    signature: tuple[Signature, ...]
    key: str


def _ordered_edge(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _connected(vertices: list[str], edges: list[tuple[str, str]]) -> bool:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        adjacency[a].add(b)
        adjacency[b].add(a)
    start = vertices[0]
    stack = [start]
    seen = {start}
    while stack:
        vertex = stack.pop()
        for neighbor in adjacency[vertex]:
            if neighbor not in seen:
                seen.add(neighbor)
                stack.append(neighbor)
    return len(seen) == len(vertices)


def enumerate_gadget_shapes(max_internal: int, max_internal_degree: int) -> list[tuple]:
    """Enumerate connected labelled two-terminal bipartite gadget shapes."""

    shapes_by_edges: dict[frozenset[tuple[str, str]], tuple] = {}
    terminals = ("T0", "T1")
    for left_count in range(max_internal + 1):
        right_count = max_internal - left_count
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
                    vertex not in terminals and degree[vertex] > max_internal_degree
                    for vertex in vertices
                ):
                    continue
                if not _connected(vertices, list(chosen)):
                    continue
                edge_set = frozenset(chosen)
                shapes_by_edges[edge_set] = (tuple(vertices), tuple(sorted(chosen)))
    return sorted(shapes_by_edges.values(), key=lambda item: (item[0], item[1]))


def build_gadgets(
    shapes: list[tuple],
    signature_time_limit: float,
    max_signature_solutions: int,
    deadline: float,
) -> tuple[list[Gadget], dict]:
    gadgets: list[Gadget] = []
    excluded_timeout = 0
    excluded_solution_cap = 0
    started = time.perf_counter()

    for shape_index, (vertices, edges) in enumerate(shapes):
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            excluded_timeout += len(shapes) - shape_index
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
            max_signature_solutions,
            min(signature_time_limit, remaining),
        )
        if signature is None:
            excluded_timeout += 1
            continue
        if len(signature) > max_signature_solutions:
            excluded_solution_cap += 1
            continue
        gadgets.append(
            Gadget(
                vertices,
                edges,
                tuple(sorted(signature, key=Signature.sort_key)),
                json.dumps(edges, separators=(",", ":")),
            )
        )

    stats = {
        "raw_shapes": len(shapes),
        "solved_gadgets": len(gadgets),
        "excluded_signature_timeout": excluded_timeout,
        "excluded_solution_cap": excluded_solution_cap,
        "signature_stage_completed": excluded_timeout == 0 and excluded_solution_cap == 0,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    return gadgets, stats


def selected_gadgets(gadgets: list[Gadget], signature_filter: str) -> list[Gadget]:
    def accepts(signature: tuple[Signature, ...]) -> bool:
        frozen = frozenset(signature)
        strict = is_concatenator(frozen, strict_extreme_only=True)
        if signature_filter == "strict_extreme":
            return strict
        if signature_filter == "union":
            return strict or is_concatenator(frozen)
        raise ValueError(f"unknown signature filter: {signature_filter}")

    return [gadget for gadget in gadgets if accepts(gadget.signature)]


def split_hub_candidate(base: Graph, mask: int, gadget: Gadget) -> Graph:
    connectors = sorted(base.bipartition[1])
    if len(connectors) != 12:
        raise ValueError("expected the 12 connectors of the hat K_(3,4) seed")

    core = [v for v in base.bipartition[0] if v != "u"]
    left = ["U0", "U1"] + core + [v for v in gadget.vertices if v.startswith("L")]
    right = connectors + [v for v in gadget.vertices if not v.startswith("L")]
    edges: list[tuple[str, str]] = []
    for index, connector in enumerate(connectors):
        edges.extend(edge for edge in base.edges if connector in edge and "u" not in edge)
        hub = "U0" if mask & (1 << index) else "U1"
        edges.append((hub, connector))
    edges.extend(gadget.edges)
    edges.extend((_ordered_edge("U0", "T0"), _ordered_edge("U1", "T1")))

    graph = Graph(left + right, edges, [left, right])
    graph.metadata = {
        "lane": "lane6-terminal-signature-r2",
        "max_internal_vertices": 5,
        "signature_filter": "configured_separately",
        "gadget_edges": [list(edge) for edge in gadget.edges],
        "mask": format(mask, "012b"),
        "u0_degree": sum(bool(mask & (1 << i)) for i in range(12)) + 1,
        "u1_degree": sum(not bool(mask & (1 << i)) for i in range(12)) + 1,
    }
    return graph


def all_spans_before_deadline(
    graph: Graph, per_span_time_limit: float, workers: int, deadline: float
) -> dict:
    """Independent fixed-span classification with a hard wall-clock deadline."""

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
            graph, span, min(per_span_time_limit, remaining), workers
        )
        results[span] = {
            "status": status,
            "coloring": {str(edge): color for edge, color in (coloring or {}).items()},
        }
        if status in ("OPTIMAL", "FEASIBLE"):
            overall = "colorable"
            break
        if status == "UNKNOWN":
            overall = "timeout"
    elapsed = time.perf_counter() - started
    return {
        "encoding": "fixed-span-sat",
        "decision": overall,
        "elapsed_seconds": round(elapsed, 3),
        "spans": results,
    }


def _result_summary(result) -> dict:
    return {key: value for key, value in result.__dict__.items() if key != "coloring"}


def _side_degrees(graph: Graph) -> dict[str, list[int]]:
    left, right = graph.bipartition
    degrees = graph.degrees
    return {
        "left": sorted(degrees[v] for v in left),
        "right": sorted(degrees[v] for v in right),
    }


def write_checkpoint(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(summary, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(rendered)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-internal", type=int, default=5)
    parser.add_argument("--max-internal-degree", type=int, default=6)
    parser.add_argument("--max-signature-solutions", type=int, default=50000)
    parser.add_argument("--signature-time-limit", type=float, default=10.0)
    parser.add_argument(
        "--signature-filter",
        choices=("strict_extreme", "union"),
        default="strict_extreme",
    )
    parser.add_argument("--minimum-hub-degree", type=int, default=3)
    parser.add_argument("--maximum-delta", type=int, default=10)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--independent-time-limit", type=float, default=15.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-interval", type=int, default=50)
    parser.add_argument("--total-time-limit", type=float, default=TOTAL_TIME_LIMIT_SECONDS)
    parser.add_argument("--output", default="results/lane6-signature-r2.json")
    args = parser.parse_args()

    if args.total_time_limit > TOTAL_TIME_LIMIT_SECONDS + 1e-9:
        parser.error("--total-time-limit may not exceed 7200 seconds")

    started = time.perf_counter()
    deadline = started + float(args.total_time_limit)
    output_path = Path(args.output)
    graph_directory = output_path.parent / "graphs" / "lane6-r2"

    _, base = seed_graph()
    shapes = enumerate_gadget_shapes(args.max_internal, args.max_internal_degree)
    solved_gadgets, signature_stats = build_gadgets(
        shapes,
        args.signature_time_limit,
        args.max_signature_solutions,
        deadline,
    )
    signature_counts = Counter(len(gadget.signature) for gadget in solved_gadgets)
    selected = selected_gadgets(solved_gadgets, args.signature_filter)

    rows: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    negative_artifacts: list[dict] = []
    candidate_stage_completed = True

    for gadget in selected:
        if time.perf_counter() >= deadline:
            candidate_stage_completed = False
            break
        for mask in range(1 << 12):
            if time.perf_counter() >= deadline:
                candidate_stage_completed = False
                break

            graph = split_hub_candidate(base, mask, gadget)
            graph.metadata["signature_filter"] = args.signature_filter
            hub_degrees = (graph.degrees["U0"], graph.degrees["U1"])
            if (
                min(hub_degrees) < args.minimum_hub_degree
                or graph.delta > args.maximum_delta
                or min(graph.degrees.values()) < 2
                or not nx.is_connected(graph._nx)
            ):
                skipped += 1
                continue

            digest = nauty_canonical_hash(graph)
            if digest in seen:
                skipped += 1
                continue
            seen.add(digest)

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                candidate_stage_completed = False
                break
            result = rank_potential_solve(
                graph, min(args.time_limit, remaining), args.workers
            )
            candidate_id = f"L6R2-{len(rows):04d}"
            row = {
                "candidate_id": candidate_id,
                "canonical_sha256": digest,
                "order": graph.n,
                "size": graph.m,
                "delta": graph.delta,
                "minimum_degree": min(graph.degrees.values()),
                "degree_sequence_by_side": _side_degrees(graph),
                "gadget_edges": [list(edge) for edge in gadget.edges],
                "signature_filter": args.signature_filter,
                "signature_size": len(gadget.signature),
                "metadata": graph.metadata,
                "weighted_hubs_best": weighted_hub_statistics(graph)[:2],
                "primary_result": _result_summary(result),
            }

            if result.status == "non-colorable":
                graph_directory.mkdir(parents=True, exist_ok=True)
                artifact_path = graph_directory / f"{candidate_id}.graph.json"
                graph.save(artifact_path)
                independent = all_spans_before_deadline(
                    graph,
                    args.independent_time_limit,
                    args.workers,
                    deadline,
                )
                row["independent_spans"] = independent
                row["certified_non_colorable"] = independent["decision"] == "non-colorable"
                row["graph_json"] = str(artifact_path)
                negative_artifacts.append(
                    {
                        "candidate_id": candidate_id,
                        "canonical_sha256": digest,
                        "graph_json": str(artifact_path),
                        "delta": graph.delta,
                        "minimum_degree": min(graph.degrees.values()),
                        "order": graph.n,
                        "size": graph.m,
                        "certified": row["certified_non_colorable"],
                    }
                )
            else:
                row["certified_non_colorable"] = False

            rows.append(row)
            if args.checkpoint_interval and len(rows) % args.checkpoint_interval == 0:
                counts = Counter(row["primary_result"]["status"] for row in rows)
                summary = {
                    "completed_unique": len(rows),
                    "counts": {
                        status: counts.get(status, 0)
                        for status in ("colorable", "non-colorable", "timeout")
                    },
                    "candidate_stage_completed": candidate_stage_completed,
                    "rows": rows,
                }
                write_checkpoint(output_path, summary)
                print(
                    json.dumps(
                        {key: value for key, value in summary.items() if key != "rows"},
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
        if not candidate_stage_completed:
            break

    counts = Counter(row["primary_result"]["status"] for row in rows)
    runtime_deadline_hit = time.perf_counter() >= deadline
    family_exhausted = (
        signature_stats["signature_stage_completed"]
        and candidate_stage_completed
        and counts.get("timeout", 0) == 0
    )
    summary = {
        "schema": "lane6-signature-search-round-2",
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
            "maximum_internal_gadget_vertices": args.max_internal,
            "maximum_internal_gadget_degree": args.max_internal_degree,
            "terminal_relation": args.signature_filter,
            "hub_neighbor_splits": 1 << 12,
            "minimum_hub_degree": args.minimum_hub_degree,
            "maximum_delta": args.maximum_delta,
            "minimum_graph_degree": 2,
            "boundary": (
                "all connected labelled two-terminal bipartite gadgets through "
                f"{args.max_internal} internal vertices whose augmented terminal "
                "signature contains an extreme-palette concatenation state"
                if args.signature_filter == "strict_extreme"
                else "strict-extreme or forced adjacent-offset terminal signatures"
            ),
        },
        "gadget_enumeration": {
            **signature_stats,
            "selected_gadgets": len(selected),
            "signature_size_counts": {
                str(size): count for size, count in sorted(signature_counts.items())
            },
        },
        "composition": {
            "generated_before_filters": len(selected) * (1 << 12),
            "skipped_filtered_or_duplicate": skipped,
            "generated": len(selected) * (1 << 12),
            "unique": len(rows),
            "completed_unique": len(rows),
            "counts": {
                status: counts.get(status, 0)
                for status in ("colorable", "non-colorable", "timeout")
            },
            "candidate_stage_completed": candidate_stage_completed,
            "family_exhausted": family_exhausted,
            "negative_independently_confirmed": sum(
                row.get("certified_non_colorable", False) for row in rows
            ),
            "unconfirmed_primary_negatives": sum(
                row["primary_result"]["status"] == "non-colorable"
                and not row.get("certified_non_colorable", False)
                for row in rows
            ),
            "conflicting_classifications": sum(
                row.get("independent_spans", {}).get("decision") == "colorable"
                for row in rows
            ),
            "unresolved_timeout_count": counts.get("timeout", 0)
            + int(not signature_stats["signature_stage_completed"]),
        },
        "negative_artifacts": negative_artifacts,
        "rows": rows,
    }
    write_checkpoint(output_path, summary)
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "rows"},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
