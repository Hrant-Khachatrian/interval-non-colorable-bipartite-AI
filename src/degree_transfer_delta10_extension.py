#!/usr/bin/env python3
"""Deterministic extension of the capped Delta<=10 degree-transfer search."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx

from degree_transfer_delta10_search import (
    MOTIFS,
    Counters,
    apply_terminal_gadget,
    covering_selections,
    demand_edges,
    degree_cap_holds,
    independent_confirmation,
    parent_graphs,
)
from interval_edge_coloring import (
    Graph,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)


@dataclass
class ExtensionCandidate:
    parent: str
    signature: tuple[tuple[int, ...], tuple[int, ...]]
    graph: Graph
    replacements: list[dict[str, Any]]
    canonical_sha256: str
    hub_best_margin: int
    normalized_degree_variance: float


class ExtensionCounters(Counters):
    baseline_signatures_skipped: int = 0
    resumed_signatures_skipped: int = 0
    resumed_hash_duplicates: int = 0


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_completed_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid state row {path}:{line_number}: {error}") from error
            required = {"parent", "signature", "canonical_sha256", "decision"}
            if not required.issubset(row):
                raise ValueError(f"incomplete state row at {path}:{line_number}")
            row["signature"] = (
                tuple(row["signature"][0]),
                tuple(row["signature"][1]),
            )
            rows.append(row)
    return rows


def counts_from_records(records: list[dict[str, Any]]) -> dict[str, int]:
    decisions = [row.get("decision") for row in records]
    return {
        "newly_classified": len(records),
        "colorable": sum(value == "colorable" for value in decisions),
        "non_colorable": sum(value == "non-colorable" for value in decisions),
        "confirmed_non_colorable": sum(
            value == "non-colorable" for value in decisions
        ),
        "primary_negative_candidates": sum(
            row.get("primary_status") == "non-colorable" for row in records
        ),
        "timeout": sum(value.startswith("unresolved") for value in decisions),
        "primary_timeout": sum(
            value == "unresolved_primary_timeout" for value in decisions
        ),
        "independent_unresolved": sum(
            value in ("unresolved", "unresolved_independent_wall_deadline")
            for value in decisions
        ),
    }


def structural_rank(graph: Graph) -> tuple[int, int, int]:
    hubs = weighted_hub_statistics(graph)
    defined_hubs = [row for row in hubs if row["weighted_neighbor_diameter"] is not None]
    hub_best_margin = min(
        (row["margin"] for row in defined_hubs),
        default=-(10**9),
    )
    degrees = graph.degrees
    side_degrees = [
        [degrees[vertex] for vertex in graph.bipartition[side]]
        for side in range(2)
    ]
    means = [sum(side) / len(side) for side in side_degrees]
    variances = [
        sum((value - mean) ** 2 for value in side) / len(side)
        for side, mean in zip(side_degrees, means)
    ]
    global_mean = sum(means) / 2
    variance = (sum(variances) / 2) / global_mean**2
    if hub_best_margin >= -1.5:
        tier = 1
    elif hub_best_margin >= -2.5:
        tier = 2
    else:
        tier = 3
    return tier, hub_best_margin, int(round(variance * 10**12))


def structural_features(graph: Graph) -> dict[str, Any]:
    hubs = weighted_hub_statistics(graph)
    defined_hubs = [row for row in hubs if row["weighted_neighbor_diameter"] is not None]
    best_hub = min(
        defined_hubs,
        key=lambda row: (-row["margin"], row["hub"]),
        default=None,
    )
    degrees = graph.degrees
    side_degrees = [
        [degrees[vertex] for vertex in graph.bipartition[side]]
        for side in range(2)
    ]
    means = [sum(side) / len(side) for side in side_degrees]
    variances = [
        sum((value - mean) ** 2 for value in side) / len(side)
        for side, mean in zip(side_degrees, means)
    ]
    global_mean = sum(means) / 2
    return {
        **(
            {
                "hub_best_margin": best_hub["margin"],
                "hub_best_weighted_neighbor_diameter": best_hub[
                    "weighted_neighbor_diameter"
                ],
            }
            if best_hub is not None
            else {
                "hub_best_margin": None,
                "hub_best_weighted_neighbor_diameter": None,
            }
        ),
        "degree_variance_normalized": (sum(variances) / 2) / global_mean**2,
        "weighted_hubs_best": hubs[:2],
    }


def baseline_signature_set(baseline: dict[str, Any], parent: str) -> set[tuple[tuple[int, ...], tuple[int, ...]]]:
    result: set[tuple[tuple[int, ...], tuple[int, ...]]] = set()
    for row in baseline.get("records", []):
        if row.get("parent") != parent:
            continue
        result.add(
            (
                tuple(row["selected_parent_edges"]),
                tuple(row["replacement_motifs"]),
            )
        )
    return result


def parse_state_signature(row: dict[str, Any]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return tuple(row["signature"][0]), tuple(row["signature"][1])


def generate_parent(
    name: str,
    base: Graph,
    kind: str,
    baseline: dict[str, Any],
    resume_signatures: set[tuple[tuple[int, ...], tuple[int, ...]]],
    resume_hashes: set[str],
    global_seen: set[str],
    maximum_delta: int,
    maximum_replacements: int,
    minimum_degree: int,
    extension_cap: int,
    already_classified: int,
    deadline: float,
) -> tuple[dict[str, Any], list[ExtensionCandidate], bool, bool]:
    started = time.monotonic()
    counters = ExtensionCounters()
    candidates: list[ExtensionCandidate] = []
    local_seen: set[str] = set()
    old_signatures = baseline_signature_set(baseline, name)
    remaining_quota = max(0, extension_cap - already_classified)
    preloaded_target_met = remaining_quota == 0
    candidate_edges = demand_edges(base, maximum_delta)
    cap_reached = False
    family_exhausted = True
    if preloaded_target_met:
        cap_reached = True
        family_exhausted = False
    construction_number = 0

    try:
        if not preloaded_target_met:
            selections = covering_selections(
                base,
                candidate_edges,
                maximum_delta,
                maximum_replacements,
                deadline,
            )
            for selected in selections:
                if len(candidates) >= remaining_quota:
                    cap_reached = True
                    family_exhausted = False
                    break
                for motif_ids in itertools.product(range(len(MOTIFS)), repeat=len(selected)):
                    if deadline - time.monotonic() <= 10.0:
                        raise TimeoutError("generation deadline reserve reached")
                    signature = (tuple(sorted(selected)), tuple(motif_ids))
                    if signature in old_signatures:
                        counters.baseline_signatures_skipped += 1
                        continue
                    if signature in resume_signatures:
                        counters.resumed_signatures_skipped += 1
                        continue
                    if not degree_cap_holds(base, selected, motif_ids, maximum_delta):
                        raise AssertionError("degree-cap invariant failed")
                    graph, replacements = apply_terminal_gadget(
                        base,
                        selected,
                        motif_ids,
                        construction_number,
                    )
                    construction_number += 1
                    counters.constructed += 1
                    nx_graph = nx.Graph(graph.edges)
                    nx_graph.add_nodes_from(graph.vertices)
                    if not nx.is_connected(nx_graph):
                        counters.rejected_disconnected += 1
                        continue
                    if min(nx_graph.degree(vertex) for vertex in graph.vertices) < minimum_degree:
                        counters.rejected_low_degree += 1
                        continue
                    if not nx.is_bipartite(nx_graph):
                        raise AssertionError("replacement graph is not bipartite")
                    if graph.delta > maximum_delta:
                        raise AssertionError("replacement graph exceeds maximum Delta")
                    counters.accepted += 1
                    digest = nauty_canonical_hash(graph)
                    if digest in resume_hashes:
                        counters.resumed_hash_duplicates += 1
                        continue
                    if digest in global_seen or digest in local_seen:
                        counters.duplicate += 1
                        continue
                    local_seen.add(digest)
                    global_seen.add(digest)
                    features = structural_features(graph)
                    candidates.append(
                        ExtensionCandidate(
                            parent=name,
                            signature=signature,
                            graph=graph,
                            replacements=replacements,
                            canonical_sha256=digest,
                            hub_best_margin=int(features["hub_best_margin"]),
                            normalized_degree_variance=float(
                                features["degree_variance_normalized"]
                            ),
                        )
                    )
                    if len(candidates) >= remaining_quota:
                        cap_reached = True
                        family_exhausted = False
                        break
                if cap_reached:
                    break
    except TimeoutError:
        family_exhausted = False

    summary = {
        "parent": name,
        "parent_kind": kind,
        "parent_order": base.n,
        "parent_size": base.m,
        "parent_delta": base.delta,
        "overcap_vertices": {
            vertex: degree - maximum_delta
            for vertex, degree in base.degrees.items()
            if degree > maximum_delta
        },
        "candidate_parent_edges": len(candidate_edges),
        "target_unique_per_parent": extension_cap,
        "resumed_completed_loaded": already_classified,
        "remaining_candidate_quota": remaining_quota,
        "preloaded_target_met": preloaded_target_met,
        "generated_this_run": counters.constructed,
        "generated": counters.constructed,
        "valid_candidates_generated": counters.accepted,
        "unique_new_this_run": len(candidates),
        "unique": len(candidates),
        "duplicates": counters.duplicate,
        "resumed_signatures_skipped": counters.resumed_signatures_skipped,
        "resumed_hash_duplicates": counters.resumed_hash_duplicates,
        "rejected_disconnected": counters.rejected_disconnected,
        "rejected_low_degree": counters.rejected_low_degree,
        "baseline_signatures_skipped": counters.baseline_signatures_skipped,
        "extension_candidate_cap": extension_cap,
        "candidate_cap_reached": cap_reached,
        "replacement_family_exhausted_before_extension_cap": family_exhausted,
        "generation_elapsed_seconds": time.monotonic() - started,
    }
    return summary, candidates, cap_reached, family_exhausted


def rank_candidates(candidates: list[ExtensionCandidate]) -> list[ExtensionCandidate]:
    def key(candidate: ExtensionCandidate) -> tuple[int, int, int, int, int, tuple[int, ...], tuple[int, ...]]:
        tier, margin, variance_scaled = structural_rank(candidate.graph)
        graph = candidate.graph
        return (
            tier,
            -variance_scaled,
            -margin,
            graph.n,
            graph.m,
            candidate.signature[0],
            candidate.signature[1],
        )

    return sorted(candidates, key=key)


def aggregate_summaries(summaries: list[dict[str, Any]], field: str) -> int:
    return sum(int(item.get(field, 0)) for item in summaries)


def make_report(
    configuration: dict[str, Any],
    baseline: dict[str, Any],
    baseline_path: Path,
    summaries: list[dict[str, Any]],
    records: list[dict[str, Any]],
    elapsed_seconds: float,
    deadline_seconds: float,
    runtime_deadline_hit: bool,
) -> dict[str, Any]:
    counts_by_parent = {
        item["parent"]: item.get("counts", counts_from_records([]))
        for item in summaries
    }
    totals = {
        "replacements_generated": aggregate_summaries(summaries, "generated"),
        "valid_candidates_generated": aggregate_summaries(
            summaries, "valid_candidates_generated"
        ),
        "generated": aggregate_summaries(summaries, "generated"),
        "unique_candidates_generated_this_resume": aggregate_summaries(
            summaries, "unique_new_this_run"
        ),
        "unique_new_this_extension": aggregate_summaries(summaries, "unique"),
        "resumed_completed_loaded": aggregate_summaries(
            summaries, "resumed_completed_loaded"
        ),
        "newly_classified_this_resume": len(records),
        "unique_classified_including_resumed": aggregate_summaries(
            summaries, "resumed_completed_loaded"
        )
        + len(records),
        "duplicates": aggregate_summaries(summaries, "duplicates"),
        "rejected_disconnected": aggregate_summaries(
            summaries, "rejected_disconnected"
        ),
        "rejected_low_degree": aggregate_summaries(summaries, "rejected_low_degree"),
        "baseline_signatures_skipped": aggregate_summaries(
            summaries, "baseline_signatures_skipped"
        ),
    }
    counts = {
        field: sum(int(item.get(field, 0)) for item in counts_by_parent.values())
        for field in (
            "newly_classified",
            "colorable",
            "non_colorable",
            "confirmed_non_colorable",
            "primary_negative_candidates",
            "timeout",
            "primary_timeout",
            "independent_unresolved",
        )
    }
    configured_complete = bool(summaries) and all(
        item.get("classification_complete", False)
        and (
            item.get("replacement_family_exhausted_before_extension_cap", False)
            or item.get("candidate_cap_reached", False)
        )
        for item in summaries
    )
    fully_classified = configured_complete and counts["timeout"] == 0
    negative_events = [
        {
            "event": "certified_non_colorable",
            "candidate_id": row.get("candidate_id"),
            "path": row.get("graph_path"),
            "parent": row["parent"],
            "canonical_sha256": row["canonical_sha256"],
        }
        for row in records
        if row.get("decision") == "non-colorable"
    ]
    ranked_top = sorted(
        records,
        key=lambda row: (
            row.get("structural_rank_tier", 99),
            -int(
                round(
                    row.get("structural_features", {}).get(
                        "degree_variance_normalized", 0.0
                    )
                    * 10**12
                )
            ),
            -(row.get("structural_features", {}).get("hub_best_margin") or -(10**9)),
            row.get("order", 10**9),
            row.get("size", 10**9),
            tuple(row.get("signature", [[], []])[0]),
            tuple(row.get("signature", [[], []])[1]),
        ),
    )[:10]
    baseline_totals = baseline.get("totals", {})
    return {
        "schema_version": 1,
        "mode": "deterministic_baseline_extension_v1",
        "configuration": configuration,
        "complete": configured_complete,
        "completion_flags": {
            "configured_extension_complete_without_timeout": configured_complete,
            "all_targets_fully_classified_exactly": fully_classified,
            "independent_confirmation_complete_without_timeout": all(
                item.get("independent_confirmation_complete_without_timeout", True)
                for item in summaries
            ),
            "replacement_family_exhausted_before_extension_cap_all_targets": bool(
                summaries
            )
            and all(
                item.get("replacement_family_exhausted_before_extension_cap", False)
                for item in summaries
            ),
            "candidate_cap_reached_any_target": any(
                item.get("candidate_cap_reached", False) for item in summaries
            ),
            "runtime_deadline_hit": runtime_deadline_hit,
        },
        "runtime_deadline_hit": runtime_deadline_hit,
        "deadline_seconds": deadline_seconds,
        "elapsed_seconds": elapsed_seconds,
        "totals": totals,
        "counts": counts,
        "cumulative_with_baseline": {
            "baseline_unique": baseline_totals.get("unique", 0),
            "extension_unique_classified": len(records),
            "total_unique_represented": baseline_totals.get("unique", 0)
            + len(records),
            "baseline_colorable": baseline_totals.get("colorable", 0),
            "extension_colorable": counts["colorable"],
            "total_colorable": baseline_totals.get("colorable", 0)
            + counts["colorable"],
            "baseline_non_colorable": baseline_totals.get("non_colorable", 0),
            "extension_confirmed_non_colorable": counts["non_colorable"],
            "total_confirmed_non_colorable": baseline_totals.get(
                "non_colorable", 0
            )
            + counts["non_colorable"],
        },
        "baseline": {
            "path": str(baseline_path),
            "schema_version": baseline.get("schema_version"),
            "complete": baseline.get("complete", False),
            "maximum_final_delta": baseline.get("configuration", {}).get(
                "maximum_final_delta"
            ),
            "candidate_cap_unique_per_parent": baseline.get(
                "configuration", {}
            ).get("candidate_cap_unique_per_parent"),
            "canonical_certificates_seeded": len(
                {row["canonical_sha256"] for row in baseline.get("records", [])}
            ),
            "capped_parents": [
                item["parent"]
                for item in baseline.get("summaries", [])
                if item.get("candidate_cap_reached", False)
            ],
        },
        "negative_events": negative_events,
        "best_near_miss_diagnostics": {
            "global_ranked_top": [
                {
                    key: row.get(key)
                    for key in (
                        "canonical_sha256",
                        "parent",
                        "signature",
                        "order",
                        "size",
                        "delta",
                        "minimum_degree",
                        "decision",
                        "primary_status",
                        "primary_span",
                        "structural_rank_tier",
                        "structural_features",
                    )
                }
                for row in ranked_top
            ]
        },
        "summaries": summaries,
        "records": records,
    }


def parse_signature(row: dict[str, Any]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return tuple(row["signature"][0]), tuple(row["signature"][1])


def classify_parent(
    summary: dict[str, Any],
    candidates: list[ExtensionCandidate],
    completed_rows: list[dict[str, Any]],
    graph_dir: Path,
    state_path: Path,
    time_limit: float,
    workers: int,
    deadline: float,
    checkpoint: dict[str, Any],
    write_report: Any,
    candidate_number_offset: int = 0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started = time.monotonic()
    completed_by_digest = {row["canonical_sha256"]: row for row in completed_rows}
    records: list[dict[str, Any]] = []
    for candidate in candidates:
        previous = completed_by_digest.get(candidate.canonical_sha256)
        if previous is not None:
            records.append(previous)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= max(30.0, time_limit + 5.0):
            summary["runtime_deadline_hit"] = True
            break
        graph = candidate.graph
        primary = rank_potential_solve(graph, time_limit, workers)
        row: dict[str, Any] = {
            "parent": candidate.parent,
            "parent_kind": summary["parent_kind"],
            "canonical_sha256": candidate.canonical_sha256,
            "signature": [list(candidate.signature[0]), list(candidate.signature[1])],
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "minimum_degree": min(graph.degrees.values()),
            "connected": True,
            "bipartite": True,
            "selected_parent_edges": list(candidate.signature[0]),
            "replacement_motifs": list(candidate.signature[1]),
            "replacement_details": candidate.replacements,
            "structural_features": structural_features(graph),
            "structural_rank_tier": structural_rank(graph)[0],
            "primary_status": primary.status,
            "primary_span": primary.span,
            "primary_elapsed_seconds": primary.elapsed_seconds,
        }
        decision = primary.status
        if primary.status == "non-colorable":
            confirmation_deadline = deadline - 3.0
            if confirmation_deadline - time.monotonic() <= 0.25:
                decision = "unresolved_independent_wall_deadline"
                row["independent_unresolved"] = True
                row["independent_spans"] = {}
            else:
                confirmed, unresolved, span_statuses = independent_confirmation(
                    graph,
                    time_limit,
                    workers,
                    confirmation_deadline,
                )
                row["independent_spans"] = {
                    str(span): status for span, status in sorted(span_statuses.items())
                }
                row["independent_unresolved"] = unresolved
                if confirmed:
                    decision = "non-colorable"
                    candidate_id = f"DTRX-{candidate_number_offset + len(records) + 1:04d}"
                    graph.metadata["candidate_id"] = candidate_id
                    graph.metadata["extension_source"] = str(state_path)
                    graph.metadata["certification"] = {
                        "primary": "rank-potential-cpsat-infeasible",
                        "independent": (
                            "fixed-span-cpsat-infeasible-all-legal-spans"
                        ),
                        "spans_checked": sorted(span_statuses),
                    }
                    path = graph_dir / f"{candidate_id}.graph.json"
                    graph.save(path)
                    row["candidate_id"] = candidate_id
                    row["graph_path"] = str(path)
                elif unresolved:
                    decision = "unresolved"
                else:
                    raise AssertionError(
                        "primary and independent classifications conflict for "
                        + candidate.canonical_sha256
                    )
        elif primary.status == "timeout":
            decision = "unresolved_primary_timeout"
        elif primary.status not in ("colorable",):
            raise AssertionError(f"unexpected solver status {primary.status}")

        row["decision"] = decision
        records.append(row)
        with state_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if decision == "non-colorable":
            event = {
                "event": "certified_non_colorable",
                "candidate_id": row.get("candidate_id"),
                "path": row.get("graph_path"),
                "parent": candidate.parent,
                "canonical_sha256": candidate.canonical_sha256,
            }
            print(json.dumps(event, sort_keys=True), flush=True)

        now = time.monotonic()
        if now - checkpoint["last_write"] >= 30.0:
            summary["classification_elapsed_seconds"] = now - started
            summary["counts"] = counts_from_records(records)
            write_report(summary, records)
            checkpoint["last_write"] = now

    summary.update(
        {
            "classification_elapsed_seconds": time.monotonic() - started,
            "counts": counts_from_records(records),
            "classification_complete": len(records) == len(candidates)
            and not summary.get("runtime_deadline_hit", False),
            "independent_confirmation_complete_without_timeout": all(
                not row.get("independent_unresolved", False) for row in records
            ),
        }
    )
    return summary, records

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        default="results/degree-transfer-delta10.json",
    )
    parser.add_argument(
        "--output",
        default="results/degree-transfer-delta10-extension.json",
    )
    parser.add_argument(
        "--seed-state",
        default="results/degree-transfer-delta10-extension-state.jsonl",
        help="Prior append-only state loaded as completed evidence; it is never rewritten.",
    )
    parser.add_argument(
        "--parents",
        default="Erd_Fano_2222221,M5_delta_555",
        help="Comma-separated capped parents to extend.",
    )
    parser.add_argument("--additional-unique-per-parent", type=int, default=1500)
    parser.add_argument("--maximum-final-delta", type=int, default=10)
    parser.add_argument("--max-replaced-edges", type=int, default=6)
    parser.add_argument("--minimum-degree", type=int, default=2)
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--deadline-seconds", type=float, default=7200.0)
    args = parser.parse_args()

    requested_parents = [name.strip() for name in args.parents.split(",") if name.strip()]
    if not requested_parents:
        parser.error("at least one parent is required")
    baseline_path = Path(args.baseline)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    output_path = Path(args.output)
    graph_dir = output_path.parent / "graphs" / output_path.stem
    graph_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_path.with_name(output_path.stem + "-state.jsonl")
    seed_path = Path(args.seed_state)
    seed_rows = load_completed_rows(seed_path) if seed_path.exists() else []
    current_rows = load_completed_rows(state_path) if state_path.exists() else []
    merged_rows: dict[str, dict[str, Any]] = {}
    for row in [*seed_rows, *current_rows]:
        digest = row["canonical_sha256"]
        previous = merged_rows.get(digest)
        if previous is not None:
            if previous.get("parent") != row.get("parent") or previous.get("decision") != row.get("decision"):
                raise SystemExit(f"conflicting durable evidence for certificate {digest}")
            continue
        merged_rows[digest] = row
    completed_rows = list(merged_rows.values())
    available = {name: (graph, kind) for name, graph, kind in parent_graphs(graph_dir)}
    missing = [name for name in requested_parents if name not in available]
    if missing:
        parser.error(f"unknown parent(s): {', '.join(missing)}")
    if baseline.get("configuration", {}).get("maximum_final_delta") != args.maximum_final_delta:
        raise SystemExit("baseline maximum Delta does not match extension configuration")

    baseline_hashes = {
        row["canonical_sha256"] for row in baseline.get("records", [])
    }
    global_seen = baseline_hashes | {row["canonical_sha256"] for row in completed_rows}
    resume_signatures = {parse_state_signature(row) for row in completed_rows}
    resume_hashes = {row["canonical_sha256"] for row in completed_rows}
    summaries: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    checkpoint = {"last_write": time.monotonic()}
    run_started_wall = time.time()
    run_started = time.monotonic()
    deadline = run_started + args.deadline_seconds
    configuration = {
        "extension_of": str(baseline_path),
        "search_lane": "resumed terminal-gadget transfer of incident over-cap edges",
        "target_parents": requested_parents,
        "target_semantics": (
            "additional_unique_per_parent is cumulative across loaded resumed "
            "evidence and this run; each parent generates only its remaining quota"
        ),
        "seed_state": str(seed_path),
        "additional_unique_per_parent": args.additional_unique_per_parent,
        "maximum_final_delta": args.maximum_final_delta,
        "maximum_replaced_edges_per_parent": args.max_replaced_edges,
        "minimum_graph_degree": args.minimum_degree,
        "require_connected": True,
        "require_bipartite": True,
        "deduplication": "bipartition-colored Nauty certificate SHA-256 seeded from baseline",
        "baseline_resume_rule": (
            "skip baseline-recorded accepted edge/motif signatures before graph "
            "construction; reject any reconstructed graph whose Nauty certificate "
            "matches a baseline or earlier extension certificate"
        ),
        "structural_ranking": [
            "tier 1: hub_best_margin >= -1.5",
            "tier 2: otherwise hub_best_margin >= -2.5",
            "tier 3: otherwise normalized degree variance descending",
            "secondary keys: variance descending, hub margin descending, order, size",
        ],
        "excluded_ranking_predicates": ["Delta", "forced span bounds equivalent to Delta"],
        "primary_classification": "rank-potential CP-SAT",
        "negative_confirmation": "fixed-span CP-SAT independently over every legal span",
        "timeout_policy": "timeout is unresolved and never counted as non-colorable",
        "solver_time_limit_seconds": args.time_limit,
        "workers": args.workers,
        "state_path": str(state_path),
    }

    def write_report(
        current_summary: dict[str, Any] | None = None,
        current_records: list[dict[str, Any]] | None = None,
        *,
        partial: bool = False,
    ) -> None:
        combined_summaries = [*summaries]
        combined_records = [*all_records]
        if current_summary is not None:
            combined_summaries.append(current_summary)
        if current_records is not None:
            combined_records.extend(current_records)
        report = make_report(
            configuration,
            baseline,
            baseline_path,
            combined_summaries,
            combined_records,
            time.monotonic() - run_started,
            args.deadline_seconds,
            time.monotonic() >= deadline,
        )
        if partial or current_summary is not None:
            report["checkpoint"] = {
                "written_unix_time": time.time(),
                "partial": True,
                "current_parent": current_summary.get("parent")
                if current_summary
                else None,
            }
        atomic_write_json(output_path, report)

    write_report()
    for name in requested_parents:
        base, kind = available[name]
        print(
            json.dumps(
                {"event": "generation_start", "parent": name, "kind": kind},
                sort_keys=True,
            ),
            flush=True,
        )
        summary, candidates, cap_reached, family_exhausted = generate_parent(
            name,
            base,
            kind,
            baseline,
            {
                parse_state_signature(row)
                for row in completed_rows
                if row["parent"] == name
            },
            resume_hashes,
            global_seen,
            args.maximum_final_delta,
            args.max_replaced_edges,
            args.minimum_degree,
            args.additional_unique_per_parent,
            sum(row["parent"] == name for row in completed_rows),
            deadline,
        )
        summary["counts"] = counts_from_records([])
        summary["classification_complete"] = not candidates
        summary["runtime_deadline_hit"] = time.monotonic() >= deadline
        print(
            json.dumps(
                {
                    "event": "generation_complete",
                    "parent": name,
                    **{
                        key: summary[key]
                        for key in (
                            "generated",
                            "valid_candidates_generated",
                            "unique",
                            "duplicates",
                            "baseline_signatures_skipped",
                            "candidate_cap_reached",
                            "replacement_family_exhausted_before_extension_cap",
                        )
                    },
                },
                sort_keys=True,
            ),
            flush=True,
        )
        ordered_candidates = rank_candidates(candidates)
        parent_completed_rows = [
            row for row in completed_rows if row["parent"] == name
        ]
        summary, records = classify_parent(
            summary,
            ordered_candidates,
            parent_completed_rows,
            graph_dir,
            state_path,
            args.time_limit,
            args.workers,
            deadline,
            checkpoint,
            lambda current_summary, current_records: write_report(
                current_summary,
                current_records,
                partial=True,
            ),
        )
        summaries.append(summary)
        all_records.extend(records)
        # Include rows restored from durable state in the reported extension set.
        all_records = sorted(
            all_records,
            key=lambda row: (requested_parents.index(row["parent"]), parse_signature(row)),
        )
        write_report()

    final_report = make_report(
        configuration,
        baseline,
        baseline_path,
        summaries,
        all_records,
        time.monotonic() - run_started,
        args.deadline_seconds,
        time.monotonic() >= deadline,
    )
    final_report["environment"] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "started_unix_time": run_started_wall,
        "finished_unix_time": time.time(),
    }
    atomic_write_json(output_path, final_report)
    print(
        json.dumps(
            {
                "event": "run_complete",
                "complete": final_report["complete"],
                "runtime_deadline_hit": final_report["runtime_deadline_hit"],
                "totals": final_report["totals"],
                "counts": final_report["counts"],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
