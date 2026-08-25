#!/usr/bin/env python3
"""Reconcile the durable order-18 v4 JSONL stream after an external stop."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path


ROOT = Path("results/order18-targeted-v4")


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


def main() -> None:
    checkpoint = json.loads((ROOT / "report.checkpoint.json").read_text())
    events = [
        json.loads(line)
        for line in (ROOT / "classification-events.jsonl").read_text().splitlines()
    ]
    rows_by_rank = {
        event["row"]["rank"]
        : event["row"]
        for event in events
        if event["event"] == "classification_completed"
    }
    rows = [rows_by_rank[rank] for rank in sorted(rows_by_rank)]
    counts = Counter(row["status"] for row in rows)
    lane_counts: dict[str, Counter] = {}
    for row in rows:
        lane = row["metadata"]["lane"]
        lane_counts.setdefault(lane, Counter())[row["status"]] += 1
    counts["primary_noncolorable"] = sum(
        row["primary_status"] == "non-colorable" for row in rows
    )
    rank_start, rank_end = checkpoint["configuration"]["rank_window_one_based"]
    unresolved = [rank for rank in range(rank_start, rank_end + 1) if rank not in rows_by_rank]
    checkpoint["rows"] = rows
    checkpoint["classified"] = len(rows)
    checkpoint["colorable"] = counts["colorable"]
    checkpoint["non_colorable"] = counts["confirmed_non_colorable"]
    checkpoint["timeout"] = counts["timeout"] + counts["confirmation_timeout"]
    checkpoint["counts"].update({
        "classified": len(rows),
        "colorable": counts["colorable"],
        "completion_count": len(rows),
        "confirmation_timeout": counts["confirmation_timeout"],
        "non_colorable_certified": counts["confirmed_non_colorable"],
        "primary_noncolorable": counts["primary_noncolorable"],
        "primary_timeout": counts["timeout"],
    })
    if unresolved:
        checkpoint["completion"] = "partial_classification_complete"
        checkpoint["completion_details"] = {
            "status": "partial_classification_complete",
            "stopped_reason": "external_process_termination",
            "unresolved_rank_count": len(unresolved),
            "unresolved_ranks": unresolved,
        }
    else:
        checkpoint["completion"] = "complete"
        checkpoint["completion_details"] = {
            "status": "complete",
            "stopped_reason": "all_selected_ranks_classified",
            "unresolved_rank_count": 0,
        }
    checkpoint["reconciliation"] = {
        "authoritative_classification_state": "classification-events.jsonl",
        "reason": "final report write was interrupted after a durable event append",
    }
    checkpoint["classification"] = {
        "completed_by_lane": {
            lane: dict(sorted(lane_count.items()))
            for lane, lane_count in sorted(lane_counts.items())
        },
    }
    atomic_json(ROOT / "report.json", checkpoint)
    status = {
        key: checkpoint[key]
        for key in (
            "schema_version", "goal", "completion", "completion_details",
            "elapsed_seconds", "generation", "classification", "counts", "negative_events",
        )
    }
    status["configuration"] = {
        key: checkpoint["configuration"][key]
        for key in (
            "lanes_requested", "rank_window_one_based",
            "classification_rank_window_one_based", "primary_solver",
            "primary_time_limit_seconds", "span_time_limit_seconds", "filters",
        )
    }
    atomic_json(ROOT / "status.json", status)


if __name__ == "__main__":
    main()
