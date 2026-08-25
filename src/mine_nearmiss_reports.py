#!/usr/bin/env python3
"""Extract compact, evidence-only colorable near-miss rankings from reports."""

from __future__ import annotations

import json
import statistics
from pathlib import Path


SOURCES = {
    "degree-transfer-fresh-motifs": ["results/degree-transfer-delta10-agent/fresh-motifs-m5-pilot.json"],
    "degree-transfer-extensions": [
        "results/degree-transfer-delta10.json",
        "results/degree-transfer-delta10-extension.json",
        "results/degree-transfer-delta10-extension-r3.json",
        "results/degree-transfer-delta10-extension-resumed.json",
    ],
    "multihub-sync": ["results/multihub-sync-delta10.json"],
    "vertex-split": ["results/vertex-split-delta10.json", "results/vertex-split-delta10-v2.json"],
    "order17-targeted": ["results/order17-targeted-v1/report.json"],
    "order18-targeted-v4": ["results/order18-targeted-v4/report.json"],
    "failure-guided": ["results/failure-guided-delta10.json"],
    "set-system": ["results/set-system-delta10.json"],
    "lane1-rewires": [
        "results/lane1-hub-subsets.json", "results/lane1-redistribution.json",
        "results/lane1-switch-depth1.json", "results/lane1-switch-depth2.json",
    ],
    "lane6-synchronizers": [
        "results/lane6-chained-sync-corrected.json", "results/lane6-signature-r2.json",
        "results/lane6-signature-r5.json", "results/lane6-signature-r6.json",
        "results/lane6-split-hub-delta10.json",
    ],
    "quotient-reductions": ["results/quotient-r2.json", "results/quotient-r3.json"],
    "new-targeted-envelope": ["results/nearmiss-mining-agent/targeted-pass-report.json"],
}


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def status_and_span(row: dict) -> tuple[str | None, int | None]:
    nested = row.get("primary_result") if isinstance(row.get("primary_result"), dict) else {}
    status = row.get("primary_status") or nested.get("status")
    span = row.get("primary_span", nested.get("span"))
    return status, span if isinstance(span, int) else None


def margin(row: dict) -> float | None:
    for block in (row, row.get("ranking", {}), row.get("ranking_features", {})):
        value = block.get("hub_best_margin") if isinstance(block, dict) else None
        if isinstance(value, (int, float)):
            return value
    weighted = row.get("weighted_hubs_best") or row.get("weighted_hub")
    if isinstance(weighted, list) and weighted and isinstance(weighted[0], dict):
        value = weighted[0].get("margin")
        if isinstance(value, (int, float)):
            return value
    detail = row.get("score_detail", {})
    value = detail.get("best_weighted_hub_margin") if isinstance(detail, dict) else None
    return value if isinstance(value, (int, float)) else None


def variance(row: dict) -> float | None:
    for block in (row, row.get("ranking", {}), row.get("ranking_features", {})):
        if isinstance(block, dict):
            value = block.get("degree_variance_normalized") or block.get("normalized_degree_variance")
            if isinstance(value, (int, float)):
                return value
    degrees = row.get("degrees")
    if isinstance(degrees, dict) and degrees:
        values = list(degrees.values())
        mean = sum(values) / len(values)
        return sum((value - mean) ** 2 for value in values) / len(values) / (len(values) - 1)
    return None


def lane(row: dict, family: str) -> str:
    metadata = row.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("lane"), str):
        return metadata["lane"]
    if isinstance(row.get("lane"), str):
        return row["lane"]
    operation = row.get("operation")
    if isinstance(operation, dict) and isinstance(operation.get("operation_family"), str):
        return operation["operation_family"]
    return family


def compact(row: dict, family: str) -> dict | None:
    status, span = status_and_span(row)
    if status != "colorable" or span is None:
        return None
    # The fresh-motif pilot stored construction recipes but not derived hub or
    # variance metrics. Reconstruct its labelled recipe solely to fill those
    # two ranking columns; the stored exact primary classification is retained.
    reconstructed_margin = None
    reconstructed_variance = None
    if family == "degree-transfer-fresh-motifs" and span >= 21:
        from degree_transfer_delta10_fresh_motifs import FRESH_MOTIFS, apply
        from interval_edge_coloring import benchmark_graphs, weighted_hub_statistics
        lookup = {motif.name: motif for motif in FRESH_MOTIFS}
        graph = apply(
            benchmark_graphs()["M5_delta_555"],
            tuple(row["selected_parent_edges"]),
            tuple(lookup[name] for name in row["fresh_motifs"]),
            0,
        )
        reconstructed_margin = weighted_hub_statistics(graph)[0]["margin"]
        degrees = list(graph.degrees.values())
        mean = sum(degrees) / graph.n
        reconstructed_variance = sum((value - mean) ** 2 for value in degrees) / graph.n / (graph.n - 1)
    return {
        "candidate_id": row.get("candidate_id", row.get("canonical_sha256")),
        "canonical_sha256": row.get("canonical_sha256"),
        "primary_span": span,
        "hub_margin": margin(row) if margin(row) is not None else reconstructed_margin,
        "degree_variance": variance(row) if variance(row) is not None else reconstructed_variance,
        "order": row.get("order"), "size": row.get("size"), "delta": row.get("delta"),
        "lane": lane(row, family),
        "structural_metadata": row.get("fresh_motifs") or row.get("terminal_motifs") or row.get("replacement_motifs") or row.get("operation") or row.get("metadata"),
    }


def ranked(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: (
        -row["primary_span"],
        -(row["hub_margin"] if row["hub_margin"] is not None else -10**9),
        -(row["degree_variance"] if row["degree_variance"] is not None else -10**9),
        -(row["order"] if isinstance(row["order"], int) else -1),
        -(row["size"] if isinstance(row["size"], int) else -1),
        str(row["candidate_id"]),
    ))


def load_rows(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("records", data.get("rows", [])) if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def main() -> None:
    output = Path("results/nearmiss-mining-agent")
    output.mkdir(parents=True, exist_ok=True)
    all_families = {}
    for family, paths in SOURCES.items():
        extracted = []
        raw_count = 0
        for raw_path in paths:
            rows = load_rows(Path(raw_path))
            raw_count += len(rows)
            extracted.extend(item for row in rows if (item := compact(row, family)) is not None)
        ordered = ranked(extracted)
        spans = [row["primary_span"] for row in ordered]
        top_count = max(1, len(ordered) // 4)
        high, routine = ordered[:top_count], ordered[top_count:]
        all_families[family] = {
            "source_paths": paths, "raw_records": raw_count, "certified_colorable": len(ordered),
            "primary_span_max": max(spans, default=None), "primary_span_median": median(spans),
            "top_near_misses": ordered[:5],
            "higher_span_quartile": {
                "count": len(high), "span_median": median([row["primary_span"] for row in high]),
                "hub_margin_median": median([row["hub_margin"] for row in high if row["hub_margin"] is not None]),
                "degree_variance_median": median([row["degree_variance"] for row in high if row["degree_variance"] is not None]),
                "order_median": median([row["order"] for row in high if isinstance(row["order"], int)]),
                "size_median": median([row["size"] for row in high if isinstance(row["size"], int)]),
            },
            "routine_remainder": {
                "count": len(routine), "span_median": median([row["primary_span"] for row in routine]),
                "hub_margin_median": median([row["hub_margin"] for row in routine if row["hub_margin"] is not None]),
                "degree_variance_median": median([row["degree_variance"] for row in routine if row["degree_variance"] is not None]),
                "order_median": median([row["order"] for row in routine if isinstance(row["order"], int)]),
                "size_median": median([row["size"] for row in routine if isinstance(row["size"], int)]),
            },
        }
    payload = {
        "schema": "nearmiss-mining-v1",
        "scope": "exact rank-potential CP-SAT colorable records only; no causal interpretation",
        "families": all_families,
    }
    (output / "near-miss-analysis.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Certified Colorable Near-Misses", "", "All rows below have an exact rank-potential CP-SAT coloring certificate. Rankings are lexicographic: primary span, hub margin, normalized degree variance, order, size. A blank metric was not stored by that family.", ""]
    for family, data in all_families.items():
        lines.extend([f"## {family}", f"{data['certified_colorable']} certified colorable records from {data['raw_records']} source rows; maximum span {data['primary_span_max']}, median span {data['primary_span_median']}.", "", "| span | margin | variance | order | size | Delta | lane | candidate |", "|---:|---:|---:|---:|---:|---:|---|---|"])
        for row in data["top_near_misses"]:
            printable = {key: ("" if value is None else value) for key, value in row.items()}
            lines.append("| {primary_span} | {hub_margin} | {degree_variance} | {order} | {size} | {delta} | {lane} | {candidate_id} |".format(**printable))
        lines.append("")
    lines.extend([
        "## Observed Associations", "",
        "- The prior degree-transfer fresh-motif pilot supplies the global maximum span (22): all three span-22 rows use five required repairs of the M5 degree-15 hub and mix long cyclic/theta terminals with at least one dense or multi-C4 terminal. Their recorded orders are 62-73 and sizes 96-105.",
        "- The targeted envelope pass preserves the five-repair geometry but uses still longer or denser terminals. Its 16 final-run novel classifications are all colorable, with a best span of 21; this indicates that simply extending those terminals did not improve the observed span-22 ceiling in this small sample.",
        "- The split and synchronization families retain smaller orders but generally shorter spans; their best-margin structures remain near or below zero after the degree cap is imposed. This is an association in these enumerated catalogs, not an obstruction theorem.",
        "- In the degree-transfer pilot, higher span occurs alongside much larger order and size than routine split/rewire near-misses. The evidence supports preserving several independent repaired spokes and mixed terminal types as a next-lane heuristic, not treating hub margin or variance alone as a causal score.",
        "",
        "## Next-Lane Recommendation", "",
        "Keep the five-spoke M5 transfer skeleton, but target bridge-coupled terminal pairs that retain the mixed C12/theta/multi-C4 diversity while adding one bounded inter-terminal relation. Rank candidates first by prospective span-bearing path diversity, then by non-worsening hub margin; do not spend the next lane on pure terminal inflation or isolated hub splitting. Maintain global Nauty deduplication, rank-potential classification, and fixed-span confirmation for every primary negative.",
    ])
    (output / "near-miss-analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
