#!/usr/bin/env python3
"""Reconcile the durable order-18 alternate-family v3 phase logs."""

from __future__ import annotations

import collections
import json
from pathlib import Path

from order18_alternate_family import ROOT, atomic_json, prior_queue_hashes


OUTPUT = ROOT / "results/order18-alternate-family-v3"
PHASES = (
    ("v2-residual", OUTPUT / "v2-residual", {
        "restore_limit": 22, "witness_switch_limit": 40, "near_switch_limit": 360,
    }),
    ("expanded-step3", OUTPUT / "expanded-step3", {
        "restore_limit": 80, "witness_switch_limit": 180, "near_switch_limit": 1620,
    }),
)
PRIOR_LOGS = (
    ROOT / "results/order18-alternate-family-v1/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/v1-residual/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/expanded-step1/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/expanded-step2/classification-events.jsonl",
)


def rows_from_events(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        if event.get("event") != "classification_completed":
            continue
        row = event.get("row")
        if not isinstance(row, dict) or not isinstance(row.get("canonical_sha256"), str):
            raise ValueError(f"invalid completed event in {path} line {line_number}")
        rows.append(row)
    return rows


def load_report(directory: Path) -> dict | None:
    path = directory / "report.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def main() -> None:
    prior_rows = [row for path in PRIOR_LOGS for row in rows_from_events(path)]
    prior_hashes = {row["canonical_sha256"] for row in prior_rows}
    first_queue_hashes = prior_queue_hashes()
    all_rows: list[dict] = []
    phase_documents = []
    seen = set(prior_hashes)
    for name, directory, bounds in PHASES:
        rows = rows_from_events(directory / "classification-events.jsonl")
        report = load_report(directory)
        for row in rows:
            digest = row["canonical_sha256"]
            if digest in seen:
                raise ValueError(f"duplicate solved graph in {name}: {digest}")
            seen.add(digest)
        all_rows.extend(rows)
        phase_documents.append((name, directory, bounds, rows, report))

    outcomes = collections.Counter(row["status"] for row in all_rows)
    expanded = phase_documents[-1][4]
    final_generation = expanded.get("generation", {}) if expanded else {}
    residual_rows = phase_documents[0][3]
    residual_hashes = {row["canonical_sha256"] for row in residual_rows}
    if first_queue_hashes & prior_hashes:
        raise ValueError("v1/v2 events overlap the completed first queue")
    if (first_queue_hashes | prior_hashes) & residual_hashes:
        raise ValueError("v2 residual events overlap prior solved state")
    if first_queue_hashes & {row["canonical_sha256"] for row in all_rows}:
        raise ValueError("v3 events overlap the completed first queue")
    complete = bool(expanded and expanded.get("completion") == "complete" and len(all_rows) >= 1031)
    document = {
        "schema_version": 1,
        "goal": "resume the alternate order-18 family, exhaust the v2 residual, then classify at least 1,000 further globally new candidates at the next deterministic bound",
        "completion": "complete" if complete else "partial",
        "configuration": {
            "filters": ["exactly 18 vertices", "simple bipartite", "connected", "minimum degree >= 2"],
            "primary_solver": "rank-potential CP-SAT",
            "independent_negative_confirmation": "fixed-span CP-SAT over every legal span for every primary negative",
            "global_deduplication": "Nauty bipartition-colored canonical SHA-256 against the completed 12,987-candidate first queue and every v1/v2/v3 durable event log",
            "timeout_policy": "Only UNKNOWN is unresolved; no timeout is reported as non-colorable.",
        },
        "counts": {
            "generated": final_generation.get("generated_raw", 0),
            "new_unique": final_generation.get("new_unique_after_prior_solutions", 0) + len(residual_hashes),
            "previously_solved": len(first_queue_hashes) + len(prior_hashes),
            "previously_solved_before_expanded_step3": len(first_queue_hashes) + len(prior_hashes) + len(residual_hashes),
            "v2_residual_classified": len(phase_documents[0][3]),
            "newly_classified": len(all_rows),
            "colorable": outcomes["colorable"],
            "non_colorable": outcomes["confirmed_non_colorable"],
            "timeout": outcomes["timeout"] + outcomes["confirmation_timeout"],
            "primary_noncolorable": sum(row.get("primary_status") == "non-colorable" for row in all_rows),
        },
        "final_bound_generation": final_generation,
        "phases": [
            {
                "name": name,
                "construction_bounds": bounds,
                "classified": len(rows),
                "events_path": str((directory / "classification-events.jsonl").relative_to(ROOT)),
                "report_present": report is not None,
            }
            for name, directory, bounds, rows, report in phase_documents
        ],
        "negative_events": [
            {key: row[key] for key in ("rank", "candidate_id", "canonical_sha256", "status")}
            for row in all_rows if row["status"] == "confirmed_non_colorable"
        ],
    }
    atomic_json(OUTPUT / "report.json", document)
    atomic_json(OUTPUT / "status.json", document)


if __name__ == "__main__":
    main()
