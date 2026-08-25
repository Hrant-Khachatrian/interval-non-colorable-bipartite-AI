#!/usr/bin/env python3
"""Reconcile the order-18 v5 durable event stream into its final report."""

from __future__ import annotations

import collections
import json
import os
import tempfile
from pathlib import Path


ROOT = Path("results/order18-targeted-v5")
PREVIOUS_EVENTS = Path("results/order18-targeted-v4/classification-events.jsonl")
RANK_START, RANK_END = 2501, 4500


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


def classified_rows(path: Path) -> list[dict]:
    return [
        event["row"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line))["event"] == "classification_completed"
    ]


def main() -> None:
    template = json.loads((ROOT / "report.json").read_text(encoding="utf-8"))
    rows = classified_rows(ROOT / "classification-events.jsonl")
    by_rank: dict[int, dict] = {}
    duplicate_ranks = []
    for row in rows:
        rank = row["rank"]
        if rank in by_rank:
            duplicate_ranks.append(rank)
        by_rank[rank] = row
    ordered = [by_rank[rank] for rank in sorted(by_rank)]
    ranks = set(by_rank)
    expected = set(range(RANK_START, RANK_END + 1))
    hashes = [row["canonical_sha256"] for row in ordered]

    previous = classified_rows(PREVIOUS_EVENTS)
    previous_ranks = {row["rank"] for row in previous}
    previous_hashes = {row["canonical_sha256"] for row in previous}
    counts = collections.Counter(row["status"] for row in ordered)
    primary_noncolorable = sum(row["primary_status"] == "non-colorable" for row in ordered)
    lanes: dict[str, collections.Counter] = {}
    for row in ordered:
        lanes.setdefault(row["metadata"]["lane"], collections.Counter())[row["status"]] += 1

    complete = (
        not duplicate_ranks
        and ranks == expected
        and len(hashes) == len(set(hashes))
        and not (ranks & previous_ranks)
        and not (set(hashes) & previous_hashes)
    )
    template["rows"] = ordered
    template["classified"] = len(ordered)
    template["colorable"] = counts["colorable"]
    template["non_colorable"] = counts["confirmed_non_colorable"]
    template["timeout"] = counts["timeout"] + counts["confirmation_timeout"]
    template["completion"] = "complete" if complete else "partial_classification_complete"
    template["completion_details"] = {
        "status": template["completion"],
        "stopped_reason": "all_selected_ranks_classified" if complete else "event_reconciliation_incomplete",
        "unresolved_rank_count": len(expected - ranks),
        "unresolved_ranks": sorted(expected - ranks),
    }
    template["counts"].update({
        "selected_for_classification": len(expected),
        "classified": len(ordered),
        "colorable": counts["colorable"],
        "primary_noncolorable": primary_noncolorable,
        "non_colorable_certified": counts["confirmed_non_colorable"],
        "primary_timeout": counts["timeout"],
        "confirmation_timeout": counts["confirmation_timeout"],
        "completion_count": len(ordered),
    })
    template["classification"] = {
        "completed_by_lane": {lane: dict(sorted(count.items())) for lane, count in sorted(lanes.items())}
    }
    template["reconciliation"] = {
        "authoritative_classification_state": "classification-events.jsonl",
        "event_classification_records": len(rows),
        "unique_rank_records": len(ordered),
        "expected_rank_window_one_based": [RANK_START, RANK_END],
        "duplicate_rank_records": sorted(set(duplicate_ranks)),
        "canonical_hashes_unique_within_v5": len(hashes) == len(set(hashes)),
        "v4_rank_overlap": sorted(ranks & previous_ranks),
        "v4_canonical_hash_overlap_count": len(set(hashes) & previous_hashes),
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
    print(json.dumps({"complete": complete, "counts": template["counts"]}, indent=2))


if __name__ == "__main__":
    main()
