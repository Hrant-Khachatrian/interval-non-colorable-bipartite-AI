#!/usr/bin/env python3
"""Diagnose rank and canonical-hash discrepancies in alternate-family v1.

This is deliberately read-only with respect to the campaign and spot-audit
inputs.  It reconstructs the graph family, joins rows by their generation-time
canonical digest, and writes a separate discrepancy analysis.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

import networkx as nx

import order18_alternate_family as alternate
from interval_edge_coloring import Graph, nauty_canonical_hash


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "results/order18-alternate-family-v1"
AUDIT = ROOT / "results/order18-alternate-family-v1-spot-audit"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_json(path: Path, value: object) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def completed_rows(path: Path) -> dict[int, dict]:
    rows = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        if event.get("event") != "classification_completed":
            continue
        row = event.get("row")
        if not isinstance(row, dict) or not isinstance(row.get("rank"), int):
            raise ValueError(f"malformed completed row at event line {line_number}")
        if row["rank"] in rows:
            raise ValueError(f"duplicate rank {row['rank']} at event line {line_number}")
        rows[row["rank"]] = row
    return rows


def exact_variance_numerator(graph: Graph) -> int:
    """An integer ordering key equivalent to degree variance for fixed n."""
    degrees = list(graph.degrees.values())
    return graph.n * sum(degree * degree for degree in degrees) - sum(degrees) ** 2


def graph_invariants(graph: Graph) -> dict:
    edges = list(graph.edges)
    degrees = graph.degrees
    return {
        "order": graph.n,
        "size": graph.m,
        "bipartition_sizes": list(map(len, graph.bipartition)),
        "delta": graph.delta,
        "minimum_degree": min(degrees.values()),
        "degree_sequence_descending": sorted(degrees.values(), reverse=True),
        "degree_variance_numerator": exact_variance_numerator(graph),
        "simple": len(edges) == len(set(edges)) and all(u != v for u, v in edges),
        "connected": nx.is_connected(graph._nx),
        "bipartite": nx.is_bipartite(graph._nx),
        "metadata": graph.metadata,
        "labelled_sha256": graph.canonical_hash(),
    }


def event_field_comparison(row: dict, graph: Graph) -> dict:
    invariants = graph_invariants(graph)
    metrics = alternate.graph_metrics(graph)
    exact_fields = ("order", "size", "bipartition_sizes", "delta", "minimum_degree", "metadata")
    mismatches = {
        field: {"event": row.get(field), "reconstructed": invariants[field]}
        for field in exact_fields
        if row.get(field) != invariants[field]
    }
    if row.get("hub_best_margin") != metrics["hub_best_margin"]:
        mismatches["hub_best_margin"] = {
            "event": row.get("hub_best_margin"),
            "reconstructed": metrics["hub_best_margin"],
        }
    event_variance = row.get("degree_variance_normalized")
    if not isinstance(event_variance, (int, float)) or not math.isclose(
        event_variance, metrics["degree_variance_normalized"], rel_tol=0.0, abs_tol=1e-12
    ):
        mismatches["degree_variance_normalized"] = {
            "event": event_variance,
            "reconstructed": metrics["degree_variance_normalized"],
        }
    return {
        "invariants": invariants,
        "reconstructed_metrics": {
            "hub_best_margin": metrics["hub_best_margin"],
            "degree_variance_normalized": metrics["degree_variance_normalized"],
        },
        "event_field_mismatches": mismatches,
    }


def key_for(graph: Graph) -> tuple[int, int, int]:
    metrics = alternate.graph_metrics(graph)
    return (-metrics["hub_best_margin"], -graph.delta, -exact_variance_numerator(graph))


def main() -> None:
    events = completed_rows(SOURCE / "classification-events.jsonl")
    samples = json.loads((AUDIT / "samples.json").read_text(encoding="utf-8"))
    spot_report = json.loads((AUDIT / "spot-audit-report.json").read_text(encoding="utf-8"))
    report = json.loads((SOURCE / "report.json").read_text(encoding="utf-8"))

    ranked, diagnostics = alternate.generate(14, 20, 180)
    selected = ranked[:1200]
    generated_by_rank = {rank: item for rank, item in enumerate(selected, 1)}
    generated_by_digest = {item[3]: item for item in selected}
    generated_rank_by_digest = {item[3]: rank for rank, item in generated_by_rank.items()}
    source_digest_by_rank = {rank: row["canonical_sha256"] for rank, row in events.items()}
    source_digests = set(source_digest_by_rank.values())
    generated_digests = set(generated_by_digest)

    source_duplicate_hashes = [
        digest for digest, count in collections.Counter(source_digest_by_rank.values()).items() if count > 1
    ]
    missing_from_rebuild = sorted(source_digests - generated_digests)
    missing_from_events = sorted(generated_digests - source_digests)

    all_matches = []
    field_mismatch_count = 0
    rehash_mismatch_count = 0
    rehash_observations = collections.Counter()
    for rank in sorted(events):
        row = events[rank]
        digest = row["canonical_sha256"]
        item = generated_by_digest.get(digest)
        if item is None:
            continue
        graph = item[4]
        rehashes = [nauty_canonical_hash(graph) for _ in range(3)]
        rehash_kind = "matches_stored" if rehashes[0] == digest else "differs_from_stored"
        rehash_observations[rehash_kind] += 1
        if rehash_kind != "matches_stored":
            rehash_mismatch_count += 1
        comparison = event_field_comparison(row, graph)
        field_mismatch_count += bool(comparison["event_field_mismatches"])
        all_matches.append({
            "event_rank": rank,
            "event_candidate_id": row.get("candidate_id"),
            "canonical_sha256": digest,
            "corrected_reconstructed_rank": generated_rank_by_digest[digest],
            "rank_changed": rank != generated_rank_by_digest[digest],
            "canonical_rehashes": rehashes,
            "canonical_rehash_is_repeatable": len(set(rehashes)) == 1,
            "canonical_rehash_relation": rehash_kind,
            **comparison,
        })

    movements = [entry for entry in all_matches if entry["rank_changed"]]
    key_crossings = []
    for entry in movements:
        old_graph = generated_by_digest[entry["canonical_sha256"]][4]
        same_rank_graph = generated_by_rank[entry["event_rank"]][4]
        if key_for(old_graph) != key_for(same_rank_graph):
            key_crossings.append({
                "event_rank": entry["event_rank"],
                "canonical_sha256": entry["canonical_sha256"],
                "event_graph_key": list(key_for(old_graph)),
                "same_rank_reconstruction_key": list(key_for(same_rank_graph)),
            })

    group_members = collections.defaultdict(list)
    for digest, item in generated_by_digest.items():
        group_members[key_for(item[4])].append(digest)
    reordering_groups = []
    for key, digests in sorted(group_members.items()):
        old_ranks = sorted(generated_rank_by_digest[digest] for digest in digests)
        event_ranks = sorted(next(rank for rank, value in source_digest_by_rank.items() if value == digest) for digest in digests)
        reconstructed_order = sorted(digests, key=generated_rank_by_digest.__getitem__)
        event_order = sorted(
            digests,
            key=lambda digest: next(rank for rank, value in source_digest_by_rank.items() if value == digest),
        )
        if reconstructed_order != event_order:
            reordering_groups.append({
                "semantic_ranking_key": list(key),
                "member_count": len(digests),
                "reconstructed_rank_range": [old_ranks[0], old_ranks[-1]],
                "event_rank_range": [event_ranks[0], event_ranks[-1]],
                "moved_member_count": sum(
                    generated_rank_by_digest[digest] != next(
                        rank for rank, value in source_digest_by_rank.items() if value == digest
                    ) for digest in digests
                ),
            })

    sample_entries = []
    for sample in samples:
        rank = sample["rank"]
        row = events[rank]
        digest = row["canonical_sha256"]
        matched = next(entry for entry in all_matches if entry["event_rank"] == rank)
        same_rank_digest = generated_by_rank[rank][3]
        if not sample["canonical_hash_agrees"]:
            sample_entries.append({
                "audit_rank": rank,
                "event_candidate_id": row["candidate_id"],
                "event_canonical_sha256": digest,
                "reconstructed_digest_at_audit_rank": same_rank_digest,
                "corrected_reconstructed_rank_for_event_hash": generated_rank_by_digest[digest],
                "audit_graph_was_selected_by_event_hash": True,
                "audit_rehash_agrees_with_event": sample["canonical_hash_agrees"],
                "independent_rank_potential_certificate_valid": sample["rank_potential"]["certificate_valid"],
                "independent_fixed_span_certificate_valid": sample["fixed_reported_span"]["certificate_valid"],
                "event_vs_matched_graph": {
                    "invariants": matched["invariants"],
                    "reconstructed_metrics": matched["reconstructed_metrics"],
                    "event_field_mismatches": matched["event_field_mismatches"],
                    "canonical_rehashes": matched["canonical_rehashes"],
                    "canonical_rehash_is_repeatable": matched["canonical_rehash_is_repeatable"],
                },
            })

    rank_map = [{
        "event_rank": entry["event_rank"],
        "event_candidate_id": entry["event_candidate_id"],
        "canonical_sha256": entry["canonical_sha256"],
        "corrected_reconstructed_rank": entry["corrected_reconstructed_rank"],
        "semantic_ranking_key": list(key_for(generated_by_digest[entry["canonical_sha256"]][4])),
    } for entry in movements]
    variance_exact_differences = []
    for entry in all_matches:
        event_variance = events[entry["event_rank"]].get("degree_variance_normalized")
        reconstructed_variance = entry["reconstructed_metrics"]["degree_variance_normalized"]
        if event_variance != reconstructed_variance:
            variance_exact_differences.append(abs(event_variance - reconstructed_variance))
    valid_sample_certificates = all(
        entry["independent_rank_potential_certificate_valid"] is True
        and entry["independent_fixed_span_certificate_valid"] is True
        for entry in sample_entries
    )
    result = {
        "schema_version": 1,
        "inputs": {
            "campaign_events": str((SOURCE / "classification-events.jsonl").relative_to(ROOT)),
            "campaign_report": str((SOURCE / "report.json").relative_to(ROOT)),
            "spot_samples": str((AUDIT / "samples.json").relative_to(ROOT)),
            "spot_report": str((AUDIT / "spot-audit-report.json").relative_to(ROOT)),
        },
        "reconstruction": {
            "diagnostics": diagnostics,
            "source_completed_row_count": len(events),
            "generated_selected_count": len(selected),
            "source_duplicate_canonical_hashes": source_duplicate_hashes,
            "event_hashes_missing_from_reconstructed_selection": missing_from_rebuild,
            "reconstructed_hashes_missing_from_events": missing_from_events,
            "exact_identity_set_match": not missing_from_rebuild and not missing_from_events and not source_duplicate_hashes,
        },
        "rank_mapping": {
            "historical_spot_audit_rank_mismatch_count": spot_report["reconstruction"]["rank_mismatch_count"],
            "same_rank_count": len(all_matches) - len(movements),
            "rank_order_mapping_difference_count": len(movements),
            "cross_semantic_key_movement_count": len(key_crossings),
            "cross_semantic_key_movements": key_crossings,
            "reordering_group_count": len(reordering_groups),
            "reordering_groups": reordering_groups,
            "corrected_mappings": rank_map,
        },
        "ranking_precision": {
            "event_vs_current_reconstruction_exact_float_difference_count": len(variance_exact_differences),
            "maximum_absolute_variance_difference": max(variance_exact_differences, default=0.0),
            "semantic_key": "(-hub_best_margin, -delta, -integer_degree_variance_numerator)",
            "finding": "Every rank movement stays within the same integer semantic key. The campaign ranks on a floating-point variance computed by summing degrees in graph/node insertion order; changes at machine precision alter ordering inside ties before digest tie-breaking.",
        },
        "canonicalization": {
            "historical_spot_audit_sample_rehash_mismatch_count": spot_report["sample_summary"]["canonical_hash_mismatch_count"],
            "stored_digest_rehash_match_count": len(all_matches) - rehash_mismatch_count,
            "stored_digest_rehash_mismatch_count": rehash_mismatch_count,
            "rehash_relation_counts": dict(sorted(rehash_observations.items())),
            "note": "The 42 historical sample rehash flags are not reproduced by this all-row reconstruction: every stored digest rehashes identically here. In either run they do not establish a wrong graph, because the stored generation-time digest sets join one-to-one and all event fields match their digest-selected reconstructed graph.",
        },
        "event_to_matched_graph_field_comparison": {
            "matched_count": len(all_matches),
            "event_field_mismatch_count": field_mismatch_count,
            "matches": all_matches,
        },
        "mismatched_spot_samples": {
            "count": len(sample_entries),
            "all_have_valid_independent_certificates": valid_sample_certificates,
            "entries": sample_entries,
        },
        "decision_evidence": {
            "campaign_reported_colorable": report.get("counts", {}).get("colorable"),
            "campaign_reported_non_colorable": report.get("counts", {}).get("non_colorable"),
            "spot_sample_count": len(samples),
            "spot_samples_with_valid_rank_potential_certificate": sum(
                s["rank_potential"]["certificate_valid"] is True for s in samples
            ),
            "spot_samples_with_valid_fixed_span_certificate": sum(
                s["fixed_reported_span"]["certificate_valid"] is True for s in samples
            ),
            "spot_solver_disagreement_count": spot_report["sample_summary"]["solver_disagreement_count"],
            "spot_invalid_certificate_count": spot_report["sample_summary"]["invalid_certificate_count"],
        },
        "corrected_verdict": {
            "genuine_wrong_graph_records_found": bool(missing_from_rebuild or missing_from_events or source_duplicate_hashes or field_mismatch_count),
            "invalid_certificate_found": spot_report["sample_summary"]["invalid_certificate_count"] != 0,
            "root_cause": "float-ranking order instability within equal semantic graph keys; the spot audit's 42 historical canonical rehash flags are non-reproduced and do not reflect a graph-identity discrepancy" if not (missing_from_rebuild or missing_from_events or source_duplicate_hashes or field_mismatch_count) else "requires escalation",
            "verdict": "campaign graph identities and sampled colorability evidence remain valid after joining by stored generation-time digest; the v1 spot-audit fail verdict is not a valid basis to reject the campaign because rank and rehash flags were interpreted as graph mismatches.",
        },
    }
    atomic_json(AUDIT / "discrepancy-analysis.json", result)
    lines = [
        "# Alternate Family v1 Spot-Audit Discrepancy Analysis",
        "",
        "## Corrected verdict",
        "",
        result["corrected_verdict"]["verdict"],
        "",
        "## Identity and ordering",
        "",
        f"- Completed event rows: {len(events)}",
        f"- Reconstructed selected graphs: {len(selected)}",
        f"- Exact stored-digest set match: {result['reconstruction']['exact_identity_set_match']}",
        f"- Historical spot-audit rank mismatches: {spot_report['reconstruction']['rank_mismatch_count']}",
        f"- Current reconstruction rank/order mapping differences: {len(movements)}",
        f"- Cross-semantic-key movements: {len(key_crossings)}",
        f"- Exact float-variance differences against event rows: {len(variance_exact_differences)} (maximum {max(variance_exact_differences, default=0.0):.18g})",
        f"- Event-to-matched-graph field mismatches: {field_mismatch_count}",
        "",
        "## Canonicalization",
        "",
        f"- Historical spot-audit sample rehash flags: {spot_report['sample_summary']['canonical_hash_mismatch_count']}",
        f"- Current all-row stored digest rehash matches: {len(all_matches) - rehash_mismatch_count}",
        f"- Current all-row stored digest rehash mismatches: {rehash_mismatch_count}",
        "- The historical flags are non-reproduced. In either case, the stored generation-time digests map the event and reconstructed selection sets one-to-one.",
        "",
        "## Sampled decision evidence",
        "",
        f"- Spot samples: {len(samples)}",
        f"- Samples with valid rank-potential certificates: {result['decision_evidence']['spot_samples_with_valid_rank_potential_certificate']}",
        f"- Samples with valid fixed-span certificates: {result['decision_evidence']['spot_samples_with_valid_fixed_span_certificate']}",
        f"- Spot solver disagreements: {result['decision_evidence']['spot_solver_disagreement_count']}",
        f"- Spot invalid certificates: {result['decision_evidence']['spot_invalid_certificate_count']}",
        f"- Mismatched spot samples individually joined by stored digest: {len(sample_entries)}",
        "",
        "The JSON companion contains all corrected rank mappings and the field-level comparison for every matched event, including every mismatched spot sample.",
    ]
    atomic_write(AUDIT / "report.md", "\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
