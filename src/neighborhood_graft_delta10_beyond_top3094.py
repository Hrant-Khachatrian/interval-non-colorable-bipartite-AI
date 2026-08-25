#!/usr/bin/env python3
"""Classify ranks 3095--4094 of the Delta <= 10 neighborhood-graft family.

All predecessor ledgers are treated as immutable evidence.  New decisions are
durably appended to a separate JSONL ledger before compact status snapshots are
atomically replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from interval_edge_coloring import fixed_span_sat_solve, rank_potential_solve
from neighborhood_graft_delta10_beyond_top94 import (
    append_event,
    counts_for,
    load_events,
    make_record,
    reconstruct,
    records_from_events,
)
from neighborhood_graft_delta10_search import Candidate, ranking_key


DEFAULT_PREDECESSORS = (
    Path("results/neighborhood-graft-delta10-agent/extension-full-roots"),
    Path("results/neighborhood-graft-delta10-agent/extension-beyond-top94"),
    Path("results/neighborhood-graft-delta10-agent/extension-beyond-top1094"),
    Path("results/neighborhood-graft-delta10-agent/extension-beyond-top2094-v2"),
)
DEFAULT_OUTPUT = Path(
    "results/neighborhood-graft-delta10-agent/extension-beyond-top3094"
)
EXPECTED_GENERATED = 61375
EXPECTED_UNIQUE = 10950
EXPECTED_PREDECESSORS = (94, 1000, 1000, 1000)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_records(state_path: Path, label: str) -> list[dict[str, Any]]:
    records = records_from_events(load_events(state_path))
    digests = [record["canonical_sha256"] for record in records]
    if len(digests) != len(set(digests)):
        raise RuntimeError(f"{label} contains duplicate classification hashes")
    return records


def validate_record(record: dict[str, Any], candidate: Candidate, source: str) -> None:
    expected = {
        "candidate_id": candidate.construction_id,
        "parent": candidate.parent,
        "canonical_sha256": candidate.digest,
        "order": candidate.graph.n,
        "size": candidate.graph.m,
        "delta": candidate.graph.delta,
        "minimum_degree": min(candidate.graph.degrees.values()),
    }
    actual = {key: record.get(key) for key in expected}
    if actual != expected:
        raise RuntimeError(f"{source} integrity mismatch for {candidate.digest}: {actual!r} != {expected!r}")
    if record.get("decision") not in {"colorable", "non-colorable", "timeout"}:
        raise RuntimeError(f"{source} has invalid decision for {candidate.digest}")


def confirm_negative(graph, time_limit: float, workers: int) -> tuple[bool, bool, dict[int, str]]:
    statuses: dict[int, str] = {}
    for span in range(graph.delta, graph.n):
        status, _ = fixed_span_sat_solve(graph, span, time_limit, workers)
        statuses[span] = status
        if status in ("OPTIMAL", "FEASIBLE"):
            return False, False, statuses
    unresolved = any(status == "UNKNOWN" for status in statuses.values())
    return not unresolved and all(status == "INFEASIBLE" for status in statuses.values()), unresolved, statuses


def checkpoint(
    output: Path,
    configuration: dict[str, Any],
    generation: dict[str, int],
    roots: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    new: list[dict[str, Any]],
    target: int,
    selected: int,
    elapsed: float,
    status: str,
) -> None:
    previous_counts, new_counts = counts_for(previous), counts_for(new)
    total_counts = counts_for([*previous, *new])
    payload = {
        "schema_version": 1,
        "configuration": configuration,
        "generation": generation,
        "roots": roots,
        "counts": {
            "generated": generation["generated"],
            "unique": generation["unique"],
            "previously_solved": previous_counts["classified"],
            "newly_solved": new_counts["classified"],
            "solved_total": total_counts["classified"],
            "colorable": total_counts["colorable"],
            "non_colorable": total_counts["non_colorable"],
            "timeout": total_counts["timeout"],
            "new_colorable": new_counts["colorable"],
            "new_non_colorable": new_counts["non_colorable"],
            "new_timeout": new_counts["timeout"],
            "target_new_classifications": target,
            "selected_new_candidates": selected,
            "remaining_selected": max(0, selected - new_counts["classified"]),
        },
        "integrity": {
            "predecessor_records_validated": len(previous),
            "expected_predecessor_records": sum(EXPECTED_PREDECESSORS),
            "previous_decisions_recomputed": 0,
            "state_is_append_only": True,
        },
        "status": status,
        "elapsed_seconds": elapsed,
        "recent_records": new[-128:],
        "confirmed_negatives": [record for record in new if record["decision"] == "non-colorable"],
    }
    atomic_write_json(output / "status.json", payload)
    atomic_write_json(output / "report.json", payload)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--target", type=int, default=1000)
    result.add_argument("--primary-time-limit", type=float, default=2.0)
    result.add_argument("--independent-time-limit", type=float, default=2.0)
    result.add_argument("--workers", type=int, default=1)
    result.add_argument("--checkpoint-every", type=int, default=25)
    return result


def main() -> None:
    args = parser().parse_args()
    if args.target <= 0 or args.checkpoint_every <= 0:
        raise SystemExit("target and checkpoint-every must be positive")
    if not 0 < args.primary_time_limit <= 5 or not 0 < args.independent_time_limit <= 5:
        raise SystemExit("solver time limits must be in (0, 5]")
    if not 1 <= args.workers <= 4:
        raise SystemExit("workers must be between 1 and 4")

    started = time.monotonic()
    predecessor_paths = [path / "classification-state.jsonl" for path in DEFAULT_PREDECESSORS]
    predecessor_groups = [read_records(path, f"predecessor state {index + 1}") for index, path in enumerate(predecessor_paths)]
    if tuple(map(len, predecessor_groups)) != EXPECTED_PREDECESSORS:
        raise RuntimeError(f"expected predecessor decisions {EXPECTED_PREDECESSORS}, found {tuple(map(len, predecessor_groups))}")

    candidates, generation, roots, _ = reconstruct()
    if generation["generated"] != EXPECTED_GENERATED or generation["unique"] != EXPECTED_UNIQUE:
        raise RuntimeError(f"unexpected reconstruction counts: {generation!r}")
    by_digest = {candidate.digest: candidate for candidate in candidates}
    if len(by_digest) != len(candidates):
        raise RuntimeError("reconstruction produced duplicate canonical candidates")
    previous = [record for group in predecessor_groups for record in group]
    for index, group in enumerate(predecessor_groups, 1):
        for record in group:
            candidate = by_digest.get(record["canonical_sha256"])
            if candidate is None:
                raise RuntimeError(f"predecessor state {index} references an unknown candidate")
            validate_record(record, candidate, f"predecessor state {index}")
    solved = {record["canonical_sha256"] for record in previous}
    if len(solved) != len(previous):
        raise RuntimeError("predecessor histories overlap")
    ranked = sorted(candidates, key=ranking_key)
    if {candidate.digest for candidate in ranked[:len(previous)]} != solved:
        raise RuntimeError("predecessor histories are not exactly ranks 1 through 3094")
    selected = [candidate for candidate in ranked if candidate.digest not in solved][:args.target]

    state_path = args.output / "classification-state.jsonl"
    existing = read_records(state_path, "current continuation state")
    existing_digests = {record["canonical_sha256"] for record in existing}
    selected_digests = {candidate.digest for candidate in selected}
    if not existing_digests <= selected_digests:
        raise RuntimeError("current continuation state does not match ranks 3095 onward")
    for record in existing:
        validate_record(record, by_digest[record["canonical_sha256"]], "current continuation state")
    if len(existing) > args.target:
        raise RuntimeError("current continuation state exceeds requested target")

    configuration = {
        "predecessor_states": [str(path) for path in predecessor_paths],
        "predecessor_state_sha256": [hashlib.sha256(path.read_bytes()).hexdigest() for path in predecessor_paths],
        "family_reconstruction": "deterministic source enumeration and bipartition-colored Nauty hash",
        "filters": "simple connected bipartite; minimum degree >= 2; Delta <= 10",
        "ranking": "original ranking_key: hub margin/tier, variance, obstruction count, top-three margin sum, canonical hash",
        "primary_solver": "rank-potential CP-SAT",
        "primary_time_limit_seconds": args.primary_time_limit,
        "independent_solver": "fixed-span CP-SAT over every legal span Delta through n-1 for primary negatives",
        "independent_time_limit_seconds": args.independent_time_limit,
        "workers": args.workers,
    }
    if not state_path.exists():
        append_event(state_path, {"event": "run-start", "configuration": configuration, "generation": generation, "previous_records_validated": len(previous), "selected_new_candidates": len(selected)})
    checkpoint(args.output, configuration, generation, roots, previous, existing, args.target, len(selected), time.monotonic() - started, "running")

    new_records = list(existing)
    for rank, candidate in enumerate(selected, len(previous) + 1):
        if candidate.digest in existing_digests:
            continue
        primary = rank_potential_solve(candidate.graph, args.primary_time_limit, args.workers)
        record = make_record(candidate, primary)
        if primary.status == "colorable":
            record["decision"] = "colorable"
        elif primary.status == "timeout":
            record["decision"] = "timeout"
            record["independent_confirmation"] = {"required": False, "reason": "primary timeout remains unresolved"}
        elif primary.status == "non-colorable":
            confirmed, unresolved, statuses = confirm_negative(candidate.graph, args.independent_time_limit, args.workers)
            record["independent_confirmation"] = {"encoding": "fixed-span-cpsat", "spans_checked": sorted(statuses), "span_statuses": {str(span): value for span, value in sorted(statuses.items())}, "confirmed_non_colorable": confirmed, "unresolved": unresolved}
            if unresolved:
                record["decision"] = "timeout"
            elif not confirmed:
                raise AssertionError("primary negative contradicted by a feasible legal span")
            else:
                record["decision"] = "non-colorable"
        else:
            raise AssertionError(f"unexpected primary status: {primary.status}")
        append_event(state_path, {"event": "classification", "rank": rank, "elapsed_seconds": time.monotonic() - started, "record": record})
        new_records.append(record)
        existing_digests.add(candidate.digest)
        if len(new_records) % args.checkpoint_every == 0:
            checkpoint(args.output, configuration, generation, roots, previous, new_records, args.target, len(selected), time.monotonic() - started, "running")

    checkpoint(args.output, configuration, generation, roots, previous, new_records, args.target, len(selected), time.monotonic() - started, "complete")
    append_event(state_path, {"event": "run-complete", "elapsed_seconds": time.monotonic() - started, "counts": counts_for(new_records)})


if __name__ == "__main__":
    main()
