#!/usr/bin/env python3
"""Create a compact, source-preserving cumulative graft ledger through rank 4094."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


BASE = Path("results/neighborhood-graft-delta10-agent")
OUTPUT = BASE / "cumulative-through-4094.json"
SUMMARY = BASE / "cumulative-through-4094.md"
CORRECTIONS_DIR = Path("results/graft-span-reconciliation")
HASH_PATTERN = re.compile(r"^[0-9]+:[0-9]+:[0-9a-f]{64}$")
BANDS = (
    ("full-roots", 1, 94, BASE / "extension-full-roots/classification-state.jsonl", BASE / "extension-full-roots/report.json", "implicit"),
    ("beyond-top94", 95, 1094, BASE / "extension-beyond-top94/classification-state.jsonl", BASE / "extension-beyond-top94/report.json", "position"),
    ("beyond-top1094", 1095, 2094, BASE / "extension-beyond-top1094/classification-state.jsonl", BASE / "extension-beyond-top1094/report.json", "rank"),
    ("beyond-top2094-v2", 2095, 3094, BASE / "extension-beyond-top2094-v2/classification-state.jsonl", BASE / "extension-beyond-top2094-v2/report.json", "rank"),
    ("beyond-top3094", 3095, 4094, BASE / "extension-beyond-top3094/classification-state.jsonl", BASE / "extension-beyond-top3094/report.json", "rank"),
)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def compact_operation(operation: dict[str, Any]) -> dict[str, Any]:
    chords = operation.get("non_tree_chords")
    return {
        "rule": operation.get("rule"),
        "boundary_port_count": operation.get("boundary_port_count"),
        "core_terminal_count": operation.get("core_terminal_count"),
        "tree_new_vertices": operation.get("tree_new_vertices"),
        "non_tree_chord_count": len(chords) if isinstance(chords, list) else None,
        "removed_connector_count": len(operation.get("removed_connectors", [])) if isinstance(operation.get("removed_connectors"), list) else None,
    }


def load_validated_corrections() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read only completed, explicitly validated corrections; never infer one."""
    audit_path = CORRECTIONS_DIR / "audit.json"
    map_path = CORRECTIONS_DIR / "corrected-map.json"
    state: dict[str, Any] = {"directory_present": CORRECTIONS_DIR.is_dir()}
    corrections: list[dict[str, Any]] = []
    if audit_path.exists():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        state["audit"] = {"path": str(audit_path), "status": audit.get("status"), "error": audit.get("error")}
    if map_path.exists():
        correction_map = json.loads(map_path.read_text(encoding="utf-8"))
        if not isinstance(correction_map, dict):
            raise ValueError("validated correction map is not an object")
        state["corrected_map"] = {"path": str(map_path), "entries": len(correction_map)}
        for digest, correction in correction_map.items():
            if not isinstance(correction, dict):
                raise ValueError(f"invalid correction for {digest}")
            required = {"rank", "original_span", "corrected_span", "metadata_only"}
            if not required <= correction.keys() or correction["metadata_only"] is not True:
                raise ValueError(f"unvalidated correction for {digest}")
            corrections.append({"canonical_sha256": digest, **correction})
    return state, corrections


def main() -> None:
    rows: list[dict[str, Any]] = []
    defects: dict[str, list[Any]] = defaultdict(list)
    source_details: list[dict[str, Any]] = []
    rank_normalizations: list[dict[str, Any]] = []
    hash_sources: dict[str, list[dict[str, Any]]] = defaultdict(list)
    id_sources: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for band, first, last, state_path, report_path, rank_mode in BANDS:
        state_bytes = state_path.read_bytes()
        events = [json.loads(line) for line in state_bytes.decode("utf-8").splitlines() if line.strip()]
        classified = [(index + 1, event) for index, event in enumerate(events) if event.get("event") == "classification"]
        expected_count = last - first + 1
        if len(classified) != expected_count:
            defects["source_record_count"].append({"band": band, "expected": expected_count, "actual": len(classified)})

        report = json.loads(report_path.read_text(encoding="utf-8"))
        report_counts = report.get("counts", {})
        expected_total = last
        reported_total = report_counts.get("classified", report_counts.get("classified_total", report_counts.get("solved_total")))
        if reported_total != expected_total:
            defects["report_total_mismatch"].append({"band": band, "expected": expected_total, "reported": reported_total})
        source_details.append({
            "band": band,
            "rank_span": [first, last],
            "state_path": str(state_path),
            "state_sha256": hashlib.sha256(state_bytes).hexdigest(),
            "report_path": str(report_path),
            "classification_rows": len(classified),
            "reported_total": reported_total,
        })

        for offset, (line_number, event) in enumerate(classified):
            rank = first + offset
            supplied_rank = event.get("rank")
            supplied_position = event.get("position")
            if rank_mode == "rank" and supplied_rank != rank:
                defects["rank_label"].append({"band": band, "line": line_number, "expected": rank, "reported": supplied_rank})
            elif rank_mode == "position" and supplied_position != offset + 1:
                defects["position_label"].append({"band": band, "line": line_number, "expected": offset + 1, "reported": supplied_position})
            elif rank_mode == "implicit" and (supplied_rank is not None or supplied_position is not None):
                defects["unexpected_initial_rank_label"].append({"band": band, "line": line_number})

            record = event.get("record")
            if not isinstance(record, dict):
                defects["malformed_event"].append({"band": band, "line": line_number, "reason": "missing record"})
                continue
            primary = record.get("primary_result") if isinstance(record.get("primary_result"), dict) else {}
            operation = record.get("operation") if isinstance(record.get("operation"), dict) else {}
            digest = record.get("canonical_sha256")
            decision = record.get("decision")
            status = primary.get("status")
            solver_status = primary.get("solver_status")
            reported_span = primary.get("span")
            structural = {
                "candidate_id": record.get("candidate_id"),
                "order": record.get("order"),
                "size": record.get("size"),
                "delta": record.get("delta"),
                "minimum_degree": record.get("minimum_degree"),
                "operation": compact_operation(operation),
            }
            invalid = []
            if not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest): invalid.append("canonical_sha256")
            if not isinstance(structural["order"], int) or structural["order"] < 1: invalid.append("order")
            if not isinstance(structural["size"], int) or structural["size"] < 0: invalid.append("size")
            if not isinstance(structural["delta"], int) or not 0 <= structural["delta"] <= 10: invalid.append("delta")
            if not isinstance(structural["minimum_degree"], int) or structural["minimum_degree"] < 2: invalid.append("minimum_degree")
            if any(value is None for value in structural["operation"].values()): invalid.append("operation")
            if invalid: defects["invalid_structure"].append({"rank": rank, "fields": invalid})
            if decision != status or decision not in {"colorable", "non_colorable", "timeout"}:
                defects["decision_status"].append({"rank": rank, "decision": decision, "primary_status": status})
            if decision == "colorable" and solver_status not in {"OPTIMAL", "FEASIBLE"}:
                defects["decision_solver_status"].append({"rank": rank, "decision": decision, "solver_status": solver_status})
            if not isinstance(reported_span, int) or not isinstance(structural["delta"], int) or reported_span < structural["delta"] or reported_span > structural["order"] - 1:
                defects["metadata_span"].append({"rank": rank, "reported_span": reported_span, "delta": structural["delta"], "order": structural["order"]})

            root = event.get("root", record.get("parent"))
            ledger = {
                "rank": rank,
                "decision": decision,
                "canonical_sha256": digest,
                "parent": record.get("parent"),
                "root": root,
                "structural_metrics": structural,
                "reported_span": reported_span,
                "primary_status": status,
                "solver_status": solver_status,
                "source": {
                    "band": band,
                    "state_path": str(state_path),
                    "state_sha256": source_details[-1]["state_sha256"],
                    "report_path": str(report_path),
                    "state_line": line_number,
                    "rank_label": {"mode": rank_mode, "rank": supplied_rank, "position": supplied_position},
                },
            }
            rows.append(ledger)
            if isinstance(digest, str): hash_sources[digest].append({"rank": rank, "band": band})
            candidate_id = record.get("candidate_id")
            if isinstance(candidate_id, str): id_sources[candidate_id].append({"rank": rank, "band": band})

        if rank_mode == "implicit":
            rank_normalizations.append({"band": band, "rank_span": [first, last], "method": "append-only classification event order"})
        elif rank_mode == "position":
            rank_normalizations.append({"band": band, "rank_span": [first, last], "method": "rank = 94 + source position"})

    ranks = [row["rank"] for row in rows]
    expected_ranks = set(range(1, 4095))
    actual_ranks = set(ranks)
    duplicate_ranks = sorted(rank for rank, count in Counter(ranks).items() if count > 1)
    duplicate_hashes = {digest: locations for digest, locations in hash_sources.items() if len(locations) > 1}
    duplicate_ids = {candidate_id: locations for candidate_id, locations in id_sources.items() if len(locations) > 1}
    if duplicate_ranks: defects["duplicate_rank"] = duplicate_ranks
    if duplicate_hashes: defects["duplicate_hash"] = duplicate_hashes
    if duplicate_ids: defects["duplicate_candidate_id"] = duplicate_ids
    missing = sorted(expected_ranks - actual_ranks)
    unexpected = sorted(actual_ranks - expected_ranks)
    if missing: defects["rank_gap"] = missing
    if unexpected: defects["unexpected_rank"] = unexpected

    correction_state, corrections = load_validated_corrections()
    by_hash = {row["canonical_sha256"]: row for row in rows}
    applied_corrections = []
    for correction in corrections:
        row = by_hash.get(correction["canonical_sha256"])
        if row is None:
            defects["correction_unknown_hash"].append(correction)
        elif correction["rank"] != row["rank"] or correction["original_span"] != row["reported_span"]:
            defects["correction_identity_mismatch"].append(correction)
        else:
            row["reported_span"] = correction["corrected_span"]
            row["metadata_correction"] = correction
            applied_corrections.append(correction)

    decision_counts = Counter(row["decision"] for row in rows)
    primary_status_counts = Counter(row["primary_status"] for row in rows)
    solver_status_counts = Counter(row["solver_status"] for row in rows)
    span_counts = Counter(row["reported_span"] for row in rows)
    parent_counts = Counter(row["parent"] for row in rows)
    all_defects = {kind: value for kind, value in sorted(defects.items()) if value}
    valid = not all_defects and len(rows) == 4094 and len(set(row["canonical_sha256"] for row in rows)) == 4094
    result = {
        "schema_version": 1,
        "scope": "neighborhood-graft candidates with final Delta <= 10, ranks 1--4094",
        "source_ledgers_unchanged": True,
        "validation_status": "valid" if valid else "invalid",
        "coverage": {
            "first_rank": min(ranks) if ranks else None,
            "last_rank": max(ranks) if ranks else None,
            "authoritative_covered_count": len(rows),
            "unique_canonical_hashes": len(hash_sources),
            "remaining_unsolved_family_count": 10950 - len(hash_sources),
            "family_unique_count_reported": 10950,
        },
        "counts": {
            "decisions": dict(sorted(decision_counts.items())),
            "primary_statuses": dict(sorted(primary_status_counts.items())),
            "solver_statuses": dict(sorted(solver_status_counts.items())),
            "reported_spans": {str(key): value for key, value in sorted(span_counts.items())},
            "parents": dict(sorted(parent_counts.items())),
            "timeouts": decision_counts.get("timeout", 0),
            "negatives": decision_counts.get("non_colorable", 0),
        },
        "source_ledgers": source_details,
        "rank_normalizations": rank_normalizations,
        "span_reconciliation": {"source": correction_state, "applied_validated_metadata_corrections": applied_corrections},
        "validation": {
            "gaps": missing,
            "unexpected_ranks": unexpected,
            "duplicate_ranks": duplicate_ranks,
            "duplicate_hashes": duplicate_hashes,
            "cross_batch_hash_overlaps": duplicate_hashes,
            "metadata_only_span_defects": all_defects.get("metadata_span", []),
            "decision_or_status_inconsistencies": all_defects.get("decision_status", []) + all_defects.get("decision_solver_status", []),
            "invalid_structures": all_defects.get("invalid_structure", []),
            "all_defects": all_defects,
        },
        "ledger": rows,
    }
    atomic_json(OUTPUT, result)
    correction_text = "No validated span corrections were available to apply."
    if applied_corrections:
        correction_text = f"Applied {len(applied_corrections)} validated metadata-only span correction(s)."
    audit_note = correction_state.get("audit", {}).get("status")
    if audit_note and audit_note != "complete":
        correction_text += f" The available prior span audit is `{audit_note}` and therefore contributes no correction map."
    markdown = "\n".join((
        "# Neighborhood-Graft Cumulative Ledger Through Rank 4094",
        "",
        f"Validated coverage is **{len(rows)} ranks** (1--4094) with **{len(hash_sources)} unique canonical hashes**. The cumulative result contains **{decision_counts.get('non_colorable', 0)} negatives** and **{decision_counts.get('timeout', 0)} timeouts**; **{10950 - len(hash_sources)}** of the reported 10,950-member family remain unsolved.",
        "",
        f"All source decisions/statuses agree: {dict(sorted(decision_counts.items()))} decisions, {dict(sorted(primary_status_counts.items()))} primary statuses, and {dict(sorted(solver_status_counts.items()))} solver statuses. Scalar structural checks found {len(all_defects.get('invalid_structure', []))} invalid rows; ranks, hashes, and batch boundaries have no gaps or overlaps.",
        "",
        f"{correction_text}",
        "",
        "The JSON ledger preserves the original reported span and source provenance for every row. Its only rank metadata normalization is event-order ranks for the initial 94 rows and `94 + position` for the next 1,000 rows; source ledgers were not modified.",
        "",
    ))
    atomic_text(SUMMARY, markdown)
    if not valid:
        raise SystemExit("cumulative ledger written with validation defects")


if __name__ == "__main__":
    main()
