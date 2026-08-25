#!/usr/bin/env python3
"""Build the non-mutating cumulative alternate-family coverage through v3."""

from __future__ import annotations

import collections
import json
from pathlib import Path

from build_order18_alternate_family_ledger import (
    PARENTS,
    ROOT,
    atomic_json,
    atomic_write,
    completed_rows,
    first_queue_hashes,
    graph_check,
    reconstruct,
)
from interval_edge_coloring import Graph, nauty_canonical_hash


OUTPUT = ROOT / "results/order18-alternate-family-cumulative-v3"
PRIOR_LEDGER = ROOT / "results/order18-alternate-family-ledger/ledger.json"
PRIOR_STATUS = ROOT / "results/order18-alternate-family-ledger/status.json"
PHASES = (
    ("v1", ROOT / "results/order18-alternate-family-v1", 1200),
    ("v2-v1-residual", ROOT / "results/order18-alternate-family-v2/v1-residual", 414),
    ("v2-expanded-step1", ROOT / "results/order18-alternate-family-v2/expanded-step1", 377),
    ("v2-expanded-step2", ROOT / "results/order18-alternate-family-v2/expanded-step2", 209),
    ("v3-v2-residual", ROOT / "results/order18-alternate-family-v3/v2-residual", 31),
    ("v3-expanded-step3", ROOT / "results/order18-alternate-family-v3/expanded-step3", 1000),
)


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def phase_summary(
    name: str,
    directory: Path,
    expected_count: int,
    parents: dict[str, Graph],
) -> tuple[dict, list[dict]]:
    """Check durable event identities and preserve provisional evidence safely."""
    events_path = directory / "classification-events.jsonl"
    report = load_json(directory / "report.json")
    status = load_json(directory / "status.json")
    rows, issues = completed_rows(events_path)
    records: list[dict] = []
    hashes: dict[str, list[int]] = collections.defaultdict(list)
    ranks: dict[int, list[int]] = collections.defaultdict(list)
    status_counts: collections.Counter[str] = collections.Counter()
    solver_counts: collections.Counter[str] = collections.Counter()
    decision_failures = 0
    graph_evidence_failures = 0

    for line, row in rows:
        digest = row.get("canonical_sha256")
        rank = row.get("rank")
        outcome = row.get("status")
        primary = row.get("primary_status")
        solver = row.get("primary_solver_status")
        status_counts[str(outcome)] += 1
        solver_counts[str(solver)] += 1
        if isinstance(digest, str):
            hashes[digest].append(line)
        else:
            issues.append({"kind": "missing_canonical_hash", "phase": name, "line": line})
        if isinstance(rank, int) and rank >= 1:
            ranks[rank].append(line)
        else:
            issues.append({"kind": "invalid_event_rank", "phase": name, "line": line, "rank": rank})

        decision_ok = (
            outcome == "colorable"
            and primary == "colorable"
            and solver in {"OPTIMAL", "FEASIBLE"}
        )
        if not decision_ok:
            decision_failures += 1
            issues.append({
                "kind": "decision_status_inconsistent", "phase": name, "line": line,
                "status": outcome, "primary_status": primary, "primary_solver_status": solver,
            })

        graph_evidence = "verified"
        try:
            graph = reconstruct(row, parents)
            checks = graph_check(graph)
            reconstructed_hash = nauty_canonical_hash(graph)
            if not checks["meets_required_filters"]:
                graph_evidence = "filter_failure"
                issues.append({"kind": "graph_filter_failure", "phase": name, "line": line, "checks": checks})
            elif reconstructed_hash != digest:
                graph_evidence = "hash_mismatch"
                issues.append({
                    "kind": "canonical_hash_reconstruction_mismatch", "phase": name, "line": line,
                    "recorded": digest, "reconstructed": reconstructed_hash,
                })
        except (KeyError, TypeError, ValueError) as exc:
            graph_evidence = "missing_or_unreconstructable"
            issues.append({
                "kind": "graph_reconstruction_unresolved", "phase": name, "line": line,
                "detail": str(exc),
            })
        if graph_evidence != "verified":
            graph_evidence_failures += 1
        records.append({
            "line": line,
            "rank": rank,
            "canonical_sha256": digest,
            "status": outcome,
            "decision_consistency": "verified" if decision_ok else "failed",
            "graph_evidence": graph_evidence,
        })

    duplicate_hashes = sorted(digest for digest, lines in hashes.items() if len(lines) > 1)
    duplicate_ranks = sorted(rank for rank, lines in ranks.items() if len(lines) > 1)
    for digest in duplicate_hashes:
        issues.append({"kind": "duplicate_phase_hash", "phase": name, "canonical_sha256": digest})
    for rank in duplicate_ranks:
        issues.append({"kind": "duplicate_phase_rank", "phase": name, "rank": rank})

    report_count = report.get("counts", {}).get("classified") if report else None
    status_count = status.get("counts", {}).get("classified") if status else None
    stable = (
        report is not None
        and status is not None
        and report.get("completion") == "complete"
        and status.get("completion") == "complete"
        and len(rows) == expected_count
        and report_count == expected_count
        and status_count == expected_count
    )
    if len(rows) != expected_count:
        issues.append({"kind": "phase_event_count_mismatch", "phase": name, "expected": expected_count, "actual": len(rows)})
    if report_count != expected_count:
        issues.append({"kind": "phase_report_count_mismatch", "phase": name, "expected": expected_count, "actual": report_count})
    if status_count != expected_count:
        issues.append({"kind": "phase_status_count_mismatch", "phase": name, "expected": expected_count, "actual": status_count})
    if not stable:
        issues.append({"kind": "phase_not_stable", "phase": name})

    return {
        "name": name,
        "events_path": str(events_path.relative_to(ROOT)),
        "state": "stable" if stable else "provisional",
        "expected_completed_count": expected_count,
        "completed_event_count": len(rows),
        "unique_canonical_hash_count": len(hashes),
        "duplicate_phase_hash_count": len(duplicate_hashes),
        "duplicate_phase_hashes": duplicate_hashes,
        "duplicate_phase_rank_count": len(duplicate_ranks),
        "duplicate_phase_ranks": duplicate_ranks,
        "decision_consistency_failure_count": decision_failures,
        "missing_graph_evidence_count": graph_evidence_failures,
        "status_counts": dict(sorted(status_counts.items())),
        "primary_solver_status_counts": dict(sorted(solver_counts.items())),
        "report_completion": report.get("completion") if report else None,
        "status_completion": status.get("completion") if status else None,
    }, records + [{"_issue": issue} for issue in issues]


def markdown(document: dict) -> str:
    coverage = document["coverage"]
    lines = [
        "# Order-18 Alternate-Family Cumulative Coverage Through v3",
        "",
        "Scope: coverage of the constructed alternate structural families, not an exhaustive order-18 graph census. Canonical SHA-256 is the cross-phase identity; phase ranks are local.",
        "",
        f"Checkpoint: **{document['checkpoint_state']}**. Covered stable hashes: {coverage['stable_covered_count']}/{coverage['constructed_unique_count']}; residual constructed candidates: {coverage['residual_unclassified_count']}.",
        f"Unique covered hashes: {coverage['unique_canonical_hash_count']}; duplicate hashes: {coverage['duplicate_hash_count']}; overlap with the completed first queue: {coverage['overlap_with_completed_first_queue_count']}.",
        "",
        "| Phase | State | Completed | Unique hashes | Duplicates | Decision failures | Missing graph evidence |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for phase in document["phases"]:
        lines.append(
            f"| {phase['name']} | {phase['state']} | {phase['completed_event_count']} | "
            f"{phase['unique_canonical_hash_count']} | {phase['duplicate_phase_hash_count']} | "
            f"{phase['decision_consistency_failure_count']} | {phase['missing_graph_evidence_count']} |"
        )
    lines.extend([
        "",
        "## Residual Accounting",
        "",
        "The 31 candidates residual at v2's final bound are all covered by `v3-v2-residual`. At the larger v3 expanded-step3 bound, 2,361 candidates are newly available after prior solutions; 1,000 are covered and 1,361 remain unclassified at that bound.",
        "",
        "## Integrity",
        "",
        "No integrity mismatches were found." if document["integrity"]["ok"] else f"Integrity issues: {document['integrity']['issue_count']}. See `coverage.json` for the machine-readable issue list.",
        "",
        "Rows from a phase without matching complete report and status files are retained only as provisional and are excluded from stable coverage until the next atomic refresh.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parents = {
        name: Graph.from_json(json.loads(path.read_text(encoding="utf-8")))
        for name, path in PARENTS.items()
    }
    phase_documents = []
    all_records: list[tuple[str, dict]] = []
    issues: list[dict] = []
    for name, directory, expected in PHASES:
        phase, entries = phase_summary(name, directory, expected, parents)
        phase_documents.append(phase)
        for entry in entries:
            if "_issue" in entry:
                issues.append(entry["_issue"])
            else:
                all_records.append((name, entry))

    stable_phases = {phase["name"] for phase in phase_documents if phase["state"] == "stable"}
    covered_records = [record for phase, record in all_records if phase in stable_phases]
    hashes: dict[str, list[tuple[str, int]]] = collections.defaultdict(list)
    for phase, record in all_records:
        digest = record["canonical_sha256"]
        if isinstance(digest, str):
            hashes[digest].append((phase, record["line"]))
    duplicate_hashes = {digest: locations for digest, locations in hashes.items() if len(locations) > 1}
    for digest, locations in duplicate_hashes.items():
        issues.append({"kind": "duplicate_alternate_hash", "canonical_sha256": digest, "locations": locations})

    first_hashes, first_queue = first_queue_hashes()
    covered_hashes = sorted({record["canonical_sha256"] for record in covered_records if isinstance(record["canonical_sha256"], str)})
    first_queue_overlap = sorted(set(covered_hashes) & first_hashes)
    for digest in first_queue_overlap:
        issues.append({"kind": "covered_overlap_with_completed_first_queue", "canonical_sha256": digest})
    if not first_queue["published_manifest_matches"]:
        issues.append({"kind": "first_queue_manifest_mismatch"})

    prior_ledger = load_json(PRIOR_LEDGER)
    prior_status = load_json(PRIOR_STATUS)
    prior_covered = prior_ledger.get("coverage", {}).get("covered_count") if prior_ledger else None
    if prior_covered != 2200 or (prior_status or {}).get("status") != "complete":
        issues.append({"kind": "prior_ledger_not_complete", "covered_count": prior_covered})
    pre_v3_count = sum(phase["completed_event_count"] for phase in phase_documents[:4])
    if pre_v3_count != 2200:
        issues.append({"kind": "prior_phase_count_mismatch", "expected": 2200, "actual": pre_v3_count})

    v2_report = load_json(ROOT / "results/order18-alternate-family-v2/report.json") or {}
    v3_report = load_json(ROOT / "results/order18-alternate-family-v3/report.json") or {}
    v3_expanded = load_json(ROOT / "results/order18-alternate-family-v3/expanded-step3/report.json") or {}
    v2_uncovered = v2_report.get("completion_details", {}).get("unclassified_new_unique_remaining_at_final_bound")
    v3_new_unique = v3_expanded.get("generation", {}).get("new_unique_after_prior_solutions")
    v3_expanded_covered = next(phase["completed_event_count"] for phase in phase_documents if phase["name"] == "v3-expanded-step3")
    residual = v3_new_unique - v3_expanded_covered if isinstance(v3_new_unique, int) else None
    constructed_unique = v3_expanded.get("counts", {}).get("unique")
    if v2_uncovered != 31:
        issues.append({"kind": "v2_residual_count_mismatch", "expected": 31, "actual": v2_uncovered})
    if residual is None or residual < 0:
        issues.append({"kind": "v3_residual_unavailable", "new_unique": v3_new_unique, "covered": v3_expanded_covered})
        residual = 0
    if constructed_unique != len(covered_hashes) + residual:
        issues.append({
            "kind": "cumulative_coverage_mismatch", "constructed_unique": constructed_unique,
            "covered": len(covered_hashes), "residual": residual,
        })
    if v3_report.get("counts", {}).get("newly_classified") != 1031:
        issues.append({"kind": "v3_report_phase_count_mismatch", "expected": 1031, "actual": v3_report.get("counts", {}).get("newly_classified")})

    document = {
        "schema_version": 1,
        "purpose": "Non-mutating cumulative coverage ledger through alternate-family v3; it never reruns classification or rewrites source event logs.",
        "checkpoint_state": "stable" if all(phase["state"] == "stable" for phase in phase_documents) else "provisional",
        "sources": {
            "prior_alternate_ledger": str(PRIOR_LEDGER.relative_to(ROOT)),
            "v3_summary": "results/order18-alternate-family-v3/report.json",
            "completed_first_queue_ledger": "results/order18-targeted-ledger/coverage.json",
            "parent_graphs": {name: str(path.relative_to(ROOT)) for name, path in PARENTS.items()},
        },
        "identity_policy": "canonical_sha256 is the only cross-phase identity; phase-local rank and candidate_id are not globally portable.",
        "phases": phase_documents,
        "coverage": {
            "constructed_unique_count": constructed_unique,
            "stable_covered_count": len(covered_hashes),
            "provisional_completed_count": len(all_records) - len(covered_records),
            "residual_unclassified_count": residual,
            "unique_canonical_hash_count": len(covered_hashes),
            "duplicate_hash_count": len(duplicate_hashes),
            "overlap_with_completed_first_queue_count": len(first_queue_overlap),
            "covered_canonical_hashes": covered_hashes,
            "provisional_canonical_hashes": sorted({
                record["canonical_sha256"] for phase, record in all_records
                if phase not in stable_phases and isinstance(record["canonical_sha256"], str)
            }),
            "residual_candidates": {
                "v2_final_bound_before_v3": v2_uncovered,
                "v3_v2_residual_resolved": next(phase["completed_event_count"] for phase in phase_documents if phase["name"] == "v3-v2-residual"),
                "v3_expanded_step3_new_unique_after_prior_solutions": v3_new_unique,
                "v3_expanded_step3_classified": v3_expanded_covered,
                "v3_expanded_step3_unclassified": residual,
            },
        },
        "first_queue_membership_reconstruction": first_queue,
        "integrity": {"ok": not issues, "issue_count": len(issues), "issues": issues},
    }
    atomic_json(OUTPUT / "coverage.json", document)
    atomic_write(OUTPUT / "coverage.md", markdown(document))
    print(json.dumps({
        "checkpoint_state": document["checkpoint_state"],
        "stable_covered_count": document["coverage"]["stable_covered_count"],
        "residual_unclassified_count": document["coverage"]["residual_unclassified_count"],
        "duplicate_hash_count": document["coverage"]["duplicate_hash_count"],
        "overlap_with_completed_first_queue_count": document["coverage"]["overlap_with_completed_first_queue_count"],
        "integrity": document["integrity"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
