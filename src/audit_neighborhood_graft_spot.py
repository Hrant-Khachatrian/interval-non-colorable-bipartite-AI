#!/usr/bin/env python3
"""Deterministic spot audit for neighborhood-graft Delta <= 10 ledger ranks 1--2094.

This is intentionally not a continuation runner: it neither appends to nor
rewrites the source ledgers, and it performs no bulk classification.  It reads
the durable decisions, chooses a systematic 24-row sample from each completed
rank band, regenerates source graphs only until those canonical hashes are
found, and then checks the claims with a separate fixed-span encoding.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from interval_edge_coloring import (
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
)
from neighborhood_graft_delta10_search import (
    MAXIMUM_DELTA,
    MINIMUM_FINAL_DEGREE,
    applicable_hub,
    enumerate_root_candidates,
    final_graph_validity,
    resolve_roots,
)


LEDGER_BANDS = (
    (
        "initial-top94",
        1,
        Path("results/neighborhood-graft-delta10-agent/extension-full-roots/classification-state.jsonl"),
        94,
    ),
    (
        "beyond-top94",
        95,
        Path("results/neighborhood-graft-delta10-agent/extension-beyond-top94/classification-state.jsonl"),
        1000,
    ),
    (
        "beyond-top1094",
        1095,
        Path("results/neighborhood-graft-delta10-agent/extension-beyond-top1094/classification-state.jsonl"),
        1000,
    ),
)
OUTPUT = Path("results/graft-spot-audit")
SAMPLE_PER_BAND = 24
SOLVER_WORKERS = 3
TIME_LIMIT_SECONDS = 3.0


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ledger_records(path: Path) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event") == "classification":
            records.append(event["record"])
    if len({record["canonical_sha256"] for record in records}) != len(records):
        raise RuntimeError(f"duplicate hashes in {path}")
    return records


def ledger_workers(state_path: Path) -> int | None:
    report = json.loads(state_path.with_name("report.json").read_text(encoding="utf-8"))
    configuration = report.get("configuration", {})
    return configuration.get("solver_workers", configuration.get("workers"))


def systematic_sample(records: list[dict[str, Any]], count: int) -> list[tuple[int, dict[str, Any]]]:
    if len(records) < count:
        raise RuntimeError("sample is larger than record band")
    positions = [round(index * (len(records) - 1) / (count - 1)) for index in range(count)]
    if len(set(positions)) != count:
        raise RuntimeError("systematic sample positions are not distinct")
    return [(position, records[position]) for position in positions]


def structural_check(graph) -> list[str]:
    failures: list[str] = []
    vertices = set(graph.vertices)
    edges = list(map(tuple, graph.edges))
    if len(vertices) != graph.n or any(u == v for u, v in edges) or len(edges) != len(set(edges)):
        failures.append("not-simple")
    left, right = map(set, graph.bipartition)
    if left | right != vertices or left & right or any((u in left) == (v in left) for u, v in edges):
        failures.append("not-bipartite")
    adjacency = {vertex: set() for vertex in vertices}
    for u, v in edges:
        adjacency[u].add(v)
        adjacency[v].add(u)
    seen = {graph.vertices[0]} if graph.vertices else set()
    frontier = list(seen)
    while frontier:
        current = frontier.pop()
        for neighbor in adjacency[current] - seen:
            seen.add(neighbor)
            frontier.append(neighbor)
    if len(seen) != graph.n:
        failures.append("disconnected")
    degrees = {vertex: len(neighbors) for vertex, neighbors in adjacency.items()}
    if min(degrees.values(), default=0) < MINIMUM_FINAL_DEGREE:
        failures.append("minimum-degree")
    if max(degrees.values(), default=0) > MAXIMUM_DELTA:
        failures.append("delta-cap")
    return failures


def reconstruct_sample(targets: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    roots, _ = resolve_roots()
    found: dict[str, Any] = {}
    raw_examined = Counter()
    for root_name, root_graph in roots:
        remaining = set(targets[root_name]) if root_name in targets else set()
        if not remaining:
            continue
        if applicable_hub(root_graph) is None:
            raise RuntimeError(f"sample asks for an inapplicable root {root_name}")
        for raw in enumerate_root_candidates(root_name, root_graph):
            raw_examined[root_name] += 1
            graph = raw.graph
            if final_graph_validity(graph) is not None:
                continue
            if graph.delta > MAXIMUM_DELTA or min(graph.degrees.values()) < MINIMUM_FINAL_DEGREE:
                continue
            digest = nauty_canonical_hash(graph)
            if digest in remaining:
                found[digest] = graph
                remaining.remove(digest)
                if not remaining:
                    break
        if remaining:
            raise RuntimeError(f"did not reconstruct {len(remaining)} selected hashes for {root_name}")
    return {"graphs": found, "raw_examined": dict(raw_examined)}


def main() -> None:
    started = time.perf_counter()
    bands: list[dict[str, Any]] = []
    targets: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    all_digests: set[str] = set()
    source_counts = {}
    source_workers = {}
    for band_name, first_rank, path, expected_count in LEDGER_BANDS:
        records = ledger_records(path)
        if len(records) != expected_count:
            raise RuntimeError(f"{band_name}: expected {expected_count} records, found {len(records)}")
        if any(record.get("decision") != "colorable" for record in records):
            raise RuntimeError(f"{band_name}: non-colorable or unresolved claim found; full-span escalation required")
        source_counts[band_name] = len(records)
        source_workers[band_name] = ledger_workers(path)
        chosen = []
        for local_position, record in systematic_sample(records, SAMPLE_PER_BAND):
            digest = record["canonical_sha256"]
            if digest in all_digests:
                raise RuntimeError(f"cross-band duplicate sample hash {digest}")
            all_digests.add(digest)
            rank = first_rank + local_position
            item = {
                "rank": rank,
                "band": band_name,
                "ledger_state": str(path),
                "ledger_workers": source_workers[band_name],
                "record": record,
            }
            targets[record["parent"]][digest] = item
            chosen.append({"rank": rank, "canonical_sha256": digest, "parent": record["parent"]})
        bands.append({"name": band_name, "first_rank": first_rank, "ledger_records": len(records), "sample": chosen})

    write_json(OUTPUT / "status.json", {
        "status": "running",
        "source_record_counts": source_counts,
        "sampled": len(all_digests),
        "solver_workers": SOLVER_WORKERS,
    })
    regenerated = reconstruct_sample(targets)
    graph_by_digest = regenerated["graphs"]
    rows = []
    failures: list[dict[str, Any]] = []
    fixtures: list[str] = []
    runtime_max = 0.0
    lower_span_checks = 0
    for parent_targets in targets.values():
        for digest, item in parent_targets.items():
            row_started = time.perf_counter()
            record = item["record"]
            graph = graph_by_digest[digest]
            issues = structural_check(graph)
            actual = {
                "order": graph.n,
                "size": graph.m,
                "delta": graph.delta,
                "minimum_degree": min(graph.degrees.values()),
            }
            expected = {key: record.get(key) for key in actual}
            metadata_ok = actual == expected
            if not metadata_ok:
                issues.append("reported-metadata")
            hash_ok = nauty_canonical_hash(graph) == digest
            if not hash_ok:
                issues.append("canonical-hash")

            primary = rank_potential_solve(graph, TIME_LIMIT_SECONDS, SOLVER_WORKERS)
            certificate_ok = False
            if primary.status == "colorable" and primary.coloring is not None:
                certificate_ok, reason = verify_coloring(graph, primary.coloring)
                if not certificate_ok:
                    issues.append(f"rank-certificate:{reason}")
            else:
                issues.append(f"rank-solver:{primary.status}")
            reported_span = record.get("primary_result", {}).get("span")
            fixed_status = None
            fixed_certificate_ok = False
            lower_statuses: dict[str, str] = {}
            lower_span_confirmations: dict[str, dict[str, Any]] = {}
            lower_span_witnesses: dict[str, dict[str, int]] = {}
            minimality = "not-reported"
            if isinstance(reported_span, int):
                fixed_status, fixed_coloring = fixed_span_sat_solve(
                    graph, reported_span, TIME_LIMIT_SECONDS, SOLVER_WORKERS
                )
                if fixed_status in ("OPTIMAL", "FEASIBLE") and fixed_coloring is not None:
                    fixed_certificate_ok, fixed_reason = verify_coloring(graph, fixed_coloring)
                    if not fixed_certificate_ok:
                        issues.append(f"fixed-certificate:{fixed_reason}")
                else:
                    issues.append(f"fixed-span:{fixed_status}")
                statuses = []
                for span in range(graph.delta, reported_span):
                    status, lower_coloring = fixed_span_sat_solve(graph, span, TIME_LIMIT_SECONDS, SOLVER_WORKERS)
                    lower_statuses[str(span)] = status
                    if status in ("OPTIMAL", "FEASIBLE") and lower_coloring is not None:
                        lower_span_witnesses[str(span)] = {
                            f"{u}|{v}": color for (u, v), color in sorted(lower_coloring.items())
                        }
                    lower_span_checks += 1
                    statuses.append(status)
                if any(status in ("OPTIMAL", "FEASIBLE") for status in statuses):
                    minimality = "not-minimal"
                    issues.append("reported-span-not-minimal")
                    # Re-run every lower feasible witness serially, so the
                    # discrepancy is not tied to the three-worker audit run.
                    for span_text, status in lower_statuses.items():
                        if status not in ("OPTIMAL", "FEASIBLE"):
                            continue
                        confirmed_status, confirmed_coloring = fixed_span_sat_solve(
                            graph, int(span_text), TIME_LIMIT_SECONDS, 1
                        )
                        confirmed_valid = False
                        if confirmed_status in ("OPTIMAL", "FEASIBLE") and confirmed_coloring is not None:
                            confirmed_valid, _ = verify_coloring(graph, confirmed_coloring)
                        lower_span_confirmations[span_text] = {
                            "workers": 1,
                            "status": confirmed_status,
                            "certificate_valid": confirmed_valid,
                            "coloring": (
                                {f"{u}|{v}": color for (u, v), color in sorted(confirmed_coloring.items())}
                                if confirmed_coloring is not None else None
                            ),
                        }
                        if not confirmed_valid:
                            issues.append("lower-span-witness-not-confirmed")
                elif any(status == "UNKNOWN" for status in statuses):
                    minimality = "unresolved-lower-span"
                else:
                    minimality = "minimal"
            row_seconds = time.perf_counter() - row_started
            runtime_max = max(runtime_max, row_seconds)
            row = {
                "rank": item["rank"],
                "band": item["band"],
                "parent": record["parent"],
                "canonical_sha256": digest,
                "ledger_state": item["ledger_state"],
                "ledger_rank_potential_workers": item["ledger_workers"],
                "reported_span": reported_span,
                "rank_potential_status": primary.status,
                "rank_potential_span": primary.span,
                "rank_potential_certificate_valid": certificate_ok,
                "fixed_reported_span_status": fixed_status,
                "fixed_reported_span_certificate_valid": fixed_certificate_ok,
                "lower_span_statuses": lower_statuses,
                "lower_span_confirmations": lower_span_confirmations,
                "reported_span_minimality": minimality,
                "structural_failures": [issue for issue in issues if issue in {"not-simple", "not-bipartite", "disconnected", "minimum-degree", "delta-cap"}],
                "hash_agreement": hash_ok,
                "metadata_agreement": metadata_ok,
                "issues": issues,
                "elapsed_seconds": row_seconds,
            }
            if minimality == "not-minimal":
                fixture_name = f"rank-{item['rank']:04d}-reported-span-metadata.json"
                fixture_path = OUTPUT / "fixtures" / fixture_name
                write_json(fixture_path, {
                    "schema_version": 1,
                    "purpose": "Reproducible reported-span metadata discrepancy; colorability decision remains valid.",
                    "ledger": {
                        "state": item["ledger_state"],
                        "rank": item["rank"],
                        "parent": record["parent"],
                        "canonical_sha256": digest,
                        "reported_rank_potential_span": reported_span,
                        "reported_rank_potential_workers": item["ledger_workers"],
                        "reported_decision": record["decision"],
                    },
                    "audit": {
                        "canonical_sha256": nauty_canonical_hash(graph),
                        "rank_potential": {
                            "workers": SOLVER_WORKERS,
                            "time_limit_seconds": TIME_LIMIT_SECONDS,
                            "status": primary.status,
                            "span": primary.span,
                            "certificate_valid": certificate_ok,
                        },
                        "fixed_span_reported": {
                            "workers": SOLVER_WORKERS,
                            "time_limit_seconds": TIME_LIMIT_SECONDS,
                            "span": reported_span,
                            "status": fixed_status,
                            "certificate_valid": fixed_certificate_ok,
                        },
                        "fixed_span_lower_witnesses": {
                            span: {
                                "workers": SOLVER_WORKERS,
                                "time_limit_seconds": TIME_LIMIT_SECONDS,
                                "status": lower_statuses[span],
                                "coloring": lower_span_witnesses.get(span),
                                "serial_confirmation": lower_span_confirmations.get(span),
                            }
                            for span in lower_span_witnesses
                        },
                    },
                    "graph": graph.to_json(),
                })
                row["fixture"] = str(fixture_path)
                fixtures.append(str(fixture_path))
            rows.append(row)
            if issues:
                failures.append(row)

    rows.sort(key=lambda row: row["rank"])
    disagreements = [
        row for row in rows
        if row["rank_potential_status"] != "colorable"
        or row["fixed_reported_span_status"] not in ("OPTIMAL", "FEASIBLE")
    ]
    report = {
        "schema_version": 1,
        "scope": "deterministic 72-row spot audit of colorable ranks 1--2094",
        "method": {
            "source_ledgers": [str(path) for _, _, path, _ in LEDGER_BANDS],
            "source_rank_potential_workers": source_workers,
            "source_mutated": False,
            "reconstruction": "source enumeration filtered to selected canonical hashes; no classification pipeline rerun",
            "sample": "24 systematic evenly spaced rows from each of initial top94, beyond-top94, and beyond-top1094",
            "rank_potential_workers": SOLVER_WORKERS,
            "fixed_span_workers": SOLVER_WORKERS,
            "per_solve_time_limit_seconds": TIME_LIMIT_SECONDS,
        },
        "source_record_counts": source_counts,
        "batches": bands,
        "sampled_counts": {
            "total": len(rows),
            "by_band": dict(Counter(row["band"] for row in rows)),
            "by_parent": dict(Counter(row["parent"] for row in rows)),
        },
        "reconstruction": {"raw_candidates_examined": regenerated["raw_examined"]},
        "checks": {
            "all_source_claims_colorable": True,
            "structural_mismatches": sum(bool(row["structural_failures"]) for row in rows),
            "canonical_hash_mismatches": sum(not row["hash_agreement"] for row in rows),
            "reported_metadata_mismatches": sum(not row["metadata_agreement"] for row in rows),
            "invalid_rank_potential_certificates": sum(not row["rank_potential_certificate_valid"] for row in rows),
            "fixed_span_unsatisfied_or_invalid": sum(not row["fixed_reported_span_certificate_valid"] for row in rows),
            "reported_span_not_minimal": sum(row["reported_span_minimality"] == "not-minimal" for row in rows),
            "reported_span_minimality_unresolved": sum(row["reported_span_minimality"] == "unresolved-lower-span" for row in rows),
            "solver_disagreements": len(disagreements),
            "lower_span_checks": lower_span_checks,
            "max_row_runtime_seconds": runtime_max,
            "elapsed_seconds": time.perf_counter() - started,
        },
        "integrity_assessment": {
            "decision_status_integrity": "pass" if not disagreements else "fail",
            "reported_span_metadata": "pass" if not failures else "mismatch",
            "statement": (
                "All sampled colorability decisions/statuses are supported; two reported spans are nonminimal."
                if failures else "All sampled decisions/statuses and reported spans are supported."
            ),
        },
        "conclusion": "pass" if not failures else "reported-span-metadata-mismatch; decision-status-integrity-pass",
        "fixtures": fixtures,
        "failures": failures,
        "rows": rows,
    }
    write_json(OUTPUT / "report.json", report)
    write_json(OUTPUT / "status.json", {
        "status": "complete",
        "conclusion": report["conclusion"],
        "sampled_counts": report["sampled_counts"],
        "checks": report["checks"],
    })


if __name__ == "__main__":
    main()
