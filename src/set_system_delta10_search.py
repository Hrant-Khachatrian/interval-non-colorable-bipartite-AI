#!/usr/bin/env python3
"""Bounded Lane-4 search over weighted intersecting set systems.

A construction is a multiset of pairwise-intersecting blocks. Connector
weight w on a pair of blocks adds w private points incident to exactly that
pair. The resulting incidence graph is simple and bipartite; this lane has no
fixed vertex-order bound, only the maximum-degree bound.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import math
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import networkx as nx

from interval_edge_coloring import (
    Graph,
    all_spans_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)


class DeadlineReached(RuntimeError):
    """Internal control-flow exception for graceful checkpointing."""


@dataclass(frozen=True)
class Construction:
    point_count: int
    block_masks: tuple[int, ...]
    connector_weights: tuple[int, ...]
    score: float
    score_detail: dict

    def descriptor(self) -> dict:
        return {
            "point_count": self.point_count,
            "block_masks": list(self.block_masks),
            "connector_weights": list(self.connector_weights),
        }


@dataclass
class SearchState:
    generated: int = 0
    rejected_degree: int = 0
    rejected_not_connected: int = 0
    unique: int = 0
    colorable: int = 0
    certified_non_colorable: int = 0
    primary_non_colorable: int = 0
    timeout: int = 0
    independent_unresolved: int = 0
    duplicate: int = 0

    def decision_counts(self) -> dict[str, int]:
        return {
            "colorable": self.colorable,
            "non_colorable": self.certified_non_colorable,
            "timeout": self.timeout,
        }

    def primary_counts(self) -> dict[str, int]:
        return {
            "colorable": self.colorable,
            "non_colorable": self.primary_non_colorable,
            "timeout": self.timeout,
        }


def mask_size(mask: int) -> int:
    return mask.bit_count()


def incidence_connected(point_count: int, masks: Sequence[int]) -> bool:
    graph = nx.Graph()
    graph.add_nodes_from(f"B{i}" for i in range(len(masks)))
    graph.add_nodes_from(f"P{point}" for point in range(point_count))
    for block_index, mask in enumerate(masks):
        for point in range(point_count):
            if mask & (1 << point):
                graph.add_edge(f"B{block_index}", f"P{point}")
    return nx.is_connected(graph)


def valid_base_family(
    point_count: int,
    masks: Sequence[int],
    *,
    minimum_block_size: int,
    maximum_delta: int,
) -> tuple[bool, str]:
    if not masks:
        return False, "empty"
    if any(
        not minimum_block_size <= mask_size(mask) <= maximum_delta
        for mask in masks
    ):
        return False, "block-size"
    universe = (1 << point_count) - 1
    if any(mask & ~universe for mask in masks):
        return False, "point-range"
    if any(not first & second for first, second in itertools.combinations(masks, 2)):
        return False, "pairwise-disjoint"
    if any((mask & universe) == 0 for mask in masks):
        return False, "empty-block"
    covered = 0
    for mask in masks:
        covered |= mask & universe
    if covered != universe:
        return False, "isolated-point"
    point_degrees = [
        sum((mask >> point) & 1 for mask in masks)
        for point in range(point_count)
    ]
    if any(degree == 0 or degree > maximum_delta for degree in point_degrees):
        return False, "degree"
    if not incidence_connected(point_count, masks):
        return False, "not-connected"
    return True, "ok"


def canonical_family(point_count: int, masks: Sequence[int]) -> tuple[int, ...]:
    """Return the minimum representative under ground-set relabeling."""
    representatives: list[tuple[int, ...]] = []
    for permutation in itertools.permutations(range(point_count)):
        relabeled = []
        for mask in masks:
            value = 0
            for old_point in range(point_count):
                if mask & (1 << old_point):
                    value |= 1 << permutation[old_point]
            relabeled.append(value)
        representatives.append(tuple(sorted(relabeled)))
    return min(representatives)


def base_families(
    *,
    point_counts: Sequence[int],
    block_counts_by_point_count: dict[int, tuple[int, int]],
    minimum_block_size: int,
    maximum_block_size: int,
    maximum_delta: int,
) -> Iterator[tuple[int, tuple[int, ...]]]:
    largest_universe = (1 << max(point_counts)) - 1
    allowed_masks = [
        mask
        for mask in range(largest_universe + 1)
        if minimum_block_size <= mask_size(mask) <= maximum_block_size
    ]
    for point_count in point_counts:
        universe = (1 << point_count) - 1
        local_masks = [mask for mask in allowed_masks if mask <= universe]
        low, high = block_counts_by_point_count[point_count]
        seen_families: set[tuple[int, ...]] = set()
        for block_count in range(low, high + 1):
            for combination in itertools.combinations_with_replacement(
                local_masks, block_count
            ):
                valid, _reason = valid_base_family(
                    point_count,
                    combination,
                    minimum_block_size=minimum_block_size,
                    maximum_delta=maximum_delta,
                )
                if not valid:
                    continue
                family = canonical_family(point_count, combination)
                if family in seen_families:
                    continue
                seen_families.add(family)
                block_sizes = [mask_size(mask) for mask in family]
                point_degrees = [
                    sum((mask >> point) & 1 for mask in family)
                    for point in range(point_count)
                ]
                is_irregular = (
                    len(set(block_sizes)) > 1 or len(set(point_degrees)) > 1
                )
                if not is_irregular and len(family) > 3:
                    continue
                yield point_count, family


def balanced_allocation(total: int, parts: int) -> tuple[int, ...]:
    if parts == 0:
        return ()
    base, remainder = divmod(max(0, total), parts)
    return tuple(base + (index < remainder) for index in range(parts))


def bounded_compositions(
    total: int, parts: int, maximum_part: int
) -> Iterator[tuple[int, ...]]:
    if parts == 0:
        if total == 0:
            yield ()
        return
    if parts == 1:
        if 0 <= total <= maximum_part:
            yield (total,)
        return

    def visit(prefix: list[int], remaining_total: int, remaining_parts: int) -> Iterator[tuple[int, ...]]:
        if remaining_parts == 1:
            if 0 <= remaining_total <= maximum_part:
                yield (*prefix, remaining_total)
            return
        upper = min(maximum_part, remaining_total)
        for value in range(upper + 1):
            prefix.append(value)
            yield from visit(prefix, remaining_total - value, remaining_parts - 1)
            prefix.pop()

    yield from visit([], total, parts)


def connector_patterns(
    block_count: int,
    *,
    maximum_total: int,
    maximum_weight: int,
) -> Iterator[tuple[int, ...]]:
    pairs = list(itertools.combinations(range(block_count), 2))
    empty = tuple([0] * len(pairs))
    yield empty
    emitted = {empty}

    structures: list[tuple[str, list[int]]] = [("all-pairs", list(range(len(pairs))))]
    for center in range(block_count):
        indices = [i for i, pair in enumerate(pairs) if center in pair]
        if len(indices) > 1:
            structures.append((f"star-{center}", indices))
    if block_count >= 3:
        path_indices = [pairs.index((i, i + 1)) for i in range(block_count - 1)]
        structures.append(("path", path_indices))
        cycle_indices = path_indices + [pairs.index((0, block_count - 1))]
        structures.append(("cycle", cycle_indices))
    matching_indices = [
        pairs.index((left, left + 1))
        for left in range(0, block_count - 1, 2)
        if (left, left + 1) in pairs
    ]
    if matching_indices:
        structures.append(("matching", matching_indices))

    for total in range(1, maximum_total + 1):
        for _name, selected in structures:
            allocation = balanced_allocation(total, len(selected))
            if any(weight > maximum_weight for weight in allocation):
                continue
            weights = [0] * len(pairs)
            for index, weight in zip(selected, allocation):
                weights[index] = weight
            pattern = tuple(weights)
            if pattern not in emitted:
                emitted.add(pattern)
                yield pattern

    # Small families get an exhaustive asymmetric assignment sweep as well.
    if block_count <= 4:
        for total in range(1, min(maximum_total, 6) + 1):
            for allocation in bounded_compositions(
                total, len(pairs), maximum_weight
            ):
                pattern = tuple(allocation)
                if pattern not in emitted:
                    emitted.add(pattern)
                    yield pattern


def build_graph(construction: Construction, maximum_delta: int) -> Graph:
    point_count = construction.point_count
    block_count = len(construction.block_masks)
    blocks = [f"B{i}" for i in range(block_count)]
    right = [f"P{point}" for point in range(point_count)]
    edges: list[tuple[str, str]] = []
    for block_index, mask in enumerate(construction.block_masks):
        for point in range(point_count):
            if mask & (1 << point):
                edges.append((blocks[block_index], f"P{point}"))

    pairs = list(itertools.combinations(range(block_count), 2))
    connector_pairs: dict[str, int] = {}
    for pair_index, ((left, other), weight) in enumerate(
        zip(pairs, construction.connector_weights)
    ):
        for number in range(weight):
            connector = f"C{pair_index}-{number}"
            right.append(connector)
            edges.extend(((blocks[left], connector), (blocks[other], connector)))
            connector_pairs[connector] = pair_index

    metadata = {
        "lane": "lane4-weighted-intersecting-set-systems",
        "point_count": point_count,
        "base_blocks_as_point_bitmasks": list(construction.block_masks),
        "block_sizes": [mask_size(mask) for mask in construction.block_masks],
        "point_multiplicities": [
            sum((mask >> point) & 1 for mask in construction.block_masks)
            for point in range(point_count)
        ],
        "connector_weights_by_block_pair": {
            f"{left},{other}": weight
            for (left, other), weight in zip(pairs, construction.connector_weights)
        },
        "connector_points": connector_pairs,
        "maximum_delta": maximum_delta,
    }
    return Graph(blocks + right, edges, [blocks, right], metadata)


def score_construction(graph: Graph) -> tuple[tuple[float, float, int], dict]:
    statistics = [row for row in weighted_hub_statistics(graph) if row["degree"] >= 3]
    finite_rows = []
    if statistics:
        for row in statistics:
            finite_row = dict(row)
            if math.isfinite(row["margin"]):
                finite_row["margin"] = int(row["margin"])
                finite_rows.append(finite_row)
            else:
                finite_row["margin"] = None
        ordered_margins = sorted(
            (
                row["margin"] if math.isfinite(row["margin"]) else -10_000
                for row in statistics
            ),
            reverse=True,
        )
        best_margin = ordered_margins[0]
        second_margin = (
            ordered_margins[1] if len(ordered_margins) > 1 else -10_000
        )
    else:
        best_margin = -10_000
        second_margin = -10_000
    maximum_degree = max(graph.degrees.values(), default=0)
    detail = {
        "best_weighted_hub_margin": best_margin,
        "second_best_weighted_hub_margin": second_margin,
        "sufficient_obstruction_hubs": sum(
            item["sufficient_obstruction"] for item in statistics
        ),
        "maximum_degree": maximum_degree,
        "weighted_hubs": finite_rows[:4],
    }
    return (best_margin, second_margin, maximum_degree), detail


def write_checkpoint(payload: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output_path)


def classify(
    constructions: Sequence[Construction],
    state: SearchState,
    *,
    maximum_delta: int,
    solver_time_limit: float,
    workers: int,
    deadline: float,
    output_path: Path,
    graph_directory: Path,
    payload: dict,
) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    negative_sequence = 0
    graph_directory.mkdir(parents=True, exist_ok=True)

    for construction in constructions:
        remaining = deadline - time.monotonic()
        minimum_useful_limit = min(1.0, solver_time_limit)
        if remaining < minimum_useful_limit:
            payload["completion"]["classification_exhausted"] = False
            payload["completion"]["deadline_reached"] = True
            break
        effective_limit = min(solver_time_limit, remaining)
        graph = build_graph(construction, maximum_delta)
        if graph.delta > maximum_delta:
            raise AssertionError("generated graph exceeds the degree cap")

        digest = nauty_canonical_hash(graph)
        if digest in seen:
            state.duplicate += 1
            continue
        seen.add(digest)
        state.unique += 1
        primary = rank_potential_solve(graph, effective_limit, workers)
        row = {
            "candidate_id": f"SS10-{state.unique:05d}",
            "construction": construction.descriptor(),
            "score_detail": construction.score_detail,
            "canonical_sha256": digest,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "primary_status": primary.status,
            "primary_span": primary.span,
            "primary_elapsed_seconds": primary.elapsed_seconds,
            "primary_solver_status": primary.solver_status,
        }

        if primary.status == "colorable":
            state.colorable += 1
        elif primary.status == "timeout":
            state.timeout += 1
        elif primary.status == "non-colorable":
            state.primary_non_colorable += 1
            negative_sequence += 1
            candidate_id = f"SS10-N{negative_sequence:04d}"
            graph.metadata["candidate_id"] = candidate_id
            graph.metadata["primary_negative"] = True
            path = graph_directory / f"{candidate_id}.graph.json"
            graph.save(path)
            saved_event = {
                "event": "primary_negative_saved",
                "candidate_id": candidate_id,
                "path": str(path),
                "canonical_sha256": digest,
            }
            row["saved_graph"] = saved_event
            print(json.dumps(saved_event, sort_keys=True), flush=True)

            independent = all_spans_solve(
                graph, effective_limit, workers, stop_on_timeout=False
            )
            statuses = [span["status"] for span in independent["spans"].values()]
            certified = bool(statuses) and all(
                status == "INFEASIBLE" for status in statuses
            )
            unresolved = "UNKNOWN" in statuses
            row["independent_decision"] = independent["decision"]
            row["independent_statuses_by_span"] = {
                str(span): value["status"]
                for span, value in independent["spans"].items()
            }
            row["certified_non_colorable"] = certified
            row["independent_unresolved"] = unresolved
            if unresolved:
                state.independent_unresolved += 1
            if certified:
                state.certified_non_colorable += 1
                certified_event = {
                    **saved_event,
                    "event": "certified_negative",
                }
                print(json.dumps(certified_event, sort_keys=True), flush=True)
        else:
            raise AssertionError(f"unexpected solver status {primary.status!r}")

        records.append(row)
        payload["counts"] = {
            "generated": state.generated,
            "unique": state.unique,
            "duplicate": state.duplicate,
            **state.decision_counts(),
        }
        payload["primary_counts"] = state.primary_counts()
        payload["certified_non_colorable"] = state.certified_non_colorable
        payload["primary_non_colorable_not_certified"] = (
            state.primary_non_colorable - state.certified_non_colorable
        )
        payload["independent_unresolved"] = state.independent_unresolved
        payload["records"] = records
        payload["elapsed_seconds"] = (
            time.perf_counter() - payload["configuration"]["perf_start"]
        )
        write_checkpoint(payload, output_path)
        print(
            json.dumps(
                {
                    "event": "classified",
                    "completed_unique": state.unique,
                    "planned_unique_cap": len(constructions),
                    "status": primary.status,
                    "elapsed_seconds": payload["elapsed_seconds"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
    else:
        payload["completion"]["classification_exhausted"] = True
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maximum-delta", type=int, default=10)
    parser.add_argument("--minimum-block-size", type=int, default=2)
    parser.add_argument("--maximum-block-size", type=int, default=5)
    parser.add_argument("--point-counts", type=int, nargs="+", default=[3, 4, 5, 6])
    parser.add_argument(
        "--block-count-ranges",
        default="3:8,3:6,3:5,3:3",
        help="One comma-separated LOW:HIGH range per requested point count.",
    )
    parser.add_argument("--maximum-total-connectors", type=int, default=8)
    parser.add_argument("--maximum-connector-weight", type=int, default=4)
    parser.add_argument("--candidate-cap", type=int, default=1500)
    parser.add_argument("--solver-time-limit", type=float, default=8.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--deadline-seconds", type=float, default=7200.0)
    parser.add_argument("--output", default="results/set-system-delta10.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    range_texts = args.block_count_ranges.split(",")
    if len(range_texts) != len(args.point_counts):
        raise SystemExit("one LOW:HIGH block-count range is required per point count")
    block_ranges: dict[int, tuple[int, int]] = {}
    for point_count, range_text in zip(args.point_counts, range_texts):
        low_text, high_text = range_text.split(":")
        block_ranges[point_count] = (int(low_text), int(high_text))

    perf_start = time.perf_counter()
    deadline = time.monotonic() + args.deadline_seconds
    output_path = Path(args.output)
    graph_directory = output_path.parent / "graphs" / "set_system_delta10"
    state = SearchState()
    payload: dict = {
        "schema_version": 1,
        "configuration": {
            "perf_start": perf_start,
            "deadline_seconds": args.deadline_seconds,
            "maximum_delta": args.maximum_delta,
            "point_counts": args.point_counts,
            "block_count_ranges": {
                str(key): list(value) for key, value in block_ranges.items()
            },
            "minimum_block_size": args.minimum_block_size,
            "maximum_block_size": args.maximum_block_size,
            "maximum_total_connectors": args.maximum_total_connectors,
            "maximum_connector_weight": args.maximum_connector_weight,
            "candidate_cap": args.candidate_cap,
            "solver_time_limit_seconds": args.solver_time_limit,
            "workers": args.workers,
            "construction": (
                "pairwise-intersecting block multiset plus integer-weight private "
                "degree-2 connector points for selected block pairs"
            ),
            "primary": "rank-potential CP-SAT",
            "independent_negative_confirmation": (
                "fixed-span CP-SAT over every legal span"
            ),
            "deduplication": "Nauty bipartition-colored canonical certificate",
            "vertex_order_bound": None,
        },
        "counts": {
            "generated": 0,
            "unique": 0,
            "duplicate": 0,
            "colorable": 0,
            "non_colorable": 0,
            "timeout": 0,
        },
        "primary_counts": {"colorable": 0, "non_colorable": 0, "timeout": 0},
        "certified_non_colorable": 0,
        "primary_non_colorable_not_certified": 0,
        "independent_unresolved": 0,
        "rejections": {"degree": 0, "not_connected": 0},
        "generation": {},
        "records": [],
        "completion": {
            "generation_exhausted": False,
            "classification_exhausted": False,
            "candidate_cap_reached": False,
            "deadline_reached": False,
            "complete": False,
        },
        "elapsed_seconds": 0.0,
    }
    write_checkpoint(payload, output_path)

    def install_handlers() -> tuple[object, object]:
        previous = (
            signal.signal(signal.SIGINT, interrupted),
            signal.signal(signal.SIGTERM, interrupted),
        )
        return previous

    def restore_handlers(previous: tuple[object, object]) -> None:
        signal.signal(signal.SIGINT, previous[0])
        signal.signal(signal.SIGTERM, previous[1])

    def interrupted(signum, _frame) -> None:
        raise DeadlineReached(f"received signal {signum}")

    previous_handlers = install_handlers()
    top_heap: list[tuple[tuple[float, float, int], int, Construction]] = []
    insertion_serial = 0
    generation_deadline_hit = False
    try:
        for point_count, masks in base_families(
            point_counts=args.point_counts,
            block_counts_by_point_count=block_ranges,
            minimum_block_size=args.minimum_block_size,
            maximum_block_size=args.maximum_block_size,
            maximum_delta=args.maximum_delta,
        ):
            if time.monotonic() >= deadline:
                generation_deadline_hit = True
                break
            for weights in connector_patterns(
                len(masks),
                maximum_total=args.maximum_total_connectors,
                maximum_weight=args.maximum_connector_weight,
            ):
                if time.monotonic() >= deadline:
                    generation_deadline_hit = True
                    break
                construction = Construction(point_count, masks, weights, 0.0, {})
                graph = build_graph(construction, args.maximum_delta)
                if graph.delta > args.maximum_delta:
                    state.rejected_degree += 1
                    continue
                if not nx.is_connected(nx.Graph(graph.edges)):
                    state.rejected_not_connected += 1
                    continue
                state.generated += 1
                score, detail = score_construction(graph)
                scored = Construction(
                    point_count,
                    masks,
                    weights,
                    score[0],
                    {"ranking_key": list(score), **detail},
                )
                entry = (score, insertion_serial, scored)
                insertion_serial += 1
                if len(top_heap) < args.candidate_cap:
                    heapq.heappush(top_heap, entry)
                elif entry[:2] > top_heap[0][:2]:
                    heapq.heapreplace(top_heap, entry)
            if generation_deadline_hit:
                break
    except DeadlineReached:
        generation_deadline_hit = True
    finally:
        restore_handlers(previous_handlers)

    prioritized = [
        entry[2]
        for entry in sorted(
            top_heap, key=lambda item: (item[0], item[1]), reverse=True
        )
    ]
    payload["rejections"] = {
        "degree": state.rejected_degree,
        "not_connected": state.rejected_not_connected,
    }
    payload["generation"] = {
        "valid_generated": state.generated,
        "prioritized_for_classification": len(prioritized),
        "insertion_order_considered": insertion_serial,
    }
    payload["completion"]["generation_exhausted"] = not generation_deadline_hit
    payload["completion"]["candidate_cap_reached"] = (
        state.generated > args.candidate_cap
        or len(prioritized) == args.candidate_cap
    )
    payload["counts"]["generated"] = state.generated
    payload["elapsed_seconds"] = time.perf_counter() - perf_start
    write_checkpoint(payload, output_path)
    print(
        json.dumps(
            {
                "event": "generation_complete",
                **payload["generation"],
                "rejections": payload["rejections"],
                "elapsed_seconds": payload["elapsed_seconds"],
            },
            sort_keys=True,
        ),
        flush=True,
    )

    records: list[dict] = []
    try:
        previous_handlers = install_handlers()
        records = classify(
            prioritized,
            state,
            maximum_delta=args.maximum_delta,
            solver_time_limit=args.solver_time_limit,
            workers=args.workers,
            deadline=deadline,
            output_path=output_path,
            graph_directory=graph_directory,
            payload=payload,
        )
    except DeadlineReached:
        payload["completion"]["classification_exhausted"] = False
        payload["completion"]["deadline_reached"] = True
    finally:
        restore_handlers(previous_handlers)

    elapsed = time.perf_counter() - perf_start
    completion = payload["completion"]
    completion["deadline_reached"] = (
        completion.get("deadline_reached", False)
        or time.monotonic() >= deadline
    )
    confirmation_clean = payload["independent_unresolved"] == 0
    completion["all_primary_negatives_confirmed"] = (
        state.primary_non_colorable == 0
        or state.certified_non_colorable == state.primary_non_colorable
    )
    completion["confirmation_has_unknown_spans"] = not confirmation_clean
    completion["enumeration_exhausted"] = bool(
        completion["generation_exhausted"]
    )
    completion["complete"] = bool(
        completion["generation_exhausted"]
        and completion["classification_exhausted"]
        and not completion["deadline_reached"]
        and confirmation_clean
    )
    payload["records"] = records
    payload["counts"] = {
        "generated": state.generated,
        "unique": state.unique,
        "duplicate": state.duplicate,
        **state.decision_counts(),
    }
    payload["primary_counts"] = state.primary_counts()
    payload["certified_non_colorable"] = state.certified_non_colorable
    payload["primary_non_colorable_not_certified"] = (
        state.primary_non_colorable - state.certified_non_colorable
    )
    payload["independent_unresolved"] = state.independent_unresolved
    payload["elapsed_seconds"] = elapsed
    payload["wall_clock_deadline_seconds_remaining"] = max(
        0.0, deadline - time.monotonic()
    )
    write_checkpoint(payload, output_path)
    compact = {
        key: payload[key]
        for key in ("counts", "primary_counts", "completion", "elapsed_seconds")
    }
    print(json.dumps({"event": "search_complete", **compact}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
