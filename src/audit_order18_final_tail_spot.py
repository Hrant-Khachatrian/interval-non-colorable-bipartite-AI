#!/usr/bin/env python3
"""Independent deterministic spot audit of the completed order-18 final tail.

This deliberately does not amend the classified result.  It reconstructs the
full production queue, reconciles all durable records, then replays a fixed
36-rank sample with two CP-SAT workers (the production run used one).
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import random
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import networkx as nx

from interval_edge_coloring import (
    all_spans_solve,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
)
from order18_targeted_search import generate_candidates, graph_metrics, valid_candidate


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "results" / "order18-targeted-final-tail"
OUTPUT = ROOT / "results" / "order18-final-tail-spot-audit"
TAIL_FIRST, TAIL_LAST = 10501, 12987
SAMPLE_PER_THIRD = 12
REPLAY_WORKERS = 2
RANK_TIME_LIMIT = 15.0
FIXED_TIME_LIMIT = 15.0
PRIOR = {
    "v3": ROOT / "results" / "order18-targeted-v3.json",
    "v4": ROOT / "results" / "order18-targeted-v4" / "classification-events.jsonl",
    "v5": ROOT / "results" / "order18-targeted-v5" / "classification-events.jsonl",
    "v6": ROOT / "results" / "order18-targeted-v6" / "classification-events.jsonl",
    "v7": ROOT / "results" / "order18-targeted-v7" / "classification-events.jsonl",
    "v8": ROOT / "results" / "order18-targeted-v8" / "classification-events.jsonl",
}


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def event_rows(path: Path) -> tuple[list[dict], list[str], collections.Counter]:
    rows: list[dict] = []
    errors: list[str] = []
    types: collections.Counter = collections.Counter()
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"invalid_json:{number}:{exc.msg}")
                continue
            if not isinstance(event, dict):
                errors.append(f"non_object_event:{number}")
                continue
            types[str(event.get("event"))] += 1
            if event.get("event") == "classification_completed":
                if isinstance(event.get("row"), dict):
                    rows.append(event["row"])
                else:
                    errors.append(f"invalid_completion_row:{number}")
    return rows, errors, types


def rank_for(row: dict) -> int:
    rank = row.get("rank")
    if isinstance(rank, int):
        return rank
    candidate_id = row.get("candidate_id", "")
    if isinstance(candidate_id, str) and candidate_id.startswith("O18-R") and candidate_id[5:].isdigit():
        return int(candidate_id[5:])
    if isinstance(candidate_id, str) and candidate_id.startswith("O18-") and candidate_id[4:].isdigit():
        return int(candidate_id[4:]) + 1
    raise ValueError(f"unusable_rank:{candidate_id!r}")


def production_queue() -> tuple[list[tuple], dict]:
    args = SimpleNamespace(
        lanes="all",
        candidate_cap=12987,
        rank_start=0,
        max_additions=1,
        max_deleted_degree=3,
        max_rewires=750,
        extension_limit=18,
    )
    selected, raw_lanes, generated_lanes, selected_lanes, diagnostics = generate_candidates(args)
    diagnostics.update({
        "generated_raw_by_lane": dict(sorted(raw_lanes.items())),
        "unique_ranked_by_lane": dict(sorted(generated_lanes.items())),
        "selected_by_lane": dict(sorted(selected_lanes.items())),
    })
    if len(selected) != 12987 or diagnostics.get("unique_after_nauty") != 12987:
        raise RuntimeError(f"unexpected reconstructed queue: {diagnostics}")
    return selected, diagnostics


def sample_ranks() -> list[int]:
    total = TAIL_LAST - TAIL_FIRST + 1
    third = total // 3
    bands = [
        ("beginning", TAIL_FIRST, TAIL_FIRST + third - 1),
        ("middle", TAIL_FIRST + third, TAIL_FIRST + 2 * third - 1),
        ("end", TAIL_FIRST + 2 * third, TAIL_LAST),
    ]
    ranks: list[int] = []
    for label, first, last in bands:
        chosen = {first, (first + last) // 2, last}
        rng = random.Random(f"order18-final-tail-spot-audit-20260825-{label}")
        while len(chosen) < SAMPLE_PER_THIRD:
            chosen.add(rng.randint(first, last))
        ranks.extend(sorted(chosen))
    return ranks


def rows_by_rank(rows: list[dict]) -> tuple[dict[int, dict], dict]:
    indexed: dict[int, dict] = {}
    hashes: set[str] = set()
    duplicate_ranks: list[int] = []
    duplicate_hashes: list[str] = []
    bad_rows: list[str] = []
    for row in rows:
        try:
            rank = rank_for(row)
        except ValueError as exc:
            bad_rows.append(str(exc))
            continue
        digest = row.get("canonical_sha256")
        if rank in indexed:
            duplicate_ranks.append(rank)
        if digest in hashes:
            duplicate_hashes.append(str(digest))
        indexed[rank] = row
        hashes.add(digest)
    return indexed, {
        "duplicate_ranks": sorted(set(duplicate_ranks)),
        "duplicate_hashes": sorted(set(duplicate_hashes)),
        "bad_rows": bad_rows,
        "hashes": hashes,
    }


def prior_rows(label: str, path: Path) -> list[dict]:
    if label == "v3":
        return json.loads(path.read_text(encoding="utf-8"))["rows"]
    return event_rows(path)[0]


def float_equal(left: object, right: object) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
    return left == right


def full_reconciliation(queue: list[tuple], rows: list[dict], parse_errors: list[str]) -> dict:
    indexed, inventory = rows_by_rank(rows)
    expected_ranks = set(range(TAIL_FIRST, TAIL_LAST + 1))
    expected_hashes = {rank: queue[rank - 1][4] for rank in expected_ranks}
    actual_ranks = set(indexed)
    hash_mismatches = [
        rank for rank in sorted(actual_ranks & expected_ranks)
        if indexed[rank].get("canonical_sha256") != expected_hashes[rank]
    ]
    statuses = collections.Counter(str(row.get("status")) for row in rows)
    primary = collections.Counter(str(row.get("primary_status")) for row in rows)
    primary_solver = collections.Counter(str(row.get("primary_solver_status")) for row in rows)
    infeasible_solver_rows = [
        rank for rank, row in indexed.items()
        if str(row.get("primary_solver_status")) == "INFEASIBLE"
    ]
    negative_rows = [
        rank for rank, row in indexed.items()
        if row.get("primary_status") == "non-colorable" or row.get("status") in {"confirmed_non_colorable", "confirmation_timeout"} or rank in infeasible_solver_rows
    ]

    all_prior_ranks: set[int] = set()
    all_prior_hashes: set[str] = set()
    prior_summary: dict[str, dict] = {}
    for label, path in PRIOR.items():
        prior, prior_errors, _types = event_rows(path) if label != "v3" else (prior_rows(label, path), [], collections.Counter())
        prior_indexed, prior_inventory = rows_by_rank(prior)
        all_prior_ranks.update(prior_indexed)
        all_prior_hashes.update(prior_inventory["hashes"])
        prior_summary[label] = {
            "records": len(prior),
            "unique_ranks": len(prior_indexed),
            "unique_hashes": len(prior_inventory["hashes"]),
            "parse_errors": prior_errors,
            "duplicate_ranks": prior_inventory["duplicate_ranks"],
            "duplicate_hashes": prior_inventory["duplicate_hashes"],
            "rank_overlap_with_tail": sorted(actual_ranks & set(prior_indexed)),
            "hash_overlap_with_tail": sorted(inventory["hashes"] & prior_inventory["hashes"]),
        }
    expected_prior = set(range(1, TAIL_FIRST))
    return {
        "event_records": len(rows),
        "unique_rank_records": len(actual_ranks),
        "unique_hash_records": len(inventory["hashes"]),
        "event_parse_errors": parse_errors,
        "duplicate_rank_records": inventory["duplicate_ranks"],
        "duplicate_canonical_hashes": inventory["duplicate_hashes"],
        "bad_event_rows": inventory["bad_rows"],
        "missing_ranks": sorted(expected_ranks - actual_ranks),
        "unexpected_ranks": sorted(actual_ranks - expected_ranks),
        "rank_hash_mismatches": hash_mismatches,
        "status_counts": dict(sorted(statuses.items())),
        "primary_status_counts": dict(sorted(primary.items())),
        "primary_solver_status_counts": dict(sorted(primary_solver.items())),
        "infeasible_primary_solver_ranks": sorted(infeasible_solver_rows),
        "reported_negative_or_unresolved_ranks": sorted(negative_rows),
        "prior_slices": prior_summary,
        "prior_union_missing_ranks": sorted(expected_prior - all_prior_ranks),
        "prior_union_unexpected_ranks": sorted(all_prior_ranks - expected_prior),
        "prior_rank_overlap": sorted(actual_ranks & all_prior_ranks),
        "prior_canonical_hash_overlap_count": len(inventory["hashes"] & all_prior_hashes),
    }


def compare_artifacts(recon: dict, rows: list[dict]) -> dict:
    report = json.loads((SOURCE / "report.json").read_text(encoding="utf-8"))
    status = json.loads((SOURCE / "status.json").read_text(encoding="utf-8"))
    stated_recon = json.loads((SOURCE / "reconciliation.json").read_text(encoding="utf-8"))
    event_indexed, _inventory = rows_by_rank(rows)
    report_rows = report.get("rows", [])
    report_indexed, report_inventory = rows_by_rank(report_rows)
    report_row_mismatches = sorted(set(event_indexed) ^ set(report_indexed))
    for rank in sorted(set(event_indexed) & set(report_indexed)):
        if event_indexed[rank] != report_indexed[rank]:
            report_row_mismatches.append(rank)
    expected_counts = {
        "selected_for_classification": TAIL_LAST - TAIL_FIRST + 1,
        "classified": len(rows),
        "completion_count": len(rows),
        "colorable": recon["status_counts"].get("colorable", 0),
        "primary_noncolorable": recon["primary_status_counts"].get("non-colorable", 0),
        "non_colorable_certified": recon["status_counts"].get("confirmed_non_colorable", 0),
        "primary_timeout": recon["primary_status_counts"].get("timeout", 0),
        "confirmation_timeout": recon["status_counts"].get("confirmation_timeout", 0),
    }
    count_mismatches: dict[str, dict] = {}
    for name, expected in expected_counts.items():
        values = {"events": expected, "report": report.get("counts", {}).get(name), "status": status.get("counts", {}).get(name)}
        if any(value != expected for value in values.values()):
            count_mismatches[name] = values
    scalar_mismatches: dict[str, dict] = {}
    for name, expected in (("classified", len(rows)), ("colorable", expected_counts["colorable"]), ("non_colorable", expected_counts["non_colorable_certified"]), ("timeout", expected_counts["primary_timeout"] + expected_counts["confirmation_timeout"])):
        actual = report.get(name)
        if actual != expected:
            scalar_mismatches[name] = {"events": expected, "report": actual}
    coverage_keys = [
        "event_classification_records", "unique_rank_records", "duplicate_rank_records",
        "duplicate_canonical_hashes", "missing_ranks", "unexpected_ranks", "rank_hash_mismatches",
        "prior_rank_overlap", "prior_canonical_hash_overlap_count",
    ]
    source_coverage = {
        "event_classification_records": recon["event_records"],
        "unique_rank_records": recon["unique_rank_records"],
        "duplicate_rank_records": recon["duplicate_rank_records"],
        "duplicate_canonical_hashes": recon["duplicate_canonical_hashes"],
        "missing_ranks": recon["missing_ranks"],
        "unexpected_ranks": recon["unexpected_ranks"],
        "rank_hash_mismatches": recon["rank_hash_mismatches"],
        "prior_rank_overlap": recon["prior_rank_overlap"],
        "prior_canonical_hash_overlap_count": recon["prior_canonical_hash_overlap_count"],
    }
    coverage_mismatches = {
        key: {"observed": source_coverage[key], "reconciliation": stated_recon.get(key), "report": report.get("reconciliation", {}).get(key), "status": status.get("reconciliation", {}).get(key)}
        for key in coverage_keys
        if any(value != source_coverage[key] for value in (stated_recon.get(key), report.get("reconciliation", {}).get(key), status.get("reconciliation", {}).get(key)))
    }
    prior_coverage_mismatches: dict[str, dict] = {}
    for label, observed in recon["prior_slices"].items():
        expected = {
            "rank_records": observed["records"],
            "unique_rank_records": observed["unique_ranks"],
            "unique_hash_records": observed["unique_hashes"],
            "rank_overlap_with_final_tail": observed["rank_overlap_with_tail"],
            "canonical_hash_overlap_with_final_tail": observed["hash_overlap_with_tail"],
        }
        sources = {
            "file": stated_recon.get("prior_slices", {}).get(label, {}),
            "report": report.get("reconciliation", {}).get("prior_slices", {}).get(label, {}),
            "status": status.get("reconciliation", {}).get("prior_slices", {}).get(label, {}),
        }
        mismatched = {
            key: {"observed": value, **{source: source_values.get(key) for source, source_values in sources.items()}}
            for key, value in expected.items()
            if any(source_values.get(key) != value for source_values in sources.values())
        }
        if mismatched:
            prior_coverage_mismatches[label] = mismatched
    return {
        "source_completion": {"report": report.get("completion"), "status": status.get("completion")},
        "report_row_count": len(report_rows),
        "report_duplicate_rank_records": report_inventory["duplicate_ranks"],
        "event_report_row_mismatches": sorted(set(report_row_mismatches)),
        "count_mismatches": count_mismatches,
        "scalar_count_mismatches": scalar_mismatches,
        "reconciliation_coverage_mismatches": coverage_mismatches,
        "prior_slice_coverage_mismatches": prior_coverage_mismatches,
        "reported_reconciliation_status": {"file": stated_recon.get("reconciliation_status"), "report": report.get("reconciliation", {}).get("reconciliation_status"), "status": status.get("reconciliation", {}).get("reconciliation_status")},
    }


def structural_failures(rank: int, graph, row: dict) -> list[str]:
    left, right = graph.bipartition
    actual = {
        "candidate_id": f"O18-R{rank:05d}",
        "canonical_sha256": nauty_canonical_hash(graph),
        "order": graph.n,
        "size": graph.m,
        "bipartition_sizes": list(map(len, graph.bipartition)),
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "degrees": graph.degrees,
        "metadata": graph.metadata,
        **graph_metrics(graph),
        "connected": nx.is_connected(graph._nx),
        "bipartite": nx.is_bipartite(graph._nx),
        "bipartition_valid": set(left).isdisjoint(right) and set(left) | set(right) == set(graph.vertices) and all((u in left) != (v in left) for u, v in graph.edges),
        "minimum_degree_filter": min(graph.degrees.values()) >= 2,
        "valid_candidate": valid_candidate(graph),
    }
    expected = {key: row.get(key) for key in (
        "candidate_id", "canonical_sha256", "order", "size", "bipartition_sizes", "delta",
        "minimum_degree", "degrees", "metadata", "hub_best_margin", "degree_variance_normalized", "weighted_hubs_best",
    )}
    failures = [key for key, value in expected.items() if not float_equal(value, actual[key])]
    failures.extend(key for key in ("connected", "bipartite", "bipartition_valid", "minimum_degree_filter", "valid_candidate") if not actual[key])
    return failures


def valid_replayed_coloring(graph, coloring: dict | None, span: int | None) -> tuple[bool, str | None]:
    if coloring is None:
        return False, "no_coloring"
    ok, reason = verify_coloring(graph, coloring)
    if not ok:
        return False, reason
    max_color = max(coloring.values(), default=0)
    if span is not None and max_color != span:
        return False, f"span_mismatch:{max_color}!={span}"
    return True, None


def audit_sample(rank: int, graph, row: dict) -> dict:
    started = time.perf_counter()
    failures = structural_failures(rank, graph, row)
    replay = rank_potential_solve(graph, RANK_TIME_LIMIT, REPLAY_WORKERS)
    primary_ok, primary_reason = valid_replayed_coloring(graph, replay.coloring, replay.span)
    reported_span = row.get("primary_span")
    fixed_started = time.perf_counter()
    fixed_status, fixed_coloring = fixed_span_sat_solve(graph, reported_span, FIXED_TIME_LIMIT, REPLAY_WORKERS)
    fixed_elapsed = time.perf_counter() - fixed_started
    fixed_ok, fixed_reason = valid_replayed_coloring(graph, fixed_coloring, reported_span)

    all_span_check = None
    escalation = None
    negative_seen = replay.status == "non-colorable" or fixed_status == "INFEASIBLE"
    if negative_seen:
        # No negative claim is certified from this audit without this complete replay.
        all_span_check = all_spans_solve(graph, FIXED_TIME_LIMIT, REPLAY_WORKERS, stop_on_timeout=False)
        escalation = "replay_negative_observed_all_legal_spans_checked"
    status_ok = replay.status == "colorable" and fixed_status in {"OPTIMAL", "FEASIBLE"}
    outcome = "pass" if not failures and status_ok and primary_ok and fixed_ok and not negative_seen else "disagreement"
    return {
        "rank": rank,
        "candidate_id": row.get("candidate_id"),
        "canonical_sha256": row.get("canonical_sha256"),
        "reported": {"primary_status": row.get("primary_status"), "status": row.get("status"), "primary_span": reported_span},
        "structural_failures": failures,
        "rank_potential_replay": {
            "workers": REPLAY_WORKERS, "time_limit_seconds": RANK_TIME_LIMIT,
            "status": replay.status, "solver_status": replay.solver_status, "span": replay.span,
            "elapsed_seconds": replay.elapsed_seconds, "certificate_valid": primary_ok, "certificate_reason": primary_reason,
        },
        "fixed_reported_span_replay": {
            "workers": REPLAY_WORKERS, "time_limit_seconds": FIXED_TIME_LIMIT, "span": reported_span,
            "solver_status": fixed_status, "elapsed_seconds": fixed_elapsed,
            "certificate_valid": fixed_ok, "certificate_reason": fixed_reason,
        },
        "all_legal_spans_when_negative_seen": all_span_check,
        "escalation": escalation,
        "elapsed_seconds": time.perf_counter() - started,
        "outcome": outcome,
    }


def compact_status(report: dict) -> dict:
    samples = report["samples"]
    artifact = report["artifact_comparison"]
    recon = report["event_reconciliation"]
    mismatches = sum(sample["outcome"] != "pass" for sample in samples)
    invalid = sum(not sample["rank_potential_replay"]["certificate_valid"] or not sample["fixed_reported_span_replay"]["certificate_valid"] for sample in samples)
    disagreements = sum(sample["rank_potential_replay"]["status"] != "colorable" or sample["fixed_reported_span_replay"]["solver_status"] not in {"OPTIMAL", "FEASIBLE"} for sample in samples)
    structural = sum(bool(sample["structural_failures"]) for sample in samples)
    artifact_issues = bool(artifact["event_report_row_mismatches"] or artifact["count_mismatches"] or artifact["scalar_count_mismatches"] or artifact["reconciliation_coverage_mismatches"] or artifact["prior_slice_coverage_mismatches"])
    reconciliation_issues = bool(recon["event_parse_errors"] or recon["duplicate_rank_records"] or recon["duplicate_canonical_hashes"] or recon["bad_event_rows"] or recon["missing_ranks"] or recon["unexpected_ranks"] or recon["rank_hash_mismatches"] or recon["prior_union_missing_ranks"] or recon["prior_union_unexpected_ranks"] or recon["prior_rank_overlap"] or recon["prior_canonical_hash_overlap_count"] or recon["infeasible_primary_solver_ranks"] or recon["reported_negative_or_unresolved_ranks"])
    negative_replays = [sample["rank"] for sample in samples if sample["all_legal_spans_when_negative_seen"] is not None]
    complete = len(samples) == report["planned_sample_count"]
    conclusion = "pass" if complete and not mismatches and not artifact_issues and not reconciliation_issues else "issues_found"
    return {
        "schema_version": 1,
        "completion": "complete" if complete else "incomplete",
        "conclusion": conclusion,
        "queue_size": report["queue_size"],
        "tail_rank_window_one_based": [TAIL_FIRST, TAIL_LAST],
        "sampled_count": len(samples),
        "planned_sample_count": report["planned_sample_count"],
        "sampled_ranks": [sample["rank"] for sample in samples],
        "mismatches": mismatches,
        "structural_mismatches": structural,
        "invalid_certificates": invalid,
        "solver_disagreements": disagreements,
        "negative_replay_ranks": negative_replays,
        "reported_negative_or_unresolved_ranks": recon["reported_negative_or_unresolved_ranks"],
        "event_report_artifact_issues": artifact_issues,
        "reconciliation_issues": reconciliation_issues,
        "max_runtime_seconds": max((sample["elapsed_seconds"] for sample in samples), default=0.0),
        "max_rank_potential_runtime_seconds": max((sample["rank_potential_replay"]["elapsed_seconds"] for sample in samples), default=0.0),
        "max_fixed_span_runtime_seconds": max((sample["fixed_reported_span_replay"]["elapsed_seconds"] for sample in samples), default=0.0),
        "replay_workers": REPLAY_WORKERS,
        "production_workers": 1,
    }


def write_markdown(report: dict, status: dict) -> None:
    lines = [
        "# Order-18 Final-Tail Spot Audit", "",
        f"- Conclusion: `{status['conclusion']}`",
        f"- Reconstructed deterministic queue: `{status['queue_size']}` candidates",
        f"- Audited final-tail window: `{TAIL_FIRST}-{TAIL_LAST}`",
        f"- Deterministic sample: `{status['sampled_count']}/{status['planned_sample_count']}` rows (12 beginning, 12 middle, 12 end)",
        f"- Mismatches: `{status['mismatches']}`; structural mismatches: `{status['structural_mismatches']}`",
        f"- Invalid certificates: `{status['invalid_certificates']}`; solver disagreements: `{status['solver_disagreements']}`",
        f"- Reported negatives/unresolved outcomes: `{len(status['reported_negative_or_unresolved_ranks'])}`; replay negative observations: `{len(status['negative_replay_ranks'])}`",
        f"- Maximum sampled runtime: `{status['max_runtime_seconds']:.3f}s`",
        f"- Solver replay: rank-potential plus fixed-span at each reported span, `{REPLAY_WORKERS}` workers (production used `1`)", "",
        "The audit rechecks sample hashes, stored structural fields, connectivity, bipartiteness, bipartition validity, and the minimum-degree filter. It also reconciles the complete event log against report/status counts and all stated rank/hash coverage. Any negative observation triggers an all-legal-span replay before it is recorded.", "",
        "| Third | Samples | Pass | Disagreement |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label in ("beginning", "middle", "end"):
        samples = [sample for sample in report["samples"] if sample["third"] == label]
        lines.append(f"| {label} | {len(samples)} | {sum(s['outcome'] == 'pass' for s in samples)} | {sum(s['outcome'] != 'pass' for s in samples)} |")
    (OUTPUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    started = time.time()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    queue, queue_diagnostics = production_queue()
    rows, parse_errors, event_types = event_rows(SOURCE / "classification-events.jsonl")
    reconciliation = full_reconciliation(queue, rows, parse_errors)
    artifacts = compare_artifacts(reconciliation, rows)
    indexed, _inventory = rows_by_rank(rows)
    ranks = sample_ranks()
    report = {
        "schema_version": 1,
        "completion": "running",
        "source": str(SOURCE.relative_to(ROOT)),
        "started_at_unix_seconds": started,
        "queue_size": len(queue),
        "queue_generation": queue_diagnostics,
        "sampling": {"method": "first/middle/last anchors plus deterministic pseudo-random ranks in three equal tail thirds", "per_third": SAMPLE_PER_THIRD, "seed_prefix": "order18-final-tail-spot-audit-20260825-"},
        "planned_sample_count": len(ranks),
        "event_types": dict(sorted(event_types.items())),
        "event_reconciliation": reconciliation,
        "artifact_comparison": artifacts,
        "samples": [],
    }
    boundaries = [("beginning", TAIL_FIRST, TAIL_FIRST + 828), ("middle", TAIL_FIRST + 829, TAIL_FIRST + 1657), ("end", TAIL_FIRST + 1658, TAIL_LAST)]
    for rank in ranks:
        if rank not in indexed:
            raise RuntimeError(f"sample rank {rank} missing from durable events")
        label = next(name for name, first, last in boundaries if first <= rank <= last)
        sample = audit_sample(rank, queue[rank - 1][5], indexed[rank])
        sample["third"] = label
        report["samples"].append(sample)
        atomic_json(OUTPUT / "report.json", report)
        atomic_json(OUTPUT / "status.json", compact_status(report))
    report["completion"] = "complete"
    report["finished_at_unix_seconds"] = time.time()
    report["elapsed_seconds"] = report["finished_at_unix_seconds"] - started
    status = compact_status(report)
    atomic_json(OUTPUT / "report.json", report)
    atomic_json(OUTPUT / "status.json", status)
    write_markdown(report, status)
    print(json.dumps(status, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
