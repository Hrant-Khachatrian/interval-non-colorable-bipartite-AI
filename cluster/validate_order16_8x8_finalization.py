#!/usr/bin/env python3
"""Independent, read-only gate validator for the order-16 8+8 dataset.

The finalizer writes the generation manifest.  This program independently
recomputes the source checks and, once shards exist, audits their aggregate
properties before classification may proceed.  It only writes a compact report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any


RECORD_WIDTH = 22
SAMPLE_COUNT = 257
MINIMUM_HEADROOM = 2 * 1024**4
EXPECTED_DATASET = "order16-8x8-d2to11"
HEX256 = re.compile(r"^[0-9a-f]{64}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def count_lines(path: Path) -> int:
    # Streaming byte counts keep memory bounded and independently reproduce wc -l.
    total = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            total += block.count(b"\n")
    return total


def source_samples(path: Path, records: int) -> dict[str, Any]:
    if records < 1:
        return {"sampled_records": 0, "bad_records": 1, "bad_positions": []}
    rng = random.Random(1608)
    positions = [0, records - 1] + [rng.randrange(records) for _ in range(SAMPLE_COUNT - 2)]
    bad_positions: list[int] = []
    with path.open("rb") as handle:
        for position in positions:
            handle.seek(position * RECORD_WIDTH)
            record = handle.read(RECORD_WIDTH)
            if len(record) != RECORD_WIDTH or not record.startswith(b"O") or not record.endswith(b"\n"):
                bad_positions.append(position)
    return {
        "sampled_records": len(positions),
        "bad_records": len(bad_positions),
        "bad_positions": bad_positions[:10],
        "invariant": "22 bytes; starts O; ends LF",
    }


def stable_stat(path: Path, seconds: int) -> tuple[bool, dict[str, int]]:
    first = path.stat()
    if seconds:
        time.sleep(seconds)
    second = path.stat()
    values = {
        "bytes_before": first.st_size,
        "mtime_ns_before": first.st_mtime_ns,
        "bytes_after": second.st_size,
        "mtime_ns_after": second.st_mtime_ns,
    }
    return (first.st_size, first.st_mtime_ns) == (second.st_size, second.st_mtime_ns), values


def add_gate(gates: dict[str, Any], name: str, passed: bool, **details: Any) -> None:
    gates[name] = {"pass": passed, **details}


def load_json(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("top-level value is not an object")
        return data
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: {exc}")
        return None


def validate_generation(args: argparse.Namespace, gates: dict[str, Any], errors: list[str]) -> dict[str, Any] | None:
    source = args.source
    manifest_path = args.results / "generation-manifest.json"
    marker_path = args.results / ".generation-complete"
    manifest = load_json(manifest_path, "generation manifest", errors) if manifest_path.is_file() else None
    if manifest is None:
        add_gate(gates, "manifest_schema", False, reason="generation-manifest.json missing or invalid")
    else:
        required = {
            "schema_version": 1,
            "dataset": EXPECTED_DATASET,
            "generation_state": "COMPLETED",
            "record_width_bytes": RECORD_WIDTH,
            "sha256_algorithm": "sha256",
            "path": str(source),
        }
        mismatches = {key: {"expected": value, "actual": manifest.get(key)} for key, value in required.items() if manifest.get(key) != value}
        type_errors = [key for key in ("bytes", "records", "slurm_generation_job_id") if not isinstance(manifest.get(key), int)]
        if not isinstance(manifest.get("width_validation"), dict):
            type_errors.append("width_validation")
        if not isinstance(manifest.get("sha256"), str) or not HEX256.fullmatch(manifest["sha256"]):
            type_errors.append("sha256")
        add_gate(gates, "manifest_schema", not mismatches and not type_errors, mismatches=mismatches, invalid_fields=type_errors)

    marker_value = ""
    try:
        marker_value = marker_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        errors.append(f"completion marker: {exc}")
    marker_ok = bool(manifest) and bool(HEX256.fullmatch(marker_value)) and marker_value == manifest.get("sha256")
    add_gate(gates, "completion_marker", marker_ok, marker_sha256=marker_value or None)

    if not source.is_file():
        add_gate(gates, "source_exists", False, path=str(source))
        for name in ("source_stability", "record_byte_divisibility", "exact_line_count", "sampled_record_content", "source_sha256"):
            add_gate(gates, name, False, reason="source missing")
        return manifest

    add_gate(gates, "source_exists", True, path=str(source))
    stable, stability = stable_stat(source, args.stability_seconds)
    add_gate(gates, "source_stability", stable, **stability, wait_seconds=args.stability_seconds)
    size = stability["bytes_after"]
    records_from_bytes, remainder = divmod(size, RECORD_WIDTH)
    add_gate(gates, "record_byte_divisibility", remainder == 0 and size > 0, bytes=size, remainder=remainder, records_from_bytes=records_from_bytes)
    lines = count_lines(source)
    add_gate(gates, "exact_line_count", lines == records_from_bytes, lines=lines, records_from_bytes=records_from_bytes)
    sample = source_samples(source, records_from_bytes)
    add_gate(gates, "sampled_record_content", sample["bad_records"] == 0, **sample)
    actual_hash = sha256(source)
    hash_ok = bool(manifest) and actual_hash == manifest.get("sha256")
    add_gate(gates, "source_sha256", hash_ok, actual_sha256=actual_hash, manifest_sha256=manifest.get("sha256") if manifest else None)
    if manifest:
        census_ok = manifest.get("bytes") == size and manifest.get("records") == lines
        add_gate(gates, "manifest_census_matches_source", census_ok, manifest_bytes=manifest.get("bytes"), source_bytes=size, manifest_records=manifest.get("records"), source_records=lines)
    else:
        add_gate(gates, "manifest_census_matches_source", False, reason="manifest unavailable")
    return manifest


def validate_capacity(args: argparse.Namespace, gates: dict[str, Any]) -> None:
    source_size = args.source.stat().st_size if args.source.is_file() else 0
    free = shutil.disk_usage(args.root).free
    required = source_size * 2 + args.minimum_headroom_bytes
    add_gate(gates, "capacity_for_source_and_shards", free >= required, free_bytes=free, required_bytes=required, headroom_bytes=free - required)


def validate_shards(args: argparse.Namespace, manifest: dict[str, Any] | None, gates: dict[str, Any], errors: list[str]) -> None:
    shard_dir = args.shard_dir
    complete = shard_dir / ".complete"
    shard_manifest_path = shard_dir / "manifest.json"
    if not complete.is_file() or not shard_manifest_path.is_file():
        add_gate(gates, "physical_shard_map", False, state="BLOCKED_NOT_STAGED", reason="shard manifest or .complete marker missing")
        return
    shard_manifest = load_json(shard_manifest_path, "shard manifest", errors)
    if shard_manifest is None or manifest is None:
        add_gate(gates, "physical_shard_map", False, reason="required manifest unavailable")
        return
    required_fields = {
        "schema_version": 1,
        "records": manifest.get("records"),
        "validated_records": manifest.get("records"),
        "record_width_bytes": RECORD_WIDTH,
        "source_sha256": manifest.get("sha256"),
        "shard_size": args.shard_size,
    }
    mismatches = {key: {"expected": value, "actual": shard_manifest.get(key)} for key, value in required_fields.items() if shard_manifest.get(key) != value}
    expected_shards = math.ceil(manifest["records"] / args.shard_size) if isinstance(manifest.get("records"), int) else None
    chunks = sorted(shard_dir.glob("chunk-*.g6"))
    line_sum = 0
    bad_byte_shards: list[str] = []
    for chunk in chunks:
        line_sum += count_lines(chunk)
        if chunk.stat().st_size % RECORD_WIDTH:
            bad_byte_shards.append(chunk.name)
    passed = not mismatches and len(chunks) == expected_shards and line_sum == manifest["records"] and not bad_byte_shards
    add_gate(
        gates,
        "physical_shard_map",
        passed,
        state="COMPLETE" if passed else "INVALID",
        manifest_mismatches=mismatches,
        expected_shards=expected_shards,
        observed_shards=len(chunks),
        expected_lines=manifest["records"],
        observed_line_sum=line_sum,
        non_divisible_shards=bad_byte_shards[:10],
    )


def sign_report(report: dict[str, Any]) -> None:
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    report["report_signature"] = {"algorithm": "sha256-canonical-json", "value": hashlib.sha256(payload).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/mnt/weka/hrant/interval-search"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--shard-dir", type=Path)
    parser.add_argument("--shard-size", type=int, default=450000)
    parser.add_argument("--minimum-headroom-bytes", type=int, default=MINIMUM_HEADROOM)
    parser.add_argument("--stability-seconds", type=int, default=30)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    args.source = args.source or args.root / "data/order16-8x8-d2to11.g6"
    args.results = args.results or args.root / "results/order16-8x8"
    args.shard_dir = args.shard_dir or args.root / "data/order16-8x8-d2to11-shards"
    args.report = args.report or args.results / "final-validation.json"
    if args.shard_size <= 0 or args.stability_seconds < 0 or args.minimum_headroom_bytes < 0:
        parser.error("shard size must be positive; stability and headroom must be nonnegative")

    gates: dict[str, Any] = {}
    errors: list[str] = []
    manifest = validate_generation(args, gates, errors)
    validate_capacity(args, gates)
    validate_shards(args, manifest, gates, errors)
    generation_names = [name for name in gates if name != "physical_shard_map"]
    generation_passed = all(gates[name]["pass"] for name in generation_names)
    shard_passed = gates["physical_shard_map"]["pass"]
    report = {
        "schema_version": 1,
        "validator": "order16-8x8-independent-finalization-gate",
        "validated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": str(args.source),
        "classification_allowed": generation_passed and shard_passed,
        "state": "PASS" if generation_passed and shard_passed else ("BLOCKED_PENDING_SHARDS" if generation_passed else "FAIL_GENERATION_GATE"),
        "gates": gates,
        "errors": errors,
    }
    sign_report(report)
    args.results.mkdir(parents=True, exist_ok=True)
    temporary = args.report.with_name(args.report.name + f".tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, args.report)
    print(f"state={report['state']} classification_allowed={str(report['classification_allowed']).lower()} report={args.report} signature={report['report_signature']['value']}")
    return 0 if report["classification_allowed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
