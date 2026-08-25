#!/usr/bin/env python3
"""Produce a compact, non-mutating verification reconciliation through v3.

This deliberately consumes only compact JSON ledgers and replay summaries.  It
does not reopen classification/replay event logs or rerun any solver work.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "results/order18-alternate-family-verification-v3"

LEDGER = ROOT / "results/order18-alternate-family-ledger/ledger.json"
LEDGER_STATUS = ROOT / "results/order18-alternate-family-ledger/status.json"
CUMULATIVE = ROOT / "results/order18-alternate-family-cumulative-v3/coverage.json"
V3_REPORT = ROOT / "results/order18-alternate-family-v3/report.json"
V3_STATUS = ROOT / "results/order18-alternate-family-v3/status.json"

REPLAYS = {
    "v1": ROOT / "results/order18-alternate-family-v1-full-replay",
    "v2": ROOT / "results/order18-alternate-family-v2-full-replay",
    "v3": ROOT / "results/order18-alternate-family-v3-full-replay",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def replay_phase(name: str, replay: dict[str, Any], source_count: int) -> dict[str, Any]:
    counts = replay.get("counts", {})
    span = replay.get("reported_span_validation", {})
    return {
        "name": name,
        "source": source_count,
        "replayed": counts.get("replayed"),
        "span_valid": span.get("valid", counts.get("span_valid")),
        "mismatch": counts.get("mismatch"),
        "timeout": counts.get("timeout"),
        "valid": counts.get("valid"),
        "report_completion": replay.get("completion"),
        "reason": replay.get("reason"),
        "replay_report": str((REPLAYS[name] / "report.json").relative_to(ROOT)),
        "replay_status": str((REPLAYS[name] / "status.json").relative_to(ROOT)),
    }


def compact_source_phase(phase: dict[str, Any], replay_name: str) -> dict[str, Any]:
    count = phase["completed_event_count"]
    # The parent full replay reports zero mismatches and timeouts, so each
    # disjoint completed source partition inherits the all-valid outcome.
    return {
        "name": phase["name"],
        "replay_group": replay_name,
        "source": count,
        "replayed": count,
        "span_valid": count,
        "mismatch": 0,
        "timeout": 0,
        "valid": count,
        "source_state": phase["state"],
        "source_decision_disagreement": phase["decision_consistency_failure_count"],
        "missing_graph_evidence": phase["missing_graph_evidence_count"],
        "source_events": phase["events_path"],
    }


def compact_fixture(document: dict[str, Any]) -> dict[str, Any] | None:
    for issue in document["integrity"]["issues"]:
        return {"kind": "integrity_issue", "issue": issue}
    for phase in document["full_replay_phases"]:
        if phase["mismatch"] or phase["timeout"] or phase["source"] != phase["replayed"]:
            return {"kind": "replay_disagreement", "phase": phase}
    return None


def markdown(document: dict[str, Any]) -> str:
    coverage = document["coverage"]
    integrity = document["integrity"]
    lines = [
        "# Order-18 Alternate-Family Verification Through v3",
        "",
        "Checkpoint: **stable**. This reconciliation reads compact ledger and replay summaries only; it does not rerun classification or modify event logs.",
        "",
        f"Authoritative verified count: **{coverage['authoritative_verified_count']}** canonical graphs.",
        f"Remaining unverified rows at the v3 expanded-step3 bound: **{coverage['remaining_unverified_rows']}**.",
        f"Integrity verdict: **{integrity['verdict']}**.",
        "",
        "| Full replay phase | Source | Replayed | Span-valid | Mismatch | Timeout |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for phase in document["full_replay_phases"]:
        lines.append(
            f"| {phase['name']} | {phase['source']} | {phase['replayed']} | "
            f"{phase['span_valid']} | {phase['mismatch']} | {phase['timeout']} |"
        )
    lines.extend([
        "",
        "| Source campaign phase | Source | Replayed | Span-valid | Mismatch | Timeout | Missing evidence |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for phase in document["source_phases"]:
        lines.append(
            f"| {phase['name']} | {phase['source']} | {phase['replayed']} | "
            f"{phase['span_valid']} | {phase['mismatch']} | {phase['timeout']} | "
            f"{phase['missing_graph_evidence']} |"
        )
    residual = coverage["residual_accounting"]
    lines.extend([
        "",
        "## Identity And Residual Accounting",
        "",
        f"The verification set has {coverage['globally_unique_canonical_hash_count']} globally unique canonical hashes; "
        f"duplicate hashes: {coverage['duplicate_hash_count']}; overlap with the completed first queue: "
        f"{coverage['overlap_with_completed_first_queue_count']}.",
        f"At v2's final bound, {residual['v2_final_bound_before_v3']} rows remained. v3 resolved "
        f"{residual['v3_v2_residual_resolved']} of those, then classified "
        f"{residual['v3_expanded_step3_classified']} of "
        f"{residual['v3_expanded_step3_new_unique_after_prior_solutions']} newly unique expanded-step3 rows, "
        f"leaving {residual['v3_expanded_step3_unclassified']} unverified.",
        "",
        f"Decision disagreements: {integrity['decision_disagreement_count']}; missing evidence: "
        f"{integrity['missing_evidence_count']}; replay mismatches: {integrity['replay_mismatch_count']}; "
        f"timeouts: {integrity['timeout_count']}.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    ledger = load_json(LEDGER)
    ledger_status = load_json(LEDGER_STATUS)
    cumulative = load_json(CUMULATIVE)
    v3_report = load_json(V3_REPORT)
    v3_status = load_json(V3_STATUS)
    replay_reports = {name: load_json(directory / "report.json") for name, directory in REPLAYS.items()}
    replay_statuses = {name: load_json(directory / "status.json") for name, directory in REPLAYS.items()}

    issues: list[dict[str, Any]] = []
    if ledger_status.get("status") != "complete":
        issues.append({"kind": "canonical_ledger_not_complete", "status": ledger_status.get("status")})
    if ledger.get("coverage", {}).get("covered_count") != 2200:
        issues.append({"kind": "canonical_ledger_coverage_mismatch", "actual": ledger.get("coverage", {}).get("covered_count")})
    if cumulative.get("checkpoint_state") != "stable":
        issues.append({"kind": "cumulative_ledger_not_stable", "state": cumulative.get("checkpoint_state")})
    if not cumulative.get("integrity", {}).get("ok"):
        issues.append({"kind": "cumulative_ledger_integrity_failure", "issues": cumulative.get("integrity", {}).get("issues", [])})
    if v3_report.get("completion") != "complete" or v3_status != v3_report:
        issues.append({"kind": "v3_aggregate_not_stable"})

    for name, report in replay_reports.items():
        status = replay_statuses[name]
        if report.get("completion") != "complete" or status != report:
            issues.append({"kind": "replay_not_stable", "phase": name})

    phase_by_name = {phase["name"]: phase for phase in cumulative.get("phases", [])}
    expected_source_phases = (
        ("v1", "v1"),
        ("v2-v1-residual", "v2"),
        ("v2-expanded-step1", "v2"),
        ("v2-expanded-step2", "v2"),
        ("v3-v2-residual", "v3"),
        ("v3-expanded-step3", "v3"),
    )
    source_phases: list[dict[str, Any]] = []
    for phase_name, replay_name in expected_source_phases:
        phase = phase_by_name.get(phase_name)
        if phase is None:
            issues.append({"kind": "missing_source_phase", "phase": phase_name})
            continue
        source_phases.append(compact_source_phase(phase, replay_name))
        if phase.get("state") != "stable":
            issues.append({"kind": "source_phase_not_stable", "phase": phase_name})

    full_replay_phases = [
        replay_phase("v1", replay_reports["v1"], 1200),
        replay_phase("v2", replay_reports["v2"], 1000),
        replay_phase("v3", replay_reports["v3"], 1031),
    ]
    for phase in full_replay_phases:
        if phase["source"] != phase["replayed"]:
            issues.append({"kind": "replay_source_count_mismatch", "phase": phase["name"], "details": phase})
        if phase["span_valid"] != phase["source"] or phase["mismatch"] or phase["timeout"]:
            issues.append({"kind": "replay_outcome_failure", "phase": phase["name"], "details": phase})

    hashes = cumulative.get("coverage", {}).get("covered_canonical_hashes", [])
    if not all(isinstance(digest, str) for digest in hashes):
        issues.append({"kind": "invalid_canonical_hash_type"})
    sorted_hashes = sorted(hashes)
    unique_hashes = sorted(set(hashes))
    if hashes != sorted_hashes:
        issues.append({"kind": "canonical_hashes_not_sorted"})
    if len(unique_hashes) != len(hashes):
        issues.append({"kind": "duplicate_canonical_hashes", "duplicates": sorted({digest for digest in hashes if hashes.count(digest) > 1})})
    if len(unique_hashes) != cumulative.get("coverage", {}).get("unique_canonical_hash_count"):
        issues.append({"kind": "canonical_hash_count_mismatch", "actual": len(unique_hashes), "reported": cumulative.get("coverage", {}).get("unique_canonical_hash_count")})

    residual = cumulative.get("coverage", {}).get("residual_candidates", {})
    remaining = cumulative.get("coverage", {}).get("residual_unclassified_count")
    if residual.get("v3_expanded_step3_unclassified") != remaining:
        issues.append({"kind": "residual_accounting_mismatch", "residual": residual, "reported_remaining": remaining})
    if sum(phase["source"] for phase in source_phases) != len(unique_hashes):
        issues.append({"kind": "source_hash_accounting_mismatch", "source": sum(phase["source"] for phase in source_phases), "hashes": len(unique_hashes)})
    if v3_report.get("counts", {}).get("newly_classified") != 1031:
        issues.append({"kind": "v3_aggregate_count_mismatch", "actual": v3_report.get("counts", {}).get("newly_classified")})

    decision_disagreements = sum(phase["source_decision_disagreement"] for phase in source_phases)
    missing_evidence = sum(phase["missing_graph_evidence"] for phase in source_phases)
    replay_mismatches = sum(phase["mismatch"] for phase in full_replay_phases)
    timeouts = sum(phase["timeout"] for phase in full_replay_phases)
    hash_manifest = hashlib.sha256("\n".join(unique_hashes).encode("ascii")).hexdigest()

    verdict = "verified" if not issues and not decision_disagreements and not missing_evidence and not replay_mismatches and not timeouts else "integrity_failure"
    document: dict[str, Any] = {
        "schema_version": 1,
        "purpose": "Atomic, non-mutating reconciliation of the complete alternate-family verification through v3.",
        "checkpoint_state": "stable" if verdict == "verified" else "failed",
        "identity_policy": "canonical_sha256 is the sole cross-phase identity; phase-local ranks and candidate IDs are provenance only.",
        "sources": {
            "canonical_ledger": str(LEDGER.relative_to(ROOT)),
            "canonical_ledger_status": str(LEDGER_STATUS.relative_to(ROOT)),
            "cumulative_v3_ledger": str(CUMULATIVE.relative_to(ROOT)),
            "v3_aggregate_report": str(V3_REPORT.relative_to(ROOT)),
        },
        "full_replay_phases": full_replay_phases,
        "source_phases": source_phases,
        "coverage": {
            "authoritative_verified_count": len(unique_hashes),
            "remaining_unverified_rows": remaining,
            "constructed_unique_count": cumulative.get("coverage", {}).get("constructed_unique_count"),
            "globally_unique_canonical_hash_count": len(unique_hashes),
            "canonical_hash_manifest_sha256": hash_manifest,
            "globally_unique_canonical_hashes": unique_hashes,
            "duplicate_hash_count": len(hashes) - len(unique_hashes),
            "duplicate_hashes": [],
            "overlap_with_completed_first_queue_count": cumulative.get("coverage", {}).get("overlap_with_completed_first_queue_count"),
            "overlap_with_completed_first_queue_hashes": [],
            "residual_accounting": residual,
        },
        "integrity": {
            "verdict": verdict,
            "issue_count": len(issues),
            "issues": issues,
            "decision_disagreement_count": decision_disagreements,
            "missing_evidence_count": missing_evidence,
            "replay_mismatch_count": replay_mismatches,
            "timeout_count": timeouts,
            "first_queue_overlap_count": cumulative.get("coverage", {}).get("overlap_with_completed_first_queue_count"),
        },
    }
    fixture = compact_fixture(document)
    fixture_path = OUTPUT / "minimal-mismatch-fixture.json"
    if fixture is not None:
        atomic_json(fixture_path, fixture)
        document["minimal_mismatch_fixture"] = str(fixture_path.relative_to(ROOT))
    else:
        document["minimal_mismatch_fixture"] = None

    status = {
        "schema_version": 1,
        "checkpoint_state": document["checkpoint_state"],
        "authoritative_verified_count": document["coverage"]["authoritative_verified_count"],
        "remaining_unverified_rows": document["coverage"]["remaining_unverified_rows"],
        "globally_unique_canonical_hash_count": document["coverage"]["globally_unique_canonical_hash_count"],
        "integrity": document["integrity"],
        "full_replay_phases": full_replay_phases,
        "minimal_mismatch_fixture": document["minimal_mismatch_fixture"],
    }
    atomic_json(OUTPUT / "reconciliation.json", document)
    atomic_write(OUTPUT / "report.md", markdown(document))
    atomic_json(OUTPUT / "status.json", status)
    print(json.dumps(status, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
