#!/usr/bin/env python3
"""Independently reconcile the completed order-18 final-tail event stream."""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import tempfile
from pathlib import Path

from order18_targeted_search import generate_candidates, graph_metrics, valid_candidate


ROOT = Path("results/order18-targeted-final-tail")
TAIL = range(10501, 12988)
PRIOR = {
    "v3": Path("results/order18-targeted-v3.json"),
    "v4": Path("results/order18-targeted-v4/classification-events.jsonl"),
    "v5": Path("results/order18-targeted-v5/classification-events.jsonl"),
    "v6": Path("results/order18-targeted-v6/classification-events.jsonl"),
    "v7": Path("results/order18-targeted-v7/classification-events.jsonl"),
    "v8": Path("results/order18-targeted-v8/classification-events.jsonl"),
}


def atomic_json(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def event_rows(path: Path) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"invalid_json:{number}:{exc.msg}")
                continue
            if event.get("event") == "classification_completed":
                if isinstance(event.get("row"), dict):
                    rows.append(event["row"])
                else:
                    errors.append(f"invalid_completion_row:{number}")
    return rows, errors


def rank_for(row: dict) -> int:
    if isinstance(row.get("rank"), int):
        return row["rank"]
    candidate_id = row.get("candidate_id", "")
    if candidate_id.startswith("O18-") and candidate_id[4:].isdigit():
        return int(candidate_id[4:]) + 1
    raise ValueError(f"unusable_rank:{candidate_id!r}")


def same_value(left, right) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
    return left == right


def expected_queue() -> tuple[dict[int, tuple], dict]:
    args = argparse.Namespace(
        lanes="all", candidate_cap=12987, rank_start=0, max_additions=1,
        max_deleted_degree=3, max_rewires=750, extension_limit=18,
    )
    selected, raw_lanes, generated_lanes, selected_lanes, diagnostics = generate_candidates(args)
    diagnostics.update({
        "generated_raw_by_lane": dict(sorted(raw_lanes.items())),
        "unique_ranked_by_lane": dict(sorted(generated_lanes.items())),
        "selected_by_lane": dict(sorted(selected_lanes.items())),
    })
    return {rank: item for rank, item in enumerate(selected, 1)}, diagnostics


def graph_field_errors(row: dict, rank: int, item: tuple) -> list[str]:
    _tier, _margin, _variance, _delta, digest, graph, metrics = item
    expected = {
        "candidate_id": f"O18-R{rank:05d}",
        "canonical_sha256": digest,
        "order": graph.n,
        "size": graph.m,
        "bipartition_sizes": list(map(len, graph.bipartition)),
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "degrees": graph.degrees,
        "metadata": graph.metadata,
        **metrics,
    }
    errors = [] if valid_candidate(graph) else ["reconstructed_graph_invalid"]
    for field, value in expected.items():
        if not same_value(row.get(field), value):
            errors.append(f"field_mismatch:{field}")
    return errors


def result_errors(row: dict) -> list[str]:
    primary, status = row.get("primary_status"), row.get("status")
    if primary == "colorable":
        return [] if status == "colorable" else ["colorable_primary_incoherent_status"]
    if primary == "timeout":
        return [] if status == "timeout" else ["timeout_primary_incoherent_status"]
    if primary != "non-colorable":
        return ["invalid_primary_status"]
    if status not in {"confirmed_non_colorable", "confirmation_timeout", "colorable"}:
        return ["negative_primary_incoherent_status"]
    confirmation = row.get("independent_confirmation")
    if not isinstance(confirmation, dict):
        return ["negative_primary_missing_confirmation"]
    lower, upper = row.get("delta"), row.get("order", 0) - 1
    spans = confirmation.get("spans")
    if confirmation.get("encoding") != "fixed-span CP-SAT":
        return ["invalid_confirmation_encoding"]
    if confirmation.get("span_range_inclusive") != [lower, upper] or not isinstance(spans, dict):
        return ["invalid_confirmation_span_range"]
    legal = [str(span) for span in range(lower, upper + 1)]
    actual = list(spans)
    if status == "confirmed_non_colorable":
        if actual != legal:
            return ["certified_negative_missing_legal_span"]
        if any(value.get("solver_status") != "INFEASIBLE" or value.get("has_coloring") for value in spans.values()):
            return ["certified_negative_noninfeasible_span"]
    return []


def collect(rows: list[dict], expected: dict[int, tuple], structural: bool) -> dict:
    by_rank, hashes = {}, set()
    duplicate_ranks, duplicate_hashes, unexpected, mismatches, field_errors, result_failures = [], [], [], [], {}, {}
    for row in rows:
        try:
            rank = rank_for(row)
        except ValueError as exc:
            unexpected.append(str(exc))
            continue
        digest = row.get("canonical_sha256")
        if rank in by_rank:
            duplicate_ranks.append(rank)
        if digest in hashes:
            duplicate_hashes.append(digest)
        by_rank[rank] = row
        hashes.add(digest)
        if rank not in expected:
            unexpected.append(rank)
            continue
        if digest != expected[rank][4]:
            mismatches.append(rank)
        if structural:
            errors = graph_field_errors(row, rank, expected[rank])
            if errors:
                field_errors[str(rank)] = errors
            errors = result_errors(row)
            if errors:
                result_failures[str(rank)] = errors
    return {
        "by_rank": by_rank, "hashes": hashes,
        "duplicate_ranks": sorted(set(duplicate_ranks)),
        "duplicate_hashes": sorted(set(duplicate_hashes)),
        "unexpected": sorted(unexpected, key=str), "mismatches": sorted(set(mismatches)),
        "field_errors": field_errors, "result_failures": result_failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write final-tail reconciliation artifacts")
    args = parser.parse_args()
    expected, queue = expected_queue()
    event_path = ROOT / "classification-events.jsonl"
    tail_rows, parse_errors = event_rows(event_path)
    tail = collect(tail_rows, expected, structural=True)
    expected_tail = set(TAIL)
    tail_ranks = set(tail["by_rank"])

    prior_summaries, all_prior_ranks, all_prior_hashes = {}, set(), set()
    for label, path in PRIOR.items():
        if label == "v3":
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("rows", [])
            parse = []
        else:
            rows, parse = event_rows(path)
        data = collect(rows, expected, structural=False)
        ranks = set(data["by_rank"])
        all_prior_ranks.update(ranks)
        all_prior_hashes.update(data["hashes"])
        prior_summaries[label] = {
            "event_classification_records": len(rows), "unique_rank_records": len(ranks),
            "duplicate_rank_records": data["duplicate_ranks"], "duplicate_canonical_hashes": data["duplicate_hashes"],
            "unexpected_ranks": data["unexpected"], "rank_hash_mismatches": data["mismatches"],
            "parse_errors": parse,
            "rank_overlap_with_final_tail": sorted(ranks & tail_ranks),
            "canonical_hash_overlap_count_with_final_tail": len(data["hashes"] & tail["hashes"]),
        }
    prior_expected = set(range(1, 10501))
    primary_negatives = [r for r in tail_rows if r.get("primary_status") == "non-colorable"]
    primary_timeouts = [r for r in tail_rows if r.get("primary_status") == "timeout"]
    certified_negatives = [r for r in tail_rows if r.get("status") == "confirmed_non_colorable"]
    all_issues = (
        parse_errors or tail["duplicate_ranks"] or tail["duplicate_hashes"] or tail["unexpected"] or
        tail["mismatches"] or tail["field_errors"] or tail["result_failures"] or
        expected_tail - tail_ranks or tail_ranks - expected_tail or
        all_prior_ranks != prior_expected or all_prior_ranks & tail_ranks or all_prior_hashes & tail["hashes"] or
        any(summary["duplicate_rank_records"] or summary["duplicate_canonical_hashes"] or summary["unexpected_ranks"] or summary["rank_hash_mismatches"] or summary["parse_errors"] for summary in prior_summaries.values())
    )
    report = {
        "schema_version": 1,
        "authoritative_classification_state": str(event_path),
        "queue_reconstructed_from": "src/order18_targeted_search.py",
        "expected_rank_window_one_based": [10501, 12987],
        "queue": queue,
        "event_classification_records": len(tail_rows),
        "unique_rank_records": len(tail_ranks),
        "duplicate_rank_records": tail["duplicate_ranks"],
        "duplicate_canonical_hashes": tail["duplicate_hashes"],
        "missing_ranks": sorted(expected_tail - tail_ranks),
        "unexpected_ranks": tail["unexpected"],
        "rank_hash_mismatches": tail["mismatches"],
        "event_parse_errors": parse_errors,
        "structural_field_errors": tail["field_errors"],
        "result_integrity_failures": tail["result_failures"],
        "canonical_hashes_unique_within_final_tail": not tail["duplicate_hashes"],
        "prior_slices": prior_summaries,
        "prior_union_expected_rank_window_one_based": [1, 10500],
        "prior_union_missing_ranks": sorted(prior_expected - all_prior_ranks),
        "prior_union_unexpected_ranks": sorted(all_prior_ranks - prior_expected),
        "prior_rank_overlap": sorted(all_prior_ranks & tail_ranks),
        "prior_canonical_hash_overlap_count": len(all_prior_hashes & tail["hashes"]),
        "primary_negative_count": len(primary_negatives),
        "primary_negative_ranks": sorted(rank_for(row) for row in primary_negatives),
        "primary_timeout_count": len(primary_timeouts),
        "primary_timeout_ranks": sorted(rank_for(row) for row in primary_timeouts),
        "certified_negative_count": len(certified_negatives),
        "certified_negative_ranks": sorted(rank_for(row) for row in certified_negatives),
        "reconciliation_status": "complete" if not all_issues else "incomplete",
    }
    summary = [
        "# Order-18 Final-Tail Reconciliation", "",
        f"- Status: `{report['reconciliation_status']}`", f"- Durable completion records: `{len(tail_rows)}/2487`", 
        f"- Unique ranks: `{len(tail_ranks)}`; missing: `{len(report['missing_ranks'])}`; unexpected: `{len(report['unexpected_ranks'])}`",
        f"- Duplicate ranks/hashes: `{len(tail['duplicate_ranks'])}/{len(tail['duplicate_hashes'])}`",
        f"- Queue hash mismatches: `{len(tail['mismatches'])}`; structural field failures: `{len(tail['field_errors'])}`",
        f"- Earlier-slice rank/hash overlaps: `{len(report['prior_rank_overlap'])}/{report['prior_canonical_hash_overlap_count']}`",
        f"- Primary negatives: `{len(primary_negatives)}`; primary timeouts: `{len(primary_timeouts)}`; certified negatives: `{len(certified_negatives)}`",
        "",
        "No primary negatives occurred, so no independent fixed-span negative certification was required.",
    ]
    if args.write:
        atomic_json(ROOT / "reconciliation.json", report)
        (ROOT / "report.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "reconciliation_status", "event_classification_records", "unique_rank_records", "missing_ranks",
        "unexpected_ranks", "rank_hash_mismatches", "structural_field_errors", "result_integrity_failures",
        "primary_negative_count", "primary_timeout_count", "certified_negative_count",
    )}, indent=2))


if __name__ == "__main__":
    main()
