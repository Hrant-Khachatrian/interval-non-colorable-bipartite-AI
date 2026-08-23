#!/usr/bin/env python3
"""Profile representative order-14 graphs from canonical graph6 inputs."""

import sys
import time

from interval_edge_coloring import Graph, from_graph6, rank_potential_solve


def main() -> None:
    paths = [
        "data/order14-5x9-d2.g6",
        "data/order14-6x8-d2.g6",
        "data/order14-7x7-d2.g6",
    ]
    for path in paths:
        with open(path) as handle:
            lines = handle.readlines()
        for fraction in (0.25, 0.5, 0.75, 0.999):
            line = lines[int((len(lines) - 1) * fraction)]
            count, raw_edges = from_graph6(line)
            names = [f"V{i}" for i in range(count)]
            graph = Graph(names, [(names[i], names[j]) for i, j in raw_edges])
            degrees = graph.degrees
            kind = "regular" if len(set(degrees.values())) == 1 else "nonregular"
            if kind == "regular":
                print(path, fraction, kind, sorted(set(degrees.values())), "skip", flush=True)
                continue
            started = time.time()
            result = rank_potential_solve(graph, 10.0, 4)
            elapsed = time.time() - started
            print(
                path,
                fraction,
                kind,
                graph.m,
                min(degrees.values()),
                max(degrees.values()),
                result.status,
                result.span,
                f"{elapsed:.3f}s",
                flush=True,
            )


if __name__ == "__main__":
    main()
