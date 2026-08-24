#!/usr/bin/env python3
"""Remote snapshot audit for an order-16 chunk result class."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REMOTE_MAPPER = r'''
import json, os, re, sys
from datetime import datetime, timezone

directory = os.path.join("results", sys.argv[1])
if not os.path.isdir(directory):
    raise SystemExit("missing remote directory: " + directory)

chunk_re = re.compile(r"chunk-(\d+)\.jsonl")
paths = []
for name in os.listdir(directory):
    match = chunk_re.fullmatch(name)
    if match:
        paths.append((int(match.group(1)), name))
paths.sort()
if not paths:
    raise SystemExit("no chunk-*.jsonl files in " + directory)

def utc(ns):
    value = datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat()
    return value.replace("+00:00", "Z")

def clean(value):
    return str(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")

for chunk_id, filename in paths:
    path = os.path.join(directory, filename)
    before = os.stat(path)
    remaining = before.st_size
    line_number = 0
    partial = b""
    with open(path, "rb") as handle:
        while remaining:
            block = handle.read(min(1 << 20, remaining))
            if not block:
                break
            remaining -= len(block)
            pieces = (partial + block).split(b"\n")
            partial = pieces.pop()
            for raw in pieces:
                line_number += 1
                if not raw.strip():
                    print(f"M\t{clean(filename)}\t{line_number}\tblank row")
                    continue
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    print(f"M\t{clean(filename)}\t{line_number}\tinvalid JSON: {clean(exc)}")
                    continue
                if not isinstance(row, dict):
                    print(f"M\t{clean(filename)}\t{line_number}\tnot a JSON object")
                    continue
                index = row.get("index")
                canonical = row.get("canonical_sha256")
                status = row.get("status")
                problems = []
                if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= 10 ** 20:
                    problems.append("invalid index")
                if not isinstance(canonical, str) or not canonical:
                    problems.append("invalid canonical_sha256")
                if not isinstance(status, str) or not status:
                    problems.append("invalid status")
                if problems:
                    print(f"M\t{clean(filename)}\t{line_number}\t{'; '.join(problems)}")
                    continue
                padded = f"{index:020d}"
                neg = "Y" if status in {"non-colorable", "non_colorable", "confirmed_non_colorable"} else "N"
                timed_out = "Y" if status == "timeout" else "N"
                print(f"H\t{clean(canonical)}\t{padded}")
                print(f"I\t{padded}\t{clean(status)}\t{neg}\t{timed_out}")
    if partial.strip():
        print(f"M\t{clean(filename)}\t{line_number + 1}\tincomplete final line at snapshot boundary")
    after = os.stat(path)
    item = {
        "chunk_id": chunk_id,
        "changed_during_scan": after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns,
        "end_bytes": after.st_size,
        "file": filename,
        "mtime_utc": utc(before.st_mtime_ns),
        "snapshot_bytes": before.st_size,
    }
    print("F\t" + json.dumps(item, sort_keys=True, separators=(",", ":")))
'''


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def merge_ranges(values: list[int]) -> list[list[int]]:
    ordered = sorted(set(values))
    result: list[list[int]] = []
    for value in ordered:
        if result and value == result[-1][1] + 1:
            result[-1][1] = value
        else:
            result.append([value, value])
    return result


def range_count(ranges: list[list[int]]) -> int:
    return sum(end - start + 1 for start, end in ranges)


def build_remote_script(dataset: str, remote_root: str, sort_memory: str) -> str:
    return f"""set -euo pipefail
cd {shlex.quote(remote_root)}
workdir=$(mktemp -d /tmp/order16-audit.XXXXXX)
trap 'rm -rf "$workdir"' EXIT
export LC_ALL=C
python3 - {shlex.quote(dataset)} <<'PY_AUDIT_MAPPER' | sort -S {shlex.quote(sort_memory)} -T "$workdir"
{REMOTE_MAPPER}
PY_AUDIT_MAPPER
"""


def remote_stream(script: str, remote: str, options: list[str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        ["ssh", *options, remote, "bash -s"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1 << 20,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(script)
        process.stdin.close()
    except BrokenPipeError:
        pass
    return process


def aggregate(process: subprocess.Popen[str], dataset: str, issue_limit: int, started: float) -> dict[str, Any]:
    rows = malformed_rows = unique_indices = duplicate_indices = duplicate_index_rows = 0
    unique_hashes = duplicate_hashes = duplicate_hash_rows = 0
    statuses: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    inventory: list[dict[str, Any]] = []
    invalid_examples: list[dict[str, Any]] = []
    previous_index: int | None = None
    minimum_index = maximum_index = None
    missing_ranges: list[list[int]] = []
    negatives: list[int] = []
    timeouts: list[int] = []
    previous_hash: str | None = None
    hash_occurrences = 0
    previous_is_duplicate = False

    assert process.stdout is not None
    for raw_line in process.stdout:
        kind, separator, payload = raw_line.rstrip("\n").partition("\t")
        if not separator:
            unknown["unstructured"] += 1
        elif kind == "F":
            try:
                item = json.loads(payload)
                if isinstance(item, dict):
                    inventory.append(item)
                else:
                    unknown["bad-inventory"] += 1
            except json.JSONDecodeError:
                unknown["bad-inventory"] += 1
        elif kind == "M":
            malformed_rows += 1
            if len(invalid_examples) < issue_limit:
                filename, sep1, rest = payload.partition("\t")
                number, sep2, reason = rest.partition("\t")
                invalid_examples.append({
                    "file": filename if sep1 else None,
                    "line_number": int(number) if sep2 and number.isdigit() else None,
                    "reason": reason if sep2 else payload,
                })
        elif kind == "H":
            canonical = payload.partition("\t")[0]
            if canonical == previous_hash:
                hash_occurrences += 1
            else:
                if previous_hash is not None:
                    unique_hashes += 1
                    duplicate_hash_rows += hash_occurrences - 1
                    duplicate_hashes += hash_occurrences > 1
                previous_hash, hash_occurrences = canonical, 1
        elif kind == "I":
            fields = payload.split("\t")
            if len(fields) != 4 or not fields[0].isdigit():
                unknown["bad-index"] += 1
                continue
            index = int(fields[0])
            rows += 1
            statuses[fields[1]] += 1
            if fields[2] == "Y":
                negatives.append(index)
            if fields[3] == "Y":
                timeouts.append(index)
            if minimum_index is None:
                minimum_index = maximum_index = index
                unique_indices += 1
            else:
                maximum_index = max(maximum_index, index)
                if index == previous_index:
                    duplicate_index_rows += 1
                    if not previous_is_duplicate:
                        duplicate_indices += 1
                        previous_is_duplicate = True
                else:
                    previous_is_duplicate = False
                    unique_indices += 1
                    if index > previous_index + 1:
                        missing_ranges.append([previous_index + 1, index - 1])
            previous_index = index
        else:
            unknown[kind] += 1

    if previous_hash is not None:
        unique_hashes += 1
        duplicate_hash_rows += hash_occurrences - 1
        duplicate_hashes += hash_occurrences > 1
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"remote audit pipeline exited with code {code}")
    if unknown:
        raise RuntimeError(f"unexpected remote records: {dict(unknown)}")

    span = 0 if minimum_index is None else maximum_index - minimum_index + 1
    summary: dict[str, Any] = {
        "schema": "order16-class-audit/v1",
        "dataset": dataset,
        "generated_at_utc": utc_now(),
        "audit_duration_seconds": round(time.monotonic() - started, 3),
        "files_scanned": len(inventory),
        "file_inventory": inventory,
        "rows": rows,
        "malformed_rows": malformed_rows,
        "unique_indices": unique_indices,
        "duplicate_indices": duplicate_indices,
        "duplicate_index_rows": duplicate_index_rows,
        "unique_hashes": unique_hashes,
        "duplicate_hashes": duplicate_hashes,
        "duplicate_hash_rows": duplicate_hash_rows,
        "status_histogram": dict(sorted(statuses.items())),
        "minimum_index": minimum_index,
        "maximum_index": maximum_index,
        "observed_index_span": span,
        "missing_index_ranges": missing_ranges,
        "missing_index_count": range_count(missing_ranges),
        "timeout_indices": sorted(set(timeouts)),
        "timeout_index_count": len(set(timeouts)),
        "negative_candidate_indices": sorted(set(negatives)),
        "negative_candidate_count": len(set(negatives)),
        "index_coverage_in_observed_range": None if not span else unique_indices / span,
        "invalid_examples": invalid_examples,
    }
    return summary


def add_expected(summary: dict[str, Any], expected: int | None) -> None:
    rows = summary["rows"]
    summary["expected_comparison"] = {
        "expected_records": expected,
        "rows_match_expected": None if expected is None else rows == expected,
        "rows_minus_expected": None if expected is None else rows - expected,
        "missing_rows_vs_expected": None if expected is None else max(0, expected - rows),
        "extra_rows_vs_expected": None if expected is None else max(0, rows - expected),
    }


def atomic_json(path: Path, value: dict[str, Any], indent: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=indent, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def fingerprint(summary: dict[str, Any]) -> tuple[Any, ...]:
    inventory = tuple((x["file"], x["snapshot_bytes"], x["mtime_utc"]) for x in summary["file_inventory"])
    core = tuple(summary[key] for key in (
        "rows", "malformed_rows", "unique_indices", "duplicate_index_rows",
        "unique_hashes", "duplicate_hash_rows", "status_histogram", "missing_index_ranges",
    ))
    return inventory, core


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--expected", type=int)
    parser.add_argument("--output", required=True)
    parser.add_argument("--remote", default="hrant@cluster.ysu.am")
    parser.add_argument("--remote-root", default="/mnt/weka/hrant/interval-search")
    parser.add_argument("--sort-memory", default="256M")
    parser.add_argument("--issue-limit", type=int, default=100)
    parser.add_argument("--indent", type=int, default=2)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--stable-scans", type=int, default=2)
    parser.add_argument("--deadline-seconds", type=float)
    args = parser.parse_args()
    if args.issue_limit < 0 or args.stable_scans < 1 or args.poll_seconds <= 0:
        parser.error("issue-limit >= 0, stable-scans >= 1, poll-seconds > 0 are required")
    return args


def main() -> None:
    args = parse_args()
    total_started = time.monotonic()
    ssh_options = ["-o", "BatchMode=yes"]
    script = build_remote_script(args.dataset, args.remote_root, args.sort_memory)
    scans = stable = 0
    previous_print: tuple[Any, ...] | None = None
    latest: dict[str, Any] | None = None

    while True:
        scan_started = time.monotonic()
        latest = aggregate(remote_stream(script, args.remote, ssh_options), args.dataset, args.issue_limit, scan_started)
        scans += 1
        current_print = fingerprint(latest)
        stable = stable + 1 if current_print == previous_print else 1
        previous_print = current_print
        reached_expected = args.expected is not None and latest["rows"] == args.expected
        unchanged = not any(x["changed_during_scan"] for x in latest["file_inventory"])
        if not args.live or (stable >= args.stable_scans and unchanged) or (reached_expected and unchanged):
            break
        if args.deadline_seconds is not None and time.monotonic() - total_started >= args.deadline_seconds:
            latest["live_polling_stopped"] = "deadline-reached"
            break
        time.sleep(args.poll_seconds)

    assert latest is not None
    latest.update({
        "mode": "live-snapshot" if args.live else "snapshot",
        "scan_count": scans,
        "stable_scans_observed": stable,
        "remote": args.remote,
        "remote_root": args.remote_root,
        "total_duration_seconds": round(time.monotonic() - total_started, 3),
    })
    add_expected(latest, args.expected)
    atomic_json(Path(args.output), latest, None if args.indent == 0 else args.indent)
    print(json.dumps({
        "dataset": latest["dataset"],
        "files": latest["files_scanned"],
        "expected_matches": latest["expected_comparison"]["rows_match_expected"],
        "malformed_rows": latest["malformed_rows"],
        "missing_index_count": latest["missing_index_count"],
        "output": args.output,
        "rows": latest["rows"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
