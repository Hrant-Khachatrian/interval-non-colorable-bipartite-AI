#!/usr/bin/env python3
"""Verify non-colorability with MiniSat over every possible span."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from interval_edge_coloring import Graph


def fixed_span_cnf(graph: Graph, span: int):
    variables = {}

    def variable(name: str) -> int:
        if name not in variables:
            variables[name] = len(variables) + 1
        return variables[name]

    clauses = []
    edge_names = []
    edge_list = sorted(map(tuple, graph.edges))
    for ei, edge in enumerate(edge_list):
        names = [variable(f"x_{ei}_{color}") for color in range(1, span + 1)]
        edge_names.append(names)
        clauses.append(names)
        clauses.extend([-a, -b] for i, a in enumerate(names) for b in names[i + 1:])
    for vi, vertex in enumerate(graph.vertices):
        incident = []
        for ei, edge in enumerate(edge_list):
            if vertex in edge:
                incident.append(ei)
        # At most one incident edge of each color.
        for color in range(1, span + 1):
            same_color = [edge_names[ei][color - 1] for ei in incident]
            clauses.extend([-a, -b] for i, a in enumerate(same_color) for b in same_color[i + 1:])
        degree = graph.degrees[vertex]
        start_names = [variable(f"s_{vi}_{start}") for start in range(1, span - degree + 2)]
        clauses.append(start_names)
        clauses.extend([-a, -b] for i, a in enumerate(start_names) for b in start_names[i + 1:])
        for ei in incident:
            for color in range(1, span + 1):
                allowed_starts = range(
                    max(1, color - degree + 1), min(color, span - degree + 1) + 1
                )
                clauses.append(
                    [-edge_names[ei][color - 1]] + [start_names[k - 1] for k in allowed_starts]
                )
        for si, start_name in enumerate(start_names):
            start = si + 1
            for ei in incident:
                for color in range(1, span + 1):
                    if not start <= color < start + degree:
                        clauses.append([-start_name, -edge_names[ei][color - 1]])
    # Every global color must occur.
    for color in range(1, span + 1):
        clause = [edge_names[ei][color - 1] for ei in range(graph.m)]
        if not clause:
            raise ValueError("no edges")
        clauses.append(clause)
    return clauses, variables


def write_dimacs(path: Path, clauses, variable_count: int) -> None:
    lines = [f"p cnf {variable_count} {len(clauses)}"]
    lines.extend(" ".join(map(str, clause)) + " 0" for clause in clauses)
    path.write_text("\n".join(lines) + "\n")


def verify_graph(graph: Graph, work_dir: Path, time_limit: int = 30) -> dict:
    digest = nauty_digest(graph)
    directory = work_dir / digest[:16]
    directory.mkdir(parents=True, exist_ok=True)
    spans = {}
    started = time.time()
    for span in range(graph.delta, graph.n):
        clauses, variables = fixed_span_cnf(graph, span)
        cnf_path = directory / f"span-{span}.cnf"
        result_path = directory / f"span-{span}.result"
        write_dimacs(cnf_path, clauses, len(variables))
        process = subprocess.run(
            ["minisat", "-verb=0", str(cnf_path), str(result_path)],
            text=True,
            capture_output=True,
            timeout=time_limit,
        )
        if process.returncode == 20:
            status = "UNSATISFIABLE"
        elif process.returncode == 10:
            status = "SATISFIABLE"
        else:
            status = f"ERROR_{process.returncode}"
        spans[span] = {
            "status": status,
            "variables": len(variables),
            "clauses": len(clauses),
            "cnf_sha256": hashlib.sha256(cnf_path.read_bytes()).hexdigest(),
        }
        if status != "UNSATISFIABLE":
            break
    return {
        "solver": "MiniSat 2.2",
        "decision": "non-colorable" if all(x["status"] == "UNSATISFIABLE" for x in spans.values()) else "not-proved",
        "spans": spans,
        "elapsed_seconds": time.time() - started,
    }


def nauty_digest(graph: Graph) -> str:
    from interval_edge_coloring import nauty_canonical_hash
    return nauty_canonical_hash(graph)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--output", default="results/minisat-verification.json")
    parser.add_argument("--work-dir", default="results/proofs/minisat")
    parser.add_argument("--time-limit", type=int, default=30)
    args = parser.parse_args()
    report = []
    for item in args.inputs:
        graph = Graph.from_json(json.loads(Path(item).read_text()))
        row = {"file": item, **verify_graph(graph, Path(args.work_dir), args.time_limit)}
        report.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != "spans"}, indent=2), flush=True)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
