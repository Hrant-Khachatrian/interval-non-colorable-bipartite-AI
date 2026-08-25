#!/usr/bin/env python3
"""Independently resolve primary rank-potential negative decisions by span."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from interval_edge_coloring import Graph, fixed_span_sat_solve, nauty_canonical_hash, verify_coloring


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent, text=True)
    try:
        with os.fdopen(handle, "w", encoding="ascii") as stream:
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


def latest_primary_negatives(path: Path) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            row = json.loads(line)
            if not isinstance(row.get("index"), int) or "status" not in row:
                raise ValueError(f"invalid primary row at line {line_number}")
            rows[row["index"]] = row
    return {index: row for index, row in rows.items() if row["status"] == "non-colorable"}


def confirm(graph: Graph, seconds: float, workers: int) -> dict:
    spans = {}
    for span in range(graph.delta, graph.n):
        status, coloring = fixed_span_sat_solve(graph, span, seconds, workers=workers)
        item = {"solver_status": status, "has_coloring": coloring is not None}
        spans[str(span)] = item
        if status in {"OPTIMAL", "FEASIBLE"}:
            valid, reason = verify_coloring(graph, coloring)
            if not valid:
                raise AssertionError(f"invalid fixed-span witness: {reason}")
            return {"status": "primary_contradicted_colorable", "spans": spans}
        if status == "UNKNOWN":
            return {"status": "unresolved_timeout", "spans": spans}
        if status != "INFEASIBLE":
            return {"status": "unresolved_solver_error", "spans": spans}
    return {"status": "confirmed_noncolorable", "spans": spans}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-jsonl", type=Path, required=True)
    parser.add_argument("--hits-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--time-limit", type=float, default=3600.0)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    records = []
    for index, primary in sorted(latest_primary_negatives(args.primary_jsonl).items()):
        graph_path = args.hits_dir / f"candidate-{index:06d}.graph.json"
        if not graph_path.exists():
            records.append({"index": index, "status": "unresolved_missing_saved_candidate"})
            continue
        graph = Graph.from_json(json.loads(graph_path.read_text(encoding="utf-8")))
        digest = nauty_canonical_hash(graph)
        if digest != primary.get("canonical_sha256"):
            records.append({"index": index, "status": "unresolved_hash_mismatch"})
            continue
        records.append({
            "index": index,
            "canonical_sha256": digest,
            "primary_status": primary["status"],
            "confirmation": confirm(graph, args.time_limit, args.workers),
        })
    summary = {
        "schema_version": 1,
        "primary_jsonl": str(args.primary_jsonl),
        "policy": "Only confirmed_noncolorable is a negative; all other outcomes are unresolved or contradictory.",
        "time_limit_per_span_seconds": args.time_limit,
        "records": records,
        "confirmed_noncolorable": sum(row.get("confirmation", {}).get("status") == "confirmed_noncolorable" for row in records),
        "unresolved": sum(row.get("status", row.get("confirmation", {}).get("status", "")) .startswith("unresolved") for row in records),
    }
    atomic_json(args.output, summary)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
