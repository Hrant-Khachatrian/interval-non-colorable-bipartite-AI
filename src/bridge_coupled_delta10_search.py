#!/usr/bin/env python3
"""Bounded M5 five-spoke search with one inter-terminal bridge edge.

Every construction starts from the M5 degree-15 hub, transfers its five
required incident edges through mixed C12/theta/multi-C4 terminals, then adds
exactly one edge between two distinct terminals.  The bridge always crosses
the inherited bipartition and is rejected unless the final graph is simple,
connected, bipartite, minimum-degree two, and has Delta at most ten.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from pathlib import Path
from typing import Iterable, Iterator

import networkx as nx

from degree_transfer_delta10_fresh_motifs import (
    FRESH_MOTIFS,
    MOTIF_SIDES,
    RootedMotif,
    degree_cap_holds,
)
from degree_transfer_delta10_search import (
    independent_confirmation,
    parent_graphs,
)
from interval_edge_coloring import Graph, nauty_canonical_hash, rank_potential_solve, weighted_hub_statistics


PARENT = "M5_delta_555"
SELECTED_EDGES = (27, 33, 36, 39, 42)
MOTIFS = tuple(
    motif for motif in FRESH_MOTIFS
    if motif.name in {
        "rooted_C12",
        "rooted_theta_3x4",
        "rooted_theta_4x4",
        "three_rooted_C4_blocks",
    }
)
THETA_NAMES = {"rooted_theta_3x4", "rooted_theta_4x4"}
MULTI_C4_NAME = "three_rooted_C4_blocks"


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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
    """Collect known canonical certificates without treating this lane as prior work."""

    hashes: set[str] = set()
    for pattern in ("*.json", "*.jsonl"):
        for path in results_root.rglob(pattern):
            if output_root == path.parent or output_root in path.parents:
                continue
            try:
                if path.suffix == ".jsonl":
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            hashes.update(json_hashes(json.loads(line)))
                else:
                    hashes.update(json_hashes(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
    return hashes


def terminal_configurations() -> list[tuple[RootedMotif, ...]]:
    """Keep each requested terminal family present in every five-spoke graph."""

    result = []
    for motifs in itertools.product(MOTIFS, repeat=5):
        names = {motif.name for motif in motifs}
        if "rooted_C12" not in names:
            continue
        if not names & THETA_NAMES:
            continue
        if MULTI_C4_NAME not in names:
            continue
        result.append(motifs)
    result.sort(key=lambda motifs: (
        -len({motif.name for motif in motifs}),
        -sum(motif.name in THETA_NAMES for motif in motifs),
        tuple(motif.name for motif in motifs),
    ))
    return result


def motif_ports(motif: RootedMotif) -> tuple[tuple[str, int], ...]:
    """Use two deterministic low-degree attachment sites on each local side."""

    sides = MOTIF_SIDES[motif.name]
    by_side = {
        side: [label for label in motif.internal_labels if sides[label] == side]
        for side in (0, 1)
    }
    ports = []
    for side in (0, 1):
        labels = by_side[side]
        if not labels:
            raise AssertionError(f"motif {motif.name} has no side-{side} bridge port")
        picks = labels[:2] if len(labels) > 1 else labels
        ports.extend((label, side) for label in picks)
    return tuple(ports)


def apply_terminals(base: Graph, motifs: tuple[RootedMotif, ...], serial: int) -> tuple[Graph, list[dict]]:
    """Transfer five M5 hub edges through the requested terminal motifs."""

    overcap = {vertex for vertex, degree in base.degrees.items() if degree > 10}
    left, right = set(base.bipartition[0]), set(base.bipartition[1])
    removed = {tuple(sorted(base.edges[index])) for index in SELECTED_EDGES}
    vertices = list(base.vertices)
    edges = [edge for edge in base.edges if tuple(sorted(edge)) not in removed]
    details: list[dict] = []

    for terminal, (edge_index, motif) in enumerate(zip(SELECTED_EDGES, motifs)):
        u, v = base.edges[edge_index]
        root = v if u in overcap else u
        mapping = {"R": root}
        root_is_left = root in left
        for label in motif.internal_labels:
            name = f"BC{serial:06d}_T{terminal}_{label}"
            mapping[label] = name
            vertices.append(name)
            is_left = (MOTIF_SIDES[motif.name][label] == 0) == root_is_left
            (left if is_left else right).add(name)
        edges.extend((mapping[a], mapping[b]) for a, b in motif.edges)
        details.append({
            "terminal_index": terminal,
            "parent_edge_index": edge_index,
            "parent_edge": list(base.edges[edge_index]),
            "transfer_root": root,
            "motif": motif.name,
            "root_degree": motif.root_degree,
            "local_to_global": mapping,
        })

    graph = Graph(vertices, edges, [sorted(left), sorted(right)], {
        "lane": "m5-five-spoke-single-bridge-coupling",
        "parent": PARENT,
        "selected_parent_edges": list(SELECTED_EDGES),
    })
    return graph, details


def bridge_choices(motifs: tuple[RootedMotif, ...]) -> Iterator[tuple[int, int, tuple[str, int], tuple[str, int]]]:
    """Enumerate terminal pairs and opposite-parity local bridge positions."""

    for first, second in itertools.combinations(range(len(motifs)), 2):
        for first_port in motif_ports(motifs[first]):
            for second_port in motif_ports(motifs[second]):
                if first_port[1] != second_port[1]:
                    yield first, second, first_port, second_port


def bridge_graph(
    terminals: Graph,
    details: list[dict],
    first: int,
    second: int,
    first_port: tuple[str, int],
    second_port: tuple[str, int],
) -> tuple[Graph, dict]:
    a = details[first]["local_to_global"][first_port[0]]
    b = details[second]["local_to_global"][second_port[0]]
    before = terminals.degrees
    if tuple(sorted((a, b))) in terminals.edges:
        raise AssertionError("bridge unexpectedly duplicates a terminal edge")
    edges = [*terminals.edges, (a, b)]
    graph = Graph(terminals.vertices, edges, terminals.bipartition, terminals.metadata)
    return graph, {
        "terminal_indices": [first, second],
        "terminal_motif_pair": [details[first]["motif"], details[second]["motif"]],
        "positions": [first_port[0], second_port[0]],
        "local_sides": [first_port[1], second_port[1]],
        "vertices": [a, b],
        "endpoint_degrees_before": [before[a], before[b]],
        "endpoint_degrees_after": [graph.degrees[a], graph.degrees[b]],
    }


def invariants_hold(graph: Graph) -> bool:
    return (
        graph.delta <= 10
        and min(graph.degrees.values()) >= 2
        and nx.is_connected(graph._nx)
        and nx.is_bipartite(graph._nx)
        and len(graph.edges) == len({tuple(sorted(edge)) for edge in graph.edges})
    )


def degree_variance_normalized(graph: Graph) -> float:
    degrees = list(graph.degrees.values())
    mean = sum(degrees) / graph.n
    return sum((degree - mean) ** 2 for degree in degrees) / graph.n / max(1, graph.n - 1)


def candidate_stream(base: Graph) -> Iterator[tuple[tuple[RootedMotif, ...], int, int, tuple[str, int], tuple[str, int]]]:
    """Round-robin bridge pairs and positions before spending the cap on either."""

    configurations = terminal_configurations()
    indexed: list[tuple[tuple[RootedMotif, ...], list[tuple[int, int, tuple[str, int], tuple[str, int]]]]] = []
    max_choices = 0
    for motifs in configurations:
        choices = list(bridge_choices(motifs))
        indexed.append((motifs, choices))
        max_choices = max(max_choices, len(choices))
    for choice_index in range(max_choices):
        for motifs, choices in indexed:
            by_pair: dict[tuple[int, int], list[tuple[int, int, tuple[str, int], tuple[str, int]]]] = {}
            for choice in choices:
                by_pair.setdefault((choice[0], choice[1]), []).append(choice)
            for pair in sorted(by_pair):
                pair_choices = by_pair[pair]
                if choice_index < len(pair_choices):
                    yield motifs, *pair_choices[choice_index]


def compact_record(row: dict) -> dict:
    return {
        key: row[key]
        for key in (
            "candidate_id", "canonical_sha256", "terminal_motifs", "bridge",
            "order", "size", "delta", "minimum_degree", "hub_best_margin",
            "degree_variance_normalized", "primary_status", "primary_span", "decision",
        )
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
    history_path = output / "run-history.jsonl"
    rows = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines() if line.strip()] if state_path.exists() else []
    history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()] if history_path.exists() else []
    initial_row_count = len(rows)
    prior_hashes = {row["canonical_sha256"] for row in rows}
    global_hashes = load_global_hashes(Path("results"), output) | prior_hashes
    # The shared parent loader locates its certificates relative to ``results``.
    # Keep that lookup stable even when a caller places this lane elsewhere.
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

    stop = "family_exhausted"
    for serial, (motifs, first, second, first_port, second_port) in enumerate(candidate_stream(base), start=1):
        if counts["classified"] >= args.unique_cap:
            stop = "unique_cap"
            break
        counts["generated"] += 1
        if not degree_cap_holds(base, SELECTED_EDGES, motifs, 10):
            raise AssertionError("terminal construction violates Delta cap before bridging")
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
        hub_statistics = weighted_hub_statistics(graph)
        row = {
            # The canonical certificate remains stable across resumed, reordered
            # enumeration, unlike a construction counter.
            "candidate_id": f"BC10-{digest.rsplit(':', 1)[-1][:16]}",
            "lane": "m5-five-spoke-single-bridge-coupling",
            "parent": PARENT,
            "selected_parent_edges": list(SELECTED_EDGES),
            "terminal_motifs": [motif.name for motif in motifs],
            "bridge": bridge,
            "canonical_sha256": digest,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "minimum_degree": min(graph.degrees.values()),
            "connected": True,
            "bipartite": True,
            "simple": True,
            "hub_best_margin": hub_statistics[0]["margin"],
            "weighted_hubs_best": hub_statistics[:2],
            "degree_variance_normalized": degree_variance_normalized(graph),
            "primary_status": primary.status,
            "primary_span": primary.span,
            "primary_solver_status": primary.solver_status,
            "primary_elapsed_seconds": primary.elapsed_seconds,
        }
        if primary.status == "colorable":
            row["decision"] = "colorable"
            counts["colorable"] += 1
        elif primary.status == "timeout":
            row["decision"] = "timeout_primary"
            counts["timeout"] += 1
        else:
            counts["primary_negative_candidates"] += 1
            # A primary negative is only a result after every legal span is swept.
            confirmation_deadline = time.monotonic() + args.time_limit * (graph.n - graph.delta + 2)
            confirmed, unresolved, spans = independent_confirmation(
                graph, args.time_limit, args.workers, confirmation_deadline
            )
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
        prior_hashes.add(digest)
        counts["unique"] += 1
        counts["classified"] += 1

    new_rows = rows[initial_row_count:]
    run_counts = {
        "generated": counts["generated"],
        "duplicate": counts["duplicate"],
        "rejected_invariant": counts["rejected_invariant"],
        "unique": len(new_rows),
        "classified": len(new_rows),
        "colorable": sum(row["decision"].startswith("colorable") for row in new_rows),
        "non_colorable": sum(row["decision"] == "non-colorable" for row in new_rows),
        "timeout": sum(not row["decision"].startswith("colorable") and row["decision"] != "non-colorable" for row in new_rows),
        "primary_negative_candidates": counts["primary_negative_candidates"],
        "independent_unresolved": counts["independent_unresolved"],
    }
    append_jsonl(history_path, {
        "event": "invocation",
        "timestamp_unix": time.time(),
        "stop": stop,
        "counts": run_counts,
    })
    history.append({"counts": run_counts})
    cumulative_counts = {
        "generated": sum(item["counts"].get("generated", 0) for item in history),
        "duplicate": sum(item["counts"].get("duplicate", 0) for item in history),
        "rejected_invariant": sum(item["counts"].get("rejected_invariant", 0) for item in history),
        "unique": len(rows),
        "classified": len(rows),
        "colorable": sum(row["decision"].startswith("colorable") for row in rows),
        "non_colorable": sum(row["decision"] == "non-colorable" for row in rows),
        "timeout": sum(not row["decision"].startswith("colorable") and row["decision"] != "non-colorable" for row in rows),
        "primary_negative_candidates": sum(item["counts"].get("primary_negative_candidates", 0) for item in history),
        "independent_unresolved": sum(item["counts"].get("independent_unresolved", 0) for item in history),
    }
    report = {
        "schema": "bridge-coupled-delta10-v1",
        "stop": stop,
        "complete": stop == "family_exhausted" and cumulative_counts["timeout"] == 0,
        "configuration": {
            "parent": PARENT,
            "selected_parent_edges": list(SELECTED_EDGES),
            "terminal_motifs": [motif.name for motif in MOTIFS],
            "diversity_requirement": "every construction contains C12, theta, and three-rooted-C4 terminals",
            "bridge": "one direct edge joining opposite bipartition sides of two distinct terminals",
            "bridge_port_bound": "first two local vertices on each motif side",
            "unique_cap": args.unique_cap,
            "primary_oracle": "rank-potential CP-SAT",
            "negative_confirmation": "independent fixed-span CP-SAT across every legal span",
            "timeout_policy": "unresolved only",
            "global_canonical_seed_count": len(global_hashes) - len(prior_hashes),
        },
        "counts": cumulative_counts,
        "counts_this_invocation": run_counts,
        "state_rows_total": len(rows),
        "elapsed_seconds_this_invocation": time.monotonic() - started,
        "observed_span": {
            "maximum": max((row["primary_span"] or 0 for row in rows), default=None),
            "median": sorted(row["primary_span"] or 0 for row in rows)[len(rows) // 2] if rows else None,
        },
        "bridge_pair_coverage": sorted({tuple(row["bridge"]["terminal_indices"]) for row in rows}),
        "sample_records": [compact_record(row) for row in rows[:5]],
        "hypothesis_worth_scaling": cumulative_counts["non_colorable"] > 0,
        "recommendation": "not worth scaling at this checkpoint: no primary negative, confirmed non-colorable graph, or span improvement over the prior span-22 near miss was observed",
    }
    write_json(output / "report.json", report)
    write_json(output / "status.json", {
        "schema": report["schema"], "stop": stop, "counts": cumulative_counts,
        "counts_this_invocation": run_counts,
        "state_rows_total": len(rows), "observed_span": report["observed_span"],
        "bridge_pair_coverage": report["bridge_pair_coverage"],
        "hypothesis_worth_scaling": report["hypothesis_worth_scaling"],
        "assessment": report["recommendation"],
        "elapsed_seconds_this_invocation": report["elapsed_seconds_this_invocation"],
    })
    (output / "status.md").write_text(
        "# Bridge-coupled Delta<=10 status\n\n"
        f"- Stop: `{stop}`\n"
        f"- Generated: {cumulative_counts['generated']}; unique/classified: {cumulative_counts['unique']}/{cumulative_counts['classified']}\n"
        f"- Colorable: {cumulative_counts['colorable']}; non-colorable: {cumulative_counts['non_colorable']}; timeout: {cumulative_counts['timeout']}\n"
        f"- Span: maximum {report['observed_span']['maximum']}, median {report['observed_span']['median']}\n"
        f"- Distinct canonical bridge-pair placements: {len(report['bridge_pair_coverage'])}\n"
        f"- Worth scaling: {'yes' if report['hypothesis_worth_scaling'] else 'no'}\n\n"
        f"{report['recommendation']}.\n",
        encoding="utf-8",
    )
    print(json.dumps({"stop": stop, "counts": cumulative_counts}, sort_keys=True))


if __name__ == "__main__":
    main()
