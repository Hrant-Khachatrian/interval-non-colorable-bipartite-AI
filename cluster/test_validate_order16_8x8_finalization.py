#!/usr/bin/env python3
"""Synthetic end-to-end fixture for the order-16 8+8 finalization validator."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def main() -> int:
    validator = Path(__file__).with_name("validate_order16_8x8_finalization.py")
    with tempfile.TemporaryDirectory(prefix="order16-8x8-validator-") as temporary:
        root = Path(temporary)
        source = root / "data" / "order16-8x8-d2to11.g6"
        results = root / "results" / "order16-8x8"
        shards = root / "data" / "order16-8x8-d2to11-shards"
        source.parent.mkdir(parents=True)
        results.mkdir(parents=True)
        shards.mkdir(parents=True)
        record = b"O" + b"A" * 20 + b"\n"
        source.write_bytes(record * 5)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        write_json(results / "generation-manifest.json", {
            "schema_version": 1,
            "dataset": "order16-8x8-d2to11",
            "generator_command": "fixture",
            "slurm_generation_job_id": 229085,
            "generation_state": "COMPLETED",
            "path": str(source),
            "bytes": source.stat().st_size,
            "record_width_bytes": 22,
            "records": 5,
            "sha256_algorithm": "sha256",
            "sha256": digest,
            "width_validation": {"sampled_records": 257, "bad_records": 0},
        })
        (results / ".generation-complete").write_text(digest + "\n", encoding="ascii")
        for index, chunk in enumerate((record * 2, record * 2, record)):
            (shards / f"chunk-{index:05d}.g6").write_bytes(chunk)
        write_json(shards / "manifest.json", {
            "schema_version": 1,
            "source_sha256": digest,
            "records": 5,
            "validated_records": 5,
            "record_width_bytes": 22,
            "shard_size": 2,
            "shards": 3,
        })
        (shards / ".complete").touch()
        run = subprocess.run([
            sys.executable, str(validator), "--root", str(root), "--shard-size", "2",
            "--stability-seconds", "0", "--minimum-headroom-bytes", "0",
        ], capture_output=True, text=True, check=False)
        if run.returncode != 0:
            raise RuntimeError(run.stdout + run.stderr)
        report = json.loads((results / "final-validation.json").read_text(encoding="utf-8"))
        assert report["state"] == "PASS"
        assert report["classification_allowed"] is True
        assert report["gates"]["physical_shard_map"]["observed_line_sum"] == 5
        assert report["report_signature"]["algorithm"] == "sha256-canonical-json"
        signature = report.pop("report_signature")["value"]
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        assert signature == hashlib.sha256(canonical).hexdigest()
    print("synthetic fixture passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
