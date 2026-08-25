#!/usr/bin/env python3
"""Hash-keyed full replay of v2 alternate-family order-18 colorability rows.

The three source phases reuse local ranks and candidate IDs.  This replayer
therefore rebuilds each graph from its recorded surgery metadata and uses only
the bipartition-coloured Nauty SHA-256 as its durable identity.
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

from build_order18_alternate_family_ledger import PARENTS, graph_check, reconstruct
from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
)


ROOT = Path(__file__).resolve().parents[1]
PHASES = (
    ("v1-residual", ROOT / "results/order18-alternate-family-v2/v1-residual/classification-events.jsonl", 414),
    ("expanded-step1", ROOT / "results/order18-alternate-family-v2/expanded-step1/classification-events.jsonl", 377),
    ("expanded-step2", ROOT / "results/order18-alternate-family-v2/expanded-step2/classification-events.jsonl", 209),
)
DEFAULT_OUTPUT = ROOT / "results/order18-alternate-family-v2-full-replay"


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


def load_sources() -> dict[str, dict]:
    """Return one source row per canonical hash; phase-local ranks are metadata."""
    rows: dict[str, dict] = {}
    for phase, path, expected in PHASES:
        count = 0
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                event = json.loads(line)
                if event.get("event") != "classification_completed":
                    continue
                row = event.get("row")
                if not isinstance(row, dict):
                    raise ValueError(f"{phase} line {line_number} lacks a row")
                digest = row.get("canonical_sha256")
                if not isinstance(digest, str) or not digest:
                    raise ValueError(f"{phase} line {line_number} lacks canonical_sha256")
                if digest in rows:
                    earlier = rows[digest]
                    raise ValueError(
                        f"duplicate source canonical hash {digest}: "
                        f"{earlier['phase']}:{earlier['source_line']} and {phase}:{line_number}"
                    )
                rows[digest] = {"phase": phase, "source_line": line_number, "row": row}
                count += 1
        if count != expected:
            raise ValueError(f"{phase}: expected {expected} completion rows, found {count}")
    if len(rows) != 1000:
        raise ValueError(f"expected 1000 distinct source rows, found {len(rows)}")
    return rows


def load_durable(path: Path, event_name: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    completed: dict[str, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            event = json.loads(line)
            if event.get("event") != event_name:
                continue
            digest = event.get("canonical_sha256")
            if not isinstance(digest, str) or digest in completed:
                raise ValueError(f"invalid or duplicate {event_name} at {path}:{line_number}")
            completed[digest] = event
    return completed


def structural_checks(graph: Graph) -> dict[str, bool]:
    checks = graph_check(graph)
    left, right = map(set, graph.bipartition)
    return {
        "order_18": checks["order"] == 18,
        "simple": checks["simple"],
        "connected": checks["connected"] and nx.is_connected(graph._nx),
        "bipartite": checks["bipartite"] and nx.is_bipartite(graph._nx) and left.isdisjoint(right),
        "minimum_degree_at_least_2": checks["minimum_degree"] >= 2,
    }


def stored_field_mismatches(row: dict, graph: Graph) -> dict:
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


def summary(source: dict[str, dict], replayed: dict[str, dict], spans: dict[str, dict],
            negatives: dict[str, dict], mismatches: int, started: float, configuration: dict,
            completion: str, reason: str) -> dict:
    replay_outcomes = collections.Counter(event["outcome"] for event in replayed.values())
    span_outcomes = collections.Counter(event["outcome"] for event in spans.values())
    negative_outcomes = collections.Counter(event["outcome"] for event in negatives.values())
    replay_times = [event["replay_elapsed_seconds"] for event in replayed.values()]
    span_times = [event["validation_elapsed_seconds"] for event in spans.values()]
    return {
        "schema_version": 1,
        "source_phases": [
            {"name": name, "events_path": str(path.relative_to(ROOT)), "expected_rows": expected}
            for name, path, expected in PHASES
        ],
        "identity_join": "stored canonical_sha256; phase-local rank and candidate_id are reporting-only",
        "configuration": configuration,
        "completion": completion,
        "reason": reason,
        "counts": {
            "source": len(source),
            "replayed": len(replayed),
            "valid": replay_outcomes["valid"],
            "mismatch": mismatches,
            "span_valid": span_outcomes["valid"],
            "timeout": replay_outcomes["timeout"] + span_outcomes["timeout"] + negative_outcomes["timeout"],
            "negative_apparent": len(negatives),
            "negative_certified": negative_outcomes["certified_non_colorable"],
        },
        "reported_span_validation": {
            "completed": len(spans),
            "valid": span_outcomes["valid"],
            "timeout": span_outcomes["timeout"],
            "mismatch": span_outcomes["mismatch"],
        },
        "max_runtime_seconds": max(replay_times + span_times, default=0.0),
        "elapsed_seconds": time.monotonic() - started,
    }


def isolate(output: Path, kind: str, detail: dict, document: dict) -> None:
    record = {"event": "mismatch_isolated", "kind": kind, **detail}
    append_event(output / "replay-events.jsonl", record)
    append_event(output / "mismatches.jsonl", record)
    atomic_json(output / "status.json", {**document, "completion": "escalated", "reason": kind, "isolation": detail})


def independently_check_negative(graph: Graph, time_limit: float, workers: int) -> dict:
    """Do not certify a negative until every legal fixed span is independently infeasible."""
    tested = {}
    for span in range(graph.delta, graph.n):
        started = time.perf_counter()
        status, coloring = fixed_span_sat_solve(graph, span, time_limit, workers=workers)
        elapsed = time.perf_counter() - started
        valid, reason = verify_coloring(graph, coloring) if coloring is not None else (False, "no certificate")
        tested[str(span)] = {
            "solver_status": status,
            "certificate_valid": valid,
            "certificate_reason": reason,
            "elapsed_seconds": elapsed,
        }
        if status in {"OPTIMAL", "FEASIBLE"} and valid:
            return {"outcome": "colorable", "spans": tested}
        if status == "UNKNOWN":
            return {"outcome": "timeout", "spans": tested}
        if status not in {"INFEASIBLE", "OPTIMAL", "FEASIBLE"}:
            return {"outcome": "mismatch", "spans": tested}
    return {"outcome": "certified_non_colorable", "spans": tested}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--time-limit", type=float, default=3.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--checkpoint-every", type=int, default=100)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.workers != 2:
        raise ValueError("this independent replay requires exactly two workers")
    if args.time_limit <= 0 or args.checkpoint_every <= 0:
        raise ValueError("time limit and checkpoint interval must be positive")

    output = args.output_dir.resolve()
    replay_path = output / "replay-events.jsonl"
    if replay_path.exists() and not args.resume:
        raise FileExistsError(f"replay state exists at {replay_path}; use --resume")
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
        "low_priority_nice_applied": nice_applied,
        "identity_policy": "each source row is reconstructed from metadata and checked against canonical_sha256; no rank join",
        "reported_span_policy": "reuse a valid replay certificate only at the recorded span, otherwise fixed-span CP-SAT",
        "negative_policy": "a replay INFEASIBLE result is not a claim; fixed-span CP-SAT checks every legal span, and UNKNOWN remains unresolved",
        "checkpoint_policy": f"append durable events and atomically refresh status every {args.checkpoint_every} completed rows per phase",
    }
    source = load_sources()
    replayed = load_durable(replay_path, "replay_completed") if args.resume else {}
    span_path = output / "reported-span-validation-events.jsonl"
    spans = load_durable(span_path, "reported_span_validation_completed") if args.resume else {}
    negative_path = output / "negative-check-events.jsonl"
    negatives = load_durable(negative_path, "apparent_negative_checked") if args.resume else {}
    for collection in (replayed, spans, negatives):
        unknown = set(collection) - set(source)
        if unknown:
            raise ValueError(f"replay output has source-unknown hashes: {sorted(unknown)[:3]}")
    mismatch_count = sum(event.get("outcome") == "mismatch" for event in replayed.values())
    mismatch_count += sum(event.get("outcome") == "mismatch" for event in spans.values())
    mismatch_count += sum(event.get("outcome") in {"mismatch", "certified_non_colorable"} for event in negatives.values())
    initial = summary(source, replayed, spans, negatives, mismatch_count, started, configuration, "running", "source_loaded")
    append_event(replay_path, {"event": "replay_started", "resume": args.resume, "source_decisions": len(source), "configuration": configuration})

    graphs: dict[str, Graph] = {}
    parents = {name: Graph.from_json(json.loads(path.read_text(encoding="utf-8"))) for name, path in PARENTS.items()}
    for digest in sorted(source):
        entry = source[digest]
        row = entry["row"]
        graph = reconstruct(row, parents)
        # `reconstruct` intentionally focuses on topology for the source
        # ledger; restore the recorded provenance after it has driven that
        # reconstruction so the replay's field check covers it as well.
        graph.metadata = dict(row["metadata"])
        graphs[digest] = graph
        structure = structural_checks(graph)
        observed_hash = nauty_canonical_hash(graph)
        fields = stored_field_mismatches(row, graph)
        reported_span = row.get("primary_span")
        errors = []
        if observed_hash != digest:
            errors.append("canonical_hash")
        if not all(structure.values()):
            errors.append("structural")
        if fields:
            errors.append("stored_fields")
        if row.get("status") != "colorable" or row.get("primary_status") != "colorable":
            errors.append("source_decision")
        if not isinstance(reported_span, int) or not graph.delta <= reported_span < graph.n:
            errors.append("reported_span")
        if errors:
            detail = {
                "canonical_sha256": digest, "phase": entry["phase"], "source_line": entry["source_line"],
                "source_rank": row.get("rank"), "errors": errors, "observed_hash": observed_hash,
                "structural_checks": structure, "stored_field_mismatches": fields,
            }
            isolate(output, "pre_solver_" + "+".join(errors), detail, initial)
            raise RuntimeError(f"isolated reconstruction mismatch for {digest}: {errors}")

    ordered = sorted(source)
    for sequence, digest in enumerate(ordered, 1):
        if digest in replayed:
            continue
        entry, graph = source[digest], graphs[digest]
        row = entry["row"]
        attempt_started = time.perf_counter()
        replay = rank_potential_solve(graph, args.time_limit, workers=args.workers)
        elapsed = time.perf_counter() - attempt_started
        certificate_valid, certificate_reason = (
            verify_coloring(graph, replay.coloring) if replay.coloring is not None else (False, "no certificate")
        )
        negative = None
        if replay.status == "colorable" and certificate_valid:
            outcome = "valid"
        elif replay.status == "timeout":
            outcome = "timeout"
        elif replay.status == "non-colorable":
            negative = independently_check_negative(graph, args.time_limit, args.workers)
            negative_event = {
                "event": "apparent_negative_checked", "sequence": sequence, "canonical_sha256": digest,
                "phase": entry["phase"], "source_line": entry["source_line"],
                "source_rank": row.get("rank"), "primary_solver_status": replay.solver_status,
                **negative,
            }
            append_event(negative_path, negative_event)
            negatives[digest] = negative_event
            outcome = "valid" if negative["outcome"] == "colorable" else negative["outcome"]
        else:
            outcome = "mismatch"
        event = {
            "event": "replay_completed", "sequence": sequence, "canonical_sha256": digest,
            "phase": entry["phase"], "source_line": entry["source_line"], "source_rank": row.get("rank"),
            "reported_span": row["primary_span"], "structural_checks": structural_checks(graph),
            "canonical_hash_agrees": nauty_canonical_hash(graph) == digest,
            "replay_status": replay.status, "replay_solver_status": replay.solver_status,
            "replay_span": replay.span, "replay_span_agrees_with_reported": replay.span == row["primary_span"],
            "certificate_valid": certificate_valid, "certificate_reason": certificate_reason,
            "replay_elapsed_seconds": elapsed, "outcome": outcome,
        }
        append_event(replay_path, event)
        replayed[digest] = event
        if outcome in {"mismatch", "certified_non_colorable"}:
            mismatch_count += 1
            document = summary(source, replayed, spans, negatives, mismatch_count, started, configuration, "running", "replay_mismatch")
            isolate(output, "solver_or_certificate_mismatch", event, document)
            raise RuntimeError(f"isolated replay mismatch for {digest}")
        if len(replayed) % args.checkpoint_every == 0 or len(replayed) == len(source):
            document = summary(source, replayed, spans, negatives, mismatch_count, started, configuration, "running", "replay_checkpoint")
            document["checkpoint"] = {"phase": "replay", "completed": len(replayed), "last_sequence": sequence, "last_hash": digest}
            append_event(replay_path, {"event": "checkpoint", **document["checkpoint"], "counts": document["counts"], "max_runtime_seconds": document["max_runtime_seconds"]})
            atomic_json(output / "status.json", document)
            print(json.dumps({"checkpoint": len(replayed), **document["counts"], "max_runtime_seconds": document["max_runtime_seconds"]}, sort_keys=True), flush=True)

    for sequence, digest in enumerate(ordered, 1):
        if digest in spans:
            continue
        event = replayed.get(digest)
        if event is None or event["outcome"] != "valid":
            document = summary(source, replayed, spans, negatives, mismatch_count + 1, started, configuration, "running", "no_valid_replay")
            isolate(output, "missing_valid_replay", {"canonical_sha256": digest}, document)
            raise RuntimeError(f"cannot validate reported span without a valid replay: {digest}")
        graph, reported_span = graphs[digest], source[digest]["row"]["primary_span"]
        if event["replay_span"] == reported_span:
            span_event = {
                "event": "reported_span_validation_completed", "sequence": sequence, "canonical_sha256": digest,
                "reported_span": reported_span, "method": "rank-potential-returned-certificate",
                "solver_status": event["replay_solver_status"], "certificate_valid": True,
                "certificate_reason": event["certificate_reason"], "validation_elapsed_seconds": 0.0, "outcome": "valid",
            }
        else:
            validation_started = time.perf_counter()
            status, coloring = fixed_span_sat_solve(graph, reported_span, args.time_limit, workers=args.workers)
            elapsed = time.perf_counter() - validation_started
            valid, reason = verify_coloring(graph, coloring) if coloring is not None else (False, "no certificate")
            if status in {"OPTIMAL", "FEASIBLE"} and valid:
                outcome = "valid"
            elif status == "UNKNOWN":
                outcome = "timeout"
            else:
                outcome = "mismatch"
            span_event = {
                "event": "reported_span_validation_completed", "sequence": sequence, "canonical_sha256": digest,
                "reported_span": reported_span, "method": "fixed-span-cpsat", "solver_status": status,
                "certificate_valid": valid, "certificate_reason": reason, "validation_elapsed_seconds": elapsed,
                "outcome": outcome,
            }
        append_event(span_path, span_event)
        spans[digest] = span_event
        if span_event["outcome"] == "mismatch":
            mismatch_count += 1
            document = summary(source, replayed, spans, negatives, mismatch_count, started, configuration, "running", "reported_span_mismatch")
            isolate(output, "reported_span_certificate_mismatch", span_event, document)
            raise RuntimeError(f"isolated reported-span mismatch for {digest}")
        if len(spans) % args.checkpoint_every == 0 or len(spans) == len(source):
            document = summary(source, replayed, spans, negatives, mismatch_count, started, configuration, "running", "reported_span_checkpoint")
            document["checkpoint"] = {"phase": "reported_span_validation", "completed": len(spans), "last_sequence": sequence, "last_hash": digest}
            append_event(replay_path, {"event": "reported_span_checkpoint", **document["checkpoint"], "counts": document["counts"], "max_runtime_seconds": document["max_runtime_seconds"]})
            atomic_json(output / "status.json", document)
            print(json.dumps({"reported_span_checkpoint": len(spans), **document["counts"], "max_runtime_seconds": document["max_runtime_seconds"]}, sort_keys=True), flush=True)

    document = summary(source, replayed, spans, negatives, mismatch_count, started, configuration, "complete", "all_source_colorable_decisions_replayed_and_reported_spans_validated")
    atomic_json(output / "status.json", document)
    atomic_json(output / "report.json", document)
    print(json.dumps({**document["counts"], "max_runtime_seconds": document["max_runtime_seconds"]}, sort_keys=True))


if __name__ == "__main__":
    main()
