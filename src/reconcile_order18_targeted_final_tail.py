#!/usr/bin/env python3
"""Reconcile durable order-18 final-tail events against all earlier slices."""

from __future__ import annotations

import argparse
import collections
import json
import os
import tempfile
from pathlib import Path

from order18_targeted_search import generate_candidates


ROOT = Path("results/order18-targeted-final-tail")
RANK_START, RANK_END = 10501, 12987
PRIOR_SLICES = {
    "v3": Path("results/order18-targeted-v3.json"),
    "v4": Path("results/order18-targeted-v4/classification-events.jsonl"),
    "v5": Path("results/order18-targeted-v5/classification-events.jsonl"),
    "v6": Path("results/order18-targeted-v6/classification-events.jsonl"),
    "v7": Path("results/order18-targeted-v7/classification-events.jsonl"),
    "v8": Path("results/order18-targeted-v8/classification-events.jsonl"),
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


def rows_from_events(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid event at {path}:{line_number}") from exc
            if event.get("event") == "classification_completed":
                row = event.get("row")
                if not isinstance(row, dict):
                    raise ValueError(f"completion event at {path}:{line_number} has no row")
                rows.append(row)
    return rows


def prior_rows(label: str, path: Path) -> list[dict]:
    if label == "v3":
        return json.loads(path.read_text(encoding="utf-8"))["rows"]
    return rows_from_events(path)


def rank_for(row: dict) -> int:
    if "rank" in row:
        return row["rank"]
    candidate_id = row["candidate_id"]
    if candidate_id.startswith("O18-") and candidate_id[4:].isdigit():
        return int(candidate_id[4:]) + 1
    raise ValueError(f"cannot infer rank from {candidate_id!r}")


def queue_hashes() -> tuple[dict[int, str], dict]:
    args = argparse.Namespace(
        lanes="all",
        candidate_cap=RANK_END,
        rank_start=RANK_START - 1,
        max_additions=1,
        max_deleted_degree=3,
        max_rewires=750,
        extension_limit=18,
    )
    selected, raw_lanes, generated_lanes, selected_lanes, diagnostics = generate_candidates(args)
    selected = selected[: RANK_END - RANK_START + 1]
    expected = {
        RANK_START + number: digest
        for number, (_tier, _margin, _variance, _delta, digest, _graph, _metrics) in enumerate(selected)
    }
    diagnostics["generated_raw_by_lane"] = dict(sorted(raw_lanes.items()))
    diagnostics["unique_ranked_by_lane"] = dict(sorted(generated_lanes.items()))
    diagnostics["selected_by_lane"] = dict(sorted(selected_lanes.items()))
    return expected, diagnostics


def main() -> None:
    expected, queue = queue_hashes()
    expected_ranks = set(expected)
    events = rows_from_events(ROOT / "classification-events.jsonl")
    rows_by_rank: dict[int, dict] = {}
    duplicate_ranks = []
    hashes = set()
    duplicate_hashes = []
    for row in events:
        rank = rank_for(row)
        digest = row.get("canonical_sha256")
        if rank in rows_by_rank:
            duplicate_ranks.append(rank)
        if digest in hashes:
            duplicate_hashes.append(digest)
        rows_by_rank[rank] = row
        hashes.add(digest)

    ranks = set(rows_by_rank)
    ordered = [rows_by_rank[rank] for rank in sorted(ranks)]
    mismatched_hash_ranks = [
        rank for rank in sorted(ranks & expected_ranks)
        if rows_by_rank[rank]["canonical_sha256"] != expected[rank]
    ]
    missing = sorted(expected_ranks - ranks)
    unexpected = sorted(ranks - expected_ranks)

    prior_summary = {}
    prior_ranks, prior_hashes = set(), set()
    for label, path in PRIOR_SLICES.items():
        rows = prior_rows(label, path)
        slice_ranks = {rank_for(row) for row in rows}
        slice_hashes = {row["canonical_sha256"] for row in rows}
        prior_summary[label] = {
            "rank_records": len(rows),
            "unique_rank_records": len(slice_ranks),
            "unique_hash_records": len(slice_hashes),
            "rank_overlap_with_final_tail": sorted(ranks & slice_ranks),
            "canonical_hash_overlap_with_final_tail": sorted(hashes & slice_hashes),
        }
        prior_ranks.update(slice_ranks)
        prior_hashes.update(slice_hashes)

    complete = (
        not duplicate_ranks
        and not duplicate_hashes
        and not missing
        and not unexpected
        and not mismatched_hash_ranks
        and not (ranks & prior_ranks)
        and not (hashes & prior_hashes)
    )
    report_path = ROOT / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    counts = collections.Counter(row["status"] for row in ordered)
    primary_noncolorable = sum(row["primary_status"] == "non-colorable" for row in ordered)
    lanes: dict[str, collections.Counter] = {}
    for row in ordered:
        lanes.setdefault(row["metadata"]["lane"], collections.Counter())[row["status"]] += 1
    report["rows"] = ordered
    report["classified"] = len(ordered)
    report["colorable"] = counts["colorable"]
    report["non_colorable"] = counts["confirmed_non_colorable"]
    report["timeout"] = counts["timeout"] + counts["confirmation_timeout"]
    report["completion"] = "complete" if complete else "partial_classification_complete"
    report["completion_details"] = {
        "status": report["completion"],
        "stopped_reason": "all_selected_ranks_classified" if complete else "event_reconciliation_incomplete",
        "unresolved_rank_count": len(missing),
        "unresolved_ranks": missing,
    }
    report["counts"].update({
        "selected_for_classification": len(expected_ranks),
        "classified": len(ordered),
        "colorable": counts["colorable"],
        "primary_noncolorable": primary_noncolorable,
        "non_colorable_certified": counts["confirmed_non_colorable"],
        "primary_timeout": counts["timeout"],
        "confirmation_timeout": counts["confirmation_timeout"],
        "completion_count": len(ordered),
    })
    report["classification"] = {
        "completed_by_lane": {lane: dict(sorted(count.items())) for lane, count in sorted(lanes.items())}
    }
    reconciliation = {
        "authoritative_classification_state": "classification-events.jsonl",
        "queue_reconstructed_from": "src/order18_targeted_search.py",
        "expected_rank_window_one_based": [RANK_START, RANK_END],
        "queue": queue,
        "event_classification_records": len(events),
        "unique_rank_records": len(ordered),
        "duplicate_rank_records": sorted(set(duplicate_ranks)),
        "duplicate_canonical_hashes": sorted(set(duplicate_hashes)),
        "missing_ranks": missing,
        "unexpected_ranks": unexpected,
        "rank_hash_mismatches": mismatched_hash_ranks,
        "canonical_hashes_unique_within_final_tail": not duplicate_hashes,
        "prior_slices": prior_summary,
        "prior_rank_overlap": sorted(ranks & prior_ranks),
        "prior_canonical_hash_overlap_count": len(hashes & prior_hashes),
        "reconciliation_status": "complete" if complete else "incomplete",
    }
    report["reconciliation"] = reconciliation
    atomic_json(ROOT / "report.json", report)
    atomic_json(ROOT / "report.checkpoint.json", report)
    status = {key: report[key] for key in (
        "schema_version", "goal", "completion", "completion_details", "elapsed_seconds",
        "generation", "classification", "counts", "negative_events", "reconciliation",
    )}
    status["configuration"] = {
        key: report["configuration"][key]
        for key in (
            "lanes_requested", "rank_window_one_based", "classification_rank_window_one_based",
            "primary_solver", "primary_time_limit_seconds", "span_time_limit_seconds", "filters",
        )
    }
    atomic_json(ROOT / "status.json", status)
    atomic_json(ROOT / "reconciliation.json", reconciliation)
    print(json.dumps({"complete": complete, "counts": report["counts"], "reconciliation": reconciliation}, indent=2))


if __name__ == "__main__":
    main()
