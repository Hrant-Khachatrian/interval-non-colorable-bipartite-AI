#!/usr/bin/env python3
"""Memory-bounded completeness and uniqueness audit for search JSONL rows."""

import argparse
import collections
import glob
import json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pattern")
    args = parser.parse_args()
    paths = glob.glob(args.pattern)
    if not paths:
        raise SystemExit(f"no files match {args.pattern}")

    indices = set()
    hashes = set()
    statuses = collections.Counter()
    duplicate_indices = 0
    rows = 0
    minimum_degree = None
    maximum_degree = None
    minimum_size = None
    maximum_size = None
    maximum_solver_seconds = 0.0

    for path in paths:
        with open(path) as handle:
            for line in handle:
                row = json.loads(line)
                index = row["index"]
                if index in indices:
                    duplicate_indices += 1
                else:
                    indices.add(index)
                hashes.add(row["canonical_sha256"])
                statuses[row["status"]] += 1
                rows += 1
                minimum_degree = (
                    row["minimum_degree"]
                    if minimum_degree is None
                    else min(minimum_degree, row["minimum_degree"])
                )
                maximum_degree = (
                    row["delta"]
                    if maximum_degree is None
                    else max(maximum_degree, row["delta"])
                )
                minimum_size = (
                    row["size"] if minimum_size is None else min(minimum_size, row["size"])
                )
                maximum_size = (
                    row["size"] if maximum_size is None else max(maximum_size, row["size"])
                )
                maximum_solver_seconds = max(
                    maximum_solver_seconds, float(row["solver_seconds"])
                )

    print("files", len(paths))
    print("rows", rows)
    print("unique_indices", len(indices))
    print("duplicate_indices", duplicate_indices)
    print("unique_canonical", len(hashes))
    print("statuses", json.dumps(statuses, sort_keys=True))
    print("degree_range", minimum_degree, maximum_degree)
    print("size_range", minimum_size, maximum_size)
    print("max_sweep_solver_seconds", maximum_solver_seconds)


if __name__ == "__main__":
    main()
