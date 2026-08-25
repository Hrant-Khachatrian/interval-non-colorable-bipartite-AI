#!/usr/bin/env python3
"""Run one bounded, globally deduplicated degree-transfer near-miss lane.

The lane deliberately follows the high-span envelope found in the prior M5
pilot: five repairs of the degree-15 hub, with long rooted cycles, theta
networks, and moderately dense rooted complete bipartite terminals.  It is
not an exhaustive family claim.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from pathlib import Path
from typing import Iterable

import networkx as nx

from degree_transfer_delta10_fresh_motifs import (
    RootedMotif,
    degree_cap_holds,
    motif_sides,
)
from degree_transfer_delta10_search import (
    covering_selections,
    demand_edges,
    independent_confirmation,
    parent_graphs,
)
from interval_edge_coloring import nauty_canonical_hash, rank_potential_solve, weighted_hub_statistics


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


MOTIFS = (
    cycle_motif("rooted_C16", 16),
    theta_motif("rooted_theta_5x4", 5),
    complete_bipartite_motif("rooted_K_3_6", 3, 6),
    complete_bipartite_motif("rooted_K_2_7", 2, 7),
)
MOTIF_SIDES = {motif.name: motif_sides(motif) for motif in MOTIFS}


def json_hashes(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for key in ("canonical_sha256", "sha256_bipartition_canonical"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                yield candidate
        for child in value.values():
            yield from json_hashes(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_hashes(child)


def load_global_hashes(results_root: Path, output_root: Path) -> set[str]:
    hashes: set[str] = set()
    for path in results_root.rglob("*.json"):
        if output_root in path.parents:
            continue
        try:
            hashes.update(json_hashes(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    for path in results_root.rglob("*.jsonl"):
        if output_root in path.parents:
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    hashes.update(json_hashes(json.loads(line)))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return hashes


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def valid(graph) -> bool:
    return (
        graph.delta <= 10
        and min(graph.degrees.values()) >= 2
        and nx.is_connected(graph._nx)
        and nx.is_bipartite(graph._nx)
    )


def apply_targeted(base, selected: tuple[int, ...], motifs: tuple[RootedMotif, ...], serial: int):
    """Replace one incident edge per required hub decrement using local motifs."""
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
            name = f"NMT{serial:04d}_{sequence:02d}_{label}"
            mapping[label] = name
            vertices.append(name)
            root_is_left = root in left
            is_left = (MOTIF_SIDES[motif.name][label] == 0) == root_is_left
            (left if is_left else right).add(name)
        edges.extend((mapping[u0], mapping[v0]) for u0, v0 in motif.edges)
    return type(base)(vertices, edges, [sorted(left), sorted(right)], {"lane": "m5-five-transfer-long-terminal-envelope"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cap", type=int, default=96)
    parser.add_argument("--time-limit", type=float, default=8.0)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.cap <= 256 or not 0.1 <= args.time_limit <= 20 or not 1 <= args.workers <= 8:
        parser.error("bounds: 1<=cap<=256, 0.1<=time-limit<=20, 1<=workers<=8")

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "classification-state.jsonl"
    prior_rows = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines() if line.strip()] if state_path.exists() else []
    global_hashes = load_global_hashes(Path("results"), output)
    global_hashes.update(row["canonical_sha256"] for row in prior_rows)
    parents = {name: graph for name, graph, _ in parent_graphs(output / "graphs")}
    base = parents["M5_delta_555"]
    selected_edges = (27, 33, 36, 39, 42)
    candidate_edges = demand_edges(base, 10)
    legal_selections = set(covering_selections(base, candidate_edges, 10, 5, time.monotonic() + 30.0))
    if selected_edges not in legal_selections:
        raise AssertionError("targeted high-span M5 selection is no longer legal")

    # The envelope puts at least two long cyclic/theta terminals and at least
    # one dense terminal around the five required hub repairs.
    constructions = [
        motifs for motifs in itertools.product(MOTIFS, repeat=5)
        if sum(motif.name in {"rooted_C16", "rooted_theta_5x4"} for motif in motifs) >= 2
        and sum(motif.name in {"rooted_K_3_6", "rooted_K_2_7"} for motif in motifs) >= 1
    ]
    constructions.sort(key=lambda motifs: (
        -sum(m.name == "rooted_theta_5x4" for m in motifs),
        -sum(m.name == "rooted_C16" for m in motifs),
        tuple(m.name for m in motifs),
    ))

    counts = {"generated": 0, "duplicate": 0, "unique": 0, "classified": 0, "colorable": 0, "non_colorable": 0, "timeout": 0, "primary_negative_candidates": 0, "independent_unresolved": 0}
    rows = list(prior_rows)
    prior_hashes = {row["canonical_sha256"] for row in prior_rows}
    for serial, motifs in enumerate(constructions, start=1):
        if counts["classified"] >= args.cap:
            break
        counts["generated"] += 1
        if not degree_cap_holds(base, selected_edges, motifs, 10):
            raise AssertionError("target motif violates the degree cap")
        graph = apply_targeted(base, selected_edges, motifs, serial)
        if not valid(graph):
            raise AssertionError("target construction violates a graph invariant")
        digest = nauty_canonical_hash(graph)
        if digest in global_hashes:
            counts["duplicate"] += 1
            continue
        global_hashes.add(digest)
        counts["unique"] += 1
        primary = rank_potential_solve(graph, args.time_limit, args.workers)
        row = {
            "candidate_id": f"NM-TGT-{serial:04d}",
            "lane": "m5-five-transfer-long-terminal-envelope",
            "parent": "M5_delta_555",
            "selected_parent_edges": list(selected_edges),
            "terminal_motifs": [motif.name for motif in motifs],
            "canonical_sha256": digest,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "minimum_degree": min(graph.degrees.values()),
            "hub_best_margin": weighted_hub_statistics(graph)[0]["margin"],
            "degree_variance_normalized": sum((d - sum(graph.degrees.values()) / graph.n) ** 2 for d in graph.degrees.values()) / graph.n / (graph.n - 1),
            "primary_status": primary.status,
            "primary_span": primary.span,
            "primary_solver_status": primary.solver_status,
            "primary_elapsed_seconds": primary.elapsed_seconds,
        }
        if primary.status == "colorable":
            row["decision"] = "colorable"
            counts["colorable"] += 1
        elif primary.status == "timeout":
            row["decision"] = "timeout"
            counts["timeout"] += 1
        else:
            counts["primary_negative_candidates"] += 1
            confirmed, unresolved, spans = independent_confirmation(
                graph, args.time_limit, args.workers, time.monotonic() + args.time_limit * (graph.n - graph.delta + 2)
            )
            row["fixed_span_statuses"] = {str(span): status for span, status in spans.items()}
            if confirmed:
                row["decision"] = "non-colorable"
                counts["non_colorable"] += 1
            elif unresolved:
                row["decision"] = "timeout"
                counts["timeout"] += 1
                counts["independent_unresolved"] += 1
            else:
                row["decision"] = "colorable-by-independent-span"
                counts["colorable"] += 1
        rows.append(row)
        append_jsonl(state_path, row)
        counts["classified"] += 1
        if digest in prior_hashes:
            raise AssertionError("append-only state attempted to repeat a prior classification")

    report = {
        "schema": "nearmiss-mining-targeted-pass-v1",
        "configuration": {
            "lane": "m5-five-transfer-long-terminal-envelope",
            "parent": "M5_delta_555",
            "selected_parent_edges": list(selected_edges),
            "motifs": [motif.name for motif in MOTIFS],
            "bounded_candidate_cap": args.cap,
            "primary_oracle": "rank-potential CP-SAT",
            "negative_confirmation": "independent fixed-span CP-SAT across every legal span",
            "timeout_policy": "unresolved only",
            "global_canonical_seed_count": len(load_global_hashes(Path("results"), output)),
        },
        "counts": counts,
        "state_rows_total": len(rows),
        "records": rows,
        "complete": counts["classified"] == min(args.cap, len(constructions)),
    }
    write_json(output / "targeted-pass-report.json", report)
    print(json.dumps({"counts": counts, "complete": report["complete"]}, sort_keys=True))


if __name__ == "__main__":
    main()
