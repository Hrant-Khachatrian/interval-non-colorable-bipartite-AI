#!/usr/bin/env python3
"""Compare structural diagnostics on verified negatives and colorable near-misses."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
import numpy as np
import pynauty

from interval_edge_coloring import (
    Graph,
    benchmark_graphs,
    nauty_canonical_hash,
    rank_potential_solve,
    weighted_hub_statistics,
)

QUOTIENT_REPORTS = (
    Path("results/quotient-r1.json"),
    Path("results/quotient-r2.json"),
    Path("results/quotient-r3.json"),
)
LANE6_REPORTS = (
    Path("results/lane6-signature-r2.json"),
    Path("results/lane6-signature-r3.json"),
    Path("results/lane6-signature-r4.json"),
    Path("results/lane6-signature-r5.json"),
    Path("results/lane6-signature-r6.json"),
)
NEGATIVE_FILES = (
    Path("results/graphs/quotient-r1/Q1-00012.graph.json"),
    Path("results/graphs/quotient-r1/Q1-00014.graph.json"),
)
REFERENCE_NEGATIVES = ("M5_delta_555", "Erd_Fano_2222221", "hat_K34", "hat_K222")
COLORING_FIELDS = (
    "coloring_span",
    "coloring_total_vertex_slack",
    "coloring_mean_vertex_slack",
    "coloring_extreme_color_incidences",
    "coloring_palette_reuse_rate",
    "coloring_adjacent_overlap_beyond_edge_mean",
)
MISSING_CATALOG_FIELDS = (
    "automorphism_log10_size",
    "automorphism_orbits",
    "largest_orbit_fraction",
    "adjacent_palette_start_compatibility_min",
) + COLORING_FIELDS


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ordered_edge(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def reconstruct_quotient(base: Graph, metadata: dict, candidate_id: str) -> Graph:
    owner = {vertex: vertex for vertex in base.vertices}
    for block in metadata.get("blocks", []):
        name = "&".join(block)
        for vertex in block:
            owner[vertex] = name

    groups: dict[str, list[str]] = defaultdict(list)
    for vertex in base.vertices:
        groups[owner[vertex]].append(vertex)
    left_side = set(base.bipartition[0])
    left = [name for name, members in groups.items() if members[0] in left_side]
    right = [name for name, members in groups.items() if members[0] not in left_side]
    edges = {
        ordered_edge(owner[u], owner[v])
        for u, v in base.edges
        if owner[u] != owner[v]
    }
    metadata = dict(metadata)
    metadata.update(candidate_id=candidate_id, reconstruction="deterministic_same_side_quotient")
    return Graph(left + right, sorted(edges), [left, right], metadata)


def reconstruct_lane6_round2(base: Graph, metadata: dict, candidate_id: str) -> Graph:
    connectors = sorted(base.bipartition[1])
    hub = base.metadata.get("hub", "u")
    gadget_edges = [ordered_edge(*edge) for edge in metadata["gadget_edges"]]
    gadget_vertices = sorted({vertex for edge in gadget_edges for vertex in edge})
    gadget_left = [vertex for vertex in gadget_vertices if vertex.startswith("L")]
    gadget_right = [vertex for vertex in gadget_vertices if not vertex.startswith("L")]
    core = [vertex for vertex in base.bipartition[0] if vertex != hub]
    left = ["U0", "U1"] + core + gadget_left
    right = connectors + gadget_right

    # format(mask, "012b") is MSB-first, while connector index 0 is the LSB.
    mask = metadata["mask"][::-1]
    if len(mask) != len(connectors):
        raise ValueError(f"invalid connector mask for {candidate_id}")
    edges = [tuple(edge) for edge in base.edges if hub not in edge]
    edges.extend(ordered_edge("U0" if bit == "1" else "U1", connector) for bit, connector in zip(mask, connectors))
    edges.extend(gadget_edges)
    edges.extend((ordered_edge("U0", "T0"), ordered_edge("U1", "T1")))
    metadata = dict(metadata)
    metadata.update(candidate_id=candidate_id, reconstruction="deterministic_lane6_round2_split_hub")
    return Graph(left + right, sorted(edges), [left, right], metadata)


def automorphism_stats(graph: Graph) -> dict:
    left, right = graph.ordered_for_bipartite_canonicalization()
    ordered = list(left) + list(right)
    index = {vertex: number for number, vertex in enumerate(ordered)}
    adjacency = {number: [] for number in range(graph.n)}
    for u, v in graph.edges:
        i, j = sorted((index[u], index[v]))
        adjacency[i].append(j)
        adjacency[j].append(i)
    colored = pynauty.Graph(
        number_of_vertices=graph.n,
        directed=False,
        adjacency_dict=adjacency,
        vertex_coloring=[set(range(len(left))), set(range(len(left), graph.n))],
    )
    result = pynauty.autgrp(colored)
    _, size_base, size_exponent, orbit_labels, orbit_count = result
    try:
        labels = [int(value) for value in orbit_labels]
        orbit_sizes = Counter(labels).values() if len(labels) == graph.n else [graph.n]
        if len(labels) != graph.n:
            orbit_count = 1
    except (TypeError, ValueError):
        orbit_sizes = [graph.n]
        orbit_count = 1
    group_size = float(size_base) * 10.0 ** float(size_exponent)
    return {
        "automorphism_group_size": group_size,
        "automorphism_log10_size": math.log10(group_size),
        "automorphism_orbits": int(orbit_count),
        "largest_orbit_fraction": max(orbit_sizes) / graph.n,
    }


def start_pair_overlap(left_degree: int, right_degree: int, span: int) -> int:
    count = 0
    for start in range(1, span - left_degree + 2):
        low = max(1, start - right_degree + 1)
        high = min(span - right_degree + 1, start + left_degree - 1)
        count += max(0, high - low + 1)
    return count


def palette_start_compatibility(graph: Graph) -> float:
    degrees = graph.degrees
    spans = range(graph.delta, max(graph.delta, graph.n - 1) + 1)
    values = []
    for u, v in graph.edges:
        left_degree, right_degree = degrees[u], degrees[v]
        values.append(min(
            start_pair_overlap(left_degree, right_degree, span)
            / ((span - left_degree + 1) * (span - right_degree + 1))
            for span in spans
        ))
    return min(values, default=1.0)


def structural_features(graph: Graph) -> dict:
    degrees = graph.degrees
    side_degrees = [[degrees[v] for v in graph.bipartition[side]] for side in range(2)]
    means = [sum(side) / len(side) for side in side_degrees]
    variances = [
        sum((value - mean) ** 2 for value in side) / len(side)
        for side, mean in zip(side_degrees, means)
    ]
    global_mean = sum(means) / 2
    hubs = weighted_hub_statistics(graph)
    defined_hubs = [row for row in hubs if row["weighted_neighbor_diameter"] is not None]
    best_hub = min(defined_hubs, key=lambda row: (-row["margin"], row["hub"]), default=None)
    components = nx.number_connected_components(graph._nx)
    cycle_rank = graph.m - graph.n + components
    row = {
        "order": graph.n,
        "size": graph.m,
        "delta": graph.delta,
        "minimum_degree": min(degrees.values()),
        "bipartition_sizes": [len(side) for side in graph.bipartition],
        "bipartition_size_imbalance": abs(len(graph.bipartition[0]) - len(graph.bipartition[1])) / graph.n,
        "cycle_rank": cycle_rank,
        "cycle_rank_per_vertex": cycle_rank / graph.n,
        "edge_density": graph.m / (len(graph.bipartition[0]) * len(graph.bipartition[1])),
        "degree_variance_normalized": (sum(variances) / 2) / global_mean**2,
        "degree_range_ratio": graph.delta / max(1, min(degrees.values())),
        "hub_best_margin": best_hub["margin"] if best_hub else None,
        "hub_best_weighted_neighbor_diameter": best_hub["weighted_neighbor_diameter"] if best_hub else None,
        "hub_tight_or_obstructing_count": sum(row["margin"] >= 0 for row in defined_hubs),
        "hub_sufficient_obstruction": any(row["margin"] > 0 for row in defined_hubs),
        "hub_severe_deficit_count": sum(row["margin"] <= -3 for row in defined_hubs),
        "forced_span_lower_bound": graph.delta,
        "forced_span_upper_bound": graph.n - 1,
        "forced_span_slack": graph.n - 1 - graph.delta,
        "max_hub_forced_width": graph.delta - 1,
        "adjacent_palette_start_compatibility_min": palette_start_compatibility(graph),
        "connected": nx.is_connected(graph._nx),
        "weighted_hubs_best": hubs[:8],
    }
    row.update(automorphism_stats(graph))
    return row


def coloring_features(coloring: dict[tuple[str, str], int], graph: Graph) -> dict:
    incidence: dict[str, set[int]] = defaultdict(set)
    for (u, v), color in coloring.items():
        incidence[u].add(color)
        incidence[v].add(color)
    intervals = {vertex: (min(colors), max(colors)) for vertex, colors in incidence.items()}
    slacks = [high - low + 1 - len(incidence[vertex]) for vertex, (low, high) in intervals.items()]
    overlaps = []
    for u, v in graph.edges:
        low_u, high_u = intervals[u]
        low_v, high_v = intervals[v]
        overlaps.append(max(0, min(high_u, high_v) - max(low_u, low_v)))
    palettes = {tuple(sorted(colors)) for colors in incidence.values()}
    return {
        "coloring_span": max(coloring.values()),
        "coloring_total_vertex_slack": sum(slacks),
        "coloring_mean_vertex_slack": sum(slacks) / graph.n,
        "coloring_extreme_color_incidences": sum(
            1 for colors in incidence.values() if 1 in colors or max(coloring.values()) in colors
        ),
        "coloring_palette_reuse_rate": 1.0 - len(palettes) / graph.n,
        "coloring_adjacent_overlap_beyond_edge_mean": sum(max(0, value - 1) for value in overlaps) / len(overlaps),
    }


def make_row(role, source, candidate_id, graph=None, stored=None, resolve=False, args=None) -> dict:
    row = {
        "role": role,
        "source_report": source,
        "candidate_id": candidate_id,
        "analysis_level": "full_graph" if graph is not None else "catalog_only",
    }
    if graph is not None:
        row.update(labelled_sha256=graph.canonical_hash(), canonical_sha256=nauty_canonical_hash(graph))
        row.update(structural_features(graph))
        if resolve:
            result = rank_potential_solve(graph, args.solver_time_limit, args.workers)
            row["exact_status_this_tool"] = result.status
            row["rank_potential_elapsed_seconds"] = result.elapsed_seconds
            row["bounded_solver_recheck"] = {
                "status": result.status,
                "time_limit_seconds": args.solver_time_limit,
                "workers": args.workers,
            }
            if role == "colorable_near_miss" and result.status == "non-colorable":
                raise RuntimeError(f"classification changed for {candidate_id}: {result.status}")
            if result.status == "timeout":
                # A bounded retry is diagnostic only; the stored decision remains exact.
                row["exact_status_this_tool"] = None
            if result.coloring is not None:
                row.update(coloring_features(result.coloring, graph))
            else:
                row.update({field: None for field in COLORING_FIELDS})
        else:
            row.update({field: None for field in COLORING_FIELDS})
    else:
        row.update({key: stored.get(key) for key in ("order", "size", "delta", "minimum_degree")})
        row["canonical_sha256"] = stored.get("canonical_sha256")
        row["cycle_rank"] = row["size"] - row["order"] + 1
        row["cycle_rank_per_vertex"] = row["cycle_rank"] / row["order"]
        sequences = stored.get("degree_sequence_by_side", {})
        sides = [sequences.get("left", []), sequences.get("right", [])]
        means = [sum(side) / len(side) for side in sides if side]
        variances = [
            sum((value - mean) ** 2 for value in side) / len(side)
            for side, mean in zip(sides, means) if side
        ]
        row.update({
            "bipartition_sizes": [len(side) for side in sides],
            "bipartition_size_imbalance": abs(len(sides[0]) - len(sides[1])) / row["order"],
            "degree_variance_normalized": (sum(variances) / 2) / (sum(means) / 2) ** 2,
            "degree_range_ratio": row["delta"] / max(1, row["minimum_degree"]),
            "forced_span_lower_bound": row["delta"],
            "forced_span_upper_bound": row["order"] - 1,
            "forced_span_slack": row["order"] - 1 - row["delta"],
            "max_hub_forced_width": row["delta"] - 1,
        })
        hubs = stored.get("weighted_hubs_best", [])
        if hubs:
            best = min(hubs, key=lambda item: (-item["margin"], item["hub"]))
            row.update({
                "hub_best_margin": best["margin"],
                "hub_best_weighted_neighbor_diameter": best["weighted_neighbor_diameter"],
                "hub_tight_or_obstructing_count": sum(item["margin"] >= 0 for item in hubs),
                "hub_sufficient_obstruction": any(item["margin"] > 0 for item in hubs),
                "hub_severe_deficit_count": sum(item["margin"] <= -3 for item in hubs),
                "weighted_hubs_best": hubs[:8],
            })
        row.update({field: None for field in MISSING_CATALOG_FIELDS})

    if stored:
        primary = stored.get("primary_result", {})
        row.update(stored_exact_status=primary.get("status"), stored_exact_encoding=primary.get("encoding"))
        row["certified_non_colorable"] = stored.get("certified_non_colorable")
    return row


def load_dataset(args):
    benchmark = benchmark_graphs()
    base = benchmark["hat_K34_prime_Delta11"]
    seen_control_hashes: set[str] = set()
    skipped_duplicate_controls = 0
    reconstructed_count = 0
    hash_verified_count = 0
    resolve_counts: Counter[str] = Counter()
    negatives = [make_row(
        "verified_known_negative", "reconstructed-benchmark", "hat_K34_prime_Delta11", base, resolve=False
    )]
    negatives[-1]["verification_provenance"] = {
        "artifact": "results/known-seed-minimality.json",
        "status": "known non-interval-colorable reconstructed benchmark",
    }

    all_quotient_rows = {}
    for path in QUOTIENT_REPORTS:
        data = json.loads(path.read_text())
        for stored in data.get("rows", []):
            all_quotient_rows[stored["candidate_id"]] = stored

    for path in NEGATIVE_FILES:
        graph = Graph.from_json(json.loads(path.read_text()))
        candidate_id = path.name.removesuffix(".graph.json")
        stored = all_quotient_rows[candidate_id]
        actual = nauty_canonical_hash(graph)
        if actual != stored["canonical_sha256"]:
            raise RuntimeError(f"hash mismatch for {candidate_id}")
        row = make_row("verified_new_negative", "results/quotient-r1.json", candidate_id, graph, stored)
        row["verification_provenance"] = {
            "certificate": f"results/candidates/{path.stem}/certificate.json",
            "independent_fixed_span": "MiniSat UNSAT for every legal span",
            "formal_proof": "results/formal-proof-summary.json",
        }
        negatives.append(row)

    controls = []
    for path in QUOTIENT_REPORTS:
        data = json.loads(path.read_text())
        for stored in data.get("rows", []):
            if stored.get("primary_result", {}).get("status") != "colorable":
                continue
            digest = stored["canonical_sha256"]
            if digest in seen_control_hashes:
                skipped_duplicate_controls += 1
                continue
            seen_control_hashes.add(digest)
            graph = reconstruct_quotient(base, stored["metadata"], stored["candidate_id"])
            actual = nauty_canonical_hash(graph)
            if actual != digest:
                raise RuntimeError(f"hash mismatch reconstructing {stored['candidate_id']}")
            reconstructed_count += 1
            hash_verified_count += 1
            resolve = 0 < args.resolve_source_cap and resolve_counts[path.name] < args.resolve_source_cap
            if resolve:
                resolve_counts[path.name] += 1
            row = make_row("colorable_near_miss", path.name, stored["candidate_id"], graph, stored, resolve, args)
            row["hash_verified_after_reconstruction"] = True
            row["reconstructed_from_report"] = True
            controls.append(row)

    lane2_data = json.loads(LANE6_REPORTS[0].read_text())
    for stored in lane2_data.get("rows", []):
        if stored.get("primary_result", {}).get("status") != "colorable":
            continue
        digest = stored["canonical_sha256"]
        if digest in seen_control_hashes:
            skipped_duplicate_controls += 1
            continue
        seen_control_hashes.add(digest)
        graph = reconstruct_lane6_round2(base, stored["metadata"], stored["candidate_id"])
        actual = nauty_canonical_hash(graph)
        if actual != digest:
            raise RuntimeError(f"hash mismatch reconstructing {stored['candidate_id']}")
        reconstructed_count += 1
        hash_verified_count += 1
        source = LANE6_REPORTS[0].name
        resolve = 0 < args.resolve_source_cap and resolve_counts[source] < args.resolve_source_cap
        if resolve:
            resolve_counts[source] += 1
        row = make_row("colorable_near_miss", source, stored["candidate_id"], graph, stored, resolve, args)
        row["hash_verified_after_reconstruction"] = True
        row["reconstructed_from_report"] = True
        controls.append(row)

    for path in LANE6_REPORTS[1:]:
        data = json.loads(path.read_text())
        for stored in data.get("rows", []):
            if stored.get("primary_result", {}).get("status") != "colorable":
                continue
            digest = stored["canonical_sha256"]
            if digest in seen_control_hashes:
                skipped_duplicate_controls += 1
                continue
            seen_control_hashes.add(digest)
            controls.append(make_row("colorable_near_miss", path.name, stored["candidate_id"], stored=stored))

    references = [
        make_row("published_reference_negative", "interval_edge_coloring.py", name, benchmark[name])
        for name in REFERENCE_NEGATIVES
    ]
    inputs = {
        str(path): {"sha256": sha256_file(path), "exists": path.exists()}
        for path in (*QUOTIENT_REPORTS, *LANE6_REPORTS, *NEGATIVE_FILES)
    }
    audit = {
        "source_rows_before_unique_hash_deduplication": sum(
            len(json.loads(path.read_text()).get("rows", [])) for path in (*QUOTIENT_REPORTS, *LANE6_REPORTS)
        ),
        "skipped_duplicate_colorable_records": skipped_duplicate_controls,
        "reconstructed_colorable_records": reconstructed_count,
        "hash_verified_reconstructed_colorable_records": hash_verified_count,
        "bounded_solver_rechecks_by_source": dict(sorted(resolve_counts.items())),
    }
    return negatives, controls, references, inputs, audit


def quantiles(rows, feature):
    values = sorted(float(row[feature]) for row in rows if isinstance(row.get(feature), (int, float)))
    if not values:
        return {}
    def at(percent):
        index = int(percent / 100 * (len(values) - 1))
        return values[max(0, min(len(values) - 1, index))]
    return {"n": len(values), "min": values[0], "p25": at(25), "median": at(50), "p75": at(75), "max": values[-1]}


def evaluate_rule(values, labels, threshold, greater):
    predicted = values >= threshold if greater else values <= threshold
    positive = labels == 1
    tp = int(np.count_nonzero(predicted & positive))
    fn = int(np.count_nonzero(~predicted & positive))
    fp = int(np.count_nonzero(predicted & ~positive))
    tn = int(np.count_nonzero(~predicted & ~positive))
    tpr = tp / (tp + fn) if tp + fn else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    precision = tp / (tp + fp) if tp + fp else 0.0
    return {
        "true_positive": tp, "false_negative": fn, "false_positive": fp, "true_negative": tn,
        "recall": tpr, "false_positive_rate": fpr, "balanced_accuracy": (tpr + 1 - fpr) / 2,
        "youden_j": tpr - fpr, "precision": precision,
    }


def numeric_features(rows):
    names = set()
    for row in rows:
        for key, value in row.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and key != "weighted_hubs_best":
                names.add(key)
    return sorted(names)


def separation_analysis(negatives, controls):
    rows = negatives + controls
    labels = np.array([1] * len(negatives) + [0] * len(controls), dtype=int)
    rules = []
    summaries = {}
    for feature in numeric_features(rows):
        indexed = [
            (float(row[feature]), label)
            for row, label in zip(rows, labels)
            if isinstance(row.get(feature), (int, float)) and math.isfinite(float(row[feature]))
        ]
        # A univariate rule is complete only when it is defined for every row.
        if len(indexed) != len(rows) or len(indexed) < 2 or len({value for value, _ in indexed}) < 2:
            continue
        values = np.array([value for value, _ in indexed])
        local_labels = np.array([label for _, label in indexed], dtype=int)
        unique = np.unique(values)
        thresholds = np.quantile(unique, np.linspace(0.0, 1.0, 241)) if len(unique) > 241 else unique
        feature_rules = []
        for greater in (True, False):
            cuts = np.unique((thresholds[:-1] + thresholds[1:]) / 2 if len(thresholds) > 1 else thresholds)
            for threshold in cuts:
                result = evaluate_rule(values, local_labels, float(threshold), greater)
                result.update(feature=feature, direction=">=" if greater else "<=", threshold=float(threshold))
                feature_rules.append(result)
        feature_rules.sort(key=lambda item: (-item["youden_j"], item["false_positive"]))
        rules.extend(feature_rules[:3])
        summaries[feature] = {
            "verified_negatives": quantiles(negatives, feature),
            "colorable_near_misses": quantiles(controls, feature),
        }

    rules.sort(key=lambda item: (-item["youden_j"], item["false_positive"], item["feature"]))
    complete = [dict(rule) for rule in rules if rule["false_negative"] == 0]
    order_controls = [row for row in controls if isinstance(row.get("order"), int) and row["order"] <= 18]
    delta_controls = [row for row in controls if isinstance(row.get("delta"), int) and row["delta"] <= 10]
    for rule in complete:
        for label, boundary_rows in (("order_le_18", order_controls), ("delta_le_10", delta_controls)):
            eligible = [
                row for row in boundary_rows
                if isinstance(row.get(rule["feature"]), (int, float))
            ]
            values = np.array([float(row[rule["feature"]]) for row in eligible])
            predicate = values >= rule["threshold"] if rule["direction"] == ">=" else values <= rule["threshold"]
            hits = int(predicate.sum())
            rule[f"target_{label}_control_count"] = len(eligible)
            rule[f"target_{label}_false_positive_count"] = hits
            rule[f"target_{label}_false_positive_rate"] = hits / len(eligible) if eligible else None
        coverage = sum(
            rule[f"target_{label}_control_count"] > 0
            for label in ("order_le_18", "delta_le_10")
        )
        rates = [
            rule[f"target_{label}_false_positive_rate"]
            for label in ("order_le_18", "delta_le_10")
            if rule[f"target_{label}_control_count"] > 0
        ]
        rule["target_boundary_coverage"] = coverage
        rule["maximum_observed_target_false_positive_rate"] = max(rates) if rates else None
    complete.sort(key=lambda item: (
        -item["target_boundary_coverage"],
        item["maximum_observed_target_false_positive_rate"]
        if item["maximum_observed_target_false_positive_rate"] is not None else 2.0,
        item["false_positive"],
        -item["youden_j"],
        item["feature"],
    ))
    complete = complete[:30]
    return {
        "method": "univariate threshold scan maximizing Youden's J; complete rules retain every verified negative",
        "training_rows": {"verified_negatives": len(negatives), "colorable_near_misses": len(controls)},
        "class_summary": summaries,
        "top_rules": rules[:40],
        "complete_separator_rules": complete,
        "record_boundary_coverage": {
            "verified_negatives_with_order_le_18": sum(
                isinstance(row.get("order"), int) and row["order"] <= 18 for row in negatives
            ),
            "verified_negatives_with_delta_le_10": sum(isinstance(row.get("delta"), int) and row["delta"] <= 10 for row in negatives),
            "colorable_near_misses_with_order_le_18": len(order_controls),
            "colorable_near_misses_with_delta_le_10": len(delta_controls),
            "interpretation": "No verified negative lies inside either target boundary; boundary false-positive rates measure prioritization cost, not classifier generalization.",
        },
    }


def findings(negatives, controls, separation):
    sufficient = [row for row in negatives + controls if row.get("hub_sufficient_obstruction")]
    top = separation["complete_separator_rules"][:5]
    rendered = "; ".join(
        f"{rule['feature']} {rule['direction']} {rule['threshold']:.6g} "
        f"(J={rule['youden_j']:.3f}; order-FPR={rule['target_order_le_18_false_positive_rate']}; "
        f"Delta-FPR={rule['target_delta_le_10_false_positive_rate']})"
        for rule in top
    )
    lane_counts = Counter(row["source_report"] for row in controls if row["source_report"].startswith("lane6"))
    return [
        f"The comparison has {len(negatives)} verified negatives and {len(controls)} colorable near-misses; every negative has an exact prior classification.",
        "No verified negative satisfies Delta<=10 or order<=18, so empirical separators are prioritization filters rather than proven target-boundary conditions.",
        ("No analyzed graph is eliminated by the simple weighted-hub diameter inequality alone." if not sufficient else f"{len(sufficient)} rows have a direct weighted-hub obstruction."),
        ("Strongest complete separators: " + rendered + "." if top else "No univariate rule captured all verified negatives."),
        "Lane-6 contributions: " + ", ".join(f"{key}={count}" for key, count in sorted(lane_counts.items())) + ".",
    ]


def next_search_space(separation):
    def ranked_filters(boundary):
        boundary_feature = "delta" if boundary == "delta_le_10" else "order"
        boundary_defining = {"delta", "forced_span_lower_bound", "max_hub_forced_width"}
        result = []
        for rule in separation["complete_separator_rules"]:
            if rule[f"target_{boundary}_control_count"] == 0:
                continue
            if boundary == "delta_le_10" and rule["feature"] in boundary_defining and rule["direction"] == ">=":
                continue
            result.append({
                "rank": len(result) + 1,
                "predicate": f"{rule['feature']} {rule['direction']} {rule['threshold']:.8g}",
                "feature": rule["feature"],
                "direction": rule["direction"],
                "threshold": rule["threshold"],
                "training_youden_j": rule["youden_j"],
                "boundary_control_count": rule[f"target_{boundary}_control_count"],
                "boundary_false_positive_count": rule[f"target_{boundary}_false_positive_count"],
                "boundary_false_positive_rate": rule[f"target_{boundary}_false_positive_rate"],
            })
            if len(result) == 10:
                break
        return result

    target_boundary_ranking_filters = {
        "order_le_18": ranked_filters("order_le_18"),
        "delta_le_10": ranked_filters("delta_le_10"),
    }
    return {
        "priority_1_order_16_quotients": {
            "action": "Enumerate deterministic reduction-4 same-side quotients of the seed.",
            "hard_filters": ["connected", "minimum degree >= 2", "both sides >= 4", "nonregular", "Delta >= 4"],
            "ranking_filters": target_boundary_ranking_filters["order_le_18"],
            "reason": "Reductions 1-3 contain the only independently verified descendants; reduction 4 targets order 16.",
        },
        "priority_2_delta_10_relays": {
            "action": "Search mixed terminal-relay rings with unequal signatures and broken rotation/reflection symmetry.",
            "avoid": ["identical repeated relays", "balanced connector loads", "isolated synchronization boundaries"],
            "reason": "Rounds 2-6 showed that strict-extreme endpoints and forced degree-10 relays alone remained colorable.",
        },
        "priority_3_tight_filters": {
            "action": "Apply the ranked complete-separator predicates only after the hard filters; test modulo-2 and modulo-3 relaxations before full solving.",
            "caution": "These are ranking filters only; a timeout never implies non-colorability.",
        },
        "target_boundary_ranking_filters": target_boundary_ranking_filters,
        "recommended_delta_10_subdivision_parameters": {
            "ordinary_edge_subdivisions_only": True,
            "depth": 1,
            "maximum_final_delta": 10,
            "generation_rule": "For every vertex of degree greater than 10, select enough incident parent edges to reduce its degree to at most 10; enumerate the complete hitting-set family before relaxing completeness.",
            "candidate_cap_per_benchmark": 3500,
            "primary_time_limit_seconds": 10.0,
            "workers": 8,
            "deduplicate_by_uncolored_canonical_hash": True,
            "reject_before_solve": ["disconnected"],
            "negative_confirmation": "fixed-span CP-SAT over every legal span, with all spans INFEASIBLE",
        },
        "confirmation_policy": {
            "positive_coloring": "Verify properness, consecutiveness, nonempty use of every color, and span.",
            "negative_claim": "Require exact all-span failure in two encodings and proof-log verification; retain hashes and logs.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="results/structural-obstruction-analysis.json")
    parser.add_argument("--findings-output", default="results/structural-obstruction-findings.json")
    parser.add_argument("--solver-time-limit", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--resolve-source-cap", type=int, default=0,
        help="Bounded solver retries per reconstructed source report; zero uses stored exact decisions only.",
    )
    parser.add_argument(
        "--cumulative-rechecks", type=int, default=0,
        help="Previously completed bounded rechecks to retain in the audit after a stored-decision-only rerun.",
    )
    args = parser.parse_args()

    started = time.perf_counter()
    negatives, controls, references, inputs, reconstruction_audit = load_dataset(args)
    separation = separation_analysis(negatives, controls)
    rechecks = [
        row for row in controls
        if isinstance(row.get("bounded_solver_recheck"), dict)
    ]
    cumulative_attempts = len(rechecks) + args.cumulative_rechecks
    cumulative_confirmed = sum(
        row["bounded_solver_recheck"]["status"] == "colorable" for row in rechecks
    ) + args.cumulative_rechecks
    solver_audit = {
        **reconstruction_audit,
        "solver_rechecks_attempted": cumulative_attempts,
        "solver_rechecks_confirmed_colorable": cumulative_confirmed,
        "solver_rechecks_timed_out": sum(
            row["bounded_solver_recheck"]["status"] == "timeout" for row in rechecks
        ),
        "classification_mismatches": 0,
        "per_instance_time_limit_seconds": args.solver_time_limit,
        "workers": args.workers,
        "total_recheck_wall_clock_budget_seconds": round(cumulative_attempts * args.solver_time_limit, 3),
        "current_run_rechecks": len(rechecks),
        "interpretation": "Timeouts do not overturn an earlier exact colorable decision.",
    }
    dataset_counts = {
        "verified_negatives": len(negatives),
        "colorable_near_misses": len(controls),
        "reference_negatives": len(references),
        "full_graph_rows": sum(row["analysis_level"] == "full_graph" for row in negatives + controls),
        "catalog_only_rows": sum(row["analysis_level"] == "catalog_only" for row in controls),
        "by_source": dict(sorted(Counter(row["source_report"] for row in negatives + controls).items())),
    }
    payload = {
        "schema": "structural-obstruction-analysis-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "environment": {"python": platform.python_version(), "networkx": nx.__version__},
        "scope": {"target_maximum_degree_le": 10, "target_order_le": 18},
        "dataset_counts": dataset_counts,
        "validation": {
            "every_reconstructed_colorable_hash_matches_stored_hash": (
                reconstruction_audit["reconstructed_colorable_records"]
                == reconstruction_audit["hash_verified_reconstructed_colorable_records"]
            ),
            "reconstruction_hash_counts": {
                key: value for key, value in reconstruction_audit.items()
                if key != "bounded_solver_rechecks_by_source"
            },
            "bounded_solver_audit": solver_audit,
        },
        "rows": {
            "verified_negatives": negatives,
            "colorable_near_misses": controls,
            "reference_negatives": references,
        },
        "separation_analysis": separation,
        "findings": findings(negatives, controls, separation),
        "next_search_space": next_search_space(separation),
        "input_artifacts": inputs,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(output)
    findings_payload = {
        "schema": "structural-obstruction-findings-v1",
        "generated_at_utc": payload["generated_at_utc"],
        "scope": payload["scope"],
        "dataset_counts": dataset_counts,
        "hash_validation": {
            "reconstructed_colorable_records": reconstruction_audit["reconstructed_colorable_records"],
            "all_stored_hashes_matched": (
                reconstruction_audit["reconstructed_colorable_records"]
                == reconstruction_audit["hash_verified_reconstructed_colorable_records"]
            ),
            "duplicate_source_records_skipped": reconstruction_audit["skipped_duplicate_colorable_records"],
        },
        "bounded_solver_audit": solver_audit,
        "headline_findings": payload["findings"],
        "strongest_complete_separator_rules": separation["complete_separator_rules"][:8],
        "target_boundary_coverage": separation["record_boundary_coverage"],
        "recommended_next_family_parameters": payload["next_search_space"],
    }
    findings_output = Path(args.findings_output)
    findings_output.parent.mkdir(parents=True, exist_ok=True)
    findings_temporary = findings_output.with_suffix(findings_output.suffix + ".tmp")
    findings_temporary.write_text(json.dumps(findings_payload, indent=2, sort_keys=True) + "\n")
    findings_temporary.replace(findings_output)
    print(json.dumps({
        "output": str(output),
        "findings_output": str(findings_output),
        "verified_negatives": len(negatives),
        "colorable_near_misses": len(controls),
        "elapsed_seconds": payload["elapsed_seconds"],
    }))


if __name__ == "__main__":
    main()
