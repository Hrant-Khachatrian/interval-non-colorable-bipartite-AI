#!/usr/bin/env python3
"""Independent integrity and certificate audit of the missing 4+12 slice."""

from __future__ import annotations

import collections
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import networkx as nx

from interval_edge_coloring import (
    fixed_span_sat_solve,
    from_graph6,
    Graph,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "results/order16-4x12-delta12/manifest.json"
CLASSIFICATION = ROOT / "results/order16-4x12-delta12/classification.jsonl"
OUTPUT = ROOT / "results/order16-4x12-delta12/independent-audit.json"
STATUS = ROOT / "results/order16-4x12-delta12/independent-audit-status.json"
REPLAY_WORKERS = 2
REPLAY_SECONDS = 5.0


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
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


def load_graph(line: str) -> Graph:
    order, raw_edges = from_graph6(line)
    names = [f"V{i}" for i in range(order)]
    return Graph(names, [(names[i], names[j]) for i, j in raw_edges])


def structural_checks(graph: Graph) -> dict[str, bool]:
    left, right = map(set, graph.bipartition)
    return {
        "order_16": graph.n == 16,
        "bipartition_sizes_4_12": sorted(map(len, graph.bipartition)) == [4, 12],
        "simple": len(graph.edges) == len(set(graph.edges)) and all(u != v for u, v in graph.edges),
        "connected": nx.is_connected(graph._nx),
        "bipartite": nx.is_bipartite(graph._nx) and left | right == graph.vertex_set and not left & right,
        "minimum_degree_at_least_2": min(graph.degrees.values()) >= 2,
        "delta_exactly_12": graph.delta == 12,
    }


def coloring_digest(coloring: dict | None) -> str | None:
    if coloring is None:
        return None
    payload = json.dumps(sorted((str(edge), value) for edge, value in coloring.items()), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def all_span_check(graph: Graph) -> dict:
    spans = {}
    for span in range(graph.delta, graph.n):
        status, coloring = fixed_span_sat_solve(graph, span, REPLAY_SECONDS, REPLAY_WORKERS)
        valid, reason = verify_coloring(graph, coloring) if coloring is not None else (False, "no certificate")
        spans[str(span)] = {
            "solver_status": status,
            "certificate_valid": valid,
            "certificate_reason": reason,
            "coloring_sha256": coloring_digest(coloring),
        }
    feasible = [span for span, item in spans.items() if item["solver_status"] in {"OPTIMAL", "FEASIBLE"} and item["certificate_valid"]]
    unknown = [span for span, item in spans.items() if item["solver_status"] == "UNKNOWN"]
    conclusion = (
        "colorable_disagrees_with_apparent_negative" if feasible else
        "unresolved_timeout" if unknown else
        "confirmed_noncolorable"
    )
    return {"conclusion": conclusion, "spans": spans}


def replay(index: int, manifest_row: dict, result: dict, graph: Graph) -> dict:
    replayed = rank_potential_solve(graph, REPLAY_SECONDS, REPLAY_WORKERS)
    valid, reason = verify_coloring(graph, replayed.coloring) if replayed.coloring is not None else (False, "no certificate")
    recorded_span = result.get("span")
    if replayed.status == "non-colorable":
        negative_check = all_span_check(graph)
    else:
        negative_check = None

    if replayed.status == "colorable" and valid and replayed.span == recorded_span:
        span_check = {
            "method": "replay-certificate",
            "solver_status": replayed.solver_status,
            "certificate_valid": True,
            "certificate_reason": reason,
            "coloring_sha256": coloring_digest(replayed.coloring),
        }
    else:
        fixed_status, fixed_coloring = fixed_span_sat_solve(
            graph, int(recorded_span), REPLAY_SECONDS, REPLAY_WORKERS
        )
        fixed_valid, fixed_reason = (
            verify_coloring(graph, fixed_coloring) if fixed_coloring is not None else (False, "no certificate")
        )
        span_check = {
            "method": "fixed-span-cpsat",
            "solver_status": fixed_status,
            "certificate_valid": fixed_valid,
            "certificate_reason": fixed_reason,
            "coloring_sha256": coloring_digest(fixed_coloring),
        }

    span_valid = span_check["solver_status"] in {"OPTIMAL", "FEASIBLE"} and span_check["certificate_valid"]
    if replayed.status == "timeout" or span_check["solver_status"] == "UNKNOWN":
        outcome = "timeout"
    elif replayed.status == "colorable" and valid and span_valid and result.get("status") == "colorable":
        outcome = "valid"
    else:
        outcome = "mismatch"
    return {
        "index": index,
        "canonical_sha256": manifest_row["canonical_sha256"],
        "recorded_status": result.get("status"),
        "reported_span": recorded_span,
        "replay_status": replayed.status,
        "replay_solver_status": replayed.solver_status,
        "replay_span": replayed.span,
        "replay_certificate_valid": valid,
        "replay_certificate_reason": reason,
        "replay_coloring_sha256": coloring_digest(replayed.coloring),
        "reported_span_validation": span_check,
        "apparent_negative_all_span_check": negative_check,
        "outcome": outcome,
    }


def main() -> None:
    started = time.monotonic()
    manifest_payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    records = manifest_payload["records"]
    rows = [json.loads(line) for line in CLASSIFICATION.read_text(encoding="utf-8").splitlines()]
    by_index = {row["index"]: row for row in rows}

    issues = []
    expected_indices = set(range(len(records)))
    if len(rows) != len(records):
        issues.append(f"classification row count {len(rows)} != manifest count {len(records)}")
    if set(by_index) != expected_indices:
        issues.append(f"indices differ; missing={sorted(expected_indices-set(by_index))[:20]}, unexpected={sorted(set(by_index)-expected_indices)[:20]}")

    status_counts = collections.Counter(row.get("status") for row in rows)
    replays = []
    outcomes = collections.Counter()
    mismatch_examples = []

    for offset, manifest_row in enumerate(records):
        index = offset
        result = by_index.get(index)
        graph = load_graph(manifest_row["graph6"])
        digest = nauty_canonical_hash(graph)
        checks = structural_checks(graph)
        checks["manifest_hash_agrees"] = digest == manifest_row["canonical_sha256"]
        checks["classification_hash_agrees"] = bool(result) and digest == result.get("canonical_sha256")
        checks["stored_fields_agree"] = all([
            result.get("order") == graph.n,
            result.get("size") == graph.m,
            result.get("delta") == graph.delta,
            result.get("minimum_degree") == min(graph.degrees.values()),
        ])
        if not all(checks.values()):
            issue = {"index": index, "checks": checks}
            issues.append(issue)
            continue
        item = replay(index, manifest_row, result, graph)
        outcomes[item["outcome"]] += 1
        replays.append(item)
        if item["outcome"] != "valid":
            mismatch_examples.append(item)
        if len(replays) % 100 == 0:
            atomic_json(STATUS, {
                "completion": "running",
                "replayed": len(replays),
                "outcomes": dict(outcomes),
                "issues_so_far": issues[:50],
                "elapsed_seconds": time.monotonic() - started,
            })

    elapsed = time.monotonic() - started
    report = {
        "schema": "order16-4x12-delta12-independent-audit-v1",
        "completion": "complete",
        "verdict": "pass" if not issues and not mismatch_examples and not outcomes.get("timeout") else "fail_or_unresolved",
        "counts": {
            "manifest_records": len(records),
            "classification_records": len(rows),
            "structural_checks": len(records) - len(issues),
            "replays": len(replays),
            **{f"outcome_{key}": value for key, value in sorted(outcomes.items())},
            "issues": len(issues),
            "mismatches": len(mismatch_examples),
        },
        "status_histogram": dict(sorted(status_counts.items())),
        "issues": issues[:500],
        "mismatch_examples": mismatch_examples[:100],
        "configuration": {
            "replay_workers": REPLAY_WORKERS,
            "production_workers": 2,
            "time_limit_seconds": REPLAY_SECONDS,
            "identity": "filtered-source-index plus canonical SHA256",
        },
        "elapsed_seconds": elapsed,
    }
    atomic_json(OUTPUT, report)
    atomic_json(STATUS, report)
    print(json.dumps(report["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
