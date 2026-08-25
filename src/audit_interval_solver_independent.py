#!/usr/bin/env python3
"""Independent CNF audit of the interval-edge-coloring decision pipeline.

This intentionally does not reuse the production fixed-span CP-SAT model.  It
builds a Boolean CNF model and solves it with PicoSAT, then compares it to the
rank-potential and fixed-span CP-SAT decisions exposed by interval_edge_coloring.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import networkx as nx

from interval_edge_coloring import (
    Graph,
    benchmark_graphs,
    fixed_span_sat_solve,
    positive_controls,
    rank_potential_solve,
    verify_coloring,
)


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "solver-audit-agent"
PICOSAT = ROOT / "tools" / "picosat-965" / "picosat"


class Cnf:
    """Small, self-contained DIMACS builder with named variables."""

    def __init__(self) -> None:
        self.names: dict[tuple, int] = {}
        self.clauses: list[list[int]] = []
        self.categories: Counter[str] = Counter()

    def var(self, key: tuple) -> int:
        if key not in self.names:
            self.names[key] = len(self.names) + 1
        return self.names[key]

    def add(self, literals: list[int], category: str) -> None:
        self.clauses.append(literals)
        self.categories[category] += 1

    def exactly_one(self, literals: list[int], category: str) -> None:
        self.add(literals, category)
        for pos, first in enumerate(literals):
            for second in literals[pos + 1:]:
                self.add([-first, -second], category)

    def dimacs(self) -> bytes:
        body = [f"p cnf {len(self.names)} {len(self.clauses)}"]
        body.extend(" ".join(map(str, clause)) + " 0" for clause in self.clauses)
        return ("\n".join(body) + "\n").encode("ascii")


def independent_cnf(graph: Graph, span: int) -> tuple[Cnf, dict[tuple, int], dict[tuple, int]]:
    """Encode interval coloring using color and local interval-start literals."""

    cnf = Cnf()
    edge_list = sorted(map(tuple, graph.edges))
    edge_index = {edge: index for index, edge in enumerate(edge_list)}
    colors = {(edge, color): cnf.var(("edge-color", edge_index[edge], color))
              for edge in edge_list for color in range(1, span + 1)}
    starts: dict[tuple, int] = {}
    adjacency = graph.adjacency()

    for edge in edge_list:
        cnf.exactly_one([colors[(edge, color)] for color in range(1, span + 1)], "edge_exactly_one")

    for vertex in graph.vertices:
        degree = graph.degrees[vertex]
        start_values = list(range(1, span - degree + 2))
        if not start_values:
            cnf.add([], "empty_degree_domain")
            continue
        local_starts = []
        for start in start_values:
            literal = cnf.var(("interval-start", vertex, start))
            starts[(vertex, start)] = literal
            local_starts.append(literal)
        cnf.exactly_one(local_starts, "start_exactly_one")

        incident = [edge for _neighbor, edge in adjacency[vertex]]
        for color in range(1, span + 1):
            at_color = [colors[(edge, color)] for edge in incident]
            for pos, first in enumerate(at_color):
                for second in at_color[pos + 1:]:
                    cnf.add([-first, -second], "incident_properness")

        for edge in incident:
            for color in range(1, span + 1):
                allowed = [starts[(vertex, start)] for start in start_values if start <= color < start + degree]
                # A selected edge color must lie in the selected local interval.
                cnf.add([-colors[(edge, color)], *allowed], "edge_implies_interval")

        for start in start_values:
            start_literal = starts[(vertex, start)]
            for color in range(start, start + degree):
                at_color = [colors[(edge, color)] for edge in incident]
                # Properness makes this at-least-one constraint exactly one.
                cnf.add([-start_literal, *at_color], "interval_coverage")

    for color in range(1, span + 1):
        cnf.add([colors[(edge, color)] for edge in edge_list], "global_color_usage")
    return cnf, colors, starts


def parse_assignment(output: str) -> set[int]:
    literals = set()
    for line in output.splitlines():
        if line.startswith("v "):
            literals.update(int(value) for value in line.split()[1:] if value != "0")
    return literals


def independent_span(graph: Graph, span: int, seconds: int = 20) -> dict:
    cnf, colors, starts = independent_cnf(graph, span)
    dimacs = cnf.dimacs()
    started = time.perf_counter()
    try:
        process = subprocess.run(
            [str(PICOSAT), "-s", "0", "-L", str(seconds)],
            input=dimacs,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=seconds + 3,
        )
        elapsed = time.perf_counter() - started
    except subprocess.TimeoutExpired:
        return {
            "status": "TIMEOUT",
            "elapsed_seconds": time.perf_counter() - started,
            "variables": len(cnf.names), "clauses": len(cnf.clauses),
            "cnf_sha256": hashlib.sha256(dimacs).hexdigest(),
            "clause_categories": dict(sorted(cnf.categories.items())),
        }
    status = {10: "SAT", 20: "UNSAT", 0: "TIMEOUT"}.get(process.returncode, "ERROR")
    row = {
        "status": status,
        "elapsed_seconds": elapsed,
        "variables": len(cnf.names),
        "clauses": len(cnf.clauses),
        "cnf_sha256": hashlib.sha256(dimacs).hexdigest(),
        "clause_categories": dict(sorted(cnf.categories.items())),
    }
    if status == "SAT":
        true = parse_assignment(process.stdout.decode("ascii", errors="replace"))
        coloring = {
            edge: color for edge in sorted(graph.edges) for color in range(1, span + 1)
            if colors[(edge, color)] in true
        }
        valid, reason = verify_coloring(graph, coloring)
        chosen_starts = {
            vertex: start for (vertex, start), literal in starts.items() if literal in true
        }
        row["certificate_valid"] = valid
        row["certificate_reason"] = reason
        row["span"] = max(coloring.values(), default=0)
        row["starts_valid"] = (
            len(chosen_starts) == graph.n
            and all(
                set(coloring[edge] for _neighbor, edge in graph.adjacency()[vertex])
                == set(range(start, start + graph.degrees[vertex]))
                for vertex, start in chosen_starts.items()
            )
        )
    elif status == "ERROR":
        row["stderr"] = process.stderr.decode("ascii", errors="replace").strip()[-500:]
    return row


def cp_span(graph: Graph, span: int, seconds: int = 20) -> dict:
    started = time.perf_counter()
    status, coloring = fixed_span_sat_solve(graph, span, seconds, workers=1)
    row = {"status": {"OPTIMAL": "SAT", "FEASIBLE": "SAT", "INFEASIBLE": "UNSAT"}.get(status, "TIMEOUT"),
           "solver_status": status, "elapsed_seconds": time.perf_counter() - started}
    if coloring:
        row["certificate_valid"], row["certificate_reason"] = verify_coloring(graph, coloring)
        row["span"] = max(coloring.values())
    return row


def random_connected_bipartite(order: int, seed: int, min_degree: int = 1) -> Graph:
    """Deterministic connected bipartite sample, with optional degree-two floor."""

    left_count = order // 2
    right_count = order - left_count
    left = [f"a{i}" for i in range(left_count)]
    right = [f"b{i}" for i in range(right_count)]
    rng = random.Random(seed)
    edges = set()
    # A spanning tree guarantees connectivity before random extra edges are added.
    for index, vertex in enumerate(right):
        edges.add((left[index % left_count], vertex))
    for index, vertex in enumerate(left[1:], 1):
        edges.add((vertex, right[rng.randrange(right_count)]))
    density = 0.24 + 0.05 * (seed % 3)
    for u in left:
        for v in right:
            if rng.random() < density:
                edges.add((u, v))
    if min_degree >= 2:
        # Add the alternating cycle; it preserves bipartiteness and degree >= 2.
        for index, u in enumerate(left):
            edges.add((u, right[index % right_count]))
            edges.add((u, right[(index - 1) % right_count]))
        for index, v in enumerate(right):
            edges.add((left[index % left_count], v))
            edges.add((left[(index + 1) % left_count], v))
    graph = Graph(left + right, sorted(edges), [left, right],
                  {"family": "audit-random-connected", "seed": seed, "minimum_degree_target": min_degree})
    if not nx.is_connected(graph._nx) or min(graph.degrees.values()) < min_degree:
        raise AssertionError("random graph construction failed its promised invariants")
    return graph


def degree_two_cycle(order: int) -> Graph:
    half = order // 2
    left = [f"l{i}" for i in range(half)]
    right = [f"r{i}" for i in range(half)]
    edges = [(left[i], right[i]) for i in range(half)]
    edges += [(left[i], right[(i - 1) % half]) for i in range(half)]
    return Graph(left + right, edges, [left, right], {"family": "even-cycle", "order": order})


def quotient_positive_controls() -> dict[str, Graph]:
    # These are among the 23 rank-colorable reduction-one candidates reported
    # in results/quotient-r1.json. Recreate two by their recorded blocks.
    from lane1_search import seed_graph
    from quotient_search import quotient

    _digest, base = seed_graph()
    wanted = {"Q1-00000", "Q1-00002"}
    found = {}
    report = json.loads((ROOT / "results" / "quotient-r1.json").read_text())
    report_rows = report["rows"]
    by_id = {row["candidate_id"]: row for row in report_rows}
    # Construct from each reported same-side merge, which is enough here because
    # these two rows are distinct before canonical de-duplication.
    for candidate_id in sorted(wanted):
        blocks = by_id[candidate_id]["metadata"]["blocks"]
        merged = tuple(tuple(item) for item in blocks)
        remaining = tuple((vertex,) for vertex in sorted(base.vertices) if vertex not in set(merged[0]))
        graph = quotient(base, tuple(sorted(remaining + (merged[0],))), candidate_id)
        found[candidate_id] = graph
    return found


def named_cases() -> dict[str, Graph]:
    cases: dict[str, Graph] = {}
    for name, graph in benchmark_graphs().items():
        cases[f"known-noncolorable:{name}"] = graph
    for name, graph in positive_controls().items():
        cases[f"known-colorable:{name}"] = graph
    for candidate_id, graph in quotient_positive_controls().items():
        cases[f"reported-colorable:{candidate_id}"] = graph
    for candidate_id in ("Q1-00012", "Q1-00014"):
        source = ROOT / "results" / "candidates" / candidate_id / f"{candidate_id}.graph.json"
        cases[f"required-witness:{candidate_id}"] = Graph.from_json(json.loads(source.read_text()))
    for order in range(8, 17):
        cases[f"random-connected:n{order}"] = random_connected_bipartite(order, 1000 + order)
    for order in range(8, 17, 2):
        cases[f"min-degree-2-cycle:n{order}"] = degree_two_cycle(order)
        cases[f"min-degree-2-random:n{order}"] = random_connected_bipartite(order, 2000 + order, 2)
    return cases


def audit_case(name: str, graph: Graph) -> dict:
    rank_runs = [rank_potential_solve(graph, time_limit=20, workers=1) for _ in range(3)]
    rank_statuses = [result.status for result in rank_runs]
    rank_spans = [result.span for result in rank_runs]
    spans = {}
    for span in range(graph.delta, graph.n):
        production = cp_span(graph, span)
        independent = independent_span(graph, span)
        spans[str(span)] = {"production_fixed_span": production, "independent_cnf": independent}
    production_sat = any(row["production_fixed_span"]["status"] == "SAT" for row in spans.values())
    independent_sat = any(row["independent_cnf"]["status"] == "SAT" for row in spans.values())
    unresolved = any(
        row[source]["status"] == "TIMEOUT"
        for row in spans.values() for source in ("production_fixed_span", "independent_cnf")
    ) or "timeout" in rank_statuses
    disagreements = []
    for span, row in spans.items():
        left = row["production_fixed_span"]["status"]
        right = row["independent_cnf"]["status"]
        if left != right and "TIMEOUT" not in (left, right):
            disagreements.append({"kind": "fixed_span", "span": int(span), "production": left, "independent": right})
    if not unresolved and len(set(rank_statuses)) == 1:
        rank_sat = rank_statuses[0] == "colorable"
        if rank_sat != production_sat:
            disagreements.append({"kind": "rank_vs_fixed_span", "rank": rank_statuses[0], "fixed_any_sat": production_sat})
        if rank_sat != independent_sat:
            disagreements.append({"kind": "rank_vs_independent", "rank": rank_statuses[0], "independent_any_sat": independent_sat})
    return {
        "name": name, "order": graph.n, "size": graph.m, "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()), "connected": nx.is_connected(graph._nx),
        "bipartite": nx.is_bipartite(graph._nx), "span_range_checked": [graph.delta, graph.n - 1],
        "rank_potential_runs": [
            {"status": result.status, "span": result.span, "solver_status": result.solver_status,
             "elapsed_seconds": result.elapsed_seconds,
             "certificate_valid": None if result.coloring is None else verify_coloring(graph, result.coloring)[0]}
            for result in rank_runs
        ],
        "rank_deterministic_status": len(set(rank_statuses)) == 1 and len(set(rank_spans)) == 1,
        "spans": spans, "unresolved": unresolved, "disagreements": disagreements,
    }


def main() -> None:
    if not PICOSAT.is_file():
        raise SystemExit(f"PicoSAT executable missing: {PICOSAT}")
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    rows = []
    for name, graph in named_cases().items():
        row = audit_case(name, graph)
        rows.append(row)
        print(f"{name}: disagreements={len(row['disagreements'])} unresolved={row['unresolved']}", flush=True)
    all_spans = [span for row in rows for span in row["spans"].values()]
    disagreements = [
        {"case": row["name"], **item}
        for row in rows for item in row["disagreements"]
    ]
    fixed_false_negative = [
        {"case": row["name"], "span": int(span), "production": "UNSAT", "independent": "SAT"}
        for row in rows for span, item in row["spans"].items()
        if item["production_fixed_span"]["status"] == "UNSAT"
        and item["independent_cnf"]["status"] == "SAT"
    ]
    fixed_false_positive = [
        {"case": row["name"], "span": int(span), "production": "SAT", "independent": "UNSAT"}
        for row in rows for span, item in row["spans"].items()
        if item["production_fixed_span"]["status"] == "SAT"
        and item["independent_cnf"]["status"] == "UNSAT"
    ]
    rank_false_negative = [
        row["name"] for row in rows
        if not row["unresolved"]
        and row["rank_potential_runs"][0]["status"] == "non-colorable"
        and any(item["independent_cnf"]["status"] == "SAT" for item in row["spans"].values())
    ]
    rank_false_positive = [
        row["name"] for row in rows
        if not row["unresolved"]
        and row["rank_potential_runs"][0]["status"] == "colorable"
        and not any(item["independent_cnf"]["status"] == "SAT" for item in row["spans"].values())
    ]
    production_sat_valid = sum(
        item["production_fixed_span"].get("certificate_valid") is True for item in all_spans
    )
    independent_sat_valid = sum(
        item["independent_cnf"].get("certificate_valid") is True
        and item["independent_cnf"].get("starts_valid") is True
        for item in all_spans
    )
    report = {
        "audit": "independent Boolean-CNF / PicoSAT comparison",
        "picosat": str(PICOSAT.relative_to(ROOT)),
        "seed_policy": {"random_connected": "1000 + order", "minimum_degree_2_random": "2000 + order"},
        "elapsed_seconds": time.perf_counter() - started,
        "cases_tested": len(rows),
        "spans_tested": len(all_spans),
        "status_counts": {
            "production_fixed_span": dict(Counter(span["production_fixed_span"]["status"] for span in all_spans)),
            "independent_cnf": dict(Counter(span["independent_cnf"]["status"] for span in all_spans)),
        },
        "max_runtime_seconds": {
            "rank_potential": max(run["elapsed_seconds"] for row in rows for run in row["rank_potential_runs"]),
            "production_fixed_span": max(span["production_fixed_span"]["elapsed_seconds"] for span in all_spans),
            "independent_cnf": max(span["independent_cnf"]["elapsed_seconds"] for span in all_spans),
        },
        "unresolved_cases": [row["name"] for row in rows if row["unresolved"]],
        "disagreements": disagreements,
        "false_negative_risk": {
            "rank_noncolorable_vs_independent_sat": rank_false_negative,
            "fixed_span_unsat_vs_independent_sat": fixed_false_negative,
        },
        "false_positive_risk": {
            "rank_colorable_vs_no_independent_sat": rank_false_positive,
            "fixed_span_sat_vs_independent_unsat": fixed_false_positive,
        },
        "sat_certificate_validation": {
            "production_fixed_span_valid": production_sat_valid,
            "independent_cnf_valid_with_local_starts": independent_sat_valid,
        },
        "timeout_policy": "TIMEOUT/UNKNOWN is unresolved and is never counted as non-colorable",
        "rank_determinism": {
            "runs_per_case": 3,
            "single_worker": True,
            "failures": [row["name"] for row in rows if not row["rank_deterministic_status"]],
        },
        "conclusion": (
            "sound for this audit matrix: no resolved disagreement between rank-potential, "
            "fixed-span CP-SAT, and independently encoded PicoSAT CNF"
            if not disagreements and not any(row["unresolved"] for row in rows)
            else "inconclusive: inspect disagreements and unresolved cases; no timeout is a negative result"
        ),
        "cases": rows,
    }
    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    markdown = ["# Independent interval-solver audit", "", report["conclusion"], "",
                f"- Cases: {report['cases_tested']}", f"- Fixed spans: {report['spans_tested']}",
                f"- Disagreements: {len(disagreements)}", f"- Unresolved cases: {len(report['unresolved_cases'])}",
                f"- Fixed-span false-negative/false-positive indicators: {len(fixed_false_negative)}/{len(fixed_false_positive)}",
                f"- Validated SAT certificates (production/independent): {production_sat_valid}/{independent_sat_valid}",
                f"- Total elapsed: {report['elapsed_seconds']:.3f}s", "",
                "The audit checks every span from delta(G) through |V(G)| - 1. PicoSAT CNF uses separate "
                "edge-color and local-interval-start literals, pairwise incident-color exclusion, interval "
                "coverage, and global color use. All SAT assignments are validated as proper interval edge colorings."]
    (OUT / "report.md").write_text("\n".join(markdown) + "\n")


if __name__ == "__main__":
    main()
