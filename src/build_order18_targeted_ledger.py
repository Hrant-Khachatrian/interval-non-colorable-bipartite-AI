#!/usr/bin/env python3
"""Build a compact, non-mutating coverage ledger for the order-18 queue."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from order18_targeted_search import generate_candidates


QUEUE_SIZE = 12_987
LEDGER_ROOT = Path("results/order18-targeted-ledger")


@dataclass(frozen=True)
class Slice:
    name: str
    window: tuple[int, int]
    source: Path
    source_type: str
    state: str


SLICES = (
    Slice("v3", (1, 500), Path("results/order18-targeted-v3.json"), "report", "authoritative"),
    Slice("v4", (501, 2500), Path("results/order18-targeted-v4/classification-events.jsonl"), "events", "authoritative"),
    Slice("v5", (2501, 4500), Path("results/order18-targeted-v5/classification-events.jsonl"), "events", "authoritative"),
    Slice("v6", (4501, 6500), Path("results/order18-targeted-v6/classification-events.jsonl"), "events", "authoritative"),
    Slice("v7", (6501, 8500), Path("results/order18-targeted-v7/classification-events.jsonl"), "events", "authoritative"),
    Slice("v8", (8501, 10500), Path("results/order18-targeted-v8/classification-events.jsonl"), "events", "provisional_active"),
    Slice("final-tail", (10501, 12987), Path("results/order18-targeted-final-tail/classification-events.jsonl"), "events", "prepared_unstarted"),
)


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


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def rank_for(row: dict) -> int:
    rank = row.get("rank")
    if isinstance(rank, int):
        return rank
    candidate_id = row.get("candidate_id", "")
    if candidate_id.startswith("O18-") and candidate_id[4:].isdigit():
        return int(candidate_id[4:]) + 1
    raise ValueError(f"cannot infer one-based rank from {candidate_id!r}")


def ranges(values: set[int]) -> list[dict[str, int]]:
    if not values:
        return []
    ordered = sorted(values)
    result = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value != previous + 1:
            result.append({"start": start, "end": previous})
            start = value
        previous = value
    result.append({"start": start, "end": previous})
    return result


def compact_event_rows(path: Path) -> tuple[Iterator[tuple[int, dict]], dict, list[str]]:
    """Stream completion events; no complete event log is retained in memory."""
    metadata = {
        "path": str(path),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else 0,
        "lines_scanned": 0,
        "completion_events": 0,
    }
    issues: list[str] = []

    def read() -> Iterator[tuple[int, dict]]:
        if not path.exists():
            return
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                metadata["lines_scanned"] += 1
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # An active writer can be observed before it has emitted its newline.
                    if line_number and not line.endswith("\n"):
                        metadata["trailing_partial_line_ignored"] = True
                        return
                    issues.append(f"invalid_json_event_line:{line_number}")
                    continue
                if event.get("event") != "classification_completed":
                    continue
                row = event.get("row")
                if not isinstance(row, dict):
                    issues.append(f"completion_event_without_row:{line_number}")
                    continue
                metadata["completion_events"] += 1
                yield line_number, row

    return read(), metadata, issues


def source_rows(slice_: Slice) -> tuple[Iterator[tuple[int, dict]], dict, list[str]]:
    if slice_.source_type == "events":
        return compact_event_rows(slice_.source)
    metadata = {"path": str(slice_.source), "exists": slice_.source.exists()}
    issues: list[str] = []
    rows: list[dict] = []
    if slice_.source.exists():
        try:
            rows = json.loads(slice_.source.read_text(encoding="utf-8")).get("rows", [])
        except (json.JSONDecodeError, OSError) as exc:
            issues.append(f"invalid_report:{type(exc).__name__}")
    else:
        issues.append("missing_report")
    metadata["completion_events"] = len(rows)
    metadata["lines_scanned"] = None
    return iter(enumerate(rows, start=1)), metadata, issues


def reconstruct_queue() -> tuple[dict[int, str], dict, str]:
    args = argparse.Namespace(
        lanes="all",
        candidate_cap=QUEUE_SIZE,
        rank_start=0,
        max_additions=1,
        max_deleted_degree=3,
        max_rewires=750,
        extension_limit=18,
    )
    selected, raw_lanes, generated_lanes, selected_lanes, diagnostics = generate_candidates(args)
    queue = {rank: item[4] for rank, item in enumerate(selected, start=1)}
    if len(queue) != QUEUE_SIZE:
        raise ValueError(f"expected {QUEUE_SIZE} ranked graphs, reconstructed {len(queue)}")
    queue_manifest = hashlib.sha256(
        "".join(f"{rank}:{digest}\n" for rank, digest in queue.items()).encode("ascii")
    ).hexdigest()
    diagnostics.update({
        "generated_raw_by_lane": dict(sorted(raw_lanes.items())),
        "unique_ranked_by_lane": dict(sorted(generated_lanes.items())),
        "selected_by_lane": dict(sorted(selected_lanes.items())),
    })
    return queue, diagnostics, queue_manifest


def reconcile_slice(slice_: Slice, queue: dict[int, str]) -> tuple[dict, dict[int, str], list[str]]:
    entries, source, issues = source_rows(slice_)
    start, end = slice_.window
    expected = set(range(start, end + 1))
    ranks: dict[int, str] = {}
    hashes: set[str] = set()
    duplicate_ranks: set[int] = set()
    duplicate_hashes: set[str] = set()
    unexpected_ranks: set[int] = set()
    mismatched: set[int] = set()
    status_counts: collections.Counter[str] = collections.Counter()

    for line_number, row in entries:
        try:
            rank = rank_for(row)
        except ValueError as exc:
            issues.append(f"rank_error_at_record:{line_number}:{exc}")
            continue
        digest = row.get("canonical_sha256")
        if not isinstance(digest, str):
            issues.append(f"missing_canonical_hash_at_record:{line_number}")
            continue
        if rank in ranks:
            duplicate_ranks.add(rank)
        if digest in hashes:
            duplicate_hashes.add(digest)
        ranks[rank] = digest
        hashes.add(digest)
        status_counts[str(row.get("status", "missing"))] += 1
        if rank not in expected:
            unexpected_ranks.add(rank)
        if queue.get(rank) != digest:
            mismatched.add(rank)

    observed = set(ranks)
    missing = expected - observed
    complete_window = observed == expected and not duplicate_ranks and not duplicate_hashes and not unexpected_ranks and not mismatched
    status = "complete" if slice_.state == "authoritative" and complete_window and not issues else slice_.state
    if slice_.state == "authoritative" and status != "complete":
        status = "integrity_failure"
    result = {
        "name": slice_.name,
        "state": status,
        "configured_state": slice_.state,
        "rank_window_one_based": [start, end],
        "expected_count": len(expected),
        "durable_classified_count": len(observed),
        "durable_canonical_hash_count": len(hashes),
        "status_counts": dict(sorted(status_counts.items())),
        "source": source,
        "canonical_hash_coverage": {
            "unique_within_slice": not duplicate_hashes,
            "rank_hash_mismatch_count": len(mismatched),
            "rank_hash_mismatch_ranges": ranges(mismatched),
        },
        "duplicate_rank_ranges": ranges(duplicate_ranks),
        "duplicate_canonical_hash_count": len(duplicate_hashes),
        "unexpected_rank_ranges": ranges(unexpected_ranks),
        "unresolved_rank_count": len(missing),
        "unresolved_rank_ranges": ranges(missing),
        "integrity_issues": issues,
        "window_reconciled": complete_window and not issues,
    }
    return result, ranks, issues


def sample_checks(name: str, window: tuple[int, int], rows: dict[int, str], queue: dict[int, str]) -> dict:
    start, end = window
    samples = sorted({start, (start + end) // 2, end})
    checks = [
        {
            "rank": rank,
            "expected_canonical_sha256": queue[rank],
            "recorded_canonical_sha256": rows.get(rank),
            "matches": rows.get(rank) == queue[rank],
        }
        for rank in samples
    ]
    return {"slice": name, "checks": checks, "passed": all(item["matches"] for item in checks)}


def markdown(ledger: dict) -> str:
    coverage = ledger["coverage"]
    lines = [
        "# Order-18 Targeted Coverage Ledger",
        "",
        f"Queue: {coverage['queue_size']} unique ranked graphs. Authoritative coverage: {coverage['authoritative_covered_count']}; authoritative uncovered: {coverage['authoritative_uncovered_count']}. Durable provisional coverage: {coverage['provisional_durable_covered_count']}.",
        "",
        "| Slice | Window | State | Durable rows | Unresolved | Hash check |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for slice_ in ledger["slices"]:
        window = slice_["rank_window_one_based"]
        hash_check = "ok" if slice_["canonical_hash_coverage"]["rank_hash_mismatch_count"] == 0 else "mismatch"
        lines.append(
            f"| {slice_['name']} | {window[0]}-{window[1]} | {slice_['state']} | {slice_['durable_classified_count']} | {slice_['unresolved_rank_count']} | {hash_check} |"
        )
    lines.extend(["", "## Integrity", ""])
    if ledger["integrity"]["issues"]:
        lines.extend(f"- {issue}" for issue in ledger["integrity"]["issues"])
    else:
        lines.append("No rank, canonical-hash, overlap, duplicate, or completed-slice sample-check integrity issues.")
    lines.extend(["", "## Next Disjoint Windows", ""])
    for item in ledger["next_disjoint_windows"]:
        windows = ", ".join(f"{part['start']}-{part['end']}" for part in item["rank_ranges"])
        lines.append(f"- {item['slice']} ({item['state']}): {windows or 'none'}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=LEDGER_ROOT)
    args = parser.parse_args()

    queue, queue_diagnostics, manifest = reconstruct_queue()
    slices = []
    row_maps: dict[str, dict[int, str]] = {}
    integrity: list[str] = []
    all_ranks: dict[int, str] = {}
    all_hashes: dict[str, str] = {}
    rank_overlaps: list[dict] = []
    canonical_hash_overlaps: list[dict] = []
    for slice_ in SLICES:
        summary, rows, issues = reconcile_slice(slice_, queue)
        slices.append(summary)
        row_maps[slice_.name] = rows
        for issue in issues:
            integrity.append(f"{slice_.name}:{issue}")
        for rank, digest in rows.items():
            owner = all_ranks.get(rank)
            if owner is not None and owner != slice_.name:
                integrity.append(f"rank_overlap:{rank}:{owner}:{slice_.name}")
                rank_overlaps.append({"rank": rank, "first_slice": owner, "second_slice": slice_.name})
            all_ranks[rank] = slice_.name
            hash_owner = all_hashes.get(digest)
            if hash_owner is not None and hash_owner != slice_.name:
                integrity.append(f"canonical_hash_overlap:{digest}:{hash_owner}:{slice_.name}")
                canonical_hash_overlaps.append({
                    "canonical_sha256": digest,
                    "first_slice": hash_owner,
                    "second_slice": slice_.name,
                })
            all_hashes[digest] = slice_.name
        if slice_.state == "authoritative" and not summary["window_reconciled"]:
            integrity.append(f"completed_slice_not_reconciled:{slice_.name}")

    samples = [
        sample_checks(slice_.name, slice_.window, row_maps[slice_.name], queue)
        for slice_ in SLICES if slice_.state == "authoritative"
    ]
    for sample in samples:
        if not sample["passed"]:
            integrity.append(f"sample_check_failed:{sample['slice']}")

    authoritative_ranks = set().union(*(set(row_maps[item.name]) for item in SLICES if item.state == "authoritative"))
    provisional_ranks = set(row_maps["v8"])
    final_tail = next(item for item in slices if item["name"] == "final-tail")
    v8 = next(item for item in slices if item["name"] == "v8")
    ledger = {
        "schema_version": 1,
        "purpose": "Cumulative coverage ledger only; classification is never rerun by this builder.",
        "queue": {
            "size": QUEUE_SIZE,
            "reconstructed_from": "src/order18_targeted_search.py",
            "configuration": {
                "lanes": "all", "candidate_cap": QUEUE_SIZE, "rank_start": 0,
                "max_additions": 1, "max_deleted_degree": 3, "max_rewires": 750, "extension_limit": 18,
            },
            "unique_canonical_hash_count": len(set(queue.values())),
            "rank_to_canonical_hash_manifest_sha256": manifest,
            "generation": queue_diagnostics,
        },
        "coverage": {
            "queue_size": QUEUE_SIZE,
            "authoritative_covered_count": len(authoritative_ranks),
            "authoritative_uncovered_count": QUEUE_SIZE - len(authoritative_ranks),
            "provisional_durable_covered_count": len(provisional_ranks),
            "observed_covered_count_including_provisional": len(authoritative_ranks | provisional_ranks),
            "observed_uncovered_count_including_provisional": QUEUE_SIZE - len(authoritative_ranks | provisional_ranks),
            "authoritative_uncovered_rank_ranges": ranges(set(queue) - authoritative_ranks),
        },
        "slices": slices,
        "cross_slice_reconciliation": {
            "rank_overlap_count": len(rank_overlaps),
            "rank_overlaps": rank_overlaps,
            "canonical_hash_overlap_count": len(canonical_hash_overlaps),
            "canonical_hash_overlaps": canonical_hash_overlaps,
        },
        "completed_slice_rank_hash_sample_checks": samples,
        "next_disjoint_windows": [
            {"slice": "v8", "state": "active_provisional", "rank_ranges": v8["unresolved_rank_ranges"]},
            {"slice": "final-tail", "state": "prepared_unstarted", "rank_ranges": final_tail["unresolved_rank_ranges"]},
        ],
        "integrity": {"ok": not integrity, "issue_count": len(integrity), "issues": integrity},
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output_dir / "coverage.json", ledger)
    atomic_text(args.output_dir / "coverage.md", markdown(ledger))
    print(json.dumps({"coverage": ledger["coverage"], "integrity": ledger["integrity"]}, indent=2))


if __name__ == "__main__":
    main()
