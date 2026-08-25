#!/usr/bin/env python3
"""Reconcile the v5 alternate-family expansion against all durable coverage."""

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
    valid,
)


OUTPUT = ROOT / "results/order18-alternate-family-v5-expansion"
EVENTS = OUTPUT / "classification-events.jsonl"
MANIFEST = OUTPUT / "selection-manifest.json"
BOUNDS = {
    "restore_limit": 200,
    "witness_switch_limit": 450,
    "near_switch_limit": 4050,
}
CLASSIFIED_LIMIT = 1000
PRIOR_EVENTS = (
    ROOT / "results/order18-alternate-family-v1/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/v1-residual/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/expanded-step1/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/expanded-step2/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v3/v2-residual/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v3/expanded-step3/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v4-residual/classification-events.jsonl",
)


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
    if prior_hashes & first_queue:
        raise ValueError("durable alternate coverage overlaps the completed first queue")

    fresh = [entry for entry in ranked if entry[3] not in prior_hashes]
    selected = fresh[:CLASSIFIED_LIMIT]
    if len(fresh) < 2000:
        raise ValueError(f"v5 expansion has only {len(fresh)} globally new candidates")
    if len(selected) != CLASSIFIED_LIMIT or not all(valid(entry[4]) for entry in selected):
        raise ValueError("invalid deterministic v5 selection")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest_entries = manifest.get("entries")
    if not isinstance(manifest_entries, list) or len(manifest_entries) != CLASSIFIED_LIMIT:
        raise ValueError("invalid v5 selection manifest")
    expected_hashes = {entry.get("canonical_sha256") for entry in manifest_entries if isinstance(entry, dict)}
    if len(expected_hashes) != CLASSIFIED_LIMIT or not all(isinstance(digest, str) for digest in expected_hashes):
        raise ValueError("invalid v5 manifest hash membership")

    rows = completed_rows(EVENTS)
    completed = {row["canonical_sha256"] for row in rows}
    if len(rows) != len(completed):
        raise ValueError("v5 events contain duplicate canonical hashes")
    fresh_by_hash = {entry[3]: entry for entry in fresh}
    if not completed <= set(fresh_by_hash):
        raise ValueError(f"v5 events include {len(completed - set(fresh_by_hash))} non-fresh hashes")
    if not expected_hashes <= completed:
        raise ValueError(
            f"v5 structural ranking failure: manifest_top_1000_missing={len(expected_hashes - completed)}"
        )
    invalid_rows = [
        row["canonical_sha256"]
        for row in rows
        if row.get("order") != 18
        or row.get("minimum_degree", 0) < 2
        or not isinstance(row.get("bipartition_sizes"), list)
        or len(row["bipartition_sizes"]) != 2
        or sum(row["bipartition_sizes"]) != 18
        or min(row["bipartition_sizes"]) < 2
    ]
    if invalid_rows:
        raise ValueError(f"invalid v5 graph evidence: {invalid_rows[0]}")

    primary_negatives = [row for row in rows if row.get("primary_status") == "non-colorable"]
    for row in primary_negatives:
        confirmation = row.get("independent_confirmation", {})
        spans = confirmation.get("spans", {})
        expected_spans = {str(span) for span in range(row["delta"], row["order"])}
        if confirmation.get("encoding") != "fixed-span CP-SAT" or set(spans) != expected_spans:
            raise ValueError(f"incomplete fixed-span confirmation for {row['canonical_sha256']}")
    outcomes = collections.Counter(row.get("status") for row in rows)
    timeout_count = outcomes["timeout"] + outcomes["confirmation_timeout"]

    reconciliation = {
        "schema_version": 1,
        "purpose": "Exact reconstruction and membership reconciliation for the v5 wider alternate-family expansion.",
        "construction_bounds": BOUNDS,
        "filters": [
            "exactly 18 vertices",
            "simple bipartite",
            "connected",
            "minimum degree >= 2",
        ],
        "global_identity": "Nauty bipartition-colored canonical SHA-256",
        "generation": {
            "generated_raw": generation["generated_raw"],
            "unique_after_completed_first_queue_filter": generation["unique_new_after_global_filter"],
            "completed_first_queue_excluded": generation["overlap_with_completed_first_queue"],
            "prior_alternate_durable_hashes": len(prior_hashes),
            "globally_new_unique_candidates": len(fresh),
            "construction_enumeration_exhausted": True,
        },
        "prior_event_hash_counts": {path: len(hashes) for path, hashes in prior_by_log.items()},
        "event_membership": {
            "completed_rows": len(rows),
            "completed_unique_hashes": len(completed),
            "all_completed_globally_fresh": completed <= set(fresh_by_hash),
            "selection_manifest_path": str(MANIFEST.relative_to(ROOT)),
            "manifest_top_1000_classified": expected_hashes <= completed,
            "additional_boundary_classifications": len(completed - expected_hashes),
        },
        "classification": {
            "ranked_selection_size": len(selected),
            "remaining_unclassified_global_fresh": len(fresh) - len(selected),
            "queue_status": "still_open",
            "outcomes": {
                "colorable": outcomes["colorable"],
                "confirmed_non_colorable": outcomes["confirmed_non_colorable"],
                "timeout": timeout_count,
                "primary_negatives": len(primary_negatives),
            },
            "independent_negative_confirmation": {
                "required": "fixed-span CP-SAT across every legal span for each primary negative",
                "verified_primary_negative_count": len(primary_negatives),
            },
        },
        "new_bound_status": "still_open",
    }

    report_path = OUTPUT / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_counts = collections.Counter(row.get("status") for row in rows)
    report["counts"]["classified"] = len(rows)
    report["counts"]["colorable"] = report_counts["colorable"]
    report["counts"]["non_colorable"] = report_counts["confirmed_non_colorable"]
    report["counts"]["timeout"] = report_counts["timeout"] + report_counts["confirmation_timeout"]
    report["counts"]["newly_solved"] = report_counts["colorable"] + report_counts["confirmed_non_colorable"]
    report["counts"]["primary_noncolorable"] = sum(row.get("primary_status") == "non-colorable" for row in rows)
    report["counts"]["unclassified_global_fresh"] = len(fresh) - len(rows)
    report["counts"]["classified_by_lane"] = dict(sorted(
        collections.Counter(row["metadata"]["lane"] for row in rows).items()
    ))
    report["reconciliation"] = reconciliation
    report["counts"]["new_bound_exhausted"] = False
    report["counts"]["construction_enumeration_exhausted"] = True
    atomic_json(OUTPUT / "reconciliation.json", reconciliation)
    atomic_json(report_path, report)
    atomic_json(OUTPUT / "status.json", report)
    print(json.dumps(reconciliation, sort_keys=True))


if __name__ == "__main__":
    main()
