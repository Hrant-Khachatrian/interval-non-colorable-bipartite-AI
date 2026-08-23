#!/usr/bin/env python3
"""Bounded prioritized exact audit for the generated 5+11 order-16 family."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import networkx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from interval_edge_coloring import (
    Graph,
    fixed_span_sat_solve,
    from_graph6,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
    weighted_hub_statistics,
)


def decode_graph(line: str) -> tuple[Graph, int]:
    order, raw_edges = from_graph6(line.rstrip())
    if order != 16:
        raise ValueError(f"expected order 16, got {order}")
    names = [f"x{i}" if i < 5 else f"y{i - 5}" for i in range(order)]
    graph = Graph(names, [(names[u], names[v]) for u, v in raw_edges],
                  [names[:5], names[5:]])
    return graph, len(raw_edges)


def passes_filters(graph: Graph) -> bool:
    degrees = list(graph.degrees.values())
    return (min(degrees) >= 2 and max(degrees) <= 11 and max(degrees) >= 6
            and len(set(degrees)) != 1 and networkx.is_connected(graph._nx))


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def select_candidates(source: Path, tail_lines: int, subset_limit: int):
    recent = collections.deque(maxlen=tail_lines)
    total = 0
    with source.open(encoding="ascii") as handle:
        for total, line in enumerate(handle, 1):
            recent.append(line.rstrip())

    eligible = 0
    ranked = []
    base = total - len(recent)
    for offset, line in enumerate(recent):
        graph, size = decode_graph(line)
        index = base + offset
        if not passes_filters(graph):
            continue
        eligible += 1
        margins = [row["margin"] for row in weighted_hub_statistics(graph)]
        metrics = (max(margins), sum(max(0, x) for x in margins), graph.delta, size)
        ranked.append((metrics, index, line, graph))
    ranked.sort(key=lambda item: tuple(-x for x in item[0]) + (item[1],))

    selected = []
    hashes = set()
    for metrics, index, line, graph in ranked[:subset_limit]:
        digest = nauty_canonical_hash(graph)
        if digest in hashes:
            continue
        hashes.add(digest)
        selected.append({"index": index, "source_line": line,
                         "canonical_sha256": digest,
                         "priority": {"best_margin": metrics[0],
                                      "positive_margin_sum": metrics[1],
                                      "delta": metrics[2], "size": metrics[3]}})
    summary = {"generated_total": total, "tail_scanned": len(recent),
               "eligible_in_tail": eligible, "selected_unique": len(selected)}
    return selected, summary


def confirm_negative(graph: Graph, span_limit: float, workers: int):
    spans = {}
    unknown = False
    for span in range(graph.delta, graph.n):
        status, coloring = fixed_span_sat_solve(
            graph, span, time_limit=span_limit, workers=workers)
        spans[str(span)] = {"solver_status": status, "has_coloring": coloring is not None}
        if status in ("OPTIMAL", "FEASIBLE"):
            ok, reason = verify_coloring(graph, coloring)
            if not ok:
                raise AssertionError(reason)
            return "solver_contradiction", spans
        if status == "UNKNOWN":
            unknown = True
            break
        if status != "INFEASIBLE":
            raise AssertionError(f"unexpected fixed-span status {status}")
    return "confirmation_timeout" if unknown else "confirmed_non_colorable", spans


def classify_one(task: dict) -> dict:
    graph, _ = decode_graph(task["source_line"])
    if not passes_filters(graph):
        raise AssertionError("candidate failed filters")
    primary = rank_potential_solve(graph, task["primary_time_limit"], task["solver_workers"])
    row = {**task, "size": graph.m, "delta": graph.delta,
           "minimum_degree": min(graph.degrees.values()), "degrees": graph.degrees,
           "weighted_hubs_best": weighted_hub_statistics(graph)[:3],
           "primary_status": primary.status, "primary_span": primary.span,
           "primary_solver_status": primary.solver_status,
           "primary_solver_seconds": primary.elapsed_seconds}

    if primary.status == "colorable":
        row["status"] = "colorable"
        coloring = {tuple(e): c for e, c in (primary.coloring or {}).items()}
        ok, reason = verify_coloring(graph, coloring)
        if not ok:
            raise AssertionError(reason)
    elif primary.status == "timeout":
        row["status"] = "timeout"
    elif primary.status == "non-colorable":
        # Preserve the negative candidate before independent confirmation.
        graph.save(task["negative_graph_path"])
        row["negative_saved_before_confirmation"] = True
        result, spans = confirm_negative(graph, task["span_time_limit"], task["solver_workers"])
        row["independent_confirmation"] = {
            "encoding": "fixed-span-sat", "spans": spans,
            "time_limit_per_span_seconds": task["span_time_limit"]}
        row["status"] = ("non_colorable" if result == "confirmed_non_colorable"
                         else "timeout" if result == "confirmation_timeout" else result)
    else:
        raise AssertionError(f"unexpected primary status {primary.status}")
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tail-lines", type=int, default=30000)
    parser.add_argument("--subset-limit", type=int, default=500)
    parser.add_argument("--max-processes", type=int, default=4)
    parser.add_argument("--solver-workers", type=int, default=2)
    parser.add_argument("--primary-time-limit", type=float, default=15.0)
    parser.add_argument("--span-time-limit", type=float, default=30.0)
    parser.add_argument("--deadline-seconds", type=float, default=3600.0)
    args = parser.parse_args()

    started = time.monotonic()
    candidates, selection = select_candidates(args.input, args.tail_lines, args.subset_limit)
    atomic_json(args.output_dir / "priority-selection.json",
                {"selection": selection, "candidates": candidates})
    negative_dir = args.output_dir / "candidate-graphs"
    classified_dir = args.output_dir / "classified"
    negative_dir.mkdir(parents=True, exist_ok=True)
    classified_dir.mkdir(parents=True, exist_ok=True)

    tasks = [{**candidate,
              "negative_graph_path": str(negative_dir / f"index-{candidate['index']:09d}.graph.json"),
              "solver_workers": args.solver_workers,
              "primary_time_limit": args.primary_time_limit,
              "span_time_limit": args.span_time_limit}
             for candidate in candidates]
    counts = collections.Counter()
    interrupted = False
    stopped_reason = "all_selected_classified"
    executor = concurrent.futures.ProcessPoolExecutor(max_workers=args.max_processes)
    try:
        futures = {executor.submit(classify_one, task): task for task in tasks}
        for future in concurrent.futures.as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                stopped_reason = f"worker_error:{type(exc).__name__}:{exc}"
                interrupted = True
                break
            index = row["index"]
            atomic_json(classified_dir / f"index-{index:09d}.json", row)
            counts[row["status"]] += 1
            with (args.output_dir / "classification-results.jsonl").open("a") as handle:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            if time.monotonic() - started >= args.deadline_seconds:
                stopped_reason = "classification_deadline_reached"
                interrupted = True
                break
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    summary = {
        "completion_status": "completed" if not interrupted else "partial_classification_complete",
        "stopped_reason": stopped_reason,
        "source": str(args.input),
        "generated_total": selection["generated_total"],
        "classification_scope": selection,
        "counts": {"classified": sum(counts.values()), "colorable": counts["colorable"],
                   "non_colorable": counts["non_colorable"], "timeout": counts["timeout"],
                   "solver_contradiction": counts["solver_contradiction"]},
        "parameters": {"tail_lines": args.tail_lines, "subset_limit": args.subset_limit,
                       "max_processes": args.max_processes,
                       "solver_workers": args.solver_workers,
                       "primary_time_limit_seconds": args.primary_time_limit,
                       "span_time_limit_seconds": args.span_time_limit,
                       "deadline_seconds": args.deadline_seconds,
                       "prioritization": "descending maximum weighted-hub margin"},
        "elapsed_seconds": time.monotonic() - started,
    }
    atomic_json(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
