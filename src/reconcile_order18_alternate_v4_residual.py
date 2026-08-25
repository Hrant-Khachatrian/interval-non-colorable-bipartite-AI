#!/usr/bin/env python3
"""Verify and finalize the exhaustive v4 residual at the v3 expanded bound."""

from __future__ import annotations

import collections
import json
from pathlib import Path

from order18_alternate_family import (
    ROOT,
    atomic_json,
    completed_hashes,
    generate,
    prior_queue_hashes,
)


OUTPUT = ROOT / "results/order18-alternate-family-v4-residual"
EVENTS = OUTPUT / "classification-events.jsonl"
PRIOR_EVENTS = (
    ROOT / "results/order18-alternate-family-v1/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/v1-residual/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/expanded-step1/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/expanded-step2/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v3/v2-residual/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v3/expanded-step3/classification-events.jsonl",
)
EXPECTED_RESIDUAL = 1361
EXPECTED_PRIOR_COVERED = 3231
BOUNDS = {"restore_limit": 80, "witness_switch_limit": 180, "near_switch_limit": 1620}


def completed_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        if event.get("event") != "classification_completed":
            continue
        row = event.get("row")
        if not isinstance(row, dict) or not isinstance(row.get("canonical_sha256"), str):
            raise ValueError(f"invalid completed event in {path} line {line_number}")
        rows.append(row)
    return rows


def main() -> None:
    ranked, generation = generate(**BOUNDS)
    first_queue = prior_queue_hashes()
    prior_by_log = {str(path.relative_to(ROOT)): completed_hashes(path) for path in PRIOR_EVENTS}
    prior_hashes = set().union(*prior_by_log.values())
    if len(prior_hashes) != EXPECTED_PRIOR_COVERED:
        raise ValueError(f"unexpected pre-v4 durable alternate coverage: {len(prior_hashes)}")
    if prior_hashes & first_queue:
        raise ValueError("durable alternate logs overlap the completed first queue")

    residual = [entry for entry in ranked if entry[3] not in prior_hashes]
    expected_hashes = {entry[3] for entry in residual}
    rows = completed_rows(EVENTS)
    completed = {row["canonical_sha256"] for row in rows}
    if len(rows) != len(completed):
        raise ValueError("v4 completed events contain duplicate canonical hashes")
    if completed != expected_hashes:
        missing = sorted(expected_hashes - completed)
        extra = sorted(completed - expected_hashes)
        raise ValueError(f"v4 event membership mismatch: missing={len(missing)} extra={len(extra)}")

    outcomes = collections.Counter(row.get("status") for row in rows)
    primary_negative = sum(row.get("primary_status") == "non-colorable" for row in rows)
    invalid = [row["canonical_sha256"] for row in rows if row.get("status") not in {
        "colorable", "confirmed_non_colorable", "timeout", "confirmation_timeout",
    }]
    if invalid:
        raise ValueError(f"unexpected v4 classification status: {invalid[0]}")
    reconciliation = {
        "schema_version": 1,
        "purpose": "Exact reconstruction and membership reconciliation for the final expanded-bound alternate-family residual.",
        "construction_bounds": BOUNDS,
        "expected_residual_count": EXPECTED_RESIDUAL,
        "reconstructed_residual_count": len(residual),
        "residual_count_matches_expected": len(residual) == EXPECTED_RESIDUAL,
        "generation": {
            "generated_raw": generation["generated_raw"],
            "unique_after_completed_first_queue_filter": generation["unique_new_after_global_filter"],
            "completed_first_queue_excluded": generation["overlap_with_completed_first_queue"],
            "prior_alternate_durable_hashes": len(prior_hashes),
            "remaining_after_all_prior_coverage": len(residual),
        },
        "prior_event_hash_counts": {path: len(hashes) for path, hashes in prior_by_log.items()},
        "event_membership": {
            "completed_rows": len(rows),
            "completed_unique_hashes": len(completed),
            "expected_unique_hashes": len(expected_hashes),
            "exact_match": completed == expected_hashes,
        },
        "outcomes": {
            "colorable": outcomes["colorable"],
            "confirmed_non_colorable": outcomes["confirmed_non_colorable"],
            "timeout": outcomes["timeout"] + outcomes["confirmation_timeout"],
            "primary_negatives": primary_negative,
        },
        "expanded_bound_family_exhausted": completed == expected_hashes and len(residual) == EXPECTED_RESIDUAL,
    }
    if not reconciliation["residual_count_matches_expected"]:
        raise ValueError(json.dumps(reconciliation, sort_keys=True))

    report_path = OUTPUT / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["reconciliation"] = reconciliation
    report["counts"]["previously_solved_alternate"] = len(prior_hashes)
    report["counts"]["newly_solved"] = len(rows)
    report["counts"]["expanded_bound_family_exhausted"] = reconciliation["expanded_bound_family_exhausted"]
    atomic_json(OUTPUT / "reconciliation.json", reconciliation)
    atomic_json(report_path, report)
    atomic_json(OUTPUT / "status.json", report)
    print(json.dumps(reconciliation, sort_keys=True))


if __name__ == "__main__":
    main()
