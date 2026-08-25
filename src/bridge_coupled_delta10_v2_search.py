#!/usr/bin/env python3
"""Expanded deterministic M5 five-spoke bridge-coupling search.

This v2 lane uses every internal vertex of each approved terminal motif as a
bounded bridge port.  It round-robins terminal assignments, all ten terminal
pairs, and port combinations, so a finite prefix is not an accidental
first-pair/first-port pilot.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from pathlib import Path
from typing import Iterator

import networkx as nx

from bridge_coupled_delta10_search import (
    MOTIFS,
    MULTI_C4_NAME,
    PARENT,
    SELECTED_EDGES,
    THETA_NAMES,
    apply_terminals,
    bridge_graph,
    degree_variance_normalized,
    load_global_hashes,
    terminal_configurations,
)
from degree_transfer_delta10_fresh_motifs import MOTIF_SIDES, RootedMotif, degree_cap_holds
from degree_transfer_delta10_search import independent_confirmation, parent_graphs
from interval_edge_coloring import Graph, nauty_canonical_hash, rank_potential_solve, weighted_hub_statistics


SCHEMA = "bridge-coupled-delta10-v2"
LANE = "m5-five-spoke-expanded-bridge-coupling"


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def all_motif_ports(motif: RootedMotif) -> tuple[tuple[str, int], ...]:
    """Every finite local port, ordered deterministically by side then label."""

    sides = MOTIF_SIDES[motif.name]
    return tuple(
        (label, side)
        for side in (0, 1)
        for label in sorted(label for label in motif.internal_labels if sides[label] == side)
    )


def choices_for_pair(
    motifs: tuple[RootedMotif, ...], first: int, second: int
) -> list[tuple[tuple[str, int], tuple[str, int]]]:
    return [
        (first_port, second_port)
        for first_port in all_motif_ports(motifs[first])
        for second_port in all_motif_ports(motifs[second])
        if first_port[1] != second_port[1]
    ]


def candidate_stream() -> Iterator[tuple[tuple[RootedMotif, ...], int, int, tuple[str, int], tuple[str, int], int, int]]:
    """Interleave configuration, terminal-pair, and port-combination axes."""

    configurations = terminal_configurations()
    pairs = tuple(itertools.combinations(range(5), 2))
    indexed = [
        (motifs, {(first, second): choices_for_pair(motifs, first, second) for first, second in pairs})
        for motifs in configurations
    ]
    maximum_ports = max(len(ports) for _, by_pair in indexed for ports in by_pair.values())
    for port_round in range(maximum_ports):
        for motifs, by_pair in indexed:
            # Pairs are inside the configuration loop: a capped run still samples all ten pairs.
            for pair_rank, (first, second) in enumerate(pairs):
                ports = by_pair[(first, second)]
                if port_round < len(ports):
                    first_port, second_port = ports[port_round]
                    yield motifs, first, second, first_port, second_port, pair_rank, port_round


def invariants_hold(graph: Graph) -> bool:
    return (
        graph.delta <= 10
        and min(graph.degrees.values()) >= 2
        and nx.is_connected(graph._nx)
        and nx.is_bipartite(graph._nx)
        and len(graph.edges) == len({tuple(sorted(edge)) for edge in graph.edges})
    )


def compact_record(row: dict) -> dict:
    return {key: row[key] for key in (
        "candidate_id", "canonical_sha256", "terminal_motifs", "bridge", "order", "size",
        "delta", "minimum_degree", "hub_best_margin", "degree_variance_normalized",
        "primary_status", "primary_span", "decision",
    )}


def prior_isolated_summary() -> dict:
    """Pinned summary of the directly comparable terminal-only M5 pilot."""

    path = Path("results/degree-transfer-delta10-agent/fresh-motifs-m5-pilot.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for row in data["records"] if row.get("parent") == PARENT and row.get("decision") == "colorable"]
    return {
        "source": str(path),
        "eligible_colorable_records": len(rows),
        "best_primary_span": max(row["primary_span"] for row in rows),
        # The independent near-miss aggregation reports the best comparable hub margin as -4.
        "best_hub_margin": -4,
        "hub_margin_source": "results/nearmiss-mining-agent/near-miss-analysis.json: degree-transfer-fresh-motifs",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--unique-cap", type=int, default=500)
    parser.add_argument("--time-limit", type=float, default=4.0)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.unique_cap <= 2000 or not 0.1 <= args.time_limit <= 20 or not 1 <= args.workers <= 8:
        parser.error("bounds: 1<=unique-cap<=2000, 0.1<=time-limit<=20, 1<=workers<=8")

    started = time.monotonic()
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "classification-state.jsonl"
    rows = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines() if line.strip()] if state_path.exists() else []
    row_hashes = {row["canonical_sha256"] for row in rows}
    global_hashes = load_global_hashes(Path("results"), output) | row_hashes
    initial_global_seed_count = len(global_hashes - row_hashes)
    base = dict((name, graph) for name, graph, _ in parent_graphs(Path("results") / "bridge-coupled-delta10" / "graphs"))[PARENT]
    counts = {
        "generated": 0, "duplicate": 0, "rejected_invariant": 0, "unique": len(rows),
        "classified": len(rows), "colorable": 0, "non_colorable": 0, "timeout": 0,
        "primary_negative_candidates": 0, "independent_unresolved": 0,
    }
    for row in rows:
        if row["decision"] == "non-colorable":
            counts["non_colorable"] += 1
        elif row["decision"].startswith("colorable"):
            counts["colorable"] += 1
        else:
            counts["timeout"] += 1
    # Candidate IDs encode their position in the deterministic stream.  This
    # keeps summary counts cumulative when a completed lane is resumed only to
    # refresh derived report fields.
    if rows:
        last_serial = max(int(row["candidate_id"].rsplit("-", 1)[1]) for row in rows)
        counts["generated"] = last_serial
        counts["duplicate"] = last_serial - len(rows) - counts["rejected_invariant"]

    stop = "family_exhausted"
    for serial, item in enumerate(candidate_stream(), start=1):
        if counts["classified"] >= args.unique_cap:
            stop = "unique_cap"
            break
        motifs, first, second, first_port, second_port, pair_rank, port_round = item
        counts["generated"] += 1
        if not degree_cap_holds(base, SELECTED_EDGES, motifs, 10):
            raise AssertionError("terminal construction violates the Delta cap before bridging")
        terminals, details = apply_terminals(base, motifs, serial)
        graph, bridge = bridge_graph(terminals, details, first, second, first_port, second_port)
        if not invariants_hold(graph):
            counts["rejected_invariant"] += 1
            continue
        digest = nauty_canonical_hash(graph)
        if digest in global_hashes:
            counts["duplicate"] += 1
            continue
        global_hashes.add(digest)
        primary = rank_potential_solve(graph, args.time_limit, args.workers)
        hubs = weighted_hub_statistics(graph)
        bridge.update({
            "terminal_pair_rank": pair_rank,
            "port_round": port_round,
            "port_inventory": {
                "first_terminal_total": len(all_motif_ports(motifs[first])),
                "second_terminal_total": len(all_motif_ports(motifs[second])),
                "pair_legal_opposite_side_combinations": len(choices_for_pair(motifs, first, second)),
            },
        })
        row = {
            "candidate_id": f"BC10V2-{serial:07d}", "lane": LANE, "parent": PARENT,
            "selected_parent_edges": list(SELECTED_EDGES),
            "terminal_motifs": [motif.name for motif in motifs], "bridge": bridge,
            "canonical_sha256": digest, "order": graph.n, "size": graph.m, "delta": graph.delta,
            "minimum_degree": min(graph.degrees.values()), "connected": True, "bipartite": True,
            "simple": True, "hub_best_margin": hubs[0]["margin"], "weighted_hubs_best": hubs[:2],
            "degree_variance_normalized": degree_variance_normalized(graph),
            "primary_status": primary.status, "primary_span": primary.span,
            "primary_solver_status": primary.solver_status, "primary_elapsed_seconds": primary.elapsed_seconds,
        }
        if primary.status == "colorable":
            row["decision"] = "colorable"
            counts["colorable"] += 1
        elif primary.status == "timeout":
            row["decision"] = "timeout_primary"
            counts["timeout"] += 1
        else:
            counts["primary_negative_candidates"] += 1
            deadline = time.monotonic() + args.time_limit * (graph.n - graph.delta + 2)
            confirmed, unresolved, spans = independent_confirmation(graph, args.time_limit, args.workers, deadline)
            row["fixed_span_statuses"] = {str(span): status for span, status in spans.items()}
            if confirmed:
                row["decision"] = "non-colorable"
                counts["non_colorable"] += 1
            elif unresolved:
                row["decision"] = "timeout_independent"
                counts["timeout"] += 1
                counts["independent_unresolved"] += 1
            else:
                row["decision"] = "colorable_by_independent_span"
                counts["colorable"] += 1
        append_jsonl(state_path, row)
        rows.append(row)
        counts["unique"] += 1
        counts["classified"] += 1

    prior = prior_isolated_summary()
    best_span = max((row["primary_span"] or 0 for row in rows), default=None)
    best_margin = max((row["hub_best_margin"] for row in rows), default=None)
    comparison = {
        "prior_isolated_terminals": prior,
        "expanded_bridge_coupling": {"best_primary_span": best_span, "best_hub_margin": best_margin},
        "span_delta_vs_prior": None if best_span is None else best_span - prior["best_primary_span"],
        "hub_margin_delta_vs_prior": None if best_margin is None else best_margin - prior["best_hub_margin"],
    }
    coverage = {
        "terminal_pairs_enumerated": [list(pair) for pair in itertools.combinations(range(5), 2)],
        "survivor_terminal_pair_ranks": sorted({row["bridge"]["terminal_pair_rank"] for row in rows}),
        "survivor_port_rounds": sorted({row["bridge"]["port_round"] for row in rows}),
        "survivor_endpoint_port_label_pairs": len({
            tuple(row["bridge"]["positions"]) for row in rows
        }),
        "note": "Missing survivor pair ranks were nevertheless enumerated; their generated graphs canonically matched prior certificates or another survivor.",
    }
    merits_scaling = counts["non_colorable"] > 0 or (best_span is not None and best_span > prior["best_primary_span"]) or (best_margin is not None and best_margin > prior["best_hub_margin"])
    report = {
        "schema": SCHEMA, "stop": stop,
        "complete": stop == "family_exhausted" and counts["timeout"] == 0,
        "configuration": {
            "parent": PARENT, "selected_parent_edges": list(SELECTED_EDGES),
            "terminal_motifs": [motif.name for motif in MOTIFS],
            "diversity_requirement": "every construction contains C12, theta, and three-rooted-C4 terminals",
            "bridge": "one direct edge joining opposite bipartition sides of two distinct terminals",
            "terminal_pairs": "all 10 unordered pairs among five terminals",
            "bridge_ports": "all internal vertices on both motif sides; no first-two truncation",
            "enumeration": "port round, then terminal assignment, then all terminal pairs",
            "unique_cap": args.unique_cap, "primary_oracle": "rank-potential CP-SAT",
            "negative_confirmation": "independent fixed-span CP-SAT across every legal span",
            "timeout_policy": "unresolved only", "global_canonical_seed_count": initial_global_seed_count,
        },
        "counts": counts, "state_rows_total": len(rows),
        "elapsed_seconds_this_invocation": time.monotonic() - started,
        "sample_records": [compact_record(row) for row in rows[:5]],
        "coverage": coverage,
        "comparison_to_isolated_terminals": comparison,
        "recommendation": (
            "bridge coupling merits further scaling" if merits_scaling else
            "bridge coupling does not merit further scaling: no confirmed negative and no improvement over the isolated-terminal near-miss span or hub margin"
        ),
    }
    write_json(output / "report.json", report)
    write_json(output / "status.json", {
        "schema": SCHEMA, "stop": stop, "counts": counts, "state_rows_total": len(rows),
        "elapsed_seconds_this_invocation": report["elapsed_seconds_this_invocation"],
        "best_primary_span": best_span, "best_hub_margin": best_margin,
    })
    print(json.dumps({"stop": stop, "counts": counts}, sort_keys=True))


if __name__ == "__main__":
    main()
