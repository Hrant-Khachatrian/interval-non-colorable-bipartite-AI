#!/usr/bin/env python3
"""Independent hash-joined spot audit for alternate order-18 family v3.

The input event ranks are phase-local and intentionally not used as graph
identity.  Graphs are reconstructed from their event metadata and joined to
the durable Nauty digest recorded with each event.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import tempfile
import time
from pathlib import Path

import networkx as nx

from build_order18_alternate_family_ledger import PARENTS, reconstruct
from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
)
from order18_alternate_family import prior_queue_hashes


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "results/order18-alternate-family-v3"
OUTPUT = ROOT / "results/order18-alternate-family-v3-spot-audit"
PHASES = (
    ("v2-residual", SOURCE / "v2-residual/classification-events.jsonl", 31),
    ("expanded-step3", SOURCE / "expanded-step3/classification-events.jsonl", 1000),
)
PRIOR_LOGS = (
    ROOT / "results/order18-alternate-family-v1/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/v1-residual/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/expanded-step1/classification-events.jsonl",
    ROOT / "results/order18-alternate-family-v2/expanded-step2/classification-events.jsonl",
)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def rows_from_events(path: Path) -> list[dict]:
    result = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            event = json.loads(line)
            if event.get("event") != "classification_completed":
                continue
            row = event.get("row")
            if not isinstance(row, dict) or not isinstance(row.get("canonical_sha256"), str):
                raise ValueError(f"invalid completed row at {path}:{line_number}")
            result.append({**row, "_event_line": line_number})
    return result


def required_checks(graph: Graph) -> dict[str, bool]:
    edges = list(graph.edges)
    left, right = map(set, graph.bipartition)
    return {
        "order_18": graph.n == 18,
        "simple": len(edges) == len(set(edges)) and all(u != v for u, v in edges),
        "connected": nx.is_connected(graph._nx),
        "bipartite": nx.is_bipartite(graph._nx) and left | right == graph.vertex_set
        and not left & right and all((u in left) != (v in left) for u, v in edges),
        "minimum_degree_at_least_2": min(graph.degrees.values(), default=0) >= 2,
    }


def select_expanded(rows: list[dict], count: int) -> list[dict]:
    """Evenly sample the expanded phase in canonical-hash order, not rank order."""
    ordered = sorted(rows, key=lambda row: row["canonical_sha256"])
    positions = [((index * len(ordered)) // count) for index in range(count)]
    return [ordered[position] for position in positions]


def solve_sample(row: dict, graph: Graph, workers: int, seconds: float) -> dict:
    started = time.perf_counter()
    primary = rank_potential_solve(graph, seconds, workers=workers)
    primary_elapsed = time.perf_counter() - started
    primary_valid, primary_reason = (
        verify_coloring(graph, primary.coloring) if primary.coloring else (None, None)
    )
    span = row.get("primary_span")
    fixed_status = None
    fixed_valid = None
    fixed_reason = None
    fixed_elapsed = None
    if isinstance(span, int):
        started = time.perf_counter()
        fixed_status, coloring = fixed_span_sat_solve(graph, span, seconds, workers=workers)
        fixed_elapsed = time.perf_counter() - started
        fixed_valid, fixed_reason = verify_coloring(graph, coloring) if coloring else (None, None)
    return {
        "phase": row["_phase"],
        "event_line": row["_event_line"],
        "event_rank": row.get("rank"),
        "candidate_id": row.get("candidate_id"),
        "canonical_sha256": row["canonical_sha256"],
        "reported_status": row.get("status"),
        "reported_span": span,
        "structural_checks": required_checks(graph),
        "canonical_hash_agrees": nauty_canonical_hash(graph) == row["canonical_sha256"],
        "rank_potential": {
            "status": primary.status,
            "solver_status": primary.solver_status,
            "span": primary.span,
            "elapsed_seconds": primary_elapsed,
            "certificate_valid": primary_valid,
            "certificate_reason": primary_reason,
        },
        "fixed_reported_span": {
            "solver_status": fixed_status,
            "elapsed_seconds": fixed_elapsed,
            "certificate_valid": fixed_valid,
            "certificate_reason": fixed_reason,
        },
    }


def audit_negative(row: dict, graph: Graph, workers: int, seconds: float) -> dict:
    spans = {}
    for span in range(graph.delta, graph.n):
        started = time.perf_counter()
        status, coloring = fixed_span_sat_solve(graph, span, seconds, workers=workers)
        valid, reason = verify_coloring(graph, coloring) if coloring else (None, None)
        spans[str(span)] = {
            "solver_status": status,
            "elapsed_seconds": time.perf_counter() - started,
            "certificate_valid": valid,
            "certificate_reason": reason,
        }
        if status in {"OPTIMAL", "FEASIBLE"}:
            return {"canonical_sha256": row["canonical_sha256"], "conclusion": "colorable_disagrees_with_claim", "spans": spans}
        if status == "UNKNOWN":
            return {"canonical_sha256": row["canonical_sha256"], "conclusion": "unresolved_timeout", "spans": spans}
        if status != "INFEASIBLE":
            raise ValueError(f"unexpected fixed-span status {status}")
    return {"canonical_sha256": row["canonical_sha256"], "conclusion": "confirmed_noncolorable", "spans": spans}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--time-limit", type=float, default=8.0)
    args = parser.parse_args()
    if args.workers == 1:
        raise ValueError("the audit requires a different solver worker count than the source run")
    started = time.perf_counter()
    parents = {name: Graph.from_json(json.loads(path.read_text(encoding="utf-8"))) for name, path in PARENTS.items()}
    rows_by_phase = {}
    all_rows = []
    for name, path, expected_count in PHASES:
        rows = rows_from_events(path)
        if len(rows) != expected_count:
            raise ValueError(f"{name}: expected {expected_count} rows, found {len(rows)}")
        for row in rows:
            row["_phase"] = name
        rows_by_phase[name] = rows
        all_rows.extend(rows)

    summary = json.loads((SOURCE / "report.json").read_text(encoding="utf-8"))
    expected_counts = {"generated": 49118, "new_unique": 2392, "newly_classified": 1031, "colorable": 1031, "timeout": 0}
    count_mismatches = {
        key: {"expected": value, "observed": summary.get("counts", {}).get(key)}
        for key, value in expected_counts.items() if summary.get("counts", {}).get(key) != value
    }

    reconstructed = {}
    reconstruction_errors = []
    structural_errors = []
    hash_errors = []
    for row in all_rows:
        digest = row["canonical_sha256"]
        if digest in reconstructed:
            reconstruction_errors.append({"kind": "duplicate_v3_hash", "canonical_sha256": digest})
            continue
        try:
            graph = reconstruct(row, parents)
            reconstructed[digest] = graph
            checks = required_checks(graph)
            if not all(checks.values()):
                structural_errors.append({"canonical_sha256": digest, "phase": row["_phase"], "checks": checks})
            rehash = nauty_canonical_hash(graph)
            if rehash != digest:
                hash_errors.append({"canonical_sha256": digest, "reconstructed_canonical_sha256": rehash, "phase": row["_phase"]})
        except Exception as exc:  # Report bad event evidence instead of hiding it.
            reconstruction_errors.append({"canonical_sha256": digest, "phase": row["_phase"], "detail": f"{type(exc).__name__}: {exc}"})

    prior_hashes = set()
    for path in PRIOR_LOGS:
        prior_hashes.update(row["canonical_sha256"] for row in rows_from_events(path))
    first_hashes = prior_queue_hashes()
    v3_hashes = set(reconstructed)
    overlap_errors = []
    for label, hashes in (("completed_first_queue", first_hashes), ("prior_v1_v2", prior_hashes)):
        for digest in sorted(v3_hashes & hashes):
            overlap_errors.append({"kind": f"overlap_{label}", "canonical_sha256": digest})

    # Include every residual row and 30 evenly distributed expanded rows.
    sampled_rows = list(rows_by_phase["v2-residual"]) + select_expanded(rows_by_phase["expanded-step3"], 30)
    samples = []
    sample_reconstruction_missing = []
    for row in sampled_rows:
        graph = reconstructed.get(row["canonical_sha256"])
        if graph is None:
            sample_reconstruction_missing.append(row["canonical_sha256"])
            continue
        samples.append(solve_sample(row, graph, args.workers, args.time_limit))

    apparent_negatives = [row for row in all_rows if row.get("status") != "colorable"]
    negative_checks = []
    for row in apparent_negatives:
        graph = reconstructed.get(row["canonical_sha256"])
        if graph is not None:
            negative_checks.append(audit_negative(row, graph, args.workers, args.time_limit))

    invalid_certificates = [sample for sample in samples if (
        sample["rank_potential"]["status"] != "colorable"
        or sample["rank_potential"]["certificate_valid"] is not True
        or sample["fixed_reported_span"]["solver_status"] not in {"OPTIMAL", "FEASIBLE"}
        or sample["fixed_reported_span"]["certificate_valid"] is not True
    )]
    solver_disagreements = [sample for sample in samples if (
        sample["rank_potential"]["status"] != "colorable"
        or sample["fixed_reported_span"]["solver_status"] not in {"OPTIMAL", "FEASIBLE"}
    )]
    runtimes = [
        seconds for sample in samples for seconds in (
            sample["rank_potential"]["elapsed_seconds"], sample["fixed_reported_span"]["elapsed_seconds"]
        ) if isinstance(seconds, (int, float))
    ]
    unresolved_negatives = [check for check in negative_checks if check["conclusion"] == "unresolved_timeout"]
    passed = not any((count_mismatches, reconstruction_errors, structural_errors, hash_errors,
                      overlap_errors, sample_reconstruction_missing, invalid_certificates,
                      solver_disagreements, unresolved_negatives))
    report = {
        "schema_version": 1,
        "source": str(SOURCE.relative_to(ROOT)),
        "identity_policy": "canonical_sha256 is the durable graph identity; phase-local ranks are retained only as source metadata",
        "method": {
            "all_rows": "reconstructed from event construction metadata and independently rehashed",
            "sample": "all 31 v2-residual rows plus 30 expanded-step3 rows selected evenly by sorted canonical hash",
            "audit_solver_workers": args.workers,
            "source_solver_workers": 1,
            "time_limit_seconds_per_solver_call": args.time_limit,
        },
        "reported_count_mismatches": count_mismatches,
        "reconstruction": {
            "event_rows": len(all_rows),
            "reconstructed_by_canonical_hash": len(reconstructed),
            "reconstruction_error_count": len(reconstruction_errors),
            "reconstruction_errors": reconstruction_errors,
            "structural_mismatch_count": len(structural_errors),
            "structural_mismatches": structural_errors,
            "canonical_hash_mismatch_count": len(hash_errors),
            "canonical_hash_mismatches": hash_errors,
        },
        "overlap_audit": {
            "v3_hash_count": len(v3_hashes),
            "completed_first_queue_hash_count": len(first_hashes),
            "prior_v1_v2_hash_count": len(prior_hashes),
            "overlap_error_count": len(overlap_errors),
            "overlap_errors": overlap_errors,
        },
        "sample_summary": {
            "sampled_count": len(samples),
            "sampled_by_phase": dict(sorted(collections.Counter(sample["phase"] for sample in samples).items())),
            "invalid_certificate_count": len(invalid_certificates),
            "invalid_certificates": [sample["canonical_sha256"] for sample in invalid_certificates],
            "solver_disagreement_count": len(solver_disagreements),
            "solver_disagreements": [sample["canonical_sha256"] for sample in solver_disagreements],
            "sample_reconstruction_missing": sample_reconstruction_missing,
            "max_solver_runtime_seconds": max(runtimes, default=0.0),
        },
        "negative_claim_audit": {
            "apparent_noncolorable_claim_count": len(apparent_negatives),
            "checks": negative_checks,
            "unresolved_timeout_count": len(unresolved_negatives),
        },
        "conclusion": "pass" if passed else "fail_or_unresolved",
        "elapsed_seconds": time.perf_counter() - started,
    }
    output_dir = args.output_dir.resolve()
    atomic_json(output_dir / "samples.json", samples)
    atomic_json(output_dir / "report.json", report)
    atomic_json(output_dir / "status.json", {"status": "complete", "conclusion": report["conclusion"]})
    print(json.dumps({
        "sampled": len(samples), "structural_mismatches": len(structural_errors),
        "hash_mismatches": len(hash_errors), "invalid_certificates": len(invalid_certificates),
        "solver_disagreements": len(solver_disagreements), "overlap_errors": len(overlap_errors),
        "max_runtime_seconds": report["sample_summary"]["max_solver_runtime_seconds"],
        "conclusion": report["conclusion"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
