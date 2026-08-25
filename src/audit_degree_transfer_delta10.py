#!/usr/bin/env python3
"""Reconstruct and audit a completed degree-transfer report."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import networkx as nx

from degree_transfer_delta10_search import apply_terminal_gadget, parent_graphs
from interval_edge_coloring import nauty_canonical_hash, rank_potential_solve


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON at {path}:{line_number}") from error
            rows.append(row)
    return rows


def audit(args: argparse.Namespace) -> dict:
    started = time.monotonic()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    records = report.get("records", [])
    state_rows = load_jsonl(args.state)
    checkpoint_rows = load_jsonl(args.checkpoint_state)
    graph_dir = args.output.parent / "graphs"
    available = {name: graph for name, graph, _ in parent_graphs(graph_dir)}
    required = {
        "parent", "canonical_sha256", "signature", "decision", "primary_status",
        "order", "size", "delta", "minimum_degree", "connected", "bipartite",
    }
    missing_fields = [
        {"index": index, "missing": sorted(required - row.keys())}
        for index, row in enumerate(records)
        if required - row.keys()
    ]
    report_hashes = [row["canonical_sha256"] for row in records]
    report_signatures = [
        (row["parent"], tuple(row["signature"][0]), tuple(row["signature"][1]))
        for row in records
    ]
    state_hashes = [row["canonical_sha256"] for row in state_rows]
    checkpoint_hashes = [row["canonical_sha256"] for row in checkpoint_rows]
    mismatches: list[dict] = []
    solver_mismatches: list[dict] = []
    for index, row in enumerate(records, 1):
        base = available.get(row["parent"])
        if base is None:
            mismatches.append({"index": index, "reason": "unknown_parent"})
            continue
        graph, _ = apply_terminal_gadget(
            base,
            row["selected_parent_edges"],
            row["replacement_motifs"],
            index,
        )
        nx_graph = nx.Graph(graph.edges)
        nx_graph.add_nodes_from(graph.vertices)
        facts = {
            "canonical_sha256": nauty_canonical_hash(graph),
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "minimum_degree": min(graph.degrees.values()),
            "connected": nx.is_connected(nx_graph),
            "bipartite": nx.is_bipartite(nx_graph),
            "simple": len(graph.edges) == nx_graph.number_of_edges(),
        }
        for key in ("canonical_sha256", "order", "size", "delta", "minimum_degree", "connected", "bipartite"):
            if facts[key] != row[key]:
                mismatches.append({"index": index, "field": key, "expected": row[key], "actual": facts[key]})
        if not facts["simple"] or facts["delta"] > args.maximum_delta or facts["minimum_degree"] < args.minimum_degree:
            mismatches.append({"index": index, "field": "structural_constraints", "actual": facts})
        if args.rerun_primary:
            result = rank_potential_solve(graph, args.time_limit, args.workers)
            if result.status != row["primary_status"]:
                solver_mismatches.append({"index": index, "expected": row["primary_status"], "actual": result.status})

    counted = Counter(row["decision"] for row in records)
    reported_counts = report.get("counts", {})
    count_reconciliation = {
        "newly_classified": len(records) - len(checkpoint_rows),
        "colorable": counted["colorable"],
        "non_colorable": counted["non-colorable"],
        "timeout": sum(value for key, value in counted.items() if key.startswith("unresolved")),
    }
    count_mismatches = {
        key: {"report": reported_counts.get(key), "recomputed": value}
        for key, value in count_reconciliation.items()
        if key in reported_counts and reported_counts[key] != value
    }
    audit_pass = not any((missing_fields, mismatches, solver_mismatches, count_mismatches))
    return {
        "schema": "degree-transfer-delta10-audit-v1",
        "report": str(args.report),
        "state": str(args.state),
        "checkpoint_state": str(args.checkpoint_state),
        "record_count": len(records),
        "state_row_count": len(state_rows),
        "checkpoint_row_count": len(checkpoint_rows),
        "unique_report_hashes": len(set(report_hashes)),
        "unique_report_signatures": len(set(report_signatures)),
        "state_hashes_all_in_report": set(state_hashes) <= set(report_hashes),
        "checkpoint_hashes_all_in_report": set(checkpoint_hashes) <= set(report_hashes),
        "state_report_overlap_count": len(set(state_hashes) & set(report_hashes)),
        "checkpoint_report_overlap_count": len(set(checkpoint_hashes) & set(report_hashes)),
        "missing_fields": missing_fields,
        "structural_or_hash_mismatches": mismatches,
        "primary_oracle_mismatches": solver_mismatches,
        "count_mismatches": count_mismatches,
        "decision_counts": dict(sorted(counted.items())),
        "reran_primary": args.rerun_primary,
        "audit_pass": audit_pass,
        "elapsed_seconds": time.monotonic() - started,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--checkpoint-state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-delta", type=int, default=10)
    parser.add_argument("--minimum-degree", type=int, default=2)
    parser.add_argument("--rerun-primary", action="store_true")
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if args.maximum_delta < 2 or args.minimum_degree < 1 or args.time_limit <= 0 or args.workers < 1:
        parser.error("invalid audit bounds")
    result = audit(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("audit_pass", "record_count", "elapsed_seconds")}, sort_keys=True))
    if not result["audit_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
