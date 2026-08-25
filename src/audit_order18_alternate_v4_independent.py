#!/usr/bin/env python3
"""Independent integrity and replay audit for the order-18 alternate v4 residual.

This deliberately writes only a separate compact audit report.  Cross-campaign
identity is the bipartition-coloured Nauty hash, never the phase-local rank.
"""

from __future__ import annotations

import collections
import json
import math
import os
import tempfile
import time
from pathlib import Path

import networkx as nx

from build_order18_alternate_family_ledger import PARENTS, reconstruct
from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
)
from order18_alternate_family import generate, prior_queue_hashes


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "results/order18-alternate-family-v4-independent-audit"
V4_EVENTS = ROOT / "results/order18-alternate-family-v4-residual/classification-events.jsonl"
PRIOR_EVENTS = (
    ROOT / "results/order18-alternate-family-v1/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/v1-residual/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/expanded-step1/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/expanded-step2/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v3/v2-residual/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v3/expanded-step3/classification-events.jsonl",
)
EXPECTED_RESIDUAL = 1361
BOUNDS = {"restore_limit": 80, "witness_switch_limit": 180, "near_switch_limit": 1620}


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def completed_rows(path: Path) -> list[tuple[int, dict]]:
    rows: list[tuple[int, dict]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        if event.get("event") == "classification_completed":
            row = event.get("row")
            if not isinstance(row, dict) or not isinstance(row.get("canonical_sha256"), str):
                raise ValueError(f"invalid completed event at {path}:{line_number}")
            rows.append((line_number, row))
    return rows


def hashes(rows: list[tuple[int, dict]]) -> set[str]:
    return {row["canonical_sha256"] for _, row in rows}


def duplicate_hashes(rows: list[tuple[int, dict]]) -> dict[str, list[int]]:
    index: dict[str, list[int]] = collections.defaultdict(list)
    for line, row in rows:
        index[row["canonical_sha256"]].append(line)
    return {digest: lines for digest, lines in index.items() if len(lines) > 1}


def structural_checks(graph: Graph) -> dict[str, bool]:
    left, right = map(set, graph.bipartition)
    edges = list(graph.edges)
    return {
        "order_18": graph.n == 18,
        "simple": len(edges) == len(set(edges)) and all(u != v for u, v in edges),
        "connected": nx.is_connected(graph._nx),
        "bipartite": (
            nx.is_bipartite(graph._nx)
            and left | right == graph.vertex_set
            and not left & right
            and all((u in left) != (v in left) for u, v in edges)
        ),
        "minimum_degree_at_least_2": min(graph.degrees.values(), default=0) >= 2,
    }


def stored_field_mismatches(row: dict, graph: Graph) -> dict[str, dict]:
    observed = {
        "order": graph.n,
        "size": graph.m,
        "bipartition_sizes": list(map(len, graph.bipartition)),
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
    }
    return {
        name: {"recorded": row.get(name), "reconstructed": value}
        for name, value in observed.items()
        if row.get(name) != value
    }


def legal_span(graph: Graph, span: object) -> bool:
    return isinstance(span, int) and graph.delta <= span < graph.n


def all_span_check(graph: Graph, seconds: float, workers: int) -> dict:
    """Check an apparent negative over every legal span before drawing a conclusion."""
    span_results: dict[str, dict] = {}
    for span in range(graph.delta, graph.n):
        started = time.perf_counter()
        status, coloring = fixed_span_sat_solve(graph, span, seconds, workers=workers)
        valid, reason = verify_coloring(graph, coloring) if coloring is not None else (False, "no certificate")
        span_results[str(span)] = {
            "solver_status": status,
            "certificate_valid": valid,
            "certificate_reason": reason,
            "elapsed_seconds": time.perf_counter() - started,
        }
    feasible = [span for span, item in span_results.items()
                if item["solver_status"] in {"OPTIMAL", "FEASIBLE"} and item["certificate_valid"]]
    unknown = [span for span, item in span_results.items() if item["solver_status"] == "UNKNOWN"]
    malformed = [span for span, item in span_results.items()
                 if item["solver_status"] not in {"OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"}
                 or (item["solver_status"] in {"OPTIMAL", "FEASIBLE"} and not item["certificate_valid"])]
    conclusion = (
        "colorable_disagrees_with_apparent_negative" if feasible else
        "unresolved_timeout" if unknown else
        "solver_or_certificate_mismatch" if malformed else
        "confirmed_noncolorable"
    )
    return {"conclusion": conclusion, "spans": span_results}


def sample_hashes(ordered: list[str], count: int) -> list[str]:
    """Evenly cover the whole canonical-hash order, retaining endpoints."""
    if len(ordered) < count:
        raise ValueError("sample exceeds residual")
    return [ordered[math.floor(i * (len(ordered) - 1) / (count - 1))] for i in range(count)]


def replay_one(digest: str, row: dict, graph: Graph, seconds: float, workers: int) -> dict:
    replay = rank_potential_solve(graph, seconds, workers=workers)
    valid, reason = verify_coloring(graph, replay.coloring) if replay.coloring is not None else (False, "no certificate")
    recorded_span = row.get("primary_span")
    apparent_negative = replay.status == "non-colorable"
    escalation = all_span_check(graph, seconds, workers) if apparent_negative else None
    span_validation = None
    if replay.status == "colorable" and valid and replay.span == recorded_span:
        span_validation = {"method": "replay-certificate", "solver_status": replay.solver_status,
                           "certificate_valid": True, "certificate_reason": reason}
    else:
        fixed_status, fixed_coloring = fixed_span_sat_solve(graph, recorded_span, seconds, workers=workers)
        fixed_valid, fixed_reason = verify_coloring(graph, fixed_coloring) if fixed_coloring is not None else (False, "no certificate")
        span_validation = {"method": "fixed-span-cpsat", "solver_status": fixed_status,
                           "certificate_valid": fixed_valid, "certificate_reason": fixed_reason}
    span_valid = span_validation["solver_status"] in {"OPTIMAL", "FEASIBLE"} and span_validation["certificate_valid"]
    if replay.status == "timeout" or span_validation["solver_status"] == "UNKNOWN" or (escalation and escalation["conclusion"] == "unresolved_timeout"):
        outcome = "timeout"
    elif replay.status == "colorable" and valid and span_valid and not apparent_negative:
        outcome = "valid"
    else:
        outcome = "mismatch"
    return {
        "canonical_sha256": digest,
        "source_rank": row.get("rank"),
        "source_lane": row.get("metadata", {}).get("lane"),
        "reported_span": recorded_span,
        "replay_status": replay.status,
        "replay_solver_status": replay.solver_status,
        "replay_span": replay.span,
        "replay_certificate_valid": valid,
        "replay_certificate_reason": reason,
        "replay_span_agrees_with_reported": replay.span == recorded_span,
        "reported_span_validation": span_validation,
        "apparent_negative_all_span_check": escalation,
        "outcome": outcome,
    }


def main() -> None:
    started = time.monotonic()
    workers = 3
    seconds = 5.0
    sample_count = 144
    prior = {str(path.relative_to(ROOT)): completed_rows(path) for path in PRIOR_EVENTS}
    v4 = completed_rows(V4_EVENTS)
    prior_hashes = set().union(*(hashes(rows) for rows in prior.values()))
    v4_hashes = hashes(v4)
    all_covered = prior_hashes | v4_hashes
    first_queue = prior_queue_hashes()
    ranked, generation = generate(**BOUNDS)
    after_first_queue = {entry[3] for entry in ranked}
    expected_residual = after_first_queue - prior_hashes
    set_checks = {
        "first_queue_hash_count": len(first_queue),
        "generated_after_first_queue_hash_count": len(after_first_queue),
        "prior_v1_to_v3_hash_count": len(prior_hashes),
        "v4_hash_count": len(v4_hashes),
        "v1_to_v4_covered_hash_count": len(all_covered),
        "expected_residual_hash_count": len(expected_residual),
        "expected_residual_count_is_1361": len(expected_residual) == EXPECTED_RESIDUAL,
        "v4_equals_reconstructed_residual": v4_hashes == expected_residual,
        "missing_v4_hash_count": len(expected_residual - v4_hashes),
        "extra_v4_hash_count": len(v4_hashes - expected_residual),
        "first_queue_overlap_prior_count": len(first_queue & prior_hashes),
        "first_queue_overlap_v4_count": len(first_queue & v4_hashes),
        "residual_overlap_prior_count": len(expected_residual & prior_hashes),
        "remaining_after_v1_to_v4_coverage_count": len(after_first_queue - all_covered),
        "all_generated_after_first_queue_covered_by_v1_to_v4": after_first_queue <= all_covered,
        "generation_diagnostics": generation,
        "prior_phase_hash_counts": {name: len(hashes(rows)) for name, rows in prior.items()},
    }
    duplicate_sets = {
        "v4_duplicate_hash_count": len(duplicate_hashes(v4)),
        "prior_phase_duplicate_hash_count": sum(len(duplicate_hashes(rows)) for rows in prior.values()),
        "v1_to_v4_cross_phase_duplicate_hash_count": sum(
            1 for digest in all_covered
            if sum(digest in hashes(rows) for rows in prior.values()) + (digest in v4_hashes) > 1
        ),
    }

    parents = {name: Graph.from_json(json.loads(path.read_text(encoding="utf-8"))) for name, path in PARENTS.items()}
    reconstructed: dict[str, Graph] = {}
    integrity_failures: list[dict] = []
    status_counts: collections.Counter[str] = collections.Counter()
    for line, row in v4:
        digest = row["canonical_sha256"]
        status_counts[str(row.get("status"))] += 1
        try:
            graph = reconstruct(row, parents)
            checks = structural_checks(graph)
            fields = stored_field_mismatches(row, graph)
            status_ok = (
                row.get("status") == "colorable"
                and row.get("primary_status") == "colorable"
                and row.get("primary_solver_status") in {"OPTIMAL", "FEASIBLE"}
                and legal_span(graph, row.get("primary_span"))
            )
            observed_hash = nauty_canonical_hash(graph)
            errors = []
            if observed_hash != digest:
                errors.append("canonical_hash")
            if not all(checks.values()):
                errors.append("structural")
            if fields:
                errors.append("stored_fields")
            if not status_ok:
                errors.append("decision_or_status")
            if errors:
                integrity_failures.append({"event_line": line, "canonical_sha256": digest, "errors": errors,
                                           "observed_hash": observed_hash, "structural_checks": checks,
                                           "stored_field_mismatches": fields,
                                           "status": row.get("status"), "primary_status": row.get("primary_status"),
                                           "primary_solver_status": row.get("primary_solver_status"),
                                           "primary_span": row.get("primary_span")})
            else:
                reconstructed[digest] = graph
        except Exception as exc:
            integrity_failures.append({"event_line": line, "canonical_sha256": digest,
                                       "errors": ["reconstruction"], "detail": f"{type(exc).__name__}: {exc}"})

    sample = sample_hashes(sorted(expected_residual), sample_count)
    by_hash = {row["canonical_sha256"]: row for _, row in v4}
    replay_events = []
    if not integrity_failures and set(reconstructed) == expected_residual:
        for digest in sample:
            replay_events.append(replay_one(digest, by_hash[digest], reconstructed[digest], seconds, workers))
    replay_counts = collections.Counter(event["outcome"] for event in replay_events)
    span_valid = sum(
        event["reported_span_validation"]["solver_status"] in {"OPTIMAL", "FEASIBLE"}
        and event["reported_span_validation"]["certificate_valid"]
        for event in replay_events
    )
    apparent_negative_count = sum(event["apparent_negative_all_span_check"] is not None for event in replay_events)
    certification_ok = (
        set_checks["expected_residual_count_is_1361"]
        and set_checks["v4_equals_reconstructed_residual"]
        and set_checks["first_queue_overlap_prior_count"] == 0
        and set_checks["first_queue_overlap_v4_count"] == 0
        and set_checks["remaining_after_v1_to_v4_coverage_count"] == 0
        and all(value == 0 for value in duplicate_sets.values())
        and not integrity_failures
        and len(replay_events) == sample_count
        and replay_counts["valid"] == sample_count
        and span_valid == sample_count
    )
    report = {
        "schema_version": 1,
        "purpose": "Independent audit of the exhaustive expanded-bound order-18 alternate v4 residual.",
        "source": {"v4_events": str(V4_EVENTS.relative_to(ROOT)), "prior_events": list(prior)},
        "configuration": {"bounds": BOUNDS, "replay_workers": workers, "replay_time_limit_seconds": seconds,
                          "sample_selection": "144 evenly spaced canonical hashes over the reconstructed residual",
                          "negative_policy": "every apparent replay negative is checked at every legal span"},
        "set_checks": set_checks,
        "duplicate_checks": duplicate_sets,
        "integrity": {"classified_rows": len(v4), "reconstructed_rows": len(reconstructed),
                      "status_counts": dict(sorted(status_counts.items())), "failure_count": len(integrity_failures),
                      "failures": integrity_failures[:20]},
        "replay": {"sample_count": sample_count, "replayed": len(replay_events), "valid": replay_counts["valid"],
                   "mismatch": replay_counts["mismatch"], "span_valid": span_valid, "timeout": replay_counts["timeout"],
                   "apparent_negative_count": apparent_negative_count, "sample_hashes": sample},
        "verdict": "certified" if certification_ok else "not_certified",
        "elapsed_seconds": time.monotonic() - started,
    }
    atomic_json(OUTPUT / "replay-samples.json", {"schema_version": 1, "events": replay_events})
    atomic_json(OUTPUT / "report.json", report)
    atomic_json(OUTPUT / "status.json", report)
    print(json.dumps({"verdict": report["verdict"], "reconstructed": len(reconstructed), **report["replay"]}, sort_keys=True))


if __name__ == "__main__":
    main()
