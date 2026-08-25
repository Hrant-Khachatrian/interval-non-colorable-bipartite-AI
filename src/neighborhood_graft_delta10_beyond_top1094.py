#!/usr/bin/env python3
"""Classify the next ranked neighborhood-graft Delta <= 10 candidates.

This continuation rebuilds the full deterministic family, validates the 94
initial and 1,000 first-continuation decisions, and appends only decisions for
the next requested unsolved ranked candidates.  Reports are compact atomic
snapshots; the JSONL state is the durable source of decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from interval_edge_coloring import fixed_span_sat_solve, nauty_canonical_hash, rank_potential_solve
from neighborhood_graft_delta10_beyond_top94 import (
    append_event,
    counts_for,
    load_events,
    make_record,
    reconstruct,
    records_from_events,
)
from neighborhood_graft_delta10_search import Candidate, ranking_key


DEFAULT_INITIAL = Path("results/neighborhood-graft-delta10-agent/extension-full-roots")
DEFAULT_FIRST_CONTINUATION = Path("results/neighborhood-graft-delta10-agent/extension-beyond-top94")
DEFAULT_OUTPUT = Path("results/neighborhood-graft-delta10-agent/extension-beyond-top1094")
EXPECTED_GENERATED = 61375
EXPECTED_UNIQUE = 10950


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
    root_summaries: list[dict[str, Any]],
    validated_records: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
    target: int,
    selected_total: int,
    elapsed: float,
    status: str,
) -> None:
    validated_counts = counts_for(validated_records)
    new_counts = counts_for(new_records)
    all_counts = counts_for([*validated_records, *new_records])
    payload = {
        "schema_version": 1,
        "configuration": configuration,
        "generation": generation,
        "roots": root_summaries,
        "counts": {
            "generated": generation["generated"],
            "unique": generation["unique"],
            "previously_solved": validated_counts["classified"],
            "newly_solved": new_counts["classified"],
            "solved_total": all_counts["classified"],
            "colorable": all_counts["colorable"],
            "non_colorable": all_counts["non_colorable"],
            "timeout": all_counts["timeout"],
            "new_colorable": new_counts["colorable"],
            "new_non_colorable": new_counts["non_colorable"],
            "new_timeout": new_counts["timeout"],
            "target_new_classifications": target,
            "selected_new_candidates": selected_total,
            "remaining_selected": max(0, selected_total - new_counts["classified"]),
        },
        "integrity": {
            "initial_records_validated": 94,
            "first_continuation_records_validated": 1000,
            "previous_decisions_recomputed": 0,
            "state_is_append_only": True,
        },
        "status": status,
        "elapsed_seconds": elapsed,
        "recent_records": new_records[-128:],
        "confirmed_negatives": [record for record in new_records if record["decision"] == "non-colorable"],
    }
    atomic_write_json(output / "status.json", payload)
    atomic_write_json(output / "report.json", payload)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--initial", type=Path, default=DEFAULT_INITIAL)
    result.add_argument("--first-continuation", type=Path, default=DEFAULT_FIRST_CONTINUATION)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--target", type=int, default=1000)
    result.add_argument("--primary-time-limit", type=float, default=2.0)
    result.add_argument("--independent-time-limit", type=float, default=2.0)
    result.add_argument("--workers", type=int, default=4)
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
    initial_state = args.initial / "classification-state.jsonl"
    first_state = args.first_continuation / "classification-state.jsonl"
    initial_records = read_records(initial_state, "initial state")
    first_records = read_records(first_state, "first continuation state")
    if len(initial_records) != 94 or len(first_records) != 1000:
        raise RuntimeError(f"expected 94 initial and 1000 continuation decisions, found {len(initial_records)} and {len(first_records)}")

    candidates, generation, root_summaries, _ = reconstruct()
    if generation["generated"] != EXPECTED_GENERATED or generation["unique"] != EXPECTED_UNIQUE:
        raise RuntimeError(f"unexpected reconstruction counts: {generation!r}")
    by_digest = {candidate.digest: candidate for candidate in candidates}
    if len(by_digest) != len(candidates):
        raise RuntimeError("reconstruction produced duplicate canonical candidates")
    for record in initial_records:
        candidate = by_digest.get(record["canonical_sha256"])
        if candidate is None:
            raise RuntimeError("initial state references an unknown candidate")
        validate_record(record, candidate, "initial state")
    for record in first_records:
        candidate = by_digest.get(record["canonical_sha256"])
        if candidate is None:
            raise RuntimeError("first continuation references an unknown candidate")
        validate_record(record, candidate, "first continuation")

    validated_records = [*initial_records, *first_records]
    solved_digests = {record["canonical_sha256"] for record in validated_records}
    if len(solved_digests) != len(validated_records):
        raise RuntimeError("validated histories overlap")
    ranked = sorted(candidates, key=ranking_key)
    if {candidate.digest for candidate in ranked[:len(validated_records)]} != solved_digests:
        raise RuntimeError("previous histories are not exactly ranks 1 through 1094")
    selected = [candidate for candidate in ranked if candidate.digest not in solved_digests][:args.target]

    state_path = args.output / "classification-state.jsonl"
    existing_records = read_records(state_path, "current continuation state")
    selected_digests = {candidate.digest for candidate in selected}
    existing_digests = {record["canonical_sha256"] for record in existing_records}
    if not existing_digests <= selected_digests:
        raise RuntimeError("current continuation state does not match ranks 1095 onward")
    for record in existing_records:
        candidate = by_digest[record["canonical_sha256"]]
        validate_record(record, candidate, "current continuation state")
    if len(existing_records) > args.target:
        raise RuntimeError("current continuation state exceeds requested target")

    configuration = {
        "initial_state": str(initial_state),
        "initial_state_sha256": hashlib.sha256(initial_state.read_bytes()).hexdigest(),
        "first_continuation_state": str(first_state),
        "first_continuation_state_sha256": hashlib.sha256(first_state.read_bytes()).hexdigest(),
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
        append_event(state_path, {"event": "run-start", "configuration": configuration, "generation": generation, "previous_records_validated": len(validated_records), "selected_new_candidates": len(selected)})
    checkpoint(args.output, configuration, generation, root_summaries, validated_records, existing_records, args.target, len(selected), time.monotonic() - started, "running")

    new_records = list(existing_records)
    for rank, candidate in enumerate(selected, len(validated_records) + 1):
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
            checkpoint(args.output, configuration, generation, root_summaries, validated_records, new_records, args.target, len(selected), time.monotonic() - started, "running")

    checkpoint(args.output, configuration, generation, root_summaries, validated_records, new_records, args.target, len(selected), time.monotonic() - started, "complete")
    append_event(state_path, {"event": "run-complete", "elapsed_seconds": time.monotonic() - started, "counts": counts_for(new_records)})


if __name__ == "__main__":
    main()
