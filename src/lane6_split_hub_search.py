#!/usr/bin/env python3
"""Split the degree-11 hub and test two-hub synchronization graphs."""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import networkx as nx

from interval_edge_coloring import (
    Graph,
    all_spans_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)
from lane1_search import seed_graph


def split_hub_graph(base: Graph, mask: int) -> Graph:
    connectors = sorted(base.bipartition[1])
    core = [v for v in base.bipartition[0] if v != "u"]
    right = connectors + ["W"]
    edges = []
    for index, connector in enumerate(connectors):
        core_edges = [edge for edge in base.edges if connector in edge and "u" not in edge]
        if len(core_edges) != 2:
            raise ValueError("unexpected connector incidence")
        edges.extend(core_edges)
        hub = "U0" if mask & (1 << index) else "U1"
        edges.append((hub, connector))
    edges.extend((("U0", "W"), ("U1", "W")))
    return Graph(
        ["U0", "U1"] + core + right,
        edges,
        [["U0", "U1"] + core, right],
        {
            "lane": "lane6-two-hub-synchronization",
            "parent": "hat_K34_prime_Delta11",
            "mask": format(mask, "011b"),
            "u0_degree": sum(bool(mask & (1 << i)) for i in range(len(connectors))) + 1,
            "u1_degree": sum(not bool(mask & (1 << i)) for i in range(len(connectors))) + 1,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimum-side-degree", type=int, default=4)
    parser.add_argument("--maximum-delta", type=int, default=9)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", default="results/lane6-split-hub.json")
    args = parser.parse_args()

    _, base = seed_graph()
    seen = {}
    rows = []
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for mask in range(1 << 11):
        graph = split_hub_graph(base, mask)
        side_degrees = [graph.degrees["U0"], graph.degrees["U1"]]
        if (
            min(side_degrees) < args.minimum_side_degree
            or graph.delta > args.maximum_delta
            or min(graph.degrees.values()) < 2
            or not nx.is_connected(graph._nx)
        ):
            continue
        digest = nauty_canonical_hash(graph)
        if digest in seen:
            continue
        seen[digest] = True
        result = rank_potential_solve(graph, args.time_limit, args.workers)
        row = {
            "candidate_id": f"L6-{len(rows):04d}",
            "canonical_sha256": digest,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "metadata": graph.metadata,
            "weighted_hubs_best": weighted_hub_statistics(graph)[:2],
            "primary_result": {k: v for k, v in result.__dict__.items() if k != "coloring"},
        }
        if result.status == "non-colorable":
            independent = all_spans_solve(graph, args.time_limit, args.workers, False)
            row["independent_spans"] = independent
            directory = output_path.parent / "graphs" / "lane6"
            directory.mkdir(parents=True, exist_ok=True)
            graph.save(directory / f"{row['candidate_id']}.graph.json")
        rows.append(row)
        if len(rows) % 100 == 0:
            payload = {
                "completed": len(rows),
                "counts": {
                    status: sum(r["primary_result"]["status"] == status for r in rows)
                    for status in ("colorable", "non-colorable", "timeout")
                },
                "rows": rows,
            }
            output_path.write_text(json.dumps(payload, indent=2) + "\n")
            print(json.dumps({k: v for k, v in payload.items() if k != "rows"}), flush=True)
    summary = {
        "completed_unique": len(rows),
        "counts": {
            status: sum(r["primary_result"]["status"] == status for r in rows)
            for status in ("colorable", "non-colorable", "timeout")
        },
        "rows": rows,
    }
    output_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
