#!/usr/bin/env python3
"""Same-side vertex identifications applied to the known degree-11 benchmark."""

from __future__ import annotations

import argparse
import itertools
import json
import time
from pathlib import Path

import networkx as nx

from interval_edge_coloring import (
    Graph,
    all_spans_solve,
    nauty_canonical_hash,
    rank_potential_solve,
)
from lane1_search import seed_graph


def quotient(base: Graph, blocks: tuple[tuple[str, ...], ...], candidate_id: str) -> Graph:
    owner = {}
    for number, block in enumerate(blocks):
        for vertex in block:
            owner[vertex] = number
    new_name = {number: "&".join(block) for number, block in enumerate(blocks)}
    edge_set = set()
    for u, v in base.edges:
        a, b = owner[u], owner[v]
        if a == b:
            continue
        edge_set.add(tuple(sorted((new_name[a], new_name[b]))))
    left = [new_name[i] for i, block in enumerate(blocks) if block[0] in base.bipartition[0]]
    right = [new_name[i] for i, block in enumerate(blocks) if block[0] in base.bipartition[1]]
    return Graph(
        list(new_name.values()),
        list(edge_set),
        [left, right],
        {
            "lane": "same-side-quotient",
            "parent": "hat_K34_prime_Delta11",
            "candidate_id": candidate_id,
            "order_reduction": base.n - len(blocks),
            "blocks": [list(block) for block in blocks if len(block) > 1],
        },
    )


def same_side_partitions(base: Graph, order_reduction: int) -> list[tuple[tuple[str, ...], ...]]:
    side_of = {v: 0 for v in base.bipartition[0]}
    side_of.update({v: 1 for v in base.bipartition[1]})
    result = []

    def recurse(remaining: tuple[str, ...], reduction_left: int, current: tuple[tuple[str, ...], ...]):
        if not remaining:
            if reduction_left == 0:
                result.append(current)
            return
        first = remaining[0]
        maximum_block = min(3, reduction_left + 2)
        for size in range(1, maximum_block + 1):
            same_side = [v for v in remaining[1:] if side_of[v] == side_of[first]]
            if size > 1 and len(same_side) < size - 1:
                continue
            if size == 1:
                choices = ((first,),)
            else:
                choices = tuple(
                    (first,) + partners
                    for partners in itertools.combinations(same_side, size - 1)
                )
            for block in choices:
                chosen = set(block)
                rest = tuple(v for v in remaining[1:] if v not in chosen)
                recurse(rest, reduction_left - (size - 1), current + (block,))

    recurse(tuple(sorted(base.vertices)), order_reduction, ())
    return sorted(result)


def candidates(base: Graph, order_reduction: int):
    seen = set()
    number = 0
    for partition in same_side_partitions(base, order_reduction):
        graph = quotient(base, partition, f"Q{order_reduction}-{number:05d}")
        digest = nauty_canonical_hash(graph)
        if digest in seen:
            continue
        seen.add(digest)
        yield digest, graph
        number += 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reduction", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    _, base = seed_graph()
    rows = []
    started = time.time()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for digest, graph in candidates(base, args.reduction):
        result = rank_potential_solve(graph, args.time_limit, args.workers)
        row = {
            "candidate_id": graph.metadata["candidate_id"],
            "canonical_sha256": digest,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "minimum_degree": min(graph.degrees.values()),
            "connected": nx.is_connected(graph._nx),
            "metadata": graph.metadata,
            "primary_result": {k: v for k, v in result.__dict__.items() if k != "coloring"},
        }
        if result.status == "non-colorable":
            row["independent_spans"] = all_spans_solve(
                graph, args.time_limit, args.workers, False
            )
            directory = output_path.parent / "graphs" / f"quotient-r{args.reduction}"
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
                "elapsed_seconds": time.time() - started,
                "rows": rows,
            }
            output_path.write_text(json.dumps(payload, indent=2) + "\n")
            print(json.dumps({k: v for k, v in payload.items() if k != "rows"}), flush=True)
    summary = {
        "reduction": args.reduction,
        "base_order": base.n,
        "target_order": base.n - args.reduction,
        "completed_unique": len(rows),
        "counts": {
            status: sum(r["primary_result"]["status"] == status for r in rows)
            for status in ("colorable", "non-colorable", "timeout")
        },
        "elapsed_seconds": time.time() - started,
        "rows": rows,
    }
    output_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
