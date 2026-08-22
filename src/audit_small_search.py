#!/usr/bin/env python3
"""Audit JSONL rows emitted by search_small_bipartite.py."""

import argparse
import collections
import glob
import json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pattern", nargs="+")
    args = parser.parse_args()

    paths = []
    for pattern in args.pattern:
        paths.extend(glob.glob(pattern))
    if not paths:
        raise SystemExit(f"no files match {args.pattern!r}")
    rows = []
    for path in paths:
        with open(path) as handle:
            rows.extend(json.loads(line) for line in handle)
    print("files", len(paths))
    print("rows", len(rows))
    print("unique_indices", len({row["index"] for row in rows}))
    print("unique_canonical", len({row["canonical_sha256"] for row in rows}))
    print("statuses", json.dumps(collections.Counter(row["status"] for row in rows), sort_keys=True))
    print("orders", sorted({row["order"] for row in rows}))
    print("size_range", min(row["size"] for row in rows), max(row["size"] for row in rows))
    print(
        "degree_range",
        min(row["minimum_degree"] for row in rows),
        max(row["delta"] for row in rows),
    )
    print("max_solver_seconds", max(row["solver_seconds"] for row in rows))


if __name__ == "__main__":
    main()
