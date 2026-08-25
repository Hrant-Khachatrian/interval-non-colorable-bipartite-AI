#!/usr/bin/env python3
"""Bounded certified reductions of the Q1-00012 and Q1-00014 witnesses.

The search is intentionally narrow: valid one-vertex deletions, minimum hub
edge deletions needed for a chosen degree cap, and the combination of those
two operations.  A primary CP-SAT negative is only reported as confirmed when
the independent fixed-span encoding is infeasible at every legal span.
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from collections import Counter
from pathlib import Path

import networkx as nx

from interval_edge_coloring import (
    Graph,
    all_spans_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)


SOURCES = ("Q1-00012", "Q1-00014")


def delete_vertices(graph: Graph, vertices: tuple[str, ...]) -> Graph:
    removed = set(vertices)
    left, right = graph.bipartition
    return Graph(
        [v for v in graph.vertices if v not in removed],
        [edge for edge in graph.edges if not (set(edge) & removed)],
        [[v for v in left if v not in removed], [v for v in right if v not in removed]],
    )


def delete_edges(graph: Graph, edges: tuple[tuple[str, str], ...]) -> Graph:
    removed = set(edges)
    return Graph(
        graph.vertices,
        [edge for edge in graph.edges if edge not in removed],
        graph.bipartition,
    )


def valid(graph: Graph, min_degree: int) -> bool:
    return (
        graph.n > 0
        and min(graph.degrees.values(), default=0) >= min_degree
        and nx.is_connected(graph._nx)
    )


def minimal_cap_deletions(graph: Graph, cap: int, max_sets: int) -> list[tuple[tuple[str, str], ...]]:
    """Enumerate exact-size edge sets that reduce every over-cap vertex."""

    over = {v: d - cap for v, d in graph.degrees.items() if d > cap}
    if not over:
        return [()]
    required = sum(over.values())
    choices = [edge for edge in graph.edges if edge[0] in over or edge[1] in over]
    output = []
    for selected in itertools.combinations(choices, required):
        hits = Counter(v for edge in selected for v in edge if v in over)
        if all(hits[v] >= needed for v, needed in over.items()):
            output.append(selected)
            if len(output) >= max_sets:
                break
    return output


def provenance_row(source: str, family: str, deleted_vertices: tuple[str, ...], deleted_edges: tuple[tuple[str, str], ...]) -> dict:
    return {
        "source": source,
        "family": family,
        "deleted_vertices": list(deleted_vertices),
        "deleted_edges": [list(edge) for edge in deleted_edges],
    }


def graph_stats(graph: Graph) -> dict:
    return {
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "connected": nx.is_connected(graph._nx),
        "degrees": graph.degrees,
        "canonical_sha256": nauty_canonical_hash(graph),
    }


def result_summary(result) -> dict:
    """JSON-safe solver result without storing bulky successful colorings."""

    return {
        "status": result.status,
        "encoding": result.encoding,
        "elapsed_seconds": result.elapsed_seconds,
        "span": result.span,
        "solver_status": result.solver_status,
        "conflicts": result.conflicts,
        "branches": result.branches,
        "wall_time": result.wall_time,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="results/known-reduction-agent")
    parser.add_argument("--cap", type=int, default=10)
    parser.add_argument("--min-degree", type=int, default=2)
    parser.add_argument("--primary-time-limit", type=float, default=30.0)
    parser.add_argument("--span-time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-edge-sets", type=int, default=200)
    args = parser.parse_args()

    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    started = time.time()
    generated = Counter()
    rejected = Counter()
    unique: dict[str, tuple[Graph, list[dict]]] = {}

    def add(source: str, family: str, graph: Graph, vertices: tuple[str, ...] = (), edges: tuple[tuple[str, str], ...] = ()) -> None:
        generated[family] += 1
        if not valid(graph, args.min_degree):
            rejected[family] += 1
            return
        key = nauty_canonical_hash(graph)
        entry = provenance_row(source, family, vertices, edges)
        if key in unique:
            unique[key][1].append(entry)
        else:
            unique[key] = (graph, [entry])

    for source in SOURCES:
        data = json.loads((Path("results/candidates") / source / f"{source}.graph.json").read_text())
        base = Graph.from_json(data)
        # Family 1: every valid single-vertex deletion.
        for vertex in base.vertices:
            add(source, "vertex", delete_vertices(base, (vertex,)), (vertex,))

        # Family 2: all exact-minimum hub-edge sets required for the degree cap.
        for edges in minimal_cap_deletions(base, args.cap, args.max_edge_sets):
            add(source, "edge_cap", delete_edges(base, edges), (), edges)

        # Family 3: repeat the cap reduction after each valid vertex deletion.
        for vertex in base.vertices:
            child = delete_vertices(base, (vertex,))
            if not valid(child, args.min_degree):
                continue
            for edges in minimal_cap_deletions(child, args.cap, args.max_edge_sets):
                if edges:
                    add(source, "vertex_edge_cap", delete_edges(child, edges), (vertex,), edges)

    rows = []
    counts = Counter()
    for number, (key, (graph, provenance)) in enumerate(sorted(unique.items())):
        primary = rank_potential_solve(graph, args.primary_time_limit, args.workers)
        row = {
            "candidate_id": f"KR-{number:04d}",
            **graph_stats(graph),
            "provenance": provenance,
            "primary_result": result_summary(primary),
            "confirmation": None,
        }
        if primary.status == "non-colorable":
            confirmation = all_spans_solve(
                graph,
                time_limit_per_span=args.span_time_limit,
                workers=args.workers,
                stop_on_timeout=True,
            )
            row["confirmation"] = confirmation
            if confirmation["decision"] == "non-colorable":
                row["classification"] = "non-colorable"
            elif confirmation["decision"] == "colorable":
                row["classification"] = "primary-disagreement"
            else:
                row["classification"] = "timeout"
        else:
            row["classification"] = primary.status
        counts[row["classification"]] += 1
        rows.append(row)
        print(json.dumps({
            "checkpoint": number + 1,
            "generated": sum(generated.values()),
            "unique": len(unique),
            "classified": len(rows),
            "colorable": counts["colorable"],
            "non_colorable": counts["non-colorable"],
            "timeout": counts["timeout"],
        }), flush=True)

    negatives = [r for r in rows if r["classification"] == "non-colorable"]
    near_misses = []
    for row in rows:
        if row["classification"] == "non-colorable":
            continue
        graph = unique[row["canonical_sha256"]][0]
        near_misses.append({
            "candidate_id": row["candidate_id"],
            "order": row["order"],
            "size": row["size"],
            "delta": row["delta"],
            "minimum_degree": row["minimum_degree"],
            "classification": row["classification"],
            "colorable_span": row["primary_result"]["span"],
            "provenance": row["provenance"],
            "weighted_hubs_best": weighted_hub_statistics(graph)[:3],
        })
    near_misses.sort(key=lambda r: (r["delta"], r["order"], r["size"], r["candidate_id"]))
    report = {
        "scope": {
            "sources": list(SOURCES),
            "max_degree_cap": args.cap,
            "minimum_degree": args.min_degree,
            "families": ["vertex", "edge_cap", "vertex_edge_cap"],
            "primary": "rank-potential-cpsat",
            "negative_confirmation": "fixed-span-sat across every legal span delta..n-1",
        },
        "elapsed_seconds": time.time() - started,
        "generated": dict(generated),
        "rejected_invalid": dict(rejected),
        "unique": len(unique),
        "classified": len(rows),
        "counts": {
            "colorable": counts["colorable"],
            "non-colorable": counts["non-colorable"],
            "timeout": counts["timeout"],
            "primary-disagreement": counts["primary-disagreement"],
        },
        "negative_count": len(negatives),
    }
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (root / "classified.json").write_text(json.dumps(rows, indent=2) + "\n")
    (root / "near-miss-diagnostics.json").write_text(json.dumps(near_misses, indent=2) + "\n")
    (root / "confirmed-negatives.json").write_text(json.dumps(negatives, indent=2) + "\n")
    print(json.dumps({"final": report}, indent=2))


if __name__ == "__main__":
    main()
