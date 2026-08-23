#!/usr/bin/env python3
"""Third Delta<=10 extension pass seeded by all prior certificates."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

from degree_transfer_delta10_extension import (
    ExtensionCandidate,
    atomic_write_json,
    baseline_signature_set,
    classify_parent,
    counts_from_records,
    generate_parent,
    load_completed_rows,
    parse_state_signature,
    structural_rank,
)
from degree_transfer_delta10_search import parent_graphs
from interval_edge_coloring import nauty_canonical_hash


def report_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("complete") or payload.get("runtime_deadline_hit"):
        raise SystemExit(f"seed report is not complete: {path}")
    records = payload.get("records", [])
    required = {
        "parent",
        "canonical_sha256",
        "signature",
        "decision",
        "primary_status",
    }
    for row in records:
        if not required.issubset(row):
            raise SystemExit(f"incomplete seed report record in {path}")
        if row["decision"].startswith("unresolved"):
            raise SystemExit(f"seed report contains unresolved evidence: {path}")
    return records


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        row["signature"] = (
            tuple(row["signature"][0]),
            tuple(row["signature"][1]),
        )
    return rows


def merge_rows(label: str, groups: list[tuple[str, list[dict[str, Any]]]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source, rows in groups:
        for row in rows:
            digest = row["canonical_sha256"]
            previous = merged.get(digest)
            if previous is not None:
                if previous.get("parent") != row.get("parent"):
                    raise SystemExit(
                        f"conflicting parent for certificate {digest}: {label}"
                    )
                if previous.get("decision") != row.get("decision"):
                    raise SystemExit(
                        f"conflicting durable evidence for certificate {digest}: {label}"
                    )
                continue
            merged[digest] = row
    return merged


def structural_rank_r3(graph: Any) -> tuple[int, int, int]:
    _, margin, variance_scaled = structural_rank(graph)
    tier = 1 if margin >= -2.5 else 2
    return tier, -margin, -variance_scaled


def rank_candidates_r3(candidates: list[ExtensionCandidate]) -> list[ExtensionCandidate]:
    def key(candidate: ExtensionCandidate):
        tier, negative_margin, negative_variance = structural_rank_r3(candidate.graph)
        graph = candidate.graph
        return (
            tier,
            negative_margin,
            negative_variance,
            graph.n,
            graph.m,
            candidate.signature[0],
            candidate.signature[1],
        )

    return sorted(candidates, key=key)


def counts_by_parent(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    return {
        parent: counts_from_records([row for row in records if row["parent"] == parent])
        for parent in sorted({row["parent"] for row in records})
    }


def make_report(
    configuration: dict[str, Any],
    baseline: dict[str, Any],
    baseline_path: Path,
    resumed_path: Path,
    seed_state_path: Path,
    seed_audit: dict[str, Any],
    resumed_unique_count: int,
    summaries: list[dict[str, Any]],
    records: list[dict[str, Any]],
    elapsed_seconds: float,
    deadline_seconds: float,
    runtime_deadline_hit: bool,
    target: int,
    partial: bool,
) -> dict[str, Any]:
    counts = counts_from_records(records)
    per_parent_counts = counts_by_parent(records)
    summaries_by_parent = {item["parent"]: item for item in summaries}
    configured_target_names = set(configuration["target_parents"])
    represented_target_names = set(summaries_by_parent)
    target_progress = {
        parent: {
            "target": target,
            "classified_this_pass": per_parent_counts.get(parent, {}).get(
                "newly_classified", 0
            ),
            "cap_reached": per_parent_counts.get(parent, {}).get(
                "newly_classified", 0
            )
            == target,
            "complete_without_timeout": (
                per_parent_counts.get(parent, {}).get("newly_classified", 0)
                == target
                or summaries_by_parent.get(parent, {}).get(
                    "replacement_family_exhausted_before_extension_cap", False
                )
            )
            and per_parent_counts.get(parent, {}).get("timeout", 0) == 0,
        }
        for parent in configuration["target_parents"]
    }
    configured_complete = configured_target_names <= represented_target_names and all(
        item["classification_complete"] for item in summaries
    )
    fully_classified = configured_complete and counts["timeout"] == 0
    ranked_top = sorted(
        records,
        key=lambda row: (
            1
            if (row.get("structural_features", {}).get("hub_best_margin") or -(10**9))
            >= -2.5
            else 2,
            -(row.get("structural_features", {}).get("hub_best_margin") or -(10**9)),
            -int(
                round(
                    row.get("structural_features", {}).get(
                        "degree_variance_normalized", 0.0
                    )
                    * 10**12
                )
            ),
            row.get("order", 10**9),
            row.get("size", 10**9),
            tuple(row.get("signature", [[], []])[0]),
            tuple(row.get("signature", [[], []])[1]),
        ),
    )[:10]
    return {
        "schema_version": 3,
        "mode": "deterministic_baseline_extension_r3",
        "configuration": configuration,
        "complete": configured_complete,
        "completion_flags": {
            "configured_extension_complete_without_timeout": configured_complete,
            "all_targets_fully_classified_exactly": fully_classified,
            "independent_confirmation_complete_without_timeout": all(
                item.get("independent_confirmation_complete_without_timeout", False)
                for item in summaries
            ),
            "candidate_cap_reached_all_targets": configured_target_names
            <= represented_target_names
            and all(item.get("candidate_cap_reached", False) for item in summaries),
            "replacement_family_exhausted_before_extension_cap_any_target": any(
                item.get("replacement_family_exhausted_before_extension_cap", False)
                for item in summaries
            ),
            "runtime_deadline_hit": runtime_deadline_hit,
        },
        "runtime_deadline_hit": runtime_deadline_hit,
        "deadline_seconds": deadline_seconds,
        "elapsed_seconds": elapsed_seconds,
        "checkpoint": {
            "written_unix_time": time.time(),
            "partial": partial,
        },
        "totals": {
            "generated": sum(int(item.get("generated", 0)) for item in summaries),
            "valid_candidates_generated": sum(
                int(item.get("valid_candidates_generated", 0)) for item in summaries
            ),
            "unique": sum(int(item.get("unique", 0)) for item in summaries),
            "newly_classified": counts["newly_classified"],
            "colorable": counts["colorable"],
            "non_colorable": counts["non_colorable"],
            "confirmed_non_colorable": counts["confirmed_non_colorable"],
            "timeout": counts["timeout"],
            "primary_timeout": counts["primary_timeout"],
            "independent_unresolved": counts["independent_unresolved"],
            "duplicates": sum(int(item.get("duplicates", 0)) for item in summaries),
        },
        "counts": counts,
        "candidate_cap": {
            "additional_unique_per_parent": target,
            "progress": target_progress,
            "all_targets_met_exactly": configured_target_names
            <= represented_target_names
            and all(
                item["classified_this_pass"] == target
                for item in target_progress.values()
            ),
        },
        "seed_audit": seed_audit,
        "baseline": {
            "path": str(baseline_path),
            "schema_version": baseline.get("schema_version"),
            "complete": baseline.get("complete", False),
            "maximum_final_delta": baseline.get("configuration", {}).get(
                "maximum_final_delta"
            ),
            "canonical_certificates_seeded": len(
                {row["canonical_sha256"] for row in baseline.get("records", [])}
            ),
        },
        "resumed_seed": {
            "path": str(resumed_path),
            "complete": True,
            "canonical_certificates_seeded": resumed_unique_count,
        },
        "negative_events": [
            {
                "event": "certified_non_colorable",
                "candidate_id": row.get("candidate_id"),
                "path": row.get("graph_path"),
                "parent": row["parent"],
                "canonical_sha256": row["canonical_sha256"],
            }
            for row in records
            if row.get("decision") == "non-colorable"
        ],
        "best_near_miss_diagnostics": {
            "global_ranked_top": [
                {
                    key: row.get(key)
                    for key in (
                        "canonical_sha256",
                        "parent",
                        "signature",
                        "order",
                        "size",
                        "delta",
                        "minimum_degree",
                        "decision",
                        "primary_status",
                        "primary_span",
                        "structural_rank_tier",
                        "structural_features",
                    )
                }
                for row in ranked_top
            ]
        },
        "summaries": summaries,
        "records": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="results/degree-transfer-delta10.json")
    parser.add_argument(
        "--resumed-seed",
        default="results/degree-transfer-delta10-extension-resumed.json",
    )
    parser.add_argument("--seed-state", default="")
    parser.add_argument(
        "--output", default="results/degree-transfer-delta10-extension-r3.json"
    )
    parser.add_argument("--parents", default="Erd_Fano_2222221,M5_delta_555")
    parser.add_argument("--additional-unique-per-parent", type=int, default=5000)
    parser.add_argument("--maximum-final-delta", type=int, default=10)
    parser.add_argument("--max-replaced-edges", type=int, default=6)
    parser.add_argument("--minimum-degree", type=int, default=2)
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--deadline-seconds", type=float, default=14400.0)
    args = parser.parse_args()

    if args.additional_unique_per_parent <= 0:
        parser.error("the additional unique per-parent cap must be positive")
    if args.time_limit > 10.0 or args.workers > 8:
        parser.error("solver limits exceed the required maxima")
    if args.deadline_seconds > 14400.0:
        parser.error("deadline exceeds four hours")

    requested_parents = [name.strip() for name in args.parents.split(",") if name.strip()]
    if not requested_parents:
        parser.error("at least one parent is required")
    baseline_path = Path(args.baseline)
    resumed_path = Path(args.resumed_seed)
    seed_state_path = Path(args.seed_state) if args.seed_state else None
    output_path = Path(args.output)
    state_path = output_path.with_name(output_path.stem + "-state.jsonl")
    graph_dir = output_path.parent / "graphs" / output_path.stem
    graph_dir.mkdir(parents=True, exist_ok=True)

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    resumed_rows = normalize_rows(report_records(resumed_path))
    resumed_unique_count = len({row["canonical_sha256"] for row in resumed_rows})
    seed_jsonl_rows = (
        normalize_rows(load_completed_rows(seed_state_path))
        if seed_state_path and seed_state_path.exists()
        else []
    )
    current_rows = normalize_rows(load_completed_rows(state_path))
    resumed_unique_certificates = {row["canonical_sha256"] for row in resumed_rows}
    all_seed_rows = [*resumed_rows, *seed_jsonl_rows]
    seed_rows_by_parent = {
        parent: [row for row in all_seed_rows if row["parent"] == parent]
        for parent in requested_parents
    }
    current_rows_by_parent = {
        parent: [row for row in current_rows if row["parent"] == parent]
        for parent in requested_parents
    }
    seed_groups = [("baseline", baseline.get("records", [])), (str(resumed_path), resumed_rows)]
    if seed_state_path:
        seed_groups.append((str(seed_state_path), seed_jsonl_rows))
    merged_seed = merge_rows("baseline/resumed seeds", seed_groups)
    merged_current = merge_rows("current r3 state", [(str(state_path), current_rows)])
    overlap = set(merged_seed).intersection(merged_current)
    for digest in overlap:
        seed_row = merged_seed[digest]
        current_row = merged_current[digest]
        if seed_row["parent"] != current_row["parent"] or seed_row["decision"] != current_row["decision"]:
            raise SystemExit(f"conflicting r3 evidence for certificate {digest}")

    baseline_hashes = {row["canonical_sha256"] for row in baseline.get("records", [])}
    seed_hashes = set(merged_seed)
    current_hashes = set(merged_current)
    global_seen = baseline_hashes | seed_hashes | current_hashes
    resume_hashes = seed_hashes | current_hashes
    resume_signatures = {
        parse_state_signature(row) for row in [*all_seed_rows, *current_rows]
    }

    available = {name: (graph, kind) for name, graph, kind in parent_graphs(graph_dir)}
    missing = [name for name in requested_parents if name not in available]
    if missing:
        parser.error(f"unknown parent(s): {', '.join(missing)}")
    if baseline.get("configuration", {}).get("maximum_final_delta") != args.maximum_final_delta:
        raise SystemExit("baseline maximum Delta does not match extension configuration")

    configuration = {
        "extension_of": str(baseline_path),
        "search_lane": "third terminal-gadget transfer pass beyond prior extension caps",
        "target_parents": requested_parents,
        "target_semantics": "additional_unique_per_parent counts only this r3 pass; all prior certificates are deduplication seeds",
        "baseline_seed": str(baseline_path),
        "resumed_seed": str(resumed_path),
        "seed_state": str(seed_state_path) if seed_state_path else None,
        "additional_unique_per_parent": args.additional_unique_per_parent,
        "maximum_final_delta": args.maximum_final_delta,
        "maximum_replaced_edges_per_parent": args.max_replaced_edges,
        "minimum_graph_degree": args.minimum_degree,
        "require_connected": True,
        "require_bipartite": True,
        "deduplication": "bipartition-colored Nauty certificate SHA-256 seeded from baseline and completed resumed certificates",
        "deduplication_rule": "skip prior accepted signatures before construction and reject every reconstructed graph matching a prior certificate",
        "structural_ranking": [
            "tier 1: hub_best_margin >= -2.5; tier 2 otherwise",
            "hub_best_margin descending",
            "normalized degree variance descending",
            "secondary keys: order, size, signature",
        ],
        "excluded_ranking_predicates": ["Delta", "forced span bounds equivalent to Delta"],
        "primary_classification": "exact rank-potential CP-SAT",
        "solver_time_limit_seconds": args.time_limit,
        "workers": args.workers,
        "negative_confirmation": "fixed-span CP-SAT independently over every legal span",
        "timeout_policy": "timeout is unresolved and never counted as non-colorable",
        "state_path": str(state_path),
        "checkpoint_policy": "append and fsync one durable state row after every classification",
    }
    seed_audit = {
        "baseline_rows_loaded": len(baseline.get("records", [])),
        "baseline_unique_certificates": len(baseline_hashes),
        "resumed_rows_loaded": len(resumed_rows),
        "resumed_unique_certificates": len(
            {row["canonical_sha256"] for row in resumed_rows}
        ),
        "seed_jsonl_rows_loaded": len(seed_jsonl_rows),
        "seed_jsonl_unique_certificates": len(
            {row["canonical_sha256"] for row in seed_jsonl_rows}
        ),
        "merged_seed_unique_certificates": len(merged_seed),
        "current_r3_rows_loaded_at_start": len(current_rows),
        "current_r3_unique_certificates": len(current_hashes),
        "seed_signature_set_size": len(
            {parse_state_signature(row) for row in all_seed_rows}
        ),
    }

    run_started_wall = time.time()
    run_started = time.monotonic()
    deadline = run_started + args.deadline_seconds
    summaries: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    checkpoint = {"last_write": time.monotonic()}
    restored_parents: list[str] = []

    previous_report = (
        json.loads(output_path.read_text(encoding="utf-8"))
        if output_path.exists()
        else None
    )
    if previous_report is not None:
        previous_targets = previous_report.get("configuration", {}).get(
            "target_parents", []
        )
        configuration["target_parents"] = list(
            dict.fromkeys([*previous_targets, *requested_parents])
        )
        for name in configuration["target_parents"]:
            if name in requested_parents:
                continue
            rows = [row for row in current_rows if row["parent"] == name]
            if not rows:
                continue
            previous_summary = next(
                (
                    item
                    for item in previous_report.get("summaries", [])
                    if item.get("parent") == name
                ),
                {},
            )
            restored_counts = counts_from_records(rows)
            family_exhausted = bool(
                previous_summary.get(
                    "replacement_family_exhausted_before_extension_cap", False
                )
            )
            restored_summary = {
                **previous_summary,
                "parent": name,
                "counts": restored_counts,
                "unique": len(rows),
                "newly_classified": restored_counts["newly_classified"],
                "candidate_cap_reached": len(rows)
                >= args.additional_unique_per_parent,
                "runtime_deadline_hit": False,
                "classification_complete": (
                    len(rows) == args.additional_unique_per_parent
                    or family_exhausted
                )
                and restored_counts["timeout"] == 0
                and restored_counts["independent_unresolved"] == 0,
                "independent_confirmation_complete_without_timeout":
                restored_counts["independent_unresolved"] == 0,
            }
            summaries.append(restored_summary)
            all_records.extend(rows)
            restored_parents.append(name)

    def write_report(
        current_summary: dict[str, Any] | None = None,
        current_records: list[dict[str, Any]] | None = None,
    ) -> None:
        combined_summaries = [*summaries]
        combined_records = [*all_records]
        if current_summary is not None:
            combined_summaries.append(current_summary)
        if current_records is not None:
            combined_records.extend(current_records)
        report = make_report(
            configuration,
            baseline,
            baseline_path,
            resumed_path,
            seed_state_path or Path(""),
            seed_audit,
            len(resumed_unique_certificates),
            combined_summaries,
            combined_records,
            time.monotonic() - run_started,
            args.deadline_seconds,
            time.monotonic() >= deadline,
            args.additional_unique_per_parent,
            current_summary is not None,
        )
        atomic_write_json(output_path, report)

    write_report()
    for name in requested_parents:
        base, kind = available[name]
        print(json.dumps({"event": "generation_start", "parent": name, "kind": kind}), flush=True)
        seed_count = len(seed_rows_by_parent[name])
        current_count = len(current_rows_by_parent[name])
        generation_carryover = (
            next(
                (
                    item
                    for item in previous_report.get("summaries", [])
                    if item.get("parent") == name
                ),
                {},
            )
            if previous_report is not None
            else {}
        )
        current_hashes = {row["canonical_sha256"] for row in current_rows_by_parent[name]}
        quota_seed_count = sum(
            row["canonical_sha256"] not in current_hashes
            for row in seed_rows_by_parent[name]
        )
        if current_count > args.additional_unique_per_parent:
            raise SystemExit(f"current r3 state exceeds cap for {name}")
        summary, candidates, cap_reached, family_exhausted = generate_parent(
            name,
            base,
            kind,
            baseline,
            {
                parse_state_signature(row)
                for row in [*seed_rows_by_parent[name], *current_rows_by_parent[name]]
            },
            resume_hashes,
            global_seen,
            args.maximum_final_delta,
            args.max_replaced_edges,
            args.minimum_degree,
            args.additional_unique_per_parent + quota_seed_count,
            quota_seed_count + current_count,
            deadline,
        )
        for field in (
            "generated",
            "valid_candidates_generated",
            "duplicates",
            "baseline_signatures_skipped",
            "resumed_signatures_skipped",
            "resumed_hash_duplicates",
            "rejected_disconnected",
            "rejected_low_degree",
        ):
            summary[field] = int(summary.get(field, 0)) + int(
                generation_carryover.get(field, 0)
            )
        ordered_candidates = rank_candidates_r3(candidates)
        summary["parent_target_unique_this_pass"] = args.additional_unique_per_parent
        summary["seed_certificates_loaded"] = quota_seed_count
        summary["current_pass_rows_restored"] = current_count
        summary["remaining_candidate_quota"] = (
            args.additional_unique_per_parent - current_count
        )
        summary["target_unique_per_parent"] = args.additional_unique_per_parent
        summary["counts"] = counts_from_records(current_rows_by_parent[name])
        summary["classification_complete"] = current_count == args.additional_unique_per_parent
        summary["runtime_deadline_hit"] = time.monotonic() >= deadline
        print(
            json.dumps(
                {
                    "event": "generation_complete",
                    "parent": name,
                    "generated": summary["generated"],
                    "valid_candidates_generated": summary["valid_candidates_generated"],
                    "unique_new_this_run": summary["unique_new_this_run"],
                    "duplicates": summary["duplicates"],
                    "baseline_signatures_skipped": summary["baseline_signatures_skipped"],
                    "resumed_signatures_skipped": summary["resumed_signatures_skipped"],
                    "resumed_hash_duplicates": summary["resumed_hash_duplicates"],
                    "candidate_cap_reached": cap_reached,
                    "replacement_family_exhausted_before_extension_cap": family_exhausted,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        def report_current(
            current_summary: dict[str, Any],
            new_records: list[dict[str, Any]],
        ) -> None:
            write_report(current_summary, current_rows_by_parent[name] + new_records)

        summary, new_records = classify_parent(
            summary,
            ordered_candidates,
            current_rows_by_parent[name],
            graph_dir,
            state_path,
            args.time_limit,
            args.workers,
            deadline,
            checkpoint,
            report_current,
            current_count,
        )
        pass_records = current_rows_by_parent[name] + new_records
        pass_counts = counts_from_records(pass_records)
        summary["counts"] = pass_counts
        summary["unique"] = len(pass_records)
        summary["classified_this_resume"] = len(new_records)
        summary["unique_new_this_run"] = len(new_records)
        summary["newly_classified"] = pass_counts["newly_classified"]
        summary["candidate_cap_reached"] = len(pass_records) >= args.additional_unique_per_parent
        summary["classification_complete"] = (
            (
                len(pass_records) == args.additional_unique_per_parent
                or summary.get(
                    "replacement_family_exhausted_before_extension_cap", False
                )
            )
            and pass_counts["timeout"] == 0
            and not summary.get("runtime_deadline_hit", False)
        )
        summary["independent_confirmation_complete_without_timeout"] = all(
            not row.get("independent_unresolved", False) for row in pass_records
        )
        summary["classification_elapsed_seconds"] = summary.get(
            "classification_elapsed_seconds", 0.0
        )
        summaries.append(summary)
        all_records.extend(pass_records)
        write_report()

    final_report = make_report(
        configuration,
        baseline,
        baseline_path,
        resumed_path,
        seed_state_path or Path(""),
        seed_audit,
        len(resumed_unique_certificates),
        summaries,
        all_records,
        time.monotonic() - run_started,
        args.deadline_seconds,
        time.monotonic() >= deadline,
        args.additional_unique_per_parent,
        False,
    )
    if previous_report is not None:
        final_report["elapsed_seconds"] += previous_report.get(
            "elapsed_seconds", 0.0
        )
    final_report["resume_scope"] = {
        "requested_parents": requested_parents,
        "restored_parents": restored_parents,
        "classified_this_resume": {
            item["parent"]: item.get("classified_this_resume", 0)
            for item in summaries
            if item["parent"] in requested_parents
        },
    }
    final_report["environment"] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "started_unix_time": run_started_wall,
        "finished_unix_time": time.time(),
    }
    atomic_write_json(output_path, final_report)
    print(
        json.dumps(
            {
                "event": "run_complete",
                "complete": final_report["complete"],
                "runtime_deadline_hit": final_report["runtime_deadline_hit"],
                "totals": final_report["totals"],
                "counts": final_report["counts"],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
