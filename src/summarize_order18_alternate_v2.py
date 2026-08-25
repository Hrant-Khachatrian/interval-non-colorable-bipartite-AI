#!/usr/bin/env python3
"""Build the compact durable summary for the order-18 alternate-family v2 run."""

from __future__ import annotations

import collections
import json
from pathlib import Path

from order18_alternate_family import ROOT, atomic_json


OUTPUT = ROOT / "results/order18-alternate-family-v2"
PHASES = (
    ("v1-residual", OUTPUT / "v1-residual", {"restore_limit": 14, "witness_switch_limit": 20, "near_switch_limit": 180}),
    ("expanded-step1", OUTPUT / "expanded-step1", {"restore_limit": 18, "witness_switch_limit": 30, "near_switch_limit": 270}),
    ("expanded-step2", OUTPUT / "expanded-step2", {"restore_limit": 22, "witness_switch_limit": 40, "near_switch_limit": 360}),
)


def completed_rows(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        if event.get("event") == "classification_completed":
            row = event.get("row")
            if not isinstance(row, dict):
                raise ValueError(f"invalid completed row in {path} line {number}")
            rows.append(row)
    return rows


def main() -> None:
    reports = []
    rows = []
    seen = set()
    for name, directory, bounds in PHASES:
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        phase_rows = completed_rows(directory / "classification-events.jsonl")
        if len(phase_rows) != report["counts"]["classified"]:
            raise ValueError(f"event/report count mismatch in {name}")
        for row in phase_rows:
            digest = row["canonical_sha256"]
            if digest in seen:
                raise ValueError(f"duplicate classification across v2 phases: {digest}")
            seen.add(digest)
        reports.append((name, directory, bounds, report, phase_rows))
        rows.extend(phase_rows)

    outcomes = collections.Counter(row["status"] for row in rows)
    final_generation = reports[-1][3]["generation"]
    v1_generation = reports[0][3]["generation"]
    v1_solved = v1_generation["unique_new_after_global_filter"]
    v2_new_unique = (
        reports[0][3]["generation"]["new_unique_after_prior_solutions"]
        + reports[1][3]["generation"]["new_unique_after_prior_solutions"]
        + final_generation["new_unique_after_prior_solutions"]
    )
    classified = len(rows)
    if classified != 1000:
        raise ValueError(f"expected 1000 newly classified graphs, found {classified}")

    document = {
        "schema_version": 1,
        "goal": "complete the v1 residual and broaden the alternate order-18 structural family without reclassifying solved graphs",
        "completion": "complete",
        "completion_details": {
            "reason": "target_1000_new_unique_classifications_reached",
            "unclassified_new_unique_remaining_at_final_bound": v2_new_unique - classified,
        },
        "configuration": {
            "filters": ["exactly 18 vertices", "simple bipartite", "connected", "minimum degree >= 2"],
            "primary_solver": "rank-potential CP-SAT",
            "primary_time_limit_seconds": 3.0,
            "independent_negative_confirmation": "fixed-span CP-SAT over every legal span for every primary negative",
            "fixed_span_time_limit_seconds": 5.0,
            "timeout_policy": "Only UNKNOWN is unresolved; no timeout is reported as non-colorable.",
            "global_deduplication": "Nauty bipartition-colored canonical SHA-256 against the 12,987-candidate completed first queue and all prior alternate-family event logs",
        },
        "counts": {
            "generated": final_generation["generated_raw"],
            "globally_unique_after_first_queue_filter": final_generation["unique_new_after_global_filter"],
            "previously_solved_v1": v1_solved,
            "previously_classified_v2_before_final_phase": 791,
            "new_unique_v2_across_all_phases": v2_new_unique,
            "newly_classified": classified,
            "colorable": outcomes["colorable"],
            "non_colorable": outcomes["confirmed_non_colorable"],
            "timeout": outcomes["timeout"] + outcomes["confirmation_timeout"],
            "primary_noncolorable": sum(row["primary_status"] == "non-colorable" for row in rows),
        },
        "phases": [
            {
                "name": name,
                "construction_bounds": bounds,
                "generated": report["counts"]["generated"],
                "new_unique_available": report["generation"]["new_unique_after_prior_solutions"],
                "classified": report["counts"]["classified"],
                "events_path": str((directory / "classification-events.jsonl").relative_to(ROOT)),
            }
            for name, directory, bounds, report, _ in reports
        ],
        "final_bound_generation": {
            "generated_raw": final_generation["generated_raw"],
            "unique_before_first_queue_filter": final_generation["unique_before_prior_queue_filter"],
            "overlap_with_completed_first_queue": final_generation["overlap_with_completed_first_queue"],
            "globally_unique_after_first_queue_filter": final_generation["unique_new_after_global_filter"],
            "previously_solved_excluded_before_selection": final_generation["previously_solved_excluded"],
            "new_unique_available_after_all_prior_solutions": final_generation["new_unique_after_prior_solutions"],
        },
        "negative_events": [],
    }
    atomic_json(OUTPUT / "report.json", document)
    atomic_json(OUTPUT / "status.json", document)


if __name__ == "__main__":
    main()
