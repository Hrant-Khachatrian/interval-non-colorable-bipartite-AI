#!/usr/bin/env python3
"""Broad bounded reductions of every locally available interval-coloring witness.

This is deliberately separate from ``reduce_known_candidates.py``.  It starts
from the certified Q1 witnesses and from the named M5/Erdos--Fano/hat seeds,
then applies small bipartition-preserving reductions.  Only graphs satisfying
``n <= 18 or Delta <= 10`` are sent to the exact oracle.
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from collections import Counter
from pathlib import Path
from typing import Iterable

import networkx as nx

from interval_edge_coloring import (
    Graph,
    all_spans_solve,
    benchmark_graphs,
    nauty_canonical_hash,
    rank_potential_solve,
)


NAMED_SEEDS = (
    "M5_delta_555",
    "Erd_Fano_2222221",
    "hat_K222",
    "hat_K34",
    "hat_K34_prime_Delta11",
)


def remove_vertices(graph: Graph, vertices: Iterable[str]) -> Graph:
    removed = set(vertices)
    left, right = graph.bipartition
    return Graph(
        [v for v in graph.vertices if v not in removed],
        [edge for edge in graph.edges if edge[0] not in removed and edge[1] not in removed],
        [[v for v in left if v not in removed], [v for v in right if v not in removed]],
        graph.metadata,
    )


def remove_edges(graph: Graph, edges: Iterable[tuple[str, str]]) -> Graph:
    removed = {tuple(sorted(edge)) for edge in edges}
    return Graph(graph.vertices, [edge for edge in graph.edges if edge not in removed], graph.bipartition, graph.metadata)


def identify_same_side(graph: Graph, first: str, second: str) -> Graph:
    """Identify two vertices from a bipartition class, suppressing parallel edges."""

    if first == second:
        raise ValueError("identification needs distinct vertices")
    left, right = graph.bipartition
    if (first in left) != (second in left):
        raise ValueError("identification crosses the bipartition")
    merged = f"I[{first}|{second}]"
    vertices = [v for v in graph.vertices if v not in (first, second)] + [merged]
    edges = []
    for u, v in graph.edges:
        u = merged if u in (first, second) else u
        v = merged if v in (first, second) else v
        if u != v:
            edges.append((u, v))
    if first in left:
        left = [v for v in left if v not in (first, second)] + [merged]
    else:
        right = [v for v in right if v not in (first, second)] + [merged]
    return Graph(vertices, edges, [left, right], graph.metadata)


def valid_target(graph: Graph, min_degree: int, cap: int) -> bool:
    return (
        graph.n > 0
        and (graph.n <= 18 or graph.delta <= cap)
        and min(graph.degrees.values(), default=0) >= min_degree
        and nx.is_connected(graph._nx)
        and nx.is_bipartite(graph._nx)
    )


def cap_edge_sets(graph: Graph, cap: int, max_sets: int) -> list[tuple[tuple[str, str], ...]]:
    """Lexicographically bounded exact-minimum hitting sets for over-cap vertices."""

    excess = {v: d - cap for v, d in graph.degrees.items() if d > cap}
    if not excess:
        return [()]
    required = sum(excess.values())
    choices = [edge for edge in graph.edges if edge[0] in excess or edge[1] in excess]
    result = []
    for selected in itertools.combinations(choices, required):
        hits = Counter(v for edge in selected for v in edge if v in excess)
        if all(hits[v] >= need for v, need in excess.items()):
            result.append(selected)
            if len(result) >= max_sets:
                break
    return result


def cross_restorations(graph: Graph, limit: int) -> list[tuple[tuple[str, str], ...]]:
    """One or two new cross edges, always retaining simplicity and the degree cap."""

    left, right = graph.bipartition
    existing = set(graph.edges)
    absent = [tuple(sorted((u, v))) for u in left for v in right if tuple(sorted((u, v))) not in existing]
    degree = graph.degrees
    singles = [edge for edge in absent if degree[edge[0]] < 10 and degree[edge[1]] < 10]
    result: list[tuple[tuple[str, str], ...]] = [(edge,) for edge in singles[:limit]]
    if len(result) >= limit:
        return result
    for first, second in itertools.combinations(singles, 2):
        impact = Counter(first + second)
        if all(degree[v] + count <= 10 for v, count in impact.items()):
            result.append((first, second))
            if len(result) >= limit:
                break
    return result


def switches(graph: Graph, limit: int) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """Deterministic bipartite two-edge switches, preserving all degrees."""

    left, right = map(set, graph.bipartition)
    existing = set(graph.edges)
    result = []
    for (u, v), (x, y) in itertools.combinations(graph.edges, 2):
        if len({u, v, x, y}) != 4:
            continue
        if u in right:
            u, v = v, u
        if x in right:
            x, y = y, x
        replacement = (tuple(sorted((u, y))), tuple(sorted((x, v))))
        if replacement[0] in existing or replacement[1] in existing:
            continue
        result.append(replacement)
        if len(result) >= limit:
            break
    return result


def switched(graph: Graph, replacement: tuple[tuple[str, str], tuple[str, str]]) -> Graph:
    new_edges = list(graph.edges)
    # Infer the old pair from the four endpoints and the absent replacement.
    endpoints = {v for edge in replacement for v in edge}
    old = [edge for edge in graph.edges if set(edge) <= endpoints]
    if len(old) != 2:
        raise ValueError("bad switch reconstruction")
    old_set = set(old)
    return Graph(graph.vertices, [edge for edge in graph.edges if edge not in old_set] + list(replacement), graph.bipartition, graph.metadata)


def load_seeds() -> list[tuple[str, Graph]]:
    paths = list(Path("results/candidates").glob("*/*.graph.json"))
    found: dict[str, Graph] = {}
    for path in paths:
        graph = Graph.from_json(json.loads(path.read_text(encoding="utf-8")))
        found[f"candidate:{path.parent.name}"] = graph
    for name, graph in benchmark_graphs().items():
        if name in NAMED_SEEDS:
            found[f"benchmark:{name}"] = graph
    # Primary priority is closeness to the target, then fewer cap violations.
    return sorted(found.items(), key=lambda item: (
        max(0, item[1].n - 18), sum(d > 10 for d in item[1].degrees.values()), item[1].delta, item[0]
    ))


def result_summary(result) -> dict:
    return {
        "status": result.status, "encoding": result.encoding, "elapsed_seconds": result.elapsed_seconds,
        "span": result.span, "solver_status": result.solver_status, "conflicts": result.conflicts,
        "branches": result.branches, "wall_time": result.wall_time,
    }


def external_hashes(root: Path) -> set[str]:
    """Best-effort exclusion of previous reports without parsing all graph payloads."""

    seen: set[str] = set()
    for path in Path("results").glob("**/*.json"):
        if root in path.parents:
            continue
        if path.stat().st_size > 3_000_000:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        stack = [data]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                candidate = value.get("canonical_sha256") or value.get("sha256_bipartition_canonical")
                if isinstance(candidate, str) and candidate.count(":") == 2:
                    seen.add(candidate)
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    return seen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("results/multi-seed-reduction-agent"))
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--cap", type=int, default=10)
    parser.add_argument("--min-degree", type=int, default=2)
    parser.add_argument("--max-cap-sets", type=int, default=180)
    parser.add_argument("--max-restorations", type=int, default=20)
    parser.add_argument("--max-switches", type=int, default=16)
    parser.add_argument("--primary-time-limit", type=float, default=8.0)
    parser.add_argument("--span-time-limit", type=float, default=12.0)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if args.limit < 1 or args.cap < 2 or args.min_degree < 1:
        parser.error("invalid bounds")

    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "classified.jsonl"
    checkpoints = root / "checkpoints.jsonl"
    prior = []
    if state_path.exists():
        for line in state_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                prior.append(json.loads(line))
    prior_hashes = {row["canonical_sha256"] for row in prior}
    foreign = external_hashes(root)
    generated = Counter()
    rejected = Counter()
    unique: dict[str, tuple[Graph, list[dict]]] = {}

    def add(seed: str, family: str, graph: Graph, detail: dict) -> None:
        generated[family] += 1
        if not valid_target(graph, args.min_degree, args.cap):
            rejected[family] += 1
            return
        key = nauty_canonical_hash(graph)
        if key in foreign or key in prior_hashes:
            rejected["reported_duplicate"] += 1
            return
        provenance = {"seed": seed, "family": family, **detail}
        if key in unique:
            unique[key][1].append(provenance)
        else:
            unique[key] = (graph, [provenance])

    for seed, base in load_seeds():
        # One/two vertex reductions are the compact order-reduction backbone.
        for count in (1, 2):
            for removed in itertools.combinations(base.vertices, count):
                add(seed, f"vertex_delete_{count}", remove_vertices(base, removed), {"vertices": list(removed)})
        # Same-side quotient reductions give a distinct simple-graph family.
        for side_name, side in zip(("left", "right"), base.bipartition):
            for pair in itertools.combinations(side, 2):
                add(seed, "same_side_identify", identify_same_side(base, *pair), {"side": side_name, "vertices": list(pair)})
        # Exact-minimum cap deletions, then bounded restorations disjoint from deletion.
        for removed in cap_edge_sets(base, args.cap, args.max_cap_sets):
            reduced = remove_edges(base, removed)
            add(seed, "minimal_cap_delete", reduced, {"edges": [list(e) for e in removed]})
            for restored in cross_restorations(reduced, args.max_restorations):
                candidate = Graph(reduced.vertices, list(reduced.edges) + list(restored), reduced.bipartition, reduced.metadata)
                add(seed, "cap_delete_restore", candidate, {"deleted_edges": [list(e) for e in removed], "restored_edges": [list(e) for e in restored]})
        # Switch selected reduced graphs only, so every submitted graph is in target scope.
        switch_bases = []
        for vertex in base.vertices:
            child = remove_vertices(base, (vertex,))
            if valid_target(child, args.min_degree, args.cap):
                switch_bases.append((child, {"vertex": vertex}))
        for pair in itertools.chain(itertools.combinations(base.bipartition[0], 2), itertools.combinations(base.bipartition[1], 2)):
            child = identify_same_side(base, *pair)
            if valid_target(child, args.min_degree, args.cap):
                switch_bases.append((child, {"identified": list(pair)}))
        for child, detail in switch_bases:
            for replacement in switches(child, args.max_switches):
                add(seed, "two_edge_switch", switched(child, replacement), {**detail, "replacement_edges": [list(e) for e in replacement]})

    # Prioritize structural closeness and rotate families to avoid a Q1-only run.
    family_rank = {name: index for index, name in enumerate(sorted(generated))}
    queued = sorted(unique.items(), key=lambda item: (
        max(0, item[1][0].n - 18), sum(d > args.cap for d in item[1][0].degrees.values()),
        item[1][0].delta, item[1][0].n, family_rank[item[1][1][0]["family"]], item[0]
    ))
    counts = Counter(row.get("classification") for row in prior)
    started = time.monotonic()
    with state_path.open("a", encoding="utf-8") as state:
        for key, (graph, provenance) in queued:
            if len(prior) >= args.limit:
                break
            primary = rank_potential_solve(graph, args.primary_time_limit, args.workers)
            classification = primary.status
            confirmation = None
            if primary.status == "non-colorable":
                confirmation = all_spans_solve(graph, args.span_time_limit, args.workers, stop_on_timeout=True)
                classification = confirmation["decision"] if confirmation["decision"] != "colorable" else "primary-disagreement"
            row = {
                "candidate_id": f"MSR-{len(prior) + 1:05d}", "canonical_sha256": key,
                "order": graph.n, "size": graph.m, "delta": graph.delta,
                "minimum_degree": min(graph.degrees.values()), "classification": classification,
                "provenance": provenance, "primary_result": result_summary(primary), "confirmation": confirmation,
            }
            state.write(json.dumps(row, sort_keys=True) + "\n")
            state.flush()
            prior.append(row)
            counts[classification] += 1
            if len(prior) % 100 == 0 or classification == "non-colorable":
                checkpoint = {
                    "classified": len(prior), "generated": sum(generated.values()), "unique_pending": len(queued),
                    "colorable": counts["colorable"], "non_colorable": counts["non-colorable"],
                    "timeout": counts["timeout"], "elapsed_seconds": time.monotonic() - started,
                }
                with checkpoints.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(checkpoint, sort_keys=True) + "\n")
                print(json.dumps(checkpoint, sort_keys=True), flush=True)

    report = {
        "schema": "multi-seed-reduction-v1", "target": "order <= 18 or Delta <= 10", "cap": args.cap,
        "minimum_degree": args.min_degree, "seeds": [name for name, _ in load_seeds()],
        "families": sorted(generated), "generated": dict(generated), "rejected": dict(rejected),
        # ``queued`` excludes rows already in append-only state on a resume.
        "external_hashes_excluded": len(foreign), "unique": len(queued) + len(prior),
        "unique_pending": len(queued), "classified": len(prior),
        "counts": {"colorable": counts["colorable"], "non-colorable": counts["non-colorable"], "timeout": counts["timeout"], "primary-disagreement": counts["primary-disagreement"]},
        "classified_by_primary_family": dict(Counter(row["provenance"][0]["family"] for row in prior)),
        "classified_by_primary_seed": dict(Counter(row["provenance"][0]["seed"] for row in prior)),
        "negative_confirmation": "independent fixed-span CP-SAT over every legal span", "elapsed_seconds": time.monotonic() - started,
    }
    (root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    negatives = [row for row in prior if row.get("classification") == "non-colorable"]
    (root / "confirmed-negatives.json").write_text(json.dumps(negatives, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"final": report}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
