#!/usr/bin/env python3
"""Summarize exact timeout rows in completed order-15 result sets."""

import argparse
import collections
import glob
import json
import os


SOURCES = {
    "order15-small": "data/order15-small-combined.g6",
    "order15-5x10": "data/order15-5x10-d2.g6",
    "order15-6x9": "data/order15-6x9-d2.g6",
}


def chunk_number(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return int(stem.split("-", 1)[1])


def scan_root(root, status="timeout"):
    files = sorted(glob.glob(os.path.join(root, "chunk-*.jsonl")), key=chunk_number)
    statuses = collections.Counter()
    timeouts = []
    bad_rows = 0
    for path in files:
        with open(path, encoding="utf-8") as handle:
            for line_number, line in enumerate(handle):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    bad_rows += 1
                    continue
                row_status = row.get("status")
                statuses[row_status] += 1
                if row_status == status:
                    timeouts.append(
                        {
                            "dataset": os.path.basename(root),
                            "index": row.get("index"),
                            "canonical_sha256": row.get("canonical_sha256"),
                            "source_file": os.path.basename(path),
                            "line_number": line_number,
                        }
                    )
    by_index = {}
    for row in timeouts:
        by_index.setdefault(row["index"], []).append(row)
    unique = []
    for index in sorted(by_index):
        locations = by_index[index]
        first = dict(locations[0])
        first["duplicate_locations"] = [
            f"{item['source_file']}:{item['line_number']}" for item in locations[1:]
        ]
        unique.append(first)
    return {
        "root": root,
        "files": len(files),
        "rows": sum(statuses.values()),
        "statuses": dict(sorted(statuses.items(), key=lambda item: str(item[0]))),
        "invalid_json_rows": bad_rows,
        "timeout_rows": len(timeouts),
        "unique_timeout_indices": len(unique),
        "timeouts": unique,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--results-root", default="/mnt/weka/hrant/interval-search/results"
    )
    parser.add_argument("--output")
    args = parser.parse_args()
    summaries = [
        scan_root(os.path.join(args.results_root, dataset)) for dataset in SOURCES
    ]
    payload = {
        "summaries": summaries,
        "source_mapping": SOURCES,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
