#!/usr/bin/env python3
"""Test longer disjoint synchronization paths between two split hubs."""

from __future__ import annotations

import argparse
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


def candidate(base: Graph, mask: int, paths: int) -> Graph:
    connectors = sorted(base.bipartition[1])
    core = [v for v in base.bipartition[0] if v != "u"]
    left = ["U0", "U1"] + core + [f"T{j}" for j in range(paths)]
    right = connectors + [f"S{j}_{side}" for j in range(paths) for side in (0, 1)]
    edges = []
    for index, connector in enumerate(connectors):
        edges.extend(edge for edge in base.edges if connector in edge and "u" not in edge)
        edges.append(("U0" if mask & (1 << index) else "U1", connector))
    for j in range(paths):
        s0, s1, middle = f"S{j}_0", f"S{j}_1", f"T{j}"
        edges.extend((("U0", s0), (s0, middle), (middle, s1), (s1, "U1")))
    return Graph(
        left + right,
        edges,
        [left, right],
        {
            "lane": "lane6-chained-synchronization",
            "mask": format(mask, "011b"),
            "sync_paths": paths,
            "u0_degree": sum(bool(mask & (1 << i)) for i in range(11)) + paths,
            "u1_degree": sum(not bool(mask & (1 << i)) for i in range(11)) + paths,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimum-hub-degree", type=int, default=4)
    parser.add_argument("--maximum-delta", type=int, default=10)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", default="results/lane6-chained-sync.json")
    args = parser.parse_args()

    _, base = seed_graph()
    seen = {}
    rows = []
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for paths in (1, 2, 3):
        for mask in range(1 << 11):
            graph = candidate(base, mask, paths)
            hub_degrees = (graph.degrees["U0"], graph.degrees["U1"])
            if (
                min(hub_degrees) < args.minimum_hub_degree
                or max(hub_degrees) + 1 > args.maximum_delta
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
                "candidate_id": f"L6C-{len(rows):04d}",
                "canonical_sha256": digest,
                "order": graph.n,
                "size": graph.m,
                "delta": graph.delta,
                "metadata": graph.metadata,
                "weighted_hubs_best": weighted_hub_statistics(graph)[:2],
                "primary_result": {k: v for k, v in result.__dict__.items() if k != "coloring"},
            }
            if result.status == "non-colorable":
                row["independent_spans"] = all_spans_solve(graph, args.time_limit, args.workers, False)
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
