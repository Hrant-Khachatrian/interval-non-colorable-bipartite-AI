#!/usr/bin/env python3
"""Exact audit for a fully generated small order-17 class on the cluster."""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import tempfile
from pathlib import Path


REQUIRED = {"index", "canonical_sha256", "order", "size", "delta", "minimum_degree", "status", "span", "solver_seconds"}
VALID = {"colorable", "non-colorable", "timeout", "regular-skipped"}


def atomic_json(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("n1", type=int)
    parser.add_argument("n2", type=int)
    parser.add_argument("--root", type=Path, default=Path("/mnt/weka/hrant/interval-search"))
    args = parser.parse_args()
    if args.n1 + args.n2 != 17 or args.n1 > args.n2:
        parser.error("require a canonical order-17 side split")

    root = args.root
    data = root / "data" / f"order17-{args.n1}x{args.n2}-d2-shards"
    run = root / "results" / "order17-census" / f"classification-{args.n1}x{args.n2}"
    manifest = json.loads((data / "manifest.json").read_text(encoding="ascii"))
    total, shard_size, shards = (int(manifest[key]) for key in ("records", "shard_size", "shards"))
    digits = max(5, len(str(shards)))

    seen_indices: set[int] = set()
    seen_hashes: set[str] = set()
    duplicate_indices = duplicate_hashes = malformed = out_of_range = 0
    statuses: collections.Counter[str] = collections.Counter()
    primary_negatives: set[int] = set()
    timeout_indices: set[int] = set()
    missing_chunks = []
    confirmation_records: dict[int, str] = {}

    for shard in range(shards):
        name = f"chunk-{shard:0{digits}d}.jsonl"
        path = run / name
        if not path.exists():
            missing_chunks.append(shard)
            continue
        start = shard * shard_size
        stop = min(total, start + shard_size)
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(row, dict) or not REQUIRED.issubset(row):
                    malformed += 1
                    continue
                index, digest, status = row["index"], row["canonical_sha256"], row["status"]
                if not isinstance(index, int) or not start <= index < stop or not isinstance(digest, str) or status not in VALID:
                    out_of_range += 1
                    continue
                if index in seen_indices:
                    duplicate_indices += 1
                seen_indices.add(index)
                if digest in seen_hashes:
                    duplicate_hashes += 1
                seen_hashes.add(digest)
                statuses[status] += 1
                if status == "non-colorable":
                    primary_negatives.add(index)
                if status == "timeout":
                    timeout_indices.add(index)
        confirmation = run / "confirmations" / f"chunk-{shard:0{digits}d}.json"
        if confirmation.exists():
            item = json.loads(confirmation.read_text(encoding="ascii"))
            for row in item.get("records", []):
                if isinstance(row, dict) and isinstance(row.get("index"), int):
                    confirmation_records[row["index"]] = row.get("confirmation", {}).get("status", row.get("status", ""))

    missing_indices = total - len(seen_indices)
    unconfirmed = sorted(index for index in primary_negatives if confirmation_records.get(index) != "confirmed_noncolorable")
    summary = {
        "schema_version": 1,
        "dataset": f"order17-{args.n1}x{args.n2}-d2",
        "expected_records": total,
        "files_expected": shards,
        "files_missing": missing_chunks,
        "rows_unique_by_index": len(seen_indices),
        "unique_canonical_hashes": len(seen_hashes),
        "missing_indices": missing_indices,
        "duplicate_index_rows": duplicate_indices,
        "duplicate_canonical_hash_rows": duplicate_hashes,
        "malformed_rows": malformed,
        "out_of_range_or_invalid_rows": out_of_range,
        "status_counts": dict(sorted(statuses.items())),
        "timeout_indices": sorted(timeout_indices),
        "primary_negative_indices": sorted(primary_negatives),
        "confirmed_negative_indices": sorted(index for index in primary_negatives if confirmation_records.get(index) == "confirmed_noncolorable"),
        "unconfirmed_primary_negative_indices": unconfirmed,
        "coverage_complete": not missing_chunks and missing_indices == 0 and malformed == 0 and out_of_range == 0 and duplicate_indices == 0 and duplicate_hashes == 0,
        "negative_policy_satisfied": not unconfirmed,
        "timeout_policy_satisfied": not timeout_indices,
    }
    output = root / "results" / "order17-census" / f"audit-{args.n1}x{args.n2}.json"
    atomic_json(output, summary)
    print(json.dumps(summary, sort_keys=True))
    if not (summary["coverage_complete"] and summary["negative_policy_satisfied"] and summary["timeout_policy_satisfied"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
