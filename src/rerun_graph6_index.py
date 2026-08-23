#!/usr/bin/env python3
"""Reclassify one graph6 record with a longer exact-solver limit."""

import argparse
import json
from pathlib import Path

from interval_edge_coloring import (
    Graph,
    from_graph6,
    nauty_canonical_hash,
    rank_potential_solve,
)


def graph_at_index(path: Path, index: int) -> tuple[str, Graph]:
    with path.open() as handle:
        for number, line in enumerate(handle):
            if number == index:
                count, raw_edges = from_graph6(line.rstrip())
                names = [f"V{i}" for i in range(count)]
                return line.rstrip(), Graph(
                    names, [(names[i], names[j]) for i, j in raw_edges]
                )
    raise IndexError(f"index {index} not found in {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("index", type=int)
    parser.add_argument("--time-limit", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source_line, graph = graph_at_index(args.input, args.index)
    result = rank_potential_solve(graph, args.time_limit, args.workers)
    row = {
        "source": str(args.input),
        "index": args.index,
        "source_line": source_line,
        "canonical_sha256": nauty_canonical_hash(graph),
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "status": result.status,
        "span": result.span,
        "solver_seconds": result.elapsed_seconds,
        "solver_status": result.solver_status,
        "time_limit_seconds": args.time_limit,
        "workers": args.workers,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    if result.status == "non-colorable":
        candidate_dir = output.parent / "candidate-graphs"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        graph.save(candidate_dir / f"index-{args.index:09d}.graph.json")
        print(candidate_dir / f"index-{args.index:09d}.graph.json")
    print(json.dumps(row, sort_keys=True))


if __name__ == "__main__":
    main()
