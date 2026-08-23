#!/usr/bin/env python3
"""Bounded two-terminal synchronization-gadget search for split hubs.

The first stage enumerates tiny bipartite gadgets and uses a rank-potential
CP-SAT model to tabulate terminal ranks and boundary-color offsets.  The second
stage attaches only "concatenator" signatures to every split of the 12 original
hub neighbors and reclassifies the resulting graph over all spans.

A timeout is reported as ``timeout``; it is never converted into a negative.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Signature:
    """One feasible boundary state of an augmented gadget."""

    rank0: int
    rank1: int
    offset: int

    def sort_key(self) -> tuple[int, int, int]:
        return (self.rank0, self.rank1, self.offset)


@dataclass(frozen=True)
class Gadget:
    vertices: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    signature: frozenset[Signature]
    key: str


def _edge(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def enumerate_gadgets(
    max_internal: int,
    max_internal_degree: int,
    max_signature_solutions: int,
    signature_time_limit: float,
) -> tuple[list[Gadget], dict[str, int]]:
    """Enumerate connected bipartite gadgets with terminals on the same side."""

    if max_internal < 0 or max_internal > 4:
        raise ValueError("keep this focused prototype at no more than four internal vertices")

    gadgets_by_edges: dict[frozenset[tuple[str, str]], tuple] = {}
    right_terminals = ("T0", "T1")
    for left_count in range(max_internal + 1):
        for right_count in range(max_internal + 1 - left_count):
            left = [f"L{i}" for i in range(left_count)]
            right = list(right_terminals) + [f"R{i}" for i in range(right_count)]
            possible = [_edge(a, b) for a in left for b in right]
            for edge_count in range(2, len(possible) + 1):
                for chosen in itertools.combinations(possible, edge_count):
                    degree: dict[str, int] = defaultdict(int)
                    adjacency: dict[str, set[str]] = defaultdict(set)
                    for a, b in chosen:
                        degree[a] += 1
                        degree[b] += 1
                        adjacency[a].add(b)
                        adjacency[b].add(a)
                    if any(degree.get(v, 0) == 0 for v in left + right):
                        continue
                    if any(
                        v not in right_terminals and degree[v] > max_internal_degree
                        for v in left + right
                    ):
                        continue
                    component_nodes = set(left + right)
                    stack = [next(iter(component_nodes))]
                    seen = {stack[0]}
                    while stack:
                        v = stack.pop()
                        for w in adjacency[v]:
                            if w not in seen:
                                seen.add(w)
                                stack.append(w)
                    if seen != component_nodes:
                        continue
                    edge_set = frozenset(chosen)
                    if edge_set not in gadgets_by_edges:
                        gadgets_by_edges[edge_set] = (tuple(left + right), tuple(sorted(chosen)))

    output: list[Gadget] = []
    excluded_timeout = 0
    excluded_solution_cap = 0
    for edge_set, (vertices, edges) in sorted(gadgets_by_edges.items(), key=lambda item: item[1]):
        augmented_vertices = ("X0", "X1") + vertices
        augmented_edges = (_edge("X0", "T0"), _edge("X1", "T1")) + edges
        augmented = Graph(
            augmented_vertices,
            augmented_edges,
            [
                ["X0", "X1"] + [v for v in vertices if v.startswith("L")],
                [v for v in vertices if not v.startswith("L")],
            ],
        )
        signature = terminal_signatures(
            augmented,
            "T0",
            "T1",
            max_signature_solutions,
            signature_time_limit,
        )
        if signature is None:
            excluded_timeout += 1
            continue
        if len(signature) > max_signature_solutions:
            excluded_solution_cap += 1
            continue
        output.append(
            Gadget(
                vertices,
                edges,
                frozenset(signature),
                json.dumps(edges, separators=(",", ":")),
            )
        )
    stats = {
        "candidate_edge_sets": len(gadgets_by_edges),
        "excluded_signature_timeout": excluded_timeout,
        "excluded_solution_cap": excluded_solution_cap,
    }
    return output, stats


def terminal_signatures(
    augmented: Graph,
    t0: str,
    t1: str,
    max_solutions: int,
    time_limit: float,
) -> list[Signature] | None:
    """Tabulate feasible terminal ranks and boundary-color offsets exactly."""

    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    adjacency = augmented.adjacency()
    degrees = augmented.degrees
    bound = sum(d - 1 for d in degrees.values())
    potential = {
        v: model.NewIntVar(-bound, bound, f"a_{i}")
        for i, v in enumerate(augmented.vertices)
    }
    model.Add(potential[augmented.vertices[0]] == 0)
    rank: dict[tuple[str, tuple[str, str]], object] = {}
    for vi, vertex in enumerate(augmented.vertices):
        local = []
        for ni, (neighbor, edge) in enumerate(adjacency[vertex]):
            var = model.NewIntVar(0, degrees[vertex] - 1, f"p_{vi}_{ni}")
            rank[(vertex, edge)] = var
            local.append(var)
        model.AddAllDifferent(local)
    for u, v in augmented.edges:
        edge = (u, v)
        model.Add(potential[u] + rank[(u, edge)] == potential[v] + rank[(v, edge)])

    offset = model.NewIntVar(-2 * bound, 2 * bound, "offset")
    model.Add(offset == potential["X1"] - potential["X0"])

    found: set[Signature] = set()

    class Collector(cp_model.CpSolverSolutionCallback):
        def on_solution_callback(self) -> None:
            if len(found) >= max_solutions:
                self.StopSearch()
                return
            found.add(
                Signature(
                    int(self.Value(rank[(t0, _edge("X0", t0))])),
                    int(self.Value(rank[(t1, _edge("X1", t1))])),
                    int(self.Value(offset)),
                )
            )

    solver = cp_model.CpSolver()
    solver.parameters.enumerate_all_solutions = True
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = float(time_limit)
    status = solver.Solve(model, Collector())
    status_name = solver.StatusName(status)
    if status_name == "INFEASIBLE":
        return []
    if status_name not in ("OPTIMAL", "FEASIBLE"):
        return None
    # A stopped search is not a completed finite table.
    if len(found) >= max_solutions and status_name != "OPTIMAL":
        return None
    return sorted(found, key=Signature.sort_key)


def is_concatenator(signature: frozenset[Signature], strict_extreme_only: bool = False) -> bool:
    """Test for a forced adjacent-palette join in either orientation.

    The strict mode asks both boundary half-edges to be maximal at their own
    terminals.  That is sufficient for a palette endpoint but was not realized
    by any gadget through four internal vertices.  The default mode accepts any
    forced adjacent offset; hub-side rank feasibility is then checked exactly by
    the full rank-potential classification.
    """

    d0 = max(s.rank0 for s in signature) + 1
    d1 = max(s.rank1 for s in signature) + 1

    def adjacent_offset_for(r0: int, r1: int, sign: int) -> bool:
        states = [s.offset for s in signature if s.rank0 == r0 and s.rank1 == r1]
        return bool(states) and all(offset == sign for offset in states)

    forward = any(
        s.rank0 == d0 - 1 and s.rank1 == 0 and s.offset == 1 for s in signature
    )
    reverse = any(
        s.rank0 == 0 and s.rank1 == d1 - 1 and s.offset == -1 for s in signature
    )
    if strict_extreme_only:
        return bool(forward) or bool(reverse)

    for r0 in range(d0):
        for r1 in range(d1):
            if adjacent_offset_for(r0, r1, 1) or adjacent_offset_for(r0, r1, -1):
                return True
    return False


def split_hub_candidate(base: Graph, mask: int, gadget: Gadget) -> Graph:
    """Split hub ``u`` and bridge U0/U1 through the selected gadget."""

    connectors = sorted(base.bipartition[1])
    if len(connectors) != 12:
        raise ValueError("expected the 12 connectors of the hat K_(3,4) seed")
    core = [v for v in base.bipartition[0] if v != "u"]
    left = ["U0", "U1"] + core + [v for v in gadget.vertices if v.startswith("L")]
    right = list(connectors) + [v for v in gadget.vertices if not v.startswith("L")]
    edges: list[tuple[str, str]] = []
    for index, connector in enumerate(connectors):
        edges.extend(edge for edge in base.edges if connector in edge and "u" not in edge)
        hub = "U0" if mask & (1 << index) else "U1"
        edges.append((hub, connector))
    edges.extend(gadget.edges)
    edges.extend((_edge("U0", "T0"), _edge("U1", "T1")))
    graph = Graph(left + right, edges, [left, right])
    graph.metadata = {
        "lane": "lane6-terminal-signature",
        "gadget_edges": [list(edge) for edge in gadget.edges],
        "mask": format(mask, "012b"),
        "u0_degree": sum(bool(mask & (1 << i)) for i in range(12)) + 1,
        "u1_degree": sum(not bool(mask & (1 << i)) for i in range(12)) + 1,
    }
    return graph


def _result_summary(result) -> dict:
    return {key: value for key, value in result.__dict__.items() if key != "coloring"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-internal", type=int, default=2)
    parser.add_argument("--max-internal-degree", type=int, default=4)
    parser.add_argument("--max-signature-solutions", type=int, default=5000)
    parser.add_argument("--signature-time-limit", type=float, default=5.0)
    parser.add_argument("--minimum-hub-degree", type=int, default=3)
    parser.add_argument(
        "--signature-filter",
        choices=("forced_adjacent_offset", "strict_extreme"),
        default="forced_adjacent_offset",
    )
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument("--independent-time-limit", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-interval", type=int, default=50)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    started = time.perf_counter()
    _, base = seed_graph()
    gadgets, gadget_stats = enumerate_gadgets(
        args.max_internal,
        args.max_internal_degree,
        args.max_signature_solutions,
        args.signature_time_limit,
    )
    signature_counts = Counter(len(gadget.signature) for gadget in gadgets)
    concatenators = [gadget for gadget in gadgets if is_concatenator(gadget.signature)]
    strict_extreme_concatenators = [
        gadget for gadget in gadgets if is_concatenator(gadget.signature, True)
    ]
    selected_concatenators = (
        strict_extreme_concatenators
        if args.signature_filter == "strict_extreme"
        else concatenators
    )

    rows: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    for gadget in selected_concatenators:
        for mask in range(1 << 12):
            graph = split_hub_candidate(base, mask, gadget)
            hub_degrees = (graph.degrees["U0"], graph.degrees["U1"])
            if (
                min(hub_degrees) < args.minimum_hub_degree
                or graph.delta > 10
                or min(graph.degrees.values()) < 2
                or not nx.is_connected(graph._nx)
            ):
                skipped += 1
                continue
            digest = nauty_canonical_hash(graph)
            if digest in seen:
                skipped += 1
                continue
            seen.add(digest)
            result = rank_potential_solve(graph, args.time_limit, args.workers)
            row: dict = {
                "candidate_id": f"L6S-{len(rows):04d}",
                "canonical_sha256": digest,
                "order": graph.n,
                "size": graph.m,
                "delta": graph.delta,
                "gadget_edges": [list(edge) for edge in gadget.edges],
                "signature_filter": args.signature_filter,
                "signature": [s.sort_key() for s in sorted(gadget.signature, key=Signature.sort_key)],
                "metadata": graph.metadata,
                "weighted_hubs_best": weighted_hub_statistics(graph)[:2],
                "primary_result": _result_summary(result),
            }
            if result.status == "non-colorable":
                independent = all_spans_solve(
                    graph, args.independent_time_limit, args.workers, False
                )
                row["independent_spans"] = independent
                if independent["decision"] != "non-colorable":
                    row["classification"] = "conflicting"
            rows.append(row)
            if args.checkpoint_interval and len(rows) % args.checkpoint_interval == 0:
                print(
                    json.dumps(
                        {
                            "completed": len(rows),
                            "status_counts": dict(
                                Counter(row["primary_result"]["status"] for row in rows)
                            ),
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )

    counts = Counter(row["primary_result"]["status"] for row in rows)
    summary = {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "gadget_enumeration": {
            "max_internal": args.max_internal,
            "max_internal_degree": args.max_internal_degree,
            "connected_bipartite_gadgets": len(gadgets),
            **gadget_stats,
            "signature_size_counts": dict(sorted(signature_counts.items())),
            "concatenator_gadgets": len(concatenators),
            "strict_extreme_concatenator_gadgets": len(strict_extreme_concatenators),
            "concatenator_edges": [list(g.edges) for g in concatenators],
        },
        "composition": {
            "hub_neighbor_splits": 1 << 12,
            "generated_before_filters": len(concatenators) * (1 << 12),
            "signature_filter": args.signature_filter,
            "selected_concatenator_gadgets": len(selected_concatenators),
            "skipped_filtered_or_duplicate": skipped,
            "completed_unique": len(rows),
            "counts": {
                status: counts.get(status, 0)
                for status in ("colorable", "non-colorable", "timeout")
            },
            "family_exhausted": True,
            "negative_independently_confirmed": sum(
                row.get("independent_spans", {}).get("decision") == "non-colorable"
                for row in rows
            ),
            "conflicting_classifications": sum(
                row.get("classification") == "conflicting" for row in rows
            ),
        },
        "rows": rows,
    }
    rendered = json.dumps(summary, indent=2) + "\n"
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered)
        print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))
    else:
        print(rendered)


if __name__ == "__main__":
    main()
