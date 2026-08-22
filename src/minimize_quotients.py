#!/usr/bin/env python3
"""Exact edge/vertex minimality checks for discovered quotient negatives."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import networkx as nx

from interval_edge_coloring import Graph, nauty_canonical_hash, rank_potential_solve


def delete_edge(graph: Graph, edge: tuple[str, str]) -> Graph:
    edge = tuple(sorted(edge))
    return Graph(
        list(graph.vertices),
        [existing for existing in graph.edges if existing != edge],
        [graph.bipartition[0], graph.bipartition[1]],
        {"parent_sha": graph.metadata.get("sha256_bipartition_canonical"), "deleted_edge": list(edge)},
    )


def delete_vertex(graph: Graph, vertex: str) -> Graph:
    return Graph(
        [v for v in graph.vertices if v != vertex],
        [edge for edge in graph.edges if vertex not in edge],
        [[v for v in graph.bipartition[0] if v != vertex],
         [v for v in graph.bipartition[1] if v != vertex]],
        {"parent_sha": graph.metadata.get("sha256_bipartition_canonical"), "deleted_vertex": vertex},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", default="results/quotient-r1-minimality.json")
    args = parser.parse_args()

    paths = sorted(Path("results/graphs/quotient-r1").glob("*.graph.json"))
    report = []
    started = time.time()
    for path in paths:
        parent = Graph.from_json(json.loads(path.read_text()))
        parent_row = {
            "candidate_id": parent.metadata["candidate_id"],
            "canonical_sha256": nauty_canonical_hash(parent),
            "order": parent.n,
            "size": parent.m,
            "delta": parent.delta,
            "edge_deletion_negative": [],
            "vertex_deletion_negative": [],
            "edge_checks": parent.m,
            "vertex_checks": parent.n,
            "timeouts": 0,
        }
        for edge in parent.edges:
            child = delete_edge(parent, edge)
            result = rank_potential_solve(child, args.time_limit, args.workers)
            if result.status == "non-colorable":
                parent_row["edge_deletion_negative"].append(nauty_canonical_hash(child))
            parent_row["timeouts"] += result.status == "timeout"
        for vertex in parent.vertices:
            child = delete_vertex(parent, vertex)
            result = rank_potential_solve(child, args.time_limit, args.workers)
            if result.status == "non-colorable":
                parent_row["vertex_deletion_negative"].append(nauty_canonical_hash(child))
            parent_row["timeouts"] += result.status == "timeout"
        report.append(parent_row)
        print(json.dumps(parent_row), flush=True)
    payload = {
        "elapsed_seconds": time.time() - started,
        "graphs": report,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({k: v for k, v in payload.items() if k != "graphs"}, indent=2))


if __name__ == "__main__":
    main()
