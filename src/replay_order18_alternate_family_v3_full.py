#!/usr/bin/env python3
"""Hash-keyed full replay of all 1,031 order-18 alternate v3 decisions.

The v3 aggregate contains 31 earlier residual decisions and the 1,000-decision
expanded-step3 campaign.  Every row is reconstructed and checked by its
canonical SHA-256 identifier, then solver-replayed in canonical-hash order.
Phase-local ranks are retained only as provenance.
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


ROOT = Path(__file__).resolve().parents[1]
RESIDUAL_EVENTS = ROOT / "results/order18-alternate-family-v3/v2-residual/classification-events.jsonl"
EXPANDED_EVENTS = ROOT / "results/order18-alternate-family-v3/expanded-step3/classification-events.jsonl"
DEFAULT_OUTPUT = ROOT / "results/order18-alternate-family-v3-full-replay"
EXPECTED_RESIDUAL = 31
EXPECTED_EXPANDED = 1000


def atomic_json(path: Path, value: dict) -> None:
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


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(event, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def completed_source_rows(path: Path, phase: str, expected: int) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            event = json.loads(line)
            if event.get("event") != "classification_completed":
                continue
            row = event.get("row")
            digest = row.get("canonical_sha256") if isinstance(row, dict) else None
            if not isinstance(digest, str) or not digest:
                raise ValueError(f"{phase} source line {line_number} lacks canonical_sha256")
            if digest in rows:
                raise ValueError(f"{phase} source line {line_number} duplicates digest {digest}")
            rows[digest] = {"phase": phase, "source_line": line_number, "row": row}
    if len(rows) != expected:
        raise ValueError(f"{phase}: expected {expected} completed rows, found {len(rows)}")
    return rows


def prior_events(path: Path, event_name: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            event = json.loads(line)
            if event.get("event") != event_name:
                continue
            digest = event.get("canonical_sha256")
            if not isinstance(digest, str) or digest in rows:
                raise ValueError(f"invalid or duplicate {event_name} event at {path}:{line_number}")
            rows[digest] = event
    return rows


def structural_checks(graph: Graph) -> dict[str, bool]:
    edges = list(graph.edges)
    left, right = map(set, graph.bipartition)
    return {
        "order_18": graph.n == 18,
        "simple": len(edges) == len(set(edges)) and all(u != v for u, v in edges),
        "connected": nx.is_connected(graph._nx),
        "bipartite": (
            nx.is_bipartite(graph._nx)
            and left | right == graph.vertex_set
            and not left & right
            and all((u in left) != (v in left) for u, v in edges)
        ),
        "minimum_degree_at_least_2": min(graph.degrees.values(), default=0) >= 2,
    }


def stored_field_mismatches(row: dict, graph: Graph) -> dict:
    reconstructed = {
        "order": graph.n,
        "size": graph.m,
        "bipartition_sizes": list(map(len, graph.bipartition)),
        "delta": graph.delta,
        "minimum_degree": min(graph.degrees.values()),
    }
    return {
        name: {"source": row.get(name), "reconstructed": value}
        for name, value in reconstructed.items()
        if row.get(name) != value
    }


def legal_span(graph: Graph, span: object) -> bool:
    return isinstance(span, int) and graph.delta <= span < graph.n


def all_span_check(graph: Graph, seconds: float, workers: int) -> dict:
    """Escalate an apparent negative without certifying a timeout as negative."""
    spans: dict[str, dict] = {}
    for span in range(graph.delta, graph.n):
        started = time.perf_counter()
        status, coloring = fixed_span_sat_solve(graph, span, seconds, workers=workers)
        elapsed = time.perf_counter() - started
        valid, reason = verify_coloring(graph, coloring) if coloring is not None else (False, "no certificate")
        spans[str(span)] = {
            "solver_status": status,
            "elapsed_seconds": elapsed,
            "certificate_valid": valid,
            "certificate_reason": reason,
        }
        if status in {"OPTIMAL", "FEASIBLE"} and valid:
            return {"conclusion": "colorable_disagrees_with_apparent_negative", "spans": spans}
        if status == "UNKNOWN":
            return {"conclusion": "unresolved_timeout", "spans": spans}
        if status != "INFEASIBLE":
            return {"conclusion": "solver_or_certificate_mismatch", "spans": spans}
    return {"conclusion": "confirmed_noncolorable", "spans": spans}


def make_summary(
    source_count: int,
    residual_count: int,
    reconstruction: dict,
    replayed: dict[str, dict],
    spans: dict[str, dict],
    started: float,
    configuration: dict,
    completion: str,
    reason: str,
) -> dict:
    replay_outcomes = collections.Counter(event["outcome"] for event in replayed.values())
    span_outcomes = collections.Counter(event["outcome"] for event in spans.values())
    runtimes = [event.get("replay_elapsed_seconds", 0.0) for event in replayed.values()]
    runtimes.extend(event.get("validation_elapsed_seconds", 0.0) for event in spans.values())
    return {
        "schema_version": 1,
        "source_events": {
            "v2_residual": str(RESIDUAL_EVENTS.relative_to(ROOT)),
            "expanded_step3": str(EXPANDED_EVENTS.relative_to(ROOT)),
        },
        "identity_join": "canonical_sha256 only; source phase ranks are reporting metadata, never reconstruction keys",
        "configuration": configuration,
        "completion": completion,
        "reason": reason,
        "reconstruction": reconstruction,
        "counts": {
            "source": source_count,
            "replayed": len(replayed),
            "valid": replay_outcomes["valid"],
            "mismatch": replay_outcomes["mismatch"] + span_outcomes["mismatch"],
            "span_valid": span_outcomes["valid"],
            "timeout": replay_outcomes["timeout"] + span_outcomes["timeout"],
        },
        "reported_span_validation": {
            "completed": len(spans),
            "valid": span_outcomes["valid"],
            "timeout": span_outcomes["timeout"],
            "mismatch": span_outcomes["mismatch"],
        },
        "phase_counts": {
            "v2_residual": residual_count,
            "expanded_step3": source_count - residual_count,
        },
        "max_runtime_seconds": max(runtimes, default=0.0),
        "elapsed_seconds": time.monotonic() - started,
    }


def checkpoint(output: Path, source_count: int, residual_count: int, reconstruction: dict,
               replayed: dict[str, dict], spans: dict[str, dict], started: float,
               configuration: dict, phase: str, sequence: int, digest: str) -> None:
    document = make_summary(source_count, residual_count, reconstruction, replayed, spans, started,
                            configuration, "running", "checkpoint")
    document["checkpoint"] = {"phase": phase, "completed": len(replayed) if phase == "rank_potential" else len(spans),
                              "last_sequence": sequence, "last_hash": digest}
    append_event(output / "replay-events.jsonl", {"event": "checkpoint", **document["checkpoint"],
                                                   "counts": document["counts"],
                                                   "max_runtime_seconds": document["max_runtime_seconds"]})
    atomic_json(output / "status.json", document)
    print(json.dumps({"checkpoint_phase": phase, **document["counts"],
                      "max_runtime_seconds": document["max_runtime_seconds"]}, sort_keys=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--time-limit", type=float, default=3.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--checkpoint-every", type=int, default=100)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.workers != 2:
        raise ValueError("this replay is specified for exactly two CP-SAT workers")
    if args.time_limit <= 0 or args.checkpoint_every <= 0:
        raise ValueError("time limit and checkpoint interval must be positive")

    output = args.output_dir.resolve()
    events_path = output / "replay-events.jsonl"
    if events_path.exists() and not args.resume:
        raise FileExistsError(f"replay state exists at {events_path}; pass --resume")
    try:
        os.nice(10)
        nice_applied = True
    except (AttributeError, OSError):
        nice_applied = False
    started = time.monotonic()
    configuration = {
        "rank_potential_solver": "CP-SAT",
        "rank_potential_time_limit_seconds": args.time_limit,
        "workers": args.workers,
        "source_campaign_workers": 1,
        "low_priority_nice_applied": nice_applied,
        "checkpoint_policy": f"append-only events and atomic compact state every {args.checkpoint_every} completed decisions",
        "span_policy": "replay witness when it has the recorded span; otherwise fixed-span CP-SAT at the recorded span",
        "negative_policy": "every apparent negative is independently checked over all legal spans; UNKNOWN is unresolved only",
    }

    residual = completed_source_rows(RESIDUAL_EVENTS, "v2-residual", EXPECTED_RESIDUAL)
    expanded = completed_source_rows(EXPANDED_EVENTS, "expanded-step3", EXPECTED_EXPANDED)
    overlap = sorted(set(residual) & set(expanded))
    if overlap:
        raise ValueError(f"source phases overlap in {len(overlap)} canonical hashes")
    target = {**residual, **expanded}
    all_source = target
    parents = {name: Graph.from_json(json.loads(path.read_text(encoding="utf-8"))) for name, path in PARENTS.items()}
    by_hash: dict[str, Graph] = {}
    preflight_errors: list[dict] = []
    for digest in sorted(all_source):
        entry = all_source[digest]
        row = entry["row"]
        try:
            graph = reconstruct(row, parents)
            checks = structural_checks(graph)
            observed = nauty_canonical_hash(graph)
            fields = stored_field_mismatches(row, graph)
            errors = []
            if observed != digest:
                errors.append("canonical_hash")
            if not all(checks.values()):
                errors.append("structural")
            if fields:
                errors.append("stored_fields")
            if row.get("status") != "colorable" or row.get("primary_status") != "colorable":
                errors.append("source_decision")
            if not legal_span(graph, row.get("primary_span")):
                errors.append("reported_span")
            if errors:
                preflight_errors.append({"canonical_sha256": digest, "phase": entry["phase"],
                                         "source_line": entry["source_line"], "errors": errors,
                                         "observed_hash": observed, "structural_checks": checks,
                                         "stored_field_mismatches": fields})
            else:
                by_hash[digest] = graph
        except Exception as exc:
            preflight_errors.append({"canonical_sha256": digest, "phase": entry["phase"],
                                     "source_line": entry["source_line"], "errors": ["reconstruction"],
                                     "detail": f"{type(exc).__name__}: {exc}"})
    reconstruction = {
        "source_graph_count": len(all_source),
        "target_graph_count": len(target),
        "expanded_step3_graph_count": len(expanded),
        "v2_residual_graph_count": len(residual),
        "canonical_hash_agreement_count": len(by_hash),
        "preflight_mismatch_count": len(preflight_errors),
        "preflight_mismatches": preflight_errors[:20],
    }
    if preflight_errors:
        append_event(events_path, {"event": "preflight_mismatch", "mismatches": preflight_errors[:20],
                                   "mismatch_count": len(preflight_errors)})
        report = make_summary(len(target), len(residual), reconstruction, {}, {}, started, configuration,
                              "escalated", "source_reconstruction_or_integrity_mismatch")
        atomic_json(output / "status.json", report)
        atomic_json(output / "report.json", report)
        raise RuntimeError(f"preflight found {len(preflight_errors)} source integrity mismatches")

    replayed = prior_events(events_path, "replay_completed") if args.resume else {}
    span_events_path = output / "reported-span-validation-events.jsonl"
    spans = prior_events(span_events_path, "reported_span_validation_completed") if args.resume else {}
    for digest in set(replayed) | set(spans):
        if digest not in target:
            raise ValueError(f"replay state includes a digest outside the 1,031 target decisions: {digest}")
    append_event(events_path, {"event": "replay_started", "resume": args.resume,
                               "source_target_decisions": len(target), "configuration": configuration})
    ordered = sorted(target)
    for sequence, digest in enumerate(ordered, 1):
        if digest in replayed:
            continue
        entry = target[digest]
        row = entry["row"]
        graph = by_hash[digest]
        attempt = time.perf_counter()
        replay = rank_potential_solve(graph, args.time_limit, workers=args.workers)
        elapsed = time.perf_counter() - attempt
        valid, reason = verify_coloring(graph, replay.coloring) if replay.coloring is not None else (False, "no certificate")
        negative_escalation = None
        if replay.status == "colorable" and valid:
            outcome = "valid"
        elif replay.status == "timeout":
            outcome = "timeout"
        else:
            # Do not turn a failed rank-potential solve into a negative claim.
            negative_escalation = all_span_check(graph, args.time_limit, args.workers)
            outcome = "timeout" if negative_escalation["conclusion"] == "unresolved_timeout" else "mismatch"
        event = {
            "event": "replay_completed", "sequence": sequence, "canonical_sha256": digest,
            "source_phase": entry["phase"], "source_line": entry["source_line"], "source_rank": row.get("rank"),
            "reported_span": row["primary_span"], "replay_status": replay.status,
            "replay_solver_status": replay.solver_status, "replay_span": replay.span,
            "replay_span_agrees_with_reported": replay.span == row["primary_span"],
            "certificate_valid": valid, "certificate_reason": reason,
            "replay_elapsed_seconds": elapsed, "negative_escalation": negative_escalation,
            "outcome": outcome,
        }
        append_event(events_path, event)
        replayed[digest] = event
        if outcome == "mismatch":
            report = make_summary(len(target), len(residual), reconstruction, replayed, spans, started, configuration,
                                  "escalated", "rank_potential_replay_mismatch")
            append_event(events_path, {"event": "mismatch_isolated", "detail": event})
            atomic_json(output / "status.json", report)
            atomic_json(output / "report.json", report)
            raise RuntimeError(f"rank-potential mismatch at {digest}")
        if len(replayed) % args.checkpoint_every == 0 or len(replayed) == len(target):
            checkpoint(output, len(target), len(residual), reconstruction, replayed, spans, started,
                       configuration, "rank_potential", sequence, digest)

    for sequence, digest in enumerate(ordered, 1):
        if digest in spans:
            continue
        replay = replayed.get(digest)
        if replay is None or replay["outcome"] != "valid":
            report = make_summary(len(target), len(residual), reconstruction, replayed, spans, started, configuration,
                                  "escalated", "reported_span_requires_valid_rank_potential_replay")
            atomic_json(output / "status.json", report)
            raise RuntimeError(f"reported span lacks a valid replay for {digest}")
        reported_span = target[digest]["row"]["primary_span"]
        if replay["replay_span"] == reported_span:
            span_event = {
                "event": "reported_span_validation_completed", "sequence": sequence,
                "canonical_sha256": digest, "reported_span": reported_span,
                "method": "rank-potential-returned-certificate", "solver_status": replay["replay_solver_status"],
                "certificate_valid": True, "certificate_reason": replay["certificate_reason"],
                "validation_elapsed_seconds": 0.0, "outcome": "valid",
            }
        else:
            attempt = time.perf_counter()
            status, coloring = fixed_span_sat_solve(by_hash[digest], reported_span, args.time_limit, workers=args.workers)
            elapsed = time.perf_counter() - attempt
            valid, reason = verify_coloring(by_hash[digest], coloring) if coloring is not None else (False, "no certificate")
            outcome = "valid" if status in {"OPTIMAL", "FEASIBLE"} and valid else "timeout" if status == "UNKNOWN" else "mismatch"
            span_event = {
                "event": "reported_span_validation_completed", "sequence": sequence,
                "canonical_sha256": digest, "reported_span": reported_span, "method": "fixed-span-cpsat",
                "solver_status": status, "certificate_valid": valid, "certificate_reason": reason,
                "validation_elapsed_seconds": elapsed, "outcome": outcome,
            }
        append_event(span_events_path, span_event)
        spans[digest] = span_event
        if span_event["outcome"] == "mismatch":
            report = make_summary(len(target), len(residual), reconstruction, replayed, spans, started, configuration,
                                  "escalated", "reported_span_validation_mismatch")
            append_event(events_path, {"event": "mismatch_isolated", "detail": span_event})
            atomic_json(output / "status.json", report)
            atomic_json(output / "report.json", report)
            raise RuntimeError(f"reported-span mismatch at {digest}")
        if len(spans) % args.checkpoint_every == 0 or len(spans) == len(target):
            checkpoint(output, len(target), len(residual), reconstruction, replayed, spans, started,
                       configuration, "reported_span_validation", sequence, digest)

    report = make_summary(len(target), len(residual), reconstruction, replayed, spans, started, configuration,
                          "complete", "all_1031_v3_colorable_decisions_replayed_and_recorded_spans_validated")
    atomic_json(output / "status.json", report)
    atomic_json(output / "report.json", report)
    print(json.dumps({**report["counts"], "max_runtime_seconds": report["max_runtime_seconds"]}, sort_keys=True))


if __name__ == "__main__":
    main()
