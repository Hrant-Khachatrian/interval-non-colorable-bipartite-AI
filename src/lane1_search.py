#!/usr/bin/env python3
"""Lane 1: exact search of hub-edge subsets near the known Delta=11 graph."""

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
    benchmark_graphs,
    nauty_canonical_hash,
    rank_potential_solve,
)


def seed_graph() -> tuple[str, Graph]:
    graphs = benchmark_graphs()
    return "hat_K34_prime_Delta11", graphs["hat_K34_prime_Delta11"]


def apply_hub_subset(base: Graph, connectors: list[str], selected: frozenset[str], index: int) -> Graph:
    edges = []
    for connector in connectors:
        core_edges = [
            edge for edge in base.edges
            if "u" not in edge and (edge[0] == connector or edge[1] == connector)
        ]
        if len(core_edges) != 2:
            raise ValueError(f"unexpected connector {connector}")
        edges.extend(core_edges)
        if connector in selected:
            edges.append(tuple(sorted((base.metadata.get("hub", "u"), connector))))
    left, right = base.bipartition
    return Graph(
        list(base.vertices),
        edges,
        [left, right],
        {
            "lane": "lane1-hub-subsets",
            "parent": "hat_K34_prime_Delta11",
            "mutation": f"hub_degree={len(selected)}",
            "generator_index": index,
        },
    )


def generate_hub_subsets(
    base: Graph,
    minimum_hub_degree: int = 8,
    maximum_hub_degree: int | None = None,
) -> list[tuple[str, Graph]]:
    connectors = sorted(base.bipartition[1])
    maximum = len(connectors) if maximum_hub_degree is None else maximum_hub_degree
    output = []
    seen = set()
    for degree in range(minimum_hub_degree, maximum + 1):
        for number, selected in enumerate(itertools.combinations(connectors, degree)):
            candidate = apply_hub_subset(base, connectors, frozenset(selected), len(output))
            if not nx.is_connected(candidate._nx):
                continue
            if min(candidate.degrees.values()) < 2:
                continue
            digest = nauty_canonical_hash(candidate)
            if digest in seen:
                continue
            seen.add(digest)
            output.append((digest, candidate))
    return output


def _switch_edge_set(
    base: Graph,
    removed: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...] | None:
    edge_set = set(map(tuple, base.edges))
    if any(edge not in edge_set for edge in removed):
        return None
    (a, b), (c, d) = removed
    endpoints = {a, b, c, d}
    if len(endpoints) != 4:
        return None
    additions = (tuple(sorted((a, d))), tuple(sorted((b, c))))
    if any(edge in edge_set for edge in additions):
        return None
    return tuple(sorted((edge_set - set(removed)) | set(additions)))


def _candidate_from_edges(
    base: Graph,
    edges: tuple[tuple[str, str], ...],
    mutation: str,
    parent: str,
) -> Graph | None:
    try:
        left, right = base.bipartition
        candidate = Graph(
            list(base.vertices),
            edges,
            [left, right],
            {"lane": "lane1-degree-preserving-switches", "parent": parent, "mutation": mutation},
        )
    except ValueError:
        return None
    if not nx.is_connected(candidate._nx) or min(candidate.degrees.values()) < 2:
        return None
    return candidate


def generate_switch_neighborhood(
    base: Graph,
    maximum_depth: int = 2,
    include_base: bool = True,
) -> list[tuple[str, Graph]]:
    seen: dict[str, Graph] = {}
    if include_base:
        copy = Graph(
            list(base.vertices),
            list(base.edges),
            [base.bipartition[0], base.bipartition[1]],
            {
                "lane": "lane1-degree-preserving-switches",
                "parent": "published_seed",
                "mutation": "identity",
            },
        )
        seen[nauty_canonical_hash(copy)] = copy

    frontier = [(tuple(sorted(base.edges)), "identity")]
    visited_edge_sets = {frontier[0][0]}
    for depth in range(1, maximum_depth + 1):
        next_frontier = []
        for current_edges, history in frontier:
            # Switches are defined relative to the seed's fixed degree sequence;
            # every reachable member has the same multiset of edge endpoints.
            proxy = Graph(list(base.vertices), current_edges, [base.bipartition[0], base.bipartition[1]])
            edge_list = sorted(proxy.edges)
            for (e1, e2) in itertools.combinations(edge_list, 2):
                replacement = _switch_edge_set(proxy, (e1, e2))
                if replacement is None or replacement in visited_edge_sets:
                    continue
                candidate = _candidate_from_edges(
                    base,
                    replacement,
                    f"depth{depth};{history};-{e1[0]}{e1[1]},-{e2[0]}{e2[1]},+{replacement[-2][0]}{replacement[-2][1]},+{replacement[-1][0]}{replacement[-1][1]}",
                    "hat_K34_prime_Delta11",
                )
                if candidate is None:
                    continue
                digest = nauty_canonical_hash(candidate)
                if digest not in seen:
                    seen[digest] = candidate
                    next_frontier.append((replacement, f"S{len(next_frontier)}"))
                visited_edge_sets.add(replacement)
        frontier = next_frontier
        print(json.dumps({"depth": depth, "new_unique_total": len(seen)}), flush=True)
        if not frontier:
            break
    return list(seen.items())


def _core_matrices(total_connectors: int = 12, rows: int = 3, cols: int = 4):
    """Exact core-margin matrices: every row sums to 4 and column to 3."""

    def row_compositions(total: int):
        for a in range(total + 1):
            for b in range(total - a + 1):
                for c in range(total - a - b + 1):
                    yield (a, b, c, total - a - b - c)

    column_totals = (3, 3, 3, 3)
    for row0 in row_compositions(4):
        for row1 in row_compositions(4):
            row2 = tuple(column_totals[c] - row0[c] - row1[c] for c in range(cols))
            if all(value >= 0 for value in row2):
                yield (row0, row1, row2)


def generate_connector_redistributions(base: Graph) -> list[tuple[str, Graph]]:
    """Exhaustive connector multisets on the K(3,4) core with 11 hub edges."""

    core_left = sorted(v for v in base.bipartition[0] if v != "u")
    core_right = sorted(base.bipartition[0])[-4:]
    # The benchmark's naming convention is deliberately explicit rather than
    # relying on sorted-name adjacency: three vertices have degree 4 in the
    # full hat core and four have degree 3.
    core_left = ["V0_1", "V0_2", "V0_3"]
    core_right = ["V1_4", "V1_5", "V1_6", "V1_7"]
    seen: dict[str, Graph] = {}
    number = 0
    for matrix in _core_matrices():
        cells = []
        for row, matrix_row in enumerate(matrix):
            for col, multiplicity in enumerate(matrix_row):
                if multiplicity:
                    cells.extend([(row, col)] * multiplicity)
        if len(cells) != 12:
            raise AssertionError("connector redistribution does not conserve order")
        for unhubbed_index in range(len(cells)):
            right_vertices = []
            edges = []
            counters: dict[tuple[int, int], int] = {}
            for index, cell in enumerate(cells):
                counters[cell] = counters.get(cell, -1) + 1
                vertex = f"R{cell[0]}{cell[1]}_{counters[cell]}"
                right_vertices.append(vertex)
                edges.append((vertex, core_left[cell[0]]))
                edges.append((vertex, core_right[cell[1]]))
                if index != unhubbed_index:
                    edges.append(("u", vertex))
            vertices = ["u"] + core_left + core_right + right_vertices
            candidate = Graph(
                vertices,
                edges,
                [["u"] + core_left + core_right, right_vertices],
                {
                    "lane": "lane1-connector-redistribution",
                    "parent": "hat_K34_prime_Delta11",
                    "matrix": [list(col) for col in matrix],
                    "unhubbed_cell": list(cells[unhubbed_index]),
                },
            )
            if (
                candidate.delta > 11
                or min(candidate.degrees.values()) < 2
                or not nx.is_connected(candidate._nx)
            ):
                continue
            digest = nauty_canonical_hash(candidate)
            if digest not in seen:
                number += 1
                seen[digest] = candidate
    return list(seen.items())


def classify_candidates(
    candidates: list[tuple[str, Graph]],
    time_limit: float,
    workers: int,
    results_path: Path,
    checkpoint_every: int = 100,
) -> list[dict]:
    rows = []
    started = time.time()
    for number, (digest, graph) in enumerate(candidates):
        result = rank_potential_solve(graph, time_limit=time_limit, workers=workers)
        row = {
            "candidate_id": f"L1-{number:04d}",
            "canonical_sha256": digest,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "degrees": graph.degrees,
            "metadata": graph.metadata,
            "primary_result": {
                key: value for key, value in result.__dict__.items() if key != "coloring"
            },
        }
        rows.append(row)
        if result.status == "non-colorable":
            directory = results_path.parent / "graphs" / "lane1"
            directory.mkdir(parents=True, exist_ok=True)
            graph.save(directory / f"{row['candidate_id']}.graph.json")
        if (number + 1) % checkpoint_every == 0 or number + 1 == len(candidates):
            payload = {
                "stage": "discovery-primary-oracle",
                "completed": number + 1,
                "total": len(candidates),
                "elapsed_seconds": time.time() - started,
                "counts": {
                    status: sum(r["primary_result"]["status"] == status for r in rows)
                    for status in ("colorable", "non-colorable", "timeout")
                },
                "rows": rows,
            }
            results_path.write_text(json.dumps(payload, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "completed": payload["completed"],
                        "total": payload["total"],
                        "counts": payload["counts"],
                        "elapsed_seconds": round(payload["elapsed_seconds"], 3),
                    }
                ),
                flush=True,
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["hub-subsets", "switches", "redistribution"], default="hub-subsets"
    )
    parser.add_argument("--min-hub-degree", type=int, default=8)
    parser.add_argument("--max-hub-degree", type=int, default=11)
    parser.add_argument("--max-switch-depth", type=int, default=2)
    parser.add_argument("--time-limit", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", default="results/lane1-hub-subsets.json")
    args = parser.parse_args()

    _, base = seed_graph()
    print("generating and canonicalizing candidates", flush=True)
    if args.mode == "hub-subsets":
        candidates = generate_hub_subsets(base, args.min_hub_degree, args.max_hub_degree)
        counts_by_degree = {}
        for _, graph in candidates:
            degree = int(graph.metadata["mutation"].split("=")[1])
            counts_by_degree[degree] = counts_by_degree.get(degree, 0) + 1
    else:
        if args.mode == "switches":
            candidates = generate_switch_neighborhood(base, args.max_switch_depth)
            counts_by_degree = {"depth<=%d" % args.max_switch_depth: len(candidates)}
        else:
            candidates = generate_connector_redistributions(base)
            counts_by_degree = {"connector_matrices": len(candidates)}
    print(json.dumps({"unique_candidates": len(candidates), "by_hub_degree": counts_by_degree}), flush=True)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = classify_candidates(candidates, args.time_limit, args.workers, output)
    negatives = [row for row in rows if row["primary_result"]["status"] == "non-colorable"]
    print(
        json.dumps(
            {
                "total_unique": len(rows),
                "negative_count": len(negatives),
                "timeout_count": sum(r["primary_result"]["status"] == "timeout" for r in rows),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
