#!/usr/bin/env python3
"""Disposable fixtures for the order-17 classification acceptance gate."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("audit_order17_small_class.py")


def row(index: int, status: str = "colorable") -> dict:
    return {
        "index": index,
        "canonical_sha256": f"{index:064x}",
        "order": 17,
        "size": 30,
        "delta": 15,
        "minimum_degree": 2,
        "status": status,
        "span": None,
        "solver_seconds": 0.01,
    }


class AuditOrder17FixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data" / "order17-2x15-d2-shards"
        self.run = self.root / "results" / "order17-census" / "classification-2x15"
        self.data.mkdir(parents=True)
        self.run.mkdir(parents=True)
        (self.data / "manifest.json").write_text(
            json.dumps({"records": 6, "shard_size": 3, "shards": 2}), encoding="ascii"
        )
        self.write_chunk(0, [row(0), row(1), row(2)])
        self.write_chunk(1, [row(3), row(4), row(5)])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_chunk(self, chunk: int, rows: list[dict]) -> None:
        path = self.run / f"chunk-{chunk:05d}.jsonl"
        path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="ascii")

    def audit(self) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "2", "15", "--root", str(self.root)],
            text=True,
            capture_output=True,
            check=False,
        )
        output = self.root / "results" / "order17-census" / "audit-2x15.json"
        return result, json.loads(output.read_text(encoding="ascii"))

    def test_complete_fixture_is_accepted(self) -> None:
        result, summary = self.audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(summary["acceptance_ready"])
        self.assertEqual(summary["files_with_unexpected_row_count"], [])

    def test_truncated_chunk_is_rejected(self) -> None:
        self.write_chunk(1, [row(3), row(4)])
        result, summary = self.audit()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(summary["coverage_complete"])
        self.assertEqual(summary["files_with_unexpected_row_count"][0]["chunk"], 1)

    def test_malformed_schema_is_rejected(self) -> None:
        broken = row(4)
        broken["canonical_sha256"] = "not-a-sha256"
        self.write_chunk(1, [row(3), broken, row(5)])
        result, summary = self.audit()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(summary["out_of_range_or_invalid_rows"], 1)

    def test_timeout_is_rejected(self) -> None:
        self.write_chunk(1, [row(3), row(4, "timeout"), row(5)])
        result, summary = self.audit()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(summary["timeout_policy_satisfied"])

    def test_unconfirmed_negative_is_rejected(self) -> None:
        self.write_chunk(1, [row(3), row(4, "non-colorable"), row(5)])
        result, summary = self.audit()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(summary["negative_policy_satisfied"])

    def test_inconsistent_shard_manifest_fails_before_acceptance(self) -> None:
        (self.data / "manifest.json").write_text(
            json.dumps({"records": 6, "shard_size": 3, "shards": 3}), encoding="ascii"
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "2", "15", "--root", str(self.root)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid shard manifest cardinality", result.stderr)


if __name__ == "__main__":
    unittest.main()
