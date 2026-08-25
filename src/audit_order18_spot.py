#!/usr/bin/env python3
"""Independent deterministic spot audit of completed order-18 classifications."""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from interval_edge_coloring import (
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
)
from order18_targeted_search import generate_candidates


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "results" / "order18-spot-audit"
SLICES = {
    "v3": (1, 500, ROOT / "results" / "order18-targeted-v3.json"),
    "v4": (501, 2500, ROOT / "results" / "order18-targeted-v4" / "report.json"),
    "v5": (2501, 4500, ROOT / "results" / "order18-targeted-v5" / "report.json"),
    "v6": (4501, 6500, ROOT / "results" / "order18-targeted-v6" / "report.json"),
    "v7": (6501, 8500, ROOT / "results" / "order18-targeted-v7" / "report.json"),
    "v8": (8501, 10500, ROOT / "results" / "order18-targeted-v8" / "report.json"),
}
SAMPLE_SIZE = 18
RANK_TIME_LIMIT = 20.0
FIXED_TIME_LIMIT = 20.0
WORKERS = 2


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def sample_ranks(name: str, first: int, last: int) -> list[int]:
    """First/middle/last plus deterministic pseudo-random in-window ranks."""
    chosen = {first, (first + last) // 2, last}
    rng = random.Random(f"order18-spot-audit-20260825-{name}")
    while len(chosen) < SAMPLE_SIZE:
        chosen.add(rng.randint(first, last))
    return sorted(chosen)


def production_queue() -> list[tuple]:
    args = SimpleNamespace(
        lanes="all",
        max_additions=1,
        max_deleted_degree=3,
        max_rewires=750,
        extension_limit=18,
        candidate_cap=12987,
        rank_start=0,
    )
    selected, _raw, _unique, _selected, diagnostics = generate_candidates(args)
    if diagnostics["unique_after_nauty"] != 12987 or len(selected) != 12987:
        raise RuntimeError(f"unexpected deterministic queue size: {diagnostics}")
    return selected


def indexed_report_rows() -> dict[int, tuple[str, dict]]:
    indexed: dict[int, tuple[str, dict]] = {}
    for name, (first, last, path) in SLICES.items():
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("completion") != "complete":
            raise RuntimeError(f"{name} is not complete: {document.get('completion')}")
        for row in document["rows"]:
            rank = row.get("rank")
            if rank is None:
                match = re.fullmatch(r"O18-(\d+)", str(row.get("candidate_id")))
                if not match:
                    raise RuntimeError(f"{name} row lacks an interpretable rank: {row.get('candidate_id')}")
                rank = int(match.group(1)) + 1
            if not first <= rank <= last:
                raise RuntimeError(f"{name} has out-of-window rank {rank}")
            if rank in indexed:
                raise RuntimeError(f"duplicate reported rank {rank}")
            indexed[rank] = (name, row)
    return indexed


def structural_checks(graph, row: dict) -> list[str]:
    failures: list[str] = []
    actual_degrees = graph.degrees
    left, right = graph.bipartition
    actual = {
        "canonical_sha256": nauty_canonical_hash(graph),
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(actual_degrees.values()),
        "degrees": actual_degrees,
        "bipartition_sizes": sorted((len(left), len(right))),
        "connected": nx.is_connected(graph._nx),
        "bipartite": nx.is_bipartite(graph._nx),
        "bipartition_valid": set(left).isdisjoint(right)
        and set(left) | set(right) == set(graph.vertices)
        and all((u in left) != (v in left) for u, v in graph.edges),
    }
    expected = {
        "canonical_sha256": row["canonical_sha256"],
        "order": row["order"],
        "size": row["size"],
        "delta": row["delta"],
        "minimum_degree": row["minimum_degree"],
        "degrees": row["degrees"],
        "bipartition_sizes": sorted(row["bipartition_sizes"]),
        "connected": True,
        "bipartite": True,
        "bipartition_valid": True,
    }
    for key, value in expected.items():
        if actual[key] != value:
            failures.append(f"{key}: expected {value!r}, got {actual[key]!r}")
    return failures


def audit_one(rank: int, graph, row: dict) -> dict:
    started = time.perf_counter()
    structural_failures = structural_checks(graph, row)
    primary_started = time.perf_counter()
    primary = rank_potential_solve(graph, RANK_TIME_LIMIT, WORKERS)
    primary_runtime = time.perf_counter() - primary_started
    primary_certificate_valid = False
    primary_certificate_reason = None
    if primary.coloring is not None:
        primary_certificate_valid, primary_certificate_reason = verify_coloring(graph, primary.coloring)
    reported_span = row.get("primary_span")
    fixed_started = time.perf_counter()
    fixed_status, fixed_coloring = fixed_span_sat_solve(
        graph, reported_span, FIXED_TIME_LIMIT, WORKERS
    )
    fixed_runtime = time.perf_counter() - fixed_started
    fixed_certificate_valid = False
    fixed_certificate_reason = None
    if fixed_coloring is not None:
        fixed_certificate_valid, fixed_certificate_reason = verify_coloring(graph, fixed_coloring)

    primary_agrees = primary.status == "colorable"
    fixed_agrees = fixed_status in ("OPTIMAL", "FEASIBLE")
    return {
        "rank": rank,
        "candidate_id": row["candidate_id"],
        "canonical_sha256": row["canonical_sha256"],
        "reported_primary_status": row["primary_status"],
        "reported_primary_span": reported_span,
        "structural_failures": structural_failures,
        "recheck_primary": {
            "status": primary.status,
            "solver_status": primary.solver_status,
            "span": primary.span,
            "elapsed_seconds": primary_runtime,
            "certificate_valid": primary_certificate_valid,
            "certificate_reason": primary_certificate_reason,
            "agrees_with_reported_colorable_claim": primary_agrees,
        },
        "recheck_fixed_reported_span": {
            "span": reported_span,
            "solver_status": fixed_status,
            "elapsed_seconds": fixed_runtime,
            "certificate_valid": fixed_certificate_valid,
            "certificate_reason": fixed_certificate_reason,
            "agrees_with_reported_colorable_claim": fixed_agrees,
        },
        "elapsed_seconds": time.perf_counter() - started,
        "outcome": "pass" if not structural_failures and primary_agrees and fixed_agrees
        and primary_certificate_valid and fixed_certificate_valid else "disagreement",
    }


def compact_status(report: dict) -> dict:
    samples = [sample for slice_report in report["slices"].values() for sample in slice_report["samples"]]
    return {
        "completion": report["completion"],
        "queue_size": report["queue_size"],
        "sampled": len(samples),
        "planned": report["planned_sample_count"],
        "mismatches": sum(sample["outcome"] != "pass" for sample in samples),
        "invalid_certificates": sum(
            not sample["recheck_primary"]["certificate_valid"]
            or not sample["recheck_fixed_reported_span"]["certificate_valid"]
            for sample in samples
        ),
        "solver_disagreements": sum(
            not sample["recheck_primary"]["agrees_with_reported_colorable_claim"]
            or not sample["recheck_fixed_reported_span"]["agrees_with_reported_colorable_claim"]
            for sample in samples
        ),
        "max_sample_runtime_seconds": max((sample["elapsed_seconds"] for sample in samples), default=0),
        "max_primary_runtime_seconds": max((sample["recheck_primary"]["elapsed_seconds"] for sample in samples), default=0),
        "max_fixed_runtime_seconds": max((sample["recheck_fixed_reported_span"]["elapsed_seconds"] for sample in samples), default=0),
        "workers": WORKERS,
        "rank_time_limit_seconds": RANK_TIME_LIMIT,
        "fixed_time_limit_seconds": FIXED_TIME_LIMIT,
    }


def write_markdown(report: dict, status: dict) -> None:
    lines = [
        "# Order-18 spot audit",
        "",
        f"- Completion: `{report['completion']}`",
        f"- Deterministic queue: `{report['queue_size']}` candidates",
        f"- Sample: `{status['sampled']}/{report['planned_sample_count']}` claims (18 from each completed v3-v8 slice)",
        f"- Mismatches: `{status['mismatches']}`",
        f"- Invalid certificates: `{status['invalid_certificates']}`",
        f"- Solver disagreements: `{status['solver_disagreements']}`",
        f"- Max combined sample runtime: `{status['max_sample_runtime_seconds']:.3f}s`",
        f"- Recheck configuration: rank-potential and fixed-span CP-SAT, `{WORKERS}` workers, `{RANK_TIME_LIMIT:.0f}s` limits",
        "",
        "Every sample reconstructs the production-ranked graph, compares its hash and stored structural fields, validates both extracted certificates, and confirms SAT at the reported primary span.",
        "",
        "| Slice | Sampled ranks | Pass | Mismatch |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, slice_report in report["slices"].items():
        samples = slice_report["samples"]
        lines.append(
            f"| {name} | {len(samples)} | {sum(s['outcome'] == 'pass' for s in samples)} | {sum(s['outcome'] != 'pass' for s in samples)} |"
        )
    (OUTPUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true", help="Resume from this audit's report, if present.")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = time.time()
    queue = production_queue()
    reported = indexed_report_rows()
    report = {
        "schema_version": 1,
        "completion": "running",
        "started_at_unix_seconds": started,
        "queue_size": len(queue),
        "queue_generation": {
            "max_additions": 1,
            "max_deleted_degree": 3,
            "max_rewires": 750,
            "extension_limit": 18,
            "ranking": ["hub margin tier", "hub margin", "degree variance", "delta", "canonical hash"],
        },
        "sampling": {
            "method": "first, middle, last, then pseudo-random deterministic ranks",
            "seed_prefix": "order18-spot-audit-20260825-",
            "per_slice": SAMPLE_SIZE,
        },
        "solver_configuration": {
            "rank_potential_workers": WORKERS,
            "fixed_span_workers": WORKERS,
            "rank_potential_time_limit_seconds": RANK_TIME_LIMIT,
            "fixed_span_time_limit_seconds": FIXED_TIME_LIMIT,
        },
        "planned_sample_count": len(SLICES) * SAMPLE_SIZE,
        "slices": {},
    }
    for name, (first, last, _path) in SLICES.items():
        ranks = sample_ranks(name, first, last)
        slice_report = report["slices"].setdefault(name, {"range": [first, last], "sampled_ranks": ranks, "samples": []})
        for rank in ranks:
            if rank not in reported:
                raise RuntimeError(f"no durable completed row for rank {rank}")
            reported_slice, row = reported[rank]
            if reported_slice != name:
                raise RuntimeError(f"rank {rank} came from {reported_slice}, expected {name}")
            sample = audit_one(rank, queue[rank - 1][-2], row)
            slice_report["samples"].append(sample)
            atomic_json(OUTPUT / "report.json", report)
            atomic_json(OUTPUT / "status.json", compact_status(report))
    report["completion"] = "complete"
    report["finished_at_unix_seconds"] = time.time()
    report["elapsed_seconds"] = report["finished_at_unix_seconds"] - started
    status = compact_status(report)
    atomic_json(OUTPUT / "report.json", report)
    atomic_json(OUTPUT / "status.json", status)
    write_markdown(report, status)


if __name__ == "__main__":
    main()
