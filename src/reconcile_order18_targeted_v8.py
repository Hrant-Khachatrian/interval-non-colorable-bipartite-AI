#!/usr/bin/env python3
"""Reconcile durable order-18 v8 events and check earlier rank slices."""

from __future__ import annotations

import argparse
import collections
import json
import os
import tempfile
from pathlib import Path

from order18_targeted_search import generate_candidates


ROOT = Path("results/order18-targeted-v8")
RANK_START, RANK_END = 8501, 10500
PREVIOUS_SLICES = {
    "v3": Path("results/order18-targeted-v3.json"),
    "v4": Path("results/order18-targeted-v4/classification-events.jsonl"),
    "v5": Path("results/order18-targeted-v5/classification-events.jsonl"),
    "v6": Path("results/order18-targeted-v6/classification-events.jsonl"),
    "v7": Path("results/order18-targeted-v7/classification-events.jsonl"),
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


def event_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("event") == "classification_completed":
                rows.append(event["row"])
    return rows


def prior_rows(label: str, path: Path) -> list[dict]:
    if label == "v3":
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("rows", [])
    return event_rows(path)


def rank_for(row: dict) -> int:
    """Return the one-based queue rank, including the older v3 row format."""
    if "rank" in row:
        return row["rank"]
    candidate_id = row["candidate_id"]
    if candidate_id.startswith("O18-") and candidate_id[4:].isdigit():
        return int(candidate_id[4:]) + 1
    raise ValueError(f"cannot infer rank from {candidate_id!r}")


def expected_queue() -> tuple[dict[int, str], dict]:
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
    expected_by_rank, queue = expected_queue()
    template = json.loads((ROOT / "report.json").read_text(encoding="utf-8"))
    records = event_rows(ROOT / "classification-events.jsonl")
    rows_by_rank: dict[int, dict] = {}
    duplicate_ranks: list[int] = []
    duplicate_hashes: list[str] = []
    seen_hashes: set[str] = set()
    for row in records:
        rank = rank_for(row)
        digest = row["canonical_sha256"]
        if rank in rows_by_rank:
            duplicate_ranks.append(rank)
        if digest in seen_hashes:
            duplicate_hashes.append(digest)
        rows_by_rank[rank] = row
        seen_hashes.add(digest)
    rows = [rows_by_rank[rank] for rank in sorted(rows_by_rank)]
    ranks = set(rows_by_rank)
    expected = set(expected_by_rank)
    hashes = [row["canonical_sha256"] for row in rows]
    missing_ranks = sorted(expected - ranks)
    unexpected_ranks = sorted(ranks - expected)
    rank_hash_mismatches = [
        rank for rank in sorted(ranks & expected)
        if rows_by_rank[rank]["canonical_sha256"] != expected_by_rank[rank]
    ]

    previous = {label: prior_rows(label, path) for label, path in PREVIOUS_SLICES.items()}
    previous_ranks = {
        label: {rank_for(row) for row in prior_rows}
        for label, prior_rows in previous.items()
    }
    previous_hashes = {
        label: {row["canonical_sha256"] for row in prior_rows}
        for label, prior_rows in previous.items()
    }
    rank_overlaps = {label: sorted(ranks & values) for label, values in previous_ranks.items()}
    hash_overlaps = {label: sorted(set(hashes) & values) for label, values in previous_hashes.items()}
    counts = collections.Counter(row["status"] for row in rows)
    primary_noncolorable = sum(row["primary_status"] == "non-colorable" for row in rows)
    lanes: dict[str, collections.Counter] = {}
    for row in rows:
        lanes.setdefault(row["metadata"]["lane"], collections.Counter())[row["status"]] += 1

    complete = (
        not duplicate_ranks
        and not duplicate_hashes
        and ranks == expected
        and not rank_hash_mismatches
        and not any(rank_overlaps.values())
        and not any(hash_overlaps.values())
    )
    template["rows"] = rows
    template["classified"] = len(rows)
    template["colorable"] = counts["colorable"]
    template["non_colorable"] = counts["confirmed_non_colorable"]
    template["timeout"] = counts["timeout"] + counts["confirmation_timeout"]
    template["completion"] = "complete" if complete else "partial_classification_complete"
    template["completion_details"] = {
        "status": template["completion"],
        "stopped_reason": "all_selected_ranks_classified" if complete else "event_reconciliation_incomplete",
        "unresolved_rank_count": len(missing_ranks),
        "unresolved_ranks": missing_ranks,
    }
    template["counts"].update({
        "selected_for_classification": len(expected),
        "classified": len(rows),
        "colorable": counts["colorable"],
        "primary_noncolorable": primary_noncolorable,
        "non_colorable_certified": counts["confirmed_non_colorable"],
        "primary_timeout": counts["timeout"],
        "confirmation_timeout": counts["confirmation_timeout"],
        "completion_count": len(rows),
    })
    template["classification"] = {
        "completed_by_lane": {lane: dict(sorted(count.items())) for lane, count in sorted(lanes.items())}
    }
    template["reconciliation"] = {
        "authoritative_classification_state": "classification-events.jsonl",
        "queue_reconstructed_from": "src/order18_targeted_search.py",
        "queue": queue,
        "event_classification_records": len(records),
        "unique_rank_records": len(rows),
        "expected_rank_window_one_based": [RANK_START, RANK_END],
        "duplicate_rank_records": sorted(set(duplicate_ranks)),
        "duplicate_canonical_hashes": sorted(set(duplicate_hashes)),
        "missing_ranks": missing_ranks,
        "unexpected_ranks": unexpected_ranks,
        "rank_hash_mismatches": rank_hash_mismatches,
        "canonical_hashes_unique_within_v8": not duplicate_hashes,
        "previous_slice_rank_overlaps": rank_overlaps,
        "previous_slice_canonical_hash_overlap_counts": {
            label: len(values) for label, values in hash_overlaps.items()
        },
        "previous_slice_canonical_hash_overlaps": hash_overlaps,
        "reconciliation_status": "complete" if complete else "incomplete",
    }
    atomic_json(ROOT / "report.json", template)
    atomic_json(ROOT / "report.checkpoint.json", template)
    status = {key: template[key] for key in (
        "schema_version", "goal", "completion", "completion_details", "elapsed_seconds",
        "generation", "classification", "counts", "negative_events", "reconciliation",
    )}
    status["configuration"] = {
        key: template["configuration"][key]
        for key in (
            "lanes_requested", "rank_window_one_based", "classification_rank_window_one_based",
            "primary_solver", "primary_time_limit_seconds", "span_time_limit_seconds", "filters",
        )
    }
    atomic_json(ROOT / "status.json", status)
    atomic_json(ROOT / "reconciliation.json", template["reconciliation"])
    print(json.dumps({"complete": complete, "counts": template["counts"], "reconciliation": template["reconciliation"]}, indent=2))


if __name__ == "__main__":
    main()
