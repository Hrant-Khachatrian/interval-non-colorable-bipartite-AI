#!/usr/bin/env python3
"""Lane 2: partial-hub subdivisions of small cores under tight order/degree caps."""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import time
from pathlib import Path
from typing import Sequence

import networkx as nx

from interval_edge_coloring import (
    Graph,
    all_spans_solve,
    from_graph6,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)


def core_graphs(vertices: int, minimum_edges: int, maximum_edges: int):
    process = subprocess.run(
        ["/usr/bin/nauty-geng", "-q", "-c", "-d2", "-D10", str(vertices),
         f"{minimum_edges}:{maximum_edges}"],
        text=True,
        capture_output=True,
        check=True,
    )
    for line in process.stdout.splitlines():
        _, edges = from_graph6(line)
        names = [f"H{i}" for i in range(vertices)]
        yield names, [(names[u], names[v]) for u, v in edges]


def subdivision_with_hub(
    core_vertices: Sequence[str], core_edges: Sequence[Sequence[str]], selected_edges: tuple[int, ...]
) -> Graph:
    originals = list(core_vertices)
    subdivisions = [f"S{i}" for i in range(len(core_edges))]
    edges = []
    for number, (u, v) in enumerate(core_edges):
        edges.extend(((subdivisions[number], u), (subdivisions[number], v)))
    for edge_index in selected_edges:
        edges.append(("U", subdivisions[edge_index]))
    return Graph(
        ["U"] + originals + subdivisions,
        edges,
        [["U"] + originals, subdivisions],
        {
            "lane": "lane2-partial-hub-subdivision",
            "core_edge_count": len(core_edges),
            "selected_core_edges": list(selected_edges),
            "hub_degree": len(selected_edges),
        },
    )


def generate_candidates(maximum_order: int = 18) -> list[tuple[str, Graph]]:
    seen: dict[str, Graph] = {}
    for order in range(4, 10):
        maximum_core_edges = maximum_order - 1 - order
        if maximum_core_edges < max(4, order):
            continue
        for core_vertices, core_edges in core_graphs(order, 4, maximum_core_edges):
            upper_hub_degree = min(len(core_edges), 10)
            for hub_degree in range(4, upper_hub_degree + 1):
                for selected in itertools.combinations(range(len(core_edges)), hub_degree):
                    candidate = subdivision_with_hub(core_vertices, core_edges, selected)
                    if (
                        candidate.n > maximum_order
                        or candidate.delta > 10
                        or candidate.delta < 4
                        or min(candidate.degrees.values()) < 2
                        or not nx.is_connected(candidate._nx)
                    ):
                        continue
                    digest = nauty_canonical_hash(candidate)
                    if digest not in seen:
                        seen[digest] = candidate
    return list(seen.items())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--minimum-margin", type=int, default=-2)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", default="results/lane2-partial-hub.json")
    args = parser.parse_args()

    started = time.time()
    print("generating subdivision candidates", flush=True)
    raw_candidates = generate_candidates()
    print(json.dumps({"raw_unique": len(raw_candidates)}), flush=True)
    scored = []
    seen_scored = set()
    for digest, graph in raw_candidates:
        stats = weighted_hub_statistics(graph)
        hub_row = next(row for row in stats if row["hub"] == "U")
        margin = int(hub_row["margin"])
        if margin < args.minimum_margin:
            continue
        score_key = (-margin, graph.n, graph.m, digest)
        scored.append((score_key, margin, digest, graph, hub_row))
        seen_scored.add(digest)
    scored.sort(key=lambda item: item[0])
    margin_counts = {}
    for _, margin, _, _, _ in scored:
        margin_counts[margin] = margin_counts.get(margin, 0) + 1
    print(json.dumps({"scored_count": len(scored), "margin_counts": margin_counts}), flush=True)
    selected = scored[:args.limit]
    rows = []
    negative_count = 0
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for number, (_, margin, digest, graph, hub_row) in enumerate(selected):
        primary = rank_potential_solve(graph, args.time_limit, args.workers)
        row = {
            "candidate_id": f"L2-{number:05d}",
            "canonical_sha256": digest,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "metadata": graph.metadata,
            "weighted_hub": hub_row,
            "primary_result": {k: v for k, v in primary.__dict__.items() if k != "coloring"},
        }
        if primary.status == "non-colorable":
            negative_count += 1
            independent = all_spans_solve(graph, args.time_limit, args.workers, False)
            row["independent_spans"] = independent
            directory = output_path.parent / "graphs" / "lane2"
            directory.mkdir(parents=True, exist_ok=True)
            graph.save(directory / f"{row['candidate_id']}.graph.json")
        rows.append(row)
        if (number + 1) % 100 == 0 or number + 1 == len(selected):
            payload = {
                "completed": number + 1,
                "selected_total": len(selected),
                "counts": {
                    status: sum(r["primary_result"]["status"] == status for r in rows)
                    for status in ("colorable", "non-colorable", "timeout")
                },
                "elapsed_seconds": time.time() - started,
                "rows": rows,
            }
            output_path.write_text(json.dumps(payload, indent=2) + "\n")
            print(json.dumps({k: v for k, v in payload.items() if k != "rows"}), flush=True)
    print(json.dumps({"negative_count": negative_count}, indent=2))


if __name__ == "__main__":
    main()
