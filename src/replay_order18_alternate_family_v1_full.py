#!/usr/bin/env python3
"""Durably replay every colorable decision in alternate order-18 family v1.

The source ledger does not retain graph serializations or coloring bytes.  The
replay therefore regenerates the deterministic family once, joins each source
event to its graph by the stored bipartition-coloured Nauty digest (never its
historical rank), and records a newly checked rank-potential witness.
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

import order18_alternate_family as alternate
from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_EVENTS = ROOT / "results/order18-alternate-family-v1/classification-events.jsonl"
DEFAULT_OUTPUT = ROOT / "results/order18-alternate-family-v1-full-replay"


def atomic_json(path: Path, value: dict) -> None:
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


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(event, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def source_rows(events: Path) -> dict[str, dict]:
    """Load each completed source decision under its durable graph identity."""
    rows = {}
    for line_number, line in enumerate(events.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        if event.get("event") != "classification_completed":
            continue
        row = event.get("row")
        digest = row.get("canonical_sha256") if isinstance(row, dict) else None
        if not isinstance(digest, str) or not digest:
            raise ValueError(f"source event line {line_number} has no durable digest")
        if digest in rows:
            raise ValueError(f"source event line {line_number} duplicates digest {digest}")
        rows[digest] = {"source_line": line_number, "row": row}
    if len(rows) != 1200:
        raise ValueError(f"expected exactly 1200 completed decisions, found {len(rows)}")
    return rows


def replayed_rows(events: Path) -> dict[str, dict]:
    """Read only durable successful replay events; progress itself is append-only."""
    if not events.exists():
        return {}
    rows = {}
    for line_number, line in enumerate(events.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        if event.get("event") != "replay_completed":
            continue
        digest = event.get("canonical_sha256")
        if not isinstance(digest, str) or digest in rows:
            raise ValueError(f"invalid or duplicate replay event at line {line_number}")
        rows[digest] = event
    return rows


def reported_span_rows(events: Path) -> dict[str, dict]:
    if not events.exists():
        return {}
    rows = {}
    for line_number, line in enumerate(events.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        if event.get("event") != "reported_span_validation_completed":
            continue
        digest = event.get("canonical_sha256")
        if not isinstance(digest, str) or digest in rows:
            raise ValueError(f"invalid or duplicate reported-span event at line {line_number}")
        rows[digest] = event
    return rows


def structural_checks(graph: Graph) -> dict[str, bool]:
    edges = list(graph.edges)
    parts = graph.bipartition
    left = set(parts[0]) if parts else set()
    right = set(parts[1]) if parts else set()
    return {
        "order_18": graph.n == 18,
        "simple": len(edges) == len(set(edges)) and all(u != v for u, v in edges),
        "connected": nx.is_connected(graph._nx),
        "bipartite": (
            parts is not None
            and left | right == graph.vertex_set
            and not (left & right)
            and nx.is_bipartite(graph._nx)
            and all((u in left) != (v in left) for u, v in edges)
        ),
        "minimum_degree_at_least_2": min(graph.degrees.values(), default=0) >= 2,
    }


def row_field_mismatches(row: dict, graph: Graph) -> dict:
    reconstructed = {
        "order": graph.n,
        "size": graph.m,
        "bipartition_sizes": list(map(len, graph.bipartition)),
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
        "metadata": graph.metadata,
    }
    return {
        key: {"source": row.get(key), "reconstructed": value}
        for key, value in reconstructed.items()
        if row.get(key) != value
    }


def certificate_present(row: dict) -> bool:
    """The v1 ledger has none; keep the check explicit for future source changes."""
    return any(key in row for key in ("coloring", "certificate", "certificate_bytes"))


def summary(source_count: int, completed: dict[str, dict], mismatch_count: int, started: float,
            configuration: dict, completion: str, reason: str,
            span_validations: dict[str, dict] | None = None) -> dict:
    outcomes = collections.Counter(event["outcome"] for event in completed.values())
    span_outcomes = collections.Counter(
        event["outcome"] for event in (span_validations or {}).values()
    )
    runtimes = [event["replay_elapsed_seconds"] for event in completed.values()]
    return {
        "schema_version": 1,
        "source_events": str(SOURCE_EVENTS.relative_to(ROOT)),
        "identity_join": "stored canonical_sha256; historical rank is reporting-only",
        "configuration": configuration,
        "completion": completion,
        "reason": reason,
        "counts": {
            "source": source_count,
            "replayed": len(completed),
            "valid": outcomes["valid"],
            "mismatch": mismatch_count,
            "timeout": outcomes["timeout"] + span_outcomes["timeout"],
        },
        "reported_span_validation": {
            "completed": len(span_validations or {}),
            "valid": span_outcomes["valid"],
            "timeout": span_outcomes["timeout"],
            "mismatch": span_outcomes["mismatch"],
        },
        "max_runtime_seconds": max(runtimes, default=0.0),
        "elapsed_seconds": time.monotonic() - started,
    }


def isolate(output: Path, kind: str, detail: dict, base: dict) -> None:
    record = {"event": "mismatch_isolated", "kind": kind, **detail}
    append_event(output / "replay-events.jsonl", record)
    append_event(output / "mismatches.jsonl", record)
    atomic_json(output / "status.json", {**base, "completion": "escalated", "reason": kind, "isolation": detail})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--time-limit", type=float, default=3.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--checkpoint-every", type=int, default=100)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.workers == 1:
        raise ValueError("workers must differ from the source campaign's one worker")
    if args.checkpoint_every <= 0:
        raise ValueError("checkpoint interval must be positive")

    output = args.output_dir.resolve()
    events_path = output / "replay-events.jsonl"
    if events_path.exists() and not args.resume:
        raise FileExistsError(f"replay state exists at {events_path}; use --resume")
    try:
        os.nice(10)
        nice_applied = True
    except (AttributeError, OSError):
        nice_applied = False
    started = time.monotonic()
    configuration = {
        "solver": "rank-potential CP-SAT",
        "time_limit_seconds": args.time_limit,
        "workers": args.workers,
        "source_campaign_workers": 1,
        "low_priority_nice_applied": nice_applied,
        "certificate_policy": "source ledger has no certificate bytes; replay exactly once and validate the returned coloring",
        "checkpoint_policy": f"after every {args.checkpoint_every} durable hash-ordered replays",
    }

    source = source_rows(SOURCE_EVENTS)
    ranked, diagnostics = alternate.generate(14, 20, 180)
    by_hash = {item[3]: item[4] for item in ranked}
    missing_graphs = sorted(set(source) - set(by_hash))
    completed = replayed_rows(events_path) if args.resume else {}
    for digest in completed:
        if digest not in source:
            raise ValueError(f"replay state has a digest absent from source: {digest}")
    mismatch_count = sum(event["outcome"] == "mismatch" for event in completed.values())
    initial = summary(len(source), completed, mismatch_count, started, configuration, "running", "reconstruction_complete")
    initial["generation_diagnostics"] = diagnostics
    if missing_graphs:
        isolate(output, "missing_reconstructed_graph", {"hashes": missing_graphs[:20], "count": len(missing_graphs)}, initial)
        raise RuntimeError(f"{len(missing_graphs)} source graphs cannot be reconstructed")

    append_event(events_path, {"event": "replay_started", "resume": args.resume,
                               "source_decisions": len(source), "configuration": configuration})
    # Hash order supplies a durable sequence even when floating-point tie ordering changes.
    ordered = sorted(source)
    for position, digest in enumerate(ordered, 1):
        if digest in completed:
            continue
        source_entry = source[digest]
        row = source_entry["row"]
        graph = by_hash[digest]
        structure = structural_checks(graph)
        observed_hash = nauty_canonical_hash(graph)
        fields = row_field_mismatches(row, graph)
        hard_errors = []
        if observed_hash != digest:
            hard_errors.append("canonical_hash")
        if not all(structure.values()):
            hard_errors.append("structural")
        if fields:
            hard_errors.append("stored_fields")
        if row.get("status") != "colorable" or row.get("primary_status") != "colorable":
            hard_errors.append("source_decision")
        reported_span = row.get("primary_span")
        if not isinstance(reported_span, int) or not graph.delta <= reported_span < graph.n:
            hard_errors.append("reported_span")
        if hard_errors:
            base = summary(len(source), completed, mismatch_count + 1, started, configuration,
                           "running", "mismatch_detected")
            detail = {"canonical_sha256": digest, "source_line": source_entry["source_line"],
                      "source_rank": row.get("rank"), "errors": hard_errors,
                      "observed_hash": observed_hash, "structural_checks": structure,
                      "stored_field_mismatches": fields}
            isolate(output, "pre_solver_" + "+".join(hard_errors), detail, base)
            raise RuntimeError(f"isolated mismatch for {digest}: {hard_errors}")

        attempt_started = time.perf_counter()
        replay = rank_potential_solve(graph, args.time_limit, workers=args.workers)
        elapsed = time.perf_counter() - attempt_started
        certificate_valid, certificate_reason = (
            verify_coloring(graph, replay.coloring) if replay.coloring is not None else (False, "no certificate")
        )
        if replay.status == "colorable" and certificate_valid:
            outcome = "valid"
        elif replay.status == "timeout":
            outcome = "timeout"
        else:
            outcome = "mismatch"
        event = {
            "event": "replay_completed",
            "sequence": position,
            "canonical_sha256": digest,
            "source_line": source_entry["source_line"],
            "source_rank": row.get("rank"),
            "reported_span": reported_span,
            "certificate_bytes_present": certificate_present(row),
            "structural_checks": structure,
            "canonical_hash_agrees": observed_hash == digest,
            "replay_status": replay.status,
            "replay_solver_status": replay.solver_status,
            "replay_span": replay.span,
            "replay_span_agrees_with_reported": replay.span == reported_span,
            "certificate_valid": certificate_valid,
            "certificate_reason": certificate_reason,
            "replay_elapsed_seconds": elapsed,
            "outcome": outcome,
        }
        append_event(events_path, event)
        completed[digest] = event
        if outcome == "mismatch":
            mismatch_count += 1
            base = summary(len(source), completed, mismatch_count, started, configuration,
                           "running", "solver_mismatch_detected")
            isolate(output, "solver_or_certificate_mismatch", event, base)
            raise RuntimeError(f"isolated solver/certificate mismatch for {digest}")

        if len(completed) % args.checkpoint_every == 0 or len(completed) == len(source):
            checkpoint = summary(len(source), completed, mismatch_count, started, configuration,
                                 "running", "checkpoint")
            checkpoint["checkpoint"] = {"completed": len(completed), "last_sequence": position,
                                          "last_hash": digest}
            append_event(events_path, {"event": "checkpoint", **checkpoint["checkpoint"],
                                       "counts": checkpoint["counts"],
                                       "max_runtime_seconds": checkpoint["max_runtime_seconds"]})
            atomic_json(output / "status.json", checkpoint)
            print(json.dumps({"checkpoint": len(completed), **checkpoint["counts"],
                              "max_runtime_seconds": checkpoint["max_runtime_seconds"]}, sort_keys=True), flush=True)

    # A rank-potential witness that uses the recorded span is already an exact
    # replay of that span.  When its normalized span differs, check the source
    # span directly with the independent fixed-span encoding.  This is needed
    # because v1 retained only scalar span metadata, not certificate bytes.
    span_events_path = output / "reported-span-validation-events.jsonl"
    span_validations = reported_span_rows(span_events_path)
    for digest in span_validations:
        if digest not in source:
            raise ValueError(f"reported-span state has a digest absent from source: {digest}")
    for position, digest in enumerate(ordered, 1):
        if digest in span_validations:
            continue
        replay_event = completed.get(digest)
        if replay_event is None or replay_event["outcome"] != "valid":
            base = summary(len(source), completed, mismatch_count + 1, started, configuration,
                           "running", "missing_valid_replay", span_validations)
            isolate(output, "missing_valid_replay", {"canonical_sha256": digest}, base)
            raise RuntimeError(f"reported span cannot be checked without a valid replay: {digest}")
        reported_span = source[digest]["row"]["primary_span"]
        if replay_event["replay_span"] == reported_span:
            span_event = {
                "event": "reported_span_validation_completed",
                "sequence": position,
                "canonical_sha256": digest,
                "reported_span": reported_span,
                "method": "rank-potential-returned-certificate",
                "solver_status": replay_event["replay_solver_status"],
                "certificate_valid": True,
                "certificate_reason": replay_event["certificate_reason"],
                "validation_elapsed_seconds": 0.0,
                "outcome": "valid",
            }
        else:
            graph = by_hash[digest]
            validation_started = time.perf_counter()
            fixed_status, fixed_coloring = fixed_span_sat_solve(
                graph, reported_span, args.time_limit, workers=args.workers
            )
            validation_elapsed = time.perf_counter() - validation_started
            certificate_valid, certificate_reason = (
                verify_coloring(graph, fixed_coloring) if fixed_coloring is not None else (False, "no certificate")
            )
            if fixed_status in {"OPTIMAL", "FEASIBLE"} and certificate_valid:
                outcome = "valid"
            elif fixed_status == "UNKNOWN":
                outcome = "timeout"
            else:
                outcome = "mismatch"
            span_event = {
                "event": "reported_span_validation_completed",
                "sequence": position,
                "canonical_sha256": digest,
                "reported_span": reported_span,
                "method": "fixed-span-cpsat",
                "solver_status": fixed_status,
                "certificate_valid": certificate_valid,
                "certificate_reason": certificate_reason,
                "validation_elapsed_seconds": validation_elapsed,
                "outcome": outcome,
            }
        append_event(span_events_path, span_event)
        span_validations[digest] = span_event
        if span_event["outcome"] == "mismatch":
            mismatch_count += 1
            base = summary(len(source), completed, mismatch_count, started, configuration,
                           "running", "reported_span_mismatch", span_validations)
            isolate(output, "reported_span_certificate_mismatch", span_event, base)
            raise RuntimeError(f"reported-span certificate mismatch for {digest}")
        if len(span_validations) % args.checkpoint_every == 0 or len(span_validations) == len(source):
            checkpoint = summary(len(source), completed, mismatch_count, started, configuration,
                                 "running", "reported_span_checkpoint", span_validations)
            checkpoint["checkpoint"] = {
                "phase": "reported_span_validation", "completed": len(span_validations),
                "last_sequence": position, "last_hash": digest,
            }
            append_event(events_path, {"event": "reported_span_checkpoint", **checkpoint["checkpoint"],
                                       "counts": checkpoint["counts"],
                                       "max_runtime_seconds": checkpoint["max_runtime_seconds"]})
            atomic_json(output / "status.json", checkpoint)
            print(json.dumps({"reported_span_checkpoint": len(span_validations), **checkpoint["counts"],
                              "max_runtime_seconds": checkpoint["max_runtime_seconds"]}, sort_keys=True), flush=True)

    final = summary(len(source), completed, mismatch_count, started, configuration,
                    "complete", "all_source_colorable_decisions_replayed_and_reported_spans_validated",
                    span_validations)
    final["generation_diagnostics"] = diagnostics
    atomic_json(output / "status.json", final)
    atomic_json(output / "report.json", final)
    print(json.dumps({**final["counts"], "max_runtime_seconds": final["max_runtime_seconds"]}, sort_keys=True))


if __name__ == "__main__":
    main()
