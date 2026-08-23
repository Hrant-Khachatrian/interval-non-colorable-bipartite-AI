#!/usr/bin/env python3
"""Stream JSONL search rows and report duplicate graph indices."""

import argparse
import glob
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pattern")
    args = parser.parse_args()
    paths = glob.glob(args.pattern)
    if not paths:
        raise SystemExit(f"no files match {args.pattern}")
    seen = set()
    duplicates = []
    rows = 0
    for path in paths:
        with open(path) as handle:
            for line in handle:
                rows += 1
                index = json.loads(line)["index"]
                if index in seen:
                    duplicates.append(index)
                else:
                    seen.add(index)
    print("files", len(paths))
    print("rows", rows)
    print("unique_indices", len(seen))
    print("duplicate_rows", len(duplicates))
    print("duplicate_indices", sorted(set(duplicates))[:100])


if __name__ == "__main__":
    main()
