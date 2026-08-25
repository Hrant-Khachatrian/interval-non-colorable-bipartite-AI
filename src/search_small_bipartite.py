#!/usr/bin/env python3
"""Exact classification of canonically generated small bipartite graphs."""

import argparse
import json
import subprocess
import time
from itertools import islice
from pathlib import Path

from interval_edge_coloring import (
    Graph,
    from_graph6,
    nauty_canonical_hash,
    rank_potential_solve,
)


def graph_from_graph6_line(line: str) -> Graph:
    n, raw_edges = from_graph6(line)
    names = [f"V{i}" for i in range(n)]
    return Graph(names, [(names[i], names[j]) for i, j in raw_edges])


def save_result(graph: Graph, result, output_dir: Path, index: int) -> None:
    directory = output_dir / "hits"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"candidate-{index:06d}.graph.json"
    graph.save(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("n1", type=int)
    parser.add_argument("n2", type=int)
    parser.add_argument("--min-edges", type=int, required=True)
    parser.add_argument("--max-edges", type=int, required=True)
    parser.add_argument("--time-limit", type=float, default=2.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", required=True)
    parser.add_argument("--res", type=int, default=0,
                        help="genbg residue selector for array parallelism")
    parser.add_argument("--mod", type=int, default=1,
                        help="genbg modulus selector for array parallelism")
    parser.add_argument("--input",
                        help="graph6 file to classify; overrides generation and is required on clusters without genbg")
    parser.add_argument("--start-index", type=int,
                        help="first graph6 line to classify when using --input")
    parser.add_argument("--stop-index", type=int,
                        help="exclusive last graph6 line to classify when using --input")
    parser.add_argument("--index-offset", type=int, default=0,
                        help="global index of the first record in a pre-sharded input")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.input and (args.start_index is not None or args.stop_index is not None):
        parser.error("--start-index/--stop-index require --input")
    if (args.start_index is None) != (args.stop_index is None):
        parser.error("--start-index and --stop-index must be given together")
    if args.start_index is not None and (args.start_index < 0 or args.stop_index <= args.start_index):
        parser.error("require 0 <= start-index < stop-index")

    done = set()
    if output_path.exists():
        for line in output_path.read_text().splitlines():
            row = json.loads(line)
            if row["status"] != "timeout":
                done.add(row["index"])

    if args.input:
        input_stream = Path(args.input).open()
        process = None
    else:
        command = [
            "/usr/bin/nauty-genbg", "-q", "-g", "-c", "-l", "-d2",
            str(args.n1), str(args.n2), f"{args.min_edges}:{args.max_edges}",
        ]
        if args.mod != 1:
            command.append(f"{args.res}/{args.mod}")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
        input_stream = process.stdout
    if args.start_index is not None:
        # Skip without decoding records outside this contiguous chunk.
        for _ in islice(input_stream, args.start_index):
            pass
    started = time.time()
    counts = {"colorable": 0, "non-colorable": 0, "timeout": 0}
    with output_path.open("a") as out:
        with input_stream:
            for offset, line in enumerate(input_stream):
                index = (offset if args.start_index is None else args.start_index + offset) + args.index_offset
                if (
                    args.start_index is None
                    and args.mod != 1
                    and index % args.mod != args.res
                ):
                    continue
                if args.stop_index is not None and index >= args.stop_index:
                    break
                if index in done:
                    continue
                graph = graph_from_graph6_line(line.rstrip())
                degrees = graph.degrees
                if len(set(degrees.values())) == 1:
                    status = "regular-skipped"
                    span = None
                    elapsed = 0.0
                else:
                    result = rank_potential_solve(graph, args.time_limit, args.workers)
                    status = result.status
                    span = result.span
                    elapsed = result.elapsed_seconds
                counts[status] = counts.get(status, 0) + 1
                row = {
                    "index": index,
                    "canonical_sha256": nauty_canonical_hash(graph),
                    "order": graph.n,
                    "size": graph.m,
                    "delta": max(degrees.values()),
                    "minimum_degree": min(degrees.values()),
                    "status": status,
                    "span": span,
                    "solver_seconds": elapsed,
                }
                out.write(json.dumps(row, sort_keys=True) + "\n")
                if (index + 1) % 250 == 0:
                    print(
                        json.dumps({
                            "completed": index + 1,
                            "counts": counts,
                            "wall_seconds": round(time.time() - started, 3),
                        }),
                        flush=True,
                    )
                if status == "non-colorable":
                    save_result(graph, result, output_path.parent, index)
                    print(json.dumps({"negative_at_index": index}), flush=True)
    if process is not None and process.wait() != 0:
        raise SystemExit(process.returncode)
    summary = {
        "n1": args.n1,
        "n2": args.n2,
        "edge_range": [args.min_edges, args.max_edges],
        "residue": [args.res, args.mod],
        "index_offset": args.index_offset,
        "input": str(args.input) if args.input else None,
        "index_range": (
            [args.start_index, args.stop_index]
            if args.start_index is not None
            else None
        ),
        "processed": sum(counts.values()),
        "counts": counts,
        "wall_seconds": round(time.time() - started, 3),
    }
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
