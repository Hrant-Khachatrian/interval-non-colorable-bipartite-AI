#!/usr/bin/env python3
"""Incremental exact-coverage audit for order16-7x9 chunk outputs."""

import argparse
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

TOTAL = 3_604_370_591
CHUNKS = 8_192
CHUNK_SIZE = 439_987
VALID_STATUSES = {"colorable", "non-colorable", "timeout", "regular-skipped"}
REQUIRED_FIELDS = {
    "index",
    "canonical_sha256",
    "order",
    "size",
    "delta",
    "minimum_degree",
    "status",
    "span",
    "solver_seconds",
}


def expected_range(chunk_id: int) -> tuple[int, int]:
    start = chunk_id * CHUNK_SIZE
    return start, min(TOTAL, start + CHUNK_SIZE)


def bounded(values, limit=1000):
    return sorted(values)[:limit]


def scan_chunk(path: Path, chunk_id: int) -> dict:
    start, stop = expected_range(chunk_id)
    rows = {}
    raw_lines = 0
    malformed = []
    schema_invalid = []
    out_of_range = []
    bad_status = []
    hash_counts = Counter()

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            raw_lines += 1
            try:
                row = json.loads(line)
            except Exception as exc:
                if len(malformed) < 100:
                    malformed.append({"line": line_number, "error": str(exc)})
                continue
            if not isinstance(row, dict) or not REQUIRED_FIELDS.issubset(row):
                if len(schema_invalid) < 100:
                    schema_invalid.append(line_number)
                continue
            index = row.get("index")
            status = row.get("status")
            digest = row.get("canonical_sha256")
            if not isinstance(index, int) or not start <= index < stop:
                if len(out_of_range) < 100:
                    out_of_range.append({"line": line_number, "index": index})
                continue
            if status not in VALID_STATUSES:
                if len(bad_status) < 100:
                    bad_status.append({"line": line_number, "status": status})
            rows[index] = row
            if isinstance(digest, str):
                hash_counts[digest] += 1

    status_counts = Counter(row["status"] for row in rows.values())
    duplicate_hashes = sum(count - 1 for count in hash_counts.values() if count > 1)
    negative = [index for index, row in rows.items() if row.get("status") == "non-colorable"]
    timeout = [index for index, row in rows.items() if row.get("status") == "timeout"]
    return {
        "expected_count": stop - start,
        "start": start,
        "stop_exclusive": stop,
        "raw_lines": raw_lines,
        "unique_indices": len(rows),
        "superseded_or_duplicate_valid_rows": max(0, raw_lines - len(malformed) - len(schema_invalid) - len(out_of_range) - len(rows)),
        "status_counts": dict(sorted(status_counts.items())),
        "minimum_index": min(rows) if rows else None,
        "maximum_index": max(rows) if rows else None,
        "missing_count": (stop - start) - len(rows),
        "malformed_json_rows": len(malformed),
        "schema_invalid_rows": len(schema_invalid),
        "out_of_range_rows": len(out_of_range),
        "bad_status_rows": len(bad_status),
        "duplicate_canonical_hash_rows": duplicate_hashes,
        "primary_negative_count": len(negative),
        "primary_negative_examples": bounded(negative),
        "timeout_count_latest_per_index": len(timeout),
        "timeout_examples": bounded(timeout),
        "examples": {
            "malformed_json": malformed,
            "schema_invalid_lines": bounded(schema_invalid, 100),
            "out_of_range": out_of_range,
            "bad_status": bad_status,
        },
        "bytes": path.stat().st_size,
        "mtime": path.stat().st_mtime,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="/mnt/weka/hrant/interval-search/results/order16-7x9")
    parser.add_argument("--state", default="/mnt/weka/hrant/interval-search/results/order16-7x9/.worker-audit-state.json")
    parser.add_argument("--grace-seconds", type=float, default=120.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    results = Path(args.results)
    state_path = Path(args.state)
    state = {"schema_version": 1, "chunks": {}}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text())
            if loaded.get("schema_version") == 1:
                state = loaded
        except Exception:
            state = {"schema_version": 1, "chunks": {}}

    chunk_states = state.setdefault("chunks", {})
    pattern = re.compile(r"^chunk-(\d+)\.jsonl$")
    now = time.time()
    scanned_now = []
    for path in results.glob("chunk-*.jsonl"):
        match = pattern.match(path.name)
        if not match:
            continue
        chunk_id = int(match.group(1))
        if chunk_id >= CHUNKS:
            continue
        stat = path.stat()
        key = str(chunk_id)
        previous = chunk_states.get(key)
        unchanged = (
            previous
            and previous.get("bytes") == stat.st_size
            and previous.get("mtime") == stat.st_mtime
        )
        if unchanged and not args.force:
            continue
        if not args.force and now - stat.st_mtime < args.grace_seconds:
            continue
        chunk_states[key] = scan_chunk(path, chunk_id)
        scanned_now.append(chunk_id)

    summaries = []
    for chunk_id in range(CHUNKS):
        item = chunk_states.get(str(chunk_id))
        if item:
            summaries.append(item)

    status_totals = Counter()
    for item in summaries:
        status_totals.update(item.get("status_counts", {}))
    missing_count = sum(item.get("missing_count", 0) for item in summaries)
    malformed_count = sum(item.get("malformed_json_rows", 0) for item in summaries)
    schema_invalid_count = sum(item.get("schema_invalid_rows", 0) for item in summaries)
    out_of_range_count = sum(item.get("out_of_range_rows", 0) for item in summaries)
    bad_status_count = sum(item.get("bad_status_rows", 0) for item in summaries)
    duplicate_hash_count = sum(item.get("duplicate_canonical_hash_rows", 0) for item in summaries)
    raw_rows = sum(item.get("raw_lines", 0) for item in summaries)
    unique_indices = sum(item.get("unique_indices", 0) for item in summaries)
    chunks_with_missing = [
        int(key) for key, item in chunk_states.items() if item.get("missing_count", 0) > 0
    ]
    primary_negatives = []
    timeout_examples = []
    for key in sorted(chunk_states, key=int):
        item = chunk_states[key]
        if item.get("primary_negative_count"):
            primary_negatives.extend(item.get("primary_negative_examples", []))
        if item.get("timeout_count_latest_per_index"):
            timeout_examples.extend(item.get("timeout_examples", []))

    summary = {
        "audit_schema_version": 1,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "expected_total": TOTAL,
        "chunks_expected": CHUNKS,
        "chunks_audited": len(summaries),
        "chunks_scanned_this_pass": len(scanned_now),
        "raw_rows_seen": raw_rows,
        "classified_unique_indices": unique_indices,
        "missing_indices": missing_count,
        "remaining_records": max(0, TOTAL - unique_indices),
        "progress_fraction": round(unique_indices / TOTAL, 12),
        "latest_per_index_status_counts": dict(sorted(status_totals.items())),
        "timeout_unresolved": status_totals.get("timeout", 0),
        "primary_noncolorable_rows": status_totals.get("non-colorable", 0),
        "primary_negative_examples": bounded(primary_negatives),
        "timeout_examples": bounded(timeout_examples),
        "chunks_with_missing_count": len(chunks_with_missing),
        "chunks_with_missing_examples": bounded(chunks_with_missing),
        "malformed_json_rows": malformed_count,
        "schema_invalid_rows": schema_invalid_count,
        "out_of_range_rows": out_of_range_count,
        "bad_status_rows": bad_status_count,
        "within_chunk_duplicate_canonical_hash_rows": duplicate_hash_count,
        "coverage_complete": (
            len(summaries) == CHUNKS
            and unique_indices == TOTAL
            and missing_count == 0
            and malformed_count == 0
            and schema_invalid_count == 0
            and out_of_range_count == 0
            and bad_status_count == 0
        ),
    }

    state["last_summary"] = summary
    temporary = state_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n")
    os.replace(temporary, state_path)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
