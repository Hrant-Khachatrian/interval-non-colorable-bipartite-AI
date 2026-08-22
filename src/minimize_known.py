#!/usr/bin/env python3
"""Exact single-edge and single-vertex minimality checks for the known seed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from interval_edge_coloring import Graph, nauty_canonical_hash, rank_potential_solve
from lane1_search import seed_graph


def without_edge(graph: Graph, edge: tuple[str, str]) -> Graph:
    edge = tuple(sorted(edge))
    edges = [existing for existing in graph.edges if existing != edge]
    return Graph(
        list(graph.vertices), edges, [graph.bipartition[0], graph.bipartition[1]],
        {"parent": "hat_K34_prime_Delta11", "deleted_edge": list(edge)},
    )


def without_vertex(graph: Graph, vertex: str) -> Graph:
    vertices = [v for v in graph.vertices if v != vertex]
    edges = [edge for edge in graph.edges if vertex not in edge]
    left = [v for v in graph.bipartition[0] if v != vertex]
    right = [v for v in graph.bipartition[1] if v != vertex]
    return Graph(vertices, edges, [left, right], {"parent": "hat_K34_prime_Delta11", "deleted_vertex": vertex})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", default="results/known-seed-minimality.json")
    args = parser.parse_args()

    _, base = seed_graph()
    rows = []
    for edge in base.edges:
        child = without_edge(base, edge)
        result = rank_potential_solve(child, args.time_limit, args.workers)
        rows.append({
            "kind": "edge",
            "deleted": list(edge),
            "canonical_sha256": nauty_canonical_hash(child),
            "status": result.status,
            "solver_status": result.solver_status,
            "span": result.span,
        })
        print(json.dumps(rows[-1]), flush=True)
    for vertex in base.vertices:
        child = without_vertex(base, vertex)
        result = rank_potential_solve(child, args.time_limit, args.workers)
        rows.append({
            "kind": "vertex",
            "deleted": vertex,
            "canonical_sha256": nauty_canonical_hash(child),
            "status": result.status,
            "solver_status": result.solver_status,
            "span": result.span,
        })
        print(json.dumps(rows[-1]), flush=True)
    summary = {
        "base": {
            "name": "hat_K34_prime_Delta11",
            "order": base.n,
            "size": base.m,
            "delta": base.delta,
            "canonical_sha256": nauty_canonical_hash(base),
        },
        "edge_deletion_negative_count": sum(r["kind"] == "edge" and r["status"] == "non-colorable" for r in rows),
        "vertex_deletion_negative_count": sum(r["kind"] == "vertex" and r["status"] == "non-colorable" for r in rows),
        "timeout_count": sum(r["status"] == "timeout" for r in rows),
        "rows": rows,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
