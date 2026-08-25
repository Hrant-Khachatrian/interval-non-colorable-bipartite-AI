#!/usr/bin/env python3
"""Fresh rooted-gadget degree-transfer pilot, disjoint from prior certificates."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import networkx as nx

from degree_transfer_delta10_search import (
    RootedMotif,
    covering_selections,
    demand_edges,
    independent_confirmation,
    parent_graphs,
)
from interval_edge_coloring import Graph, nauty_canonical_hash, rank_potential_solve


def cycle_motif(name: str, length: int) -> RootedMotif:
    labels = tuple(f"c{i}" for i in range(length - 1))
    chain = ("R", *labels, "R")
    return RootedMotif(name, name, labels, (), tuple((chain[i], chain[i + 1]) for i in range(length)))


def theta_motif(name: str, paths: int) -> RootedMotif:
    labels = ["T"]
    edges: list[tuple[str, str]] = []
    for path in range(paths):
        a, b, c = (f"{prefix}{path}" for prefix in ("a", "b", "c"))
        labels.extend((a, b, c))
        edges.extend((("R", a), (a, b), (b, c), (c, "T")))
    return RootedMotif(name, name, tuple(labels), (), tuple(edges))


def complete_bipartite_motif(name: str, root_side: int, other_side: int) -> RootedMotif:
    left = ("R", *(f"L{i}" for i in range(root_side - 1)))
    right = tuple(f"P{i}" for i in range(other_side))
    return RootedMotif(name, name, tuple((*left[1:], *right)), (), tuple((u, v) for u in left for v in right))


FRESH_MOTIFS = (
    cycle_motif("rooted_C12", 12),
    theta_motif("rooted_theta_3x4", 3),
    theta_motif("rooted_theta_4x4", 4),
    complete_bipartite_motif("rooted_K_2_5", 2, 5),
    complete_bipartite_motif("rooted_K_3_5", 3, 5),
    RootedMotif(
        "three_rooted_C4_blocks",
        "three_rooted_C4_blocks",
        tuple(f"b{block}_{offset}" for block in range(3) for offset in range(3)),
        (),
        tuple(
            edge
            for block in range(3)
            for edge in (("R", f"b{block}_0"), (f"b{block}_0", f"b{block}_1"), (f"b{block}_1", f"b{block}_2"), (f"b{block}_2", "R"))
        ),
    ),
)


def motif_sides(motif: RootedMotif) -> dict[str, int]:
    adjacency = {label: [] for label in ("R", *motif.internal_labels)}
    for u, v in motif.edges:
        adjacency[u].append(v)
        adjacency[v].append(u)
    sides = {"R": 0}
    queue = ["R"]
    while queue:
        vertex = queue.pop()
        for neighbor in adjacency[vertex]:
            if neighbor not in sides:
                sides[neighbor] = 1 - sides[vertex]
                queue.append(neighbor)
            elif sides[neighbor] == sides[vertex]:
                raise ValueError(f"non-bipartite motif {motif.name}")
    if set(sides) != set(adjacency):
        raise ValueError(f"disconnected motif {motif.name}")
    return sides


MOTIF_SIDES = {motif.name: motif_sides(motif) for motif in FRESH_MOTIFS}


def load_seed_hashes(paths: Iterable[Path]) -> set[str]:
    hashes: set[str] = set()
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        hashes.update(row["canonical_sha256"] for row in data.get("records", []))
    return hashes


def degree_cap_holds(base: Graph, selected: tuple[int, ...], motifs: tuple[RootedMotif, ...], maximum_delta: int) -> bool:
    changes = {vertex: 0 for vertex in base.vertices}
    overcap = {vertex for vertex, degree in base.degrees.items() if degree > maximum_delta}
    for edge_index, motif in zip(selected, motifs):
        u, v = base.edges[edge_index]
        hubs = [vertex for vertex in (u, v) if vertex in overcap]
        if len(hubs) != 1:
            return False
        hub = hubs[0]
        root = v if u == hub else u
        changes[hub] -= 1
        changes[root] += motif.root_degree - 1
    return all(base.degrees[vertex] + changes[vertex] <= maximum_delta for vertex in base.vertices)


def apply(base: Graph, selected: tuple[int, ...], motifs: tuple[RootedMotif, ...], serial: int) -> Graph:
    overcap = {vertex for vertex, degree in base.degrees.items() if degree > 10}
    vertices = list(base.vertices)
    left, right = set(base.bipartition[0]), set(base.bipartition[1])
    removed = {tuple(sorted(base.edges[index])) for index in selected}
    edges = [edge for edge in base.edges if tuple(sorted(edge)) not in removed]
    for sequence, (edge_index, motif) in enumerate(zip(selected, motifs)):
        u, v = base.edges[edge_index]
        root = v if u in overcap else u
        mapping = {"R": root}
        for label in motif.internal_labels:
            name = f"F{serial:05d}_{sequence:02d}_{label}"
            mapping[label] = name
            vertices.append(name)
            root_is_left = root in left
            is_left = (MOTIF_SIDES[motif.name][label] == 0) == root_is_left
            (left if is_left else right).add(name)
        edges.extend((mapping[u0], mapping[v0]) for u0, v0 in motif.edges)
    if len(edges) != len({tuple(sorted(edge)) for edge in edges}):
        raise AssertionError("fresh construction produced a duplicate edge")
    return Graph(vertices, edges, [sorted(left), sorted(right)], {"lane": "fresh-degree-transfer-terminal-motifs"})


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed-report", type=Path, action="append", required=True)
    parser.add_argument("--parent", default="M5_delta_555")
    parser.add_argument("--unique-cap", type=int, default=1000)
    parser.add_argument("--deadline-seconds", type=float, default=900.0)
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if not 0 < args.unique_cap <= 2000 or not 0 < args.deadline_seconds <= 1800 or not 0 < args.time_limit <= 10 or not 0 < args.workers <= 8:
        parser.error("pilot bounds exceeded")
    started = time.monotonic()
    deadline = started + args.deadline_seconds
    graph_dir = args.output.parent / "graphs"
    available = {name: graph for name, graph, _ in parent_graphs(graph_dir)}
    base = available[args.parent]
    seed_hashes = load_seed_hashes(args.seed_report)
    state = args.output.with_suffix(".jsonl")
    rows = [json.loads(line) for line in state.read_text(encoding="utf-8").splitlines()] if state.exists() else []
    seen = set(seed_hashes) | {row["canonical_sha256"] for row in rows}
    generated = 0
    duplicates = 0
    accepted = len(rows)
    candidate_edges = demand_edges(base, 10)
    stop = "family_exhausted"
    for selected in covering_selections(base, candidate_edges, 10, 6, deadline):
        for motifs in itertools.product(FRESH_MOTIFS, repeat=len(selected)):
            if time.monotonic() >= deadline:
                stop = "deadline"
                break
            generated += 1
            if not degree_cap_holds(base, selected, motifs, 10):
                continue
            graph = apply(base, tuple(selected), motifs, generated)
            nx_graph = nx.Graph(graph.edges)
            nx_graph.add_nodes_from(graph.vertices)
            if not nx.is_connected(nx_graph) or not nx.is_bipartite(nx_graph) or min(graph.degrees.values()) < 2 or graph.delta > 10:
                raise AssertionError("fresh construction violated a required graph invariant")
            digest = nauty_canonical_hash(graph)
            if digest in seen:
                duplicates += 1
                continue
            primary = rank_potential_solve(graph, args.time_limit, args.workers)
            row = {
                "parent": args.parent,
                "selected_parent_edges": list(selected),
                "fresh_motifs": [motif.name for motif in motifs],
                "canonical_sha256": digest,
                "order": graph.n, "size": graph.m, "delta": graph.delta,
                "minimum_degree": min(graph.degrees.values()), "connected": True, "bipartite": True,
                "primary_status": primary.status, "primary_span": primary.span,
                "primary_elapsed_seconds": primary.elapsed_seconds,
            }
            if primary.status == "non-colorable":
                confirmed, unresolved, spans = independent_confirmation(graph, args.time_limit, args.workers, deadline - 2.0)
                row["independent_spans"] = {str(key): value for key, value in spans.items()}
                row["decision"] = "non-colorable" if confirmed else "unresolved" if unresolved else "solver_disagreement"
            else:
                row["decision"] = "colorable" if primary.status == "colorable" else "unresolved_primary_timeout"
            with state.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            rows.append(row)
            seen.add(digest)
            accepted += 1
            if accepted >= args.unique_cap:
                stop = "unique_cap"
                break
            if accepted % 50 == 0:
                print(json.dumps({"event": "checkpoint", "classified": accepted, "generated": generated}, sort_keys=True), flush=True)
        if stop in {"deadline", "unique_cap"}:
            break
    decisions = [row["decision"] for row in rows]
    report = {
        "schema": "degree-transfer-delta10-fresh-motifs-pilot-v1",
        "complete": stop in {"family_exhausted", "unique_cap"} and not any(value.startswith("unresolved") for value in decisions),
        "stop": stop,
        "configuration": {
            "parent": args.parent, "unique_cap": args.unique_cap, "maximum_final_delta": 10,
            "minimum_degree": 2, "seed_reports": [str(path) for path in args.seed_report],
            "seed_canonical_certificates": len(seed_hashes),
            "fresh_terminal_motifs": [motif.name for motif in FRESH_MOTIFS],
            "primary_classification": "rank-potential CP-SAT",
            "negative_confirmation": "fixed-span CP-SAT for every legal span",
            "timeout_policy": "unresolved, never negative",
        },
        "counts": {
            "generated": generated, "unique_classified": len(rows), "colorable": decisions.count("colorable"),
            "non_colorable": decisions.count("non-colorable"),
            "timeout": sum(value.startswith("unresolved") for value in decisions), "duplicates": duplicates,
        },
        "records": rows,
        "elapsed_seconds": time.monotonic() - started,
    }
    write_json(args.output, report)
    print(json.dumps({"event": "complete", "stop": stop, "counts": report["counts"]}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
