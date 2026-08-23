#!/usr/bin/env python3
"""Round 5: asymmetric multiway relay rings with degree-10 forced hubs.

The completed signature rounds repeatedly built one- or two-hub bridges.  This
runner changes the coupling topology: the twelve neighbours of the deleted seed
hub are distributed contiguously over three or four relay hubs, and those hubs
are joined by a directed mixed cycle of compact strict-extreme synchronizers.
Every configured ring contains at least one relay with eight seed connectors,
so that relay has degree ten and therefore a ten-color forced interval.

The two relay edge sets are selected from the completed, timeout-free round-2
catalog by fewest edges and then fewest signature states.  Their signatures are
recomputed here rather than trusted from JSON.  Composition is intentionally
low-redundancy: only positive load vectors containing an 8-load relay and mixed
two-relay patterns are generated.  A timeout is always reported as a timeout.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import platform
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)
from lane6_signature_search import Signature, terminal_signatures
from lane1_search import seed_graph


TOTAL_TIME_LIMIT_SECONDS = 2 * 60 * 60
SOURCE_CATALOG = "results/lane6-signature-r2.json"

# These are the two shortest distinct members of the completed strict-extreme
# round-2 catalog.  Keeping the catalogue fixed makes the composition family
# finite and reproducible without re-running the old 4,013-shape enumeration.
SELECTED_RELAY_EDGES = (
    (
        ("L0", "R0"),
        ("L0", "R1"),
        ("L1", "R0"),
        ("L1", "T0"),
        ("L2", "R1"),
        ("L2", "T1"),
    ),
    (
        ("L0", "R0"),
        ("L0", "R1"),
        ("L0", "T0"),
        ("L1", "R0"),
        ("L1", "T1"),
        ("L2", "R1"),
        ("L2", "T1"),
    ),
)

PRIOR_ROUND_DIAGNOSIS = {
    "palette_overlap": (
        "Round 2 attached one strict-extreme bridge between two hubs, but its "
        "terminal palettes could overlap instead of forming a long forced "
        "concatenation; all 1260 unique candidates were colorable."
    ),
    "insufficient_forced_span_length": (
        "Split-hub and chained-sync controls reached primary spans at most 11, "
        "and rounds 3/4 at most 12/13, despite targeting Delta=10.  Round 5 "
        "therefore forces an actual degree-10 relay in every composition."
    ),
    "excess_symmetry": (
        "Balanced two-hub splits and repeated identical relays leave useful "
        "swap/rotation symmetry.  Round 5 mixes two distinct relay types and "
        "uses ordered unequal connector loads."
    ),
    "local_synchronization_without_global_coupling": (
        "Rounds 3/4 synchronized one or two boundaries; chained-sync used "
        "parallel disjoint paths.  Round 5 distributes all twelve seed-neighbor "
        "constraints around a multiway relay ring coupled through the original "
        "hat K_(3,4) core."
    ),
}


@dataclass(frozen=True)
class Relay:
    index: int
    vertices: tuple[str, ...]
    left_vertices: tuple[str, ...]
    right_vertices: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    signature: tuple[Signature, ...]
    key: str


@dataclass(frozen=True)
class Plan:
    ring_size: int
    cuts: tuple[int, ...]
    loads: tuple[int, ...]
    pattern: tuple[int, ...]


def _ordered_edge(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _sha256(path: Path):
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def has_forward_strict_endpoint(signature) -> bool:
    """True when maximal rank0 can concatenate onto zero rank1 at offset +1."""

    largest_rank0 = max(state.rank0 for state in signature)
    return any(
        state.rank0 == largest_rank0
        and state.rank1 == 0
        and state.offset == 1
        for state in signature
    )


def has_reverse_strict_endpoint(signature) -> bool:
    """Diagnostic only; the configured ring orientation does not require it."""

    largest_rank1 = max(state.rank1 for state in signature)
    return any(
        state.rank0 == 0
        and state.rank1 == largest_rank1
        and state.offset == -1
        for state in signature
    )


def build_selected_relays(
    signature_time_limit: float,
    maximum_signature_states: int,
    deadline: float,
):
    relays = []
    excluded_timeout_or_cap = 0
    started = time.perf_counter()

    for index, unordered_edges in enumerate(SELECTED_RELAY_EDGES):
        if time.perf_counter() >= deadline:
            excluded_timeout_or_cap += len(SELECTED_RELAY_EDGES) - index
            break

        edges = tuple(sorted(_ordered_edge(a, b) for a, b in unordered_edges))
        vertices = tuple(sorted({vertex for edge in edges for vertex in edge}))
        left_vertices = tuple(v for v in vertices if v.startswith("L"))
        right_vertices = tuple(v for v in vertices if not v.startswith("L"))

        augmented_vertices = ("X0", "X1") + vertices
        augmented_edges = (
            _ordered_edge("X0", "T0"),
            _ordered_edge("X1", "T1"),
        ) + edges
        augmented = Graph(
            augmented_vertices,
            augmented_edges,
            [
                ["X0", "X1"] + list(left_vertices),
                list(right_vertices),
            ],
        )
        remaining = deadline - time.perf_counter()
        signature = terminal_signatures(
            augmented,
            "T0",
            "T1",
            maximum_signature_states,
            min(signature_time_limit, remaining),
        )
        if signature is None:
            excluded_timeout_or_cap += 1
            continue

        relay = Relay(
            index=index,
            vertices=vertices,
            left_vertices=left_vertices,
            right_vertices=right_vertices,
            edges=edges,
            signature=tuple(sorted(signature, key=Signature.sort_key)),
            key=json.dumps(edges, separators=(",", ":")),
        )
        if not has_forward_strict_endpoint(relay.signature):
            excluded_timeout_or_cap += 1
            continue
        relays.append(relay)

    signature_sizes = Counter(len(relay.signature) for relay in relays)
    completed = len(relays) + excluded_timeout_or_cap == len(SELECTED_RELAY_EDGES)
    stats = {
        "selected_edge_sets": len(SELECTED_RELAY_EDGES),
        "solved_and_accepted": len(relays),
        "excluded_signature_timeout_or_cap_or_filter": excluded_timeout_or_cap,
        "relay_signature_stage_completed": completed,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "signature_size_counts": {
            str(size): count for size, count in sorted(signature_sizes.items())
        },
        "relays": [
            {
                "index": relay.index,
                "key": relay.key,
                "signature": [state.sort_key() for state in relay.signature],
                "forward_strict_endpoint": has_forward_strict_endpoint(
                    relay.signature
                ),
                "reverse_strict_endpoint": has_reverse_strict_endpoint(
                    relay.signature
                ),
            }
            for relay in relays
        ],
    }
    return relays, stats


def source_catalog_summary():
    path = Path(SOURCE_CATALOG)
    payload = {}
    if path.exists():
        try:
            data = json.loads(path.read_text()).get("gadget_enumeration", {})
            payload = {
                "raw_shapes": data.get("raw_shapes"),
                "solved_gadgets": data.get("solved_gadgets"),
                "excluded_signature_timeout": data.get(
                    "excluded_signature_timeout"
                ),
                "excluded_solution_cap": data.get("excluded_solution_cap"),
                "signature_stage_completed": data.get(
                    "signature_stage_completed"
                ),
                "selected_gadgets": data.get("selected_gadgets"),
            }
        except (OSError, ValueError):
            payload = {"read_error": True}
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "catalog": payload,
        "selection_rule": (
            "distinct strict-extreme relays with fewest edges, then fewest "
            "signature states, then edge-key order"
        ),
    }


def configured_plans():
    """Yield every mixed relay pattern with a degree-10 connector relay."""

    for ring_size in (3, 4):
        for cuts in itertools.combinations(range(1, 12), ring_size - 1):
            bounds = (0,) + cuts + (12,)
            loads = tuple(b - a for a, b in zip(bounds, bounds[1:]))
            if min(loads) < 1 or max(loads) != 8:
                continue
            for pattern in itertools.product(range(2), repeat=ring_size):
                if len(set(pattern)) < 2:
                    continue
                yield Plan(ring_size, cuts, loads, pattern)


def unconstrained_composition_count(ring_size, relay_count):
    """Size of the larger positive-composition/mixed-pattern superset."""

    compositions = len(
        list(itertools.combinations(range(1, 12), ring_size - 1))
    )
    mixed_patterns = 2**ring_size - 2 if relay_count == 2 else 0
    return compositions * mixed_patterns


def build_candidate(base, plan, relays):
    if len(relays) != 2:
        raise ValueError("the configured mixed-pattern family needs two relays")
    connectors = sorted(base.bipartition[1])
    if len(connectors) != 12:
        raise ValueError("expected twelve connectors in the hat K_(3,4) seed")

    core = [vertex for vertex in base.bipartition[0] if vertex != "u"]
    hubs = [f"U{index}" for index in range(plan.ring_size)]
    left = hubs + core
    right = list(connectors)
    edges = [edge for edge in base.edges if "u" not in edge]

    connector_index = 0
    for hub_index, load in enumerate(plan.loads):
        for _ in range(load):
            connector = connectors[connector_index]
            edges.append(_ordered_edge(hubs[hub_index], connector))
            connector_index += 1
    if connector_index != len(connectors):
        raise AssertionError("connector load vector did not consume twelve hubs")

    for position, relay_index in enumerate(plan.pattern):
        relay = relays[relay_index]
        next_position = (position + 1) % plan.ring_size
        mapping = {vertex: f"G{position}_{vertex}" for vertex in relay.vertices}
        for vertex in relay.left_vertices:
            mapped = mapping[vertex]
            if mapped not in left:
                left.append(mapped)
        for vertex in relay.right_vertices:
            mapped = mapping[vertex]
            if mapped not in right:
                right.append(mapped)
        for a, b in relay.edges:
            edges.append(_ordered_edge(mapping[a], mapping[b]))
        edges.append(_ordered_edge(hubs[position], mapping["T0"]))
        edges.append(_ordered_edge(hubs[next_position], mapping["T1"]))

    left = list(dict.fromkeys(left))
    right = list(dict.fromkeys(right))
    edges = list(dict.fromkeys(edges))
    graph = Graph(left + right, edges, [left, right])
    graph.metadata = {
        "lane": "lane6-multiway-mixed-relay-ring-r5",
        "construction": "three_or_four_hub_mixed_strict_extreme_relay_ring",
        "ring_size": plan.ring_size,
        "connector_load_vector": list(plan.loads),
        "contiguous_cutpoints": list(plan.cuts),
        "relay_pattern_by_position": list(plan.pattern),
        "relay_edge_keys": [relays[index].key for index in plan.pattern],
        "forced_degree10_hubs": [
            index for index, load in enumerate(plan.loads) if load == 8
        ],
        "hub_degrees_before_relay_attachment": [
            load + 2 for load in plan.loads
        ],
    }
    return graph


def all_fixed_spans_before_deadline(
    graph,
    per_span_time_limit,
    workers,
    deadline,
):
    """Independent fixed-span certification under the global hard deadline."""

    results = {}
    overall = "non-colorable"
    started = time.perf_counter()
    for span in range(graph.delta, max(graph.delta, graph.n - 1) + 1):
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            overall = "timeout"
            results[span] = {"status": "NOT_RUN_DEADLINE", "coloring": {}}
            continue

        status, coloring = fixed_span_sat_solve(
            graph,
            span,
            min(per_span_time_limit, remaining),
            workers,
        )
        results[span] = {
            "status": status,
            "coloring": {
                str(edge): color for edge, color in (coloring or {}).items()
            },
        }
        if status in ("OPTIMAL", "FEASIBLE"):
            overall = "colorable"
            break
        if status == "UNKNOWN":
            overall = "timeout"
    return {
        "encoding": "fixed-span-sat",
        "decision": overall,
        "completed_before_deadline": overall != "timeout",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "spans": results,
    }


def result_summary(result):
    return {
        key: value for key, value in result.__dict__.items() if key != "coloring"
    }


def side_degrees(graph):
    left, right = graph.bipartition
    degrees = graph.degrees
    return {
        "left": sorted(degrees[vertex] for vertex in left),
        "right": sorted(degrees[vertex] for vertex in right),
    }


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def append_negative_event(path, event):
    if path.exists():
        try:
            events = json.loads(path.read_text()).get("events", [])
        except (OSError, ValueError):
            events = []
    else:
        events = []
    events.append(event)
    write_json(path, {"events": events})


def save_primary_negative(graph, candidate_id, digest, output_path):
    graph_directory = output_path.parent / "graphs" / "lane6-r5"
    graph_directory.mkdir(parents=True, exist_ok=True)
    graph_path = graph_directory / f"{candidate_id}.graph.json"
    graph.save(graph_path)
    event = {
        "event": "primary_negative_saved",
        "reported_immediately": True,
        "candidate_id": candidate_id,
        "canonical_sha256": digest,
        "graph_json": str(graph_path.resolve()),
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
    }
    append_negative_event(
        output_path.parent / "lane6-signature-r5-negative-events.json",
        event,
    )
    print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)
    return graph_path, event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--maximum-delta", type=int, choices=(10,), default=10)
    parser.add_argument("--minimum-graph-degree", type=int, default=2)
    parser.add_argument("--maximum-signature-states", type=int, default=500000)
    parser.add_argument("--signature-time-limit", type=float, default=10.0)
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--independent-time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-interval", type=int, default=25)
    parser.add_argument(
        "--total-time-limit",
        type=float,
        default=TOTAL_TIME_LIMIT_SECONDS,
    )
    parser.add_argument("--output", default="results/lane6-signature-r5.json")
    args = parser.parse_args()

    if args.total_time_limit > TOTAL_TIME_LIMIT_SECONDS + 1e-9:
        parser.error("--total-time-limit may not exceed 7200 seconds")
    if args.workers < 1:
        parser.error("--workers must be positive")

    started = time.perf_counter()
    deadline = started + float(args.total_time_limit)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    negative_event_path = (
        output_path.parent / "lane6-signature-r5-negative-events.json"
    )
    write_json(negative_event_path, {"events": []})

    _, base = seed_graph()
    relays, relay_stats = build_selected_relays(
        args.signature_time_limit,
        args.maximum_signature_states,
        deadline,
    )
    relay_stage_completed = relay_stats["relay_signature_stage_completed"]

    plans = list(configured_plans()) if relay_stage_completed else []
    unconstrained_before_cull = (
        unconstrained_composition_count(3, len(relays))
        + unconstrained_composition_count(4, len(relays))
        if len(relays) == 2
        else 0
    )

    rows = []
    negative_artifacts = []
    seen = set()
    generated = 0
    skipped_filtered_or_duplicate = 0
    candidate_stage_completed = True
    all_configured_candidates_processed = True

    for plan_index, plan in enumerate(plans):
        if time.perf_counter() >= deadline:
            candidate_stage_completed = False
            all_configured_candidates_processed = False
            break

        graph = build_candidate(base, plan, relays)
        generated += 1
        structural_filter_passed = (
            graph.delta <= args.maximum_delta
            and min(graph.degrees.values()) >= args.minimum_graph_degree
            and nx.is_connected(graph._nx)
        )
        if not structural_filter_passed:
            skipped_filtered_or_duplicate += 1
            continue

        digest = nauty_canonical_hash(graph)
        if digest in seen:
            skipped_filtered_or_duplicate += 1
            continue
        seen.add(digest)

        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            candidate_stage_completed = False
            all_configured_candidates_processed = False
            break
        result = rank_potential_solve(
            graph,
            min(args.time_limit, remaining),
            args.workers,
        )
        candidate_id = f"L6R5-{len(rows):04d}"
        row = {
            "candidate_id": candidate_id,
            "canonical_sha256": digest,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "minimum_degree": min(graph.degrees.values()),
            "degree_sequence_by_side": side_degrees(graph),
            "plan_index": plan_index,
            "plan": {
                "ring_size": plan.ring_size,
                "cuts": list(plan.cuts),
                "loads": list(plan.loads),
                "pattern": list(plan.pattern),
            },
            "metadata": graph.metadata,
            "weighted_hubs_best": weighted_hub_statistics(graph)[:2],
            "primary_result": result_summary(result),
            "certified_non_colorable": False,
        }

        if result.status == "non-colorable":
            graph_path, negative_event = save_primary_negative(
                graph,
                candidate_id,
                digest,
                output_path,
            )
            row["graph_json"] = str(graph_path)
            row["negative_event"] = negative_event
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                row["independent_spans"] = {
                    "encoding": "fixed-span-sat",
                    "decision": "timeout",
                    "completed_before_deadline": False,
                    "spans": {},
                }
            else:
                row["independent_spans"] = all_fixed_spans_before_deadline(
                    graph,
                    args.independent_time_limit,
                    args.workers,
                    deadline,
                )
            row["certified_non_colorable"] = (
                row["independent_spans"]["decision"] == "non-colorable"
            )
            negative_artifacts.append(
                {
                    "candidate_id": candidate_id,
                    "canonical_sha256": digest,
                    "graph_json": str(graph_path),
                    "order": graph.n,
                    "size": graph.m,
                    "delta": graph.delta,
                    "minimum_degree": min(graph.degrees.values()),
                    "independently_certified": row["certified_non_colorable"],
                }
            )
            print(
                json.dumps(negative_artifacts[-1], sort_keys=True),
                file=sys.stderr,
                flush=True,
            )

        rows.append(row)
        if (
            args.checkpoint_interval
            and len(rows) % args.checkpoint_interval == 0
        ):
            checkpoint_counts = Counter(
                row["primary_result"]["status"] for row in rows
            )
            checkpoint = {
                "checkpoint": True,
                "generated": generated,
                "unique": len(rows),
                "counts": {
                    status: checkpoint_counts.get(status, 0)
                    for status in ("colorable", "non-colorable", "timeout")
                },
                "candidate_stage_completed": candidate_stage_completed,
                "rows": rows,
            }
            write_json(output_path, checkpoint)
            print(
                json.dumps(
                    {
                        key: value
                        for key, value in checkpoint.items()
                        if key != "rows"
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )

    counts = Counter(row["primary_result"]["status"] for row in rows)
    colorable_count = counts.get("colorable", 0)
    non_colorable_count = counts.get("non-colorable", 0)
    timeout_count = counts.get("timeout", 0)
    confirmed_negative_count = sum(
        row.get("certified_non_colorable", False) for row in rows
    )
    conflicting_classifications = sum(
        row.get("independent_spans", {}).get("decision") == "colorable"
        for row in rows
    )
    unfinished_independent_certifications = sum(
        row.get("independent_spans", {}).get("decision") == "timeout"
        for row in rows
    )
    certification_stage_complete = (
        non_colorable_count
        == confirmed_negative_count + conflicting_classifications
        and unfinished_independent_certifications == 0
    )
    runtime_deadline_hit = time.perf_counter() >= deadline
    nauty_deduplication_complete = candidate_stage_completed
    exact_classification_complete = candidate_stage_completed and timeout_count == 0
    family_exhausted = (
        relay_stage_completed
        and candidate_stage_completed
        and all_configured_candidates_processed
        and nauty_deduplication_complete
        and exact_classification_complete
        and certification_stage_complete
        and conflicting_classifications == 0
    )
    unresolved_timeout_count = (
        timeout_count
        + int(not relay_stage_completed)
        + int(not candidate_stage_completed)
        + unfinished_independent_certifications
        + conflicting_classifications
    )

    spans = [
        row["primary_result"]["span"]
        for row in rows
        if row["primary_result"].get("span") is not None
    ]
    summary = {
        "schema": "lane6-signature-search-round-5",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "total_time_limit_seconds": args.total_time_limit,
        "runtime_deadline_hit": runtime_deadline_hit,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "ortools": __import__("ortools").__version__,
            "networkx": nx.__version__,
        },
        "analysis_inputs": {
            "completed_results": [
                "results/lane6-signature-r2.json",
                "results/lane6-signature-r3.json",
                "results/lane6-signature-r4.json",
                "results/lane6-split-hub-delta10.json",
                "results/lane6-chained-sync-corrected.json",
            ],
            "theory": "interval-edge-coloring-research-plan.md",
            "source_catalog_sha256": _sha256(Path(SOURCE_CATALOG)),
        },
        "prior_round_diagnosis": PRIOR_ROUND_DIAGNOSIS,
        "family_definition": {
            "seed": "hat_K34_prime_Delta11",
            "construction": (
                "distribute the twelve seed connectors contiguously over a "
                "three- or four-hub directed ring; join successive hubs with "
                "mixed compact strict-extreme relay gadgets"
            ),
            "relay_source_catalog": SOURCE_CATALOG,
            "relay_internal_vertex_bound": 5,
            "relay_selection": (
                "two shortest distinct forward strict-endpoint relays"
            ),
            "ring_sizes": [3, 4],
            "connector_load_rule": (
                "positive ordered compositions of 12 whose maximum part is "
                "exactly 8, giving at least one degree-10 relay"
            ),
            "pattern_rule": "all binary patterns using both selected relay types",
            "minimum_graph_degree": args.minimum_graph_degree,
            "maximum_delta": args.maximum_delta,
        },
        "completion_flags": {
            "relay_signature_stage_completed": relay_stage_completed,
            "candidate_stage_completed": candidate_stage_completed,
            "all_configured_candidates_processed": (
                all_configured_candidates_processed
            ),
            "nauty_deduplication_complete": nauty_deduplication_complete,
            "exact_rank_potential_classification_complete": (
                exact_classification_complete
            ),
            "independent_certification_stage_complete": (
                certification_stage_complete
            ),
            "configured_family_exhausted": family_exhausted,
            "family_exhausted": family_exhausted,
            "no_timeout_treated_as_negative": True,
        },
        "relay_enumeration": {
            "source_catalog": source_catalog_summary(),
            **relay_stats,
        },
        "composition": {
            "unconstrained_two_relay_rings_before_cull": (
                unconstrained_before_cull
            ),
            "configured_generated_before_filters": len(plans),
            "generated": generated,
            "skipped_filtered_or_duplicate": skipped_filtered_or_duplicate,
            "unique": len(rows),
            "completed_unique": len(rows),
            "colorable": colorable_count,
            "non_colorable": non_colorable_count,
            "timeout": timeout_count,
            "counts": {
                status: counts.get(status, 0)
                for status in ("colorable", "non-colorable", "timeout")
            },
            "primary_encoding": "exact-rank-potential-cpsat",
            "deduplication": "pynauty bipartition-colored certificate sha256",
            "minimum_primary_span": min(spans) if spans else None,
            "maximum_primary_span": max(spans) if spans else None,
        },
        "certification": {
            "policy": (
                "a primary negative is saved immediately but is called "
                "certified only when independent fixed-span SAT proves "
                "INFEASIBLE for every span delta through n-1"
            ),
            "negative_independently_confirmed": confirmed_negative_count,
            "unconfirmed_primary_negatives": non_colorable_count
            - confirmed_negative_count
            - conflicting_classifications,
            "conflicting_classifications": conflicting_classifications,
            "unfinished_independent_certifications": (
                unfinished_independent_certifications
            ),
            "unresolved_timeout_count": unresolved_timeout_count,
        },
        "negative_artifacts": negative_artifacts,
        "negative_events_path": str(negative_event_path.resolve()),
        "rows": rows,
    }
    write_json(output_path, summary)
    printable = {key: value for key, value in summary.items() if key != "rows"}
    print(json.dumps(printable, indent=2))


if __name__ == "__main__":
    main()
