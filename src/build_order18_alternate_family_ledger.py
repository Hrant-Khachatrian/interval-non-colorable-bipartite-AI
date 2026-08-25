#!/usr/bin/env python3
"""Build a non-mutating, source-verified ledger for alternate order-18 families."""

from __future__ import annotations

import collections
import hashlib
import json
import os
import tempfile
from pathlib import Path

import networkx as nx

from interval_edge_coloring import Graph, nauty_canonical_hash


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "results/order18-alternate-family-ledger"
FIRST_QUEUE_LEDGER = ROOT / "results/order18-targeted-ledger/coverage.json"
PHASES = (
    ("v1", ROOT / "results/order18-alternate-family-v1/classification-events.jsonl", 1200),
    ("v2-v1-residual", ROOT / "results/order18-alternate-family-v2/v1-residual/classification-events.jsonl", 414),
    ("v2-expanded-step1", ROOT / "results/order18-alternate-family-v2/expanded-step1/classification-events.jsonl", 377),
    ("v2-expanded-step2", ROOT / "results/order18-alternate-family-v2/expanded-step2/classification-events.jsonl", 209),
)
PARENTS = {
    "Q1-00012.graph": ROOT / "results/candidates/Q1-00012/Q1-00012.graph.json",
    "Q1-00014.graph": ROOT / "results/candidates/Q1-00014/Q1-00014.graph.json",
}


def atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_json(path: Path, value: object) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def completed_rows(path: Path) -> tuple[list[tuple[int, dict]], list[dict]]:
    rows: list[tuple[int, dict]] = []
    discrepancies: list[dict] = []
    if not path.exists():
        return rows, [{"kind": "missing_event_log", "path": str(path.relative_to(ROOT))}]
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                discrepancies.append({
                    "kind": "invalid_event_json", "path": str(path.relative_to(ROOT)),
                    "line": line_number, "detail": str(exc),
                })
                continue
            if event.get("event") != "classification_completed":
                continue
            row = event.get("row")
            if not isinstance(row, dict):
                discrepancies.append({
                    "kind": "completion_without_row", "path": str(path.relative_to(ROOT)),
                    "line": line_number,
                })
                continue
            rows.append((line_number, row))
    return rows, discrepancies


def identify_same_side(base: Graph, first: str, second: str) -> Graph:
    if first == second or first not in base.vertex_set or second not in base.vertex_set:
        raise ValueError("identification vertices are invalid")
    side_number = next(
        (index for index, side in enumerate(base.bipartition) if first in side and second in side),
        None,
    )
    if side_number is None:
        raise ValueError("identification is not within one bipartition class")
    owner = {vertex: vertex for vertex in base.vertices}
    merged = "&".join(sorted((first, second)))
    owner[first] = merged
    owner[second] = merged
    edges = {
        tuple(sorted((owner[u], owner[v])))
        for u, v in base.edges
        if owner[u] != owner[v]
    }
    reduced = [merged] + [owner[v] for v in base.bipartition[side_number] if v not in {first, second}]
    other = list(base.bipartition[1 - side_number])
    left, right = (reduced, other) if side_number == 0 else (other, reduced)
    return Graph(sorted(left + right), sorted(edges), [sorted(left), sorted(right)])


def reconstruct(row: dict, parents: dict[str, Graph]) -> Graph:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("metadata missing")
    parent_name = metadata.get("parent")
    base = parents.get(parent_name)
    if base is None:
        raise ValueError(f"unknown parent {parent_name!r}")
    identified = metadata.get("identified")
    if not isinstance(identified, list) or len(identified) != 2 or not all(isinstance(v, str) for v in identified):
        raise ValueError("invalid identified pair")
    quotient = identify_same_side(base, identified[0], identified[1])
    edges = set(quotient.edges)
    lane = metadata.get("lane")
    if lane == "identify-then-edge-delete-restore":
        removed = metadata.get("removed_edge")
        restored = metadata.get("restored_edge")
        if not isinstance(removed, list) or not isinstance(restored, list) or len(removed) != 2 or len(restored) != 2:
            raise ValueError("invalid delete/restore metadata")
        removed_edge = tuple(sorted(removed))
        restored_edge = tuple(sorted(restored))
        if removed_edge not in edges:
            raise ValueError("removed edge absent from quotient")
        edges.remove(removed_edge)
        edges.add(restored_edge)
    elif lane == "identify-then-three-edge-switch":
        removed = metadata.get("removed_edges")
        added = metadata.get("added_edges")
        if not isinstance(removed, list) or not isinstance(added, list) or len(removed) != 3 or len(added) != 3:
            raise ValueError("invalid three-edge-switch metadata")
        # The historical generator formed its removal set from left-to-right
        # oriented triples but stored the base edge set in lexical order.  On
        # these Q1 parents the tuples therefore do not compare equal, so its
        # generated graph retained the three recorded "removed" edges.  Match
        # that exact serialized construction; do not silently repair it.
        removed_edges = {
            (edge[0], edge[1]) if edge[0] in quotient.bipartition[0] else (edge[1], edge[0])
            for edge in removed
        }
        added_edges = {tuple(sorted(edge)) for edge in added}
        if len(removed_edges) != 3 or len(added_edges) != 3:
            raise ValueError("invalid three-edge-switch edge set")
        edges.difference_update(removed_edges)
        edges.update(added_edges)
    else:
        raise ValueError(f"unknown lane {lane!r}")
    return Graph(quotient.vertices, sorted(edges), quotient.bipartition)


def graph_check(graph: Graph) -> dict:
    degrees = graph.degrees
    loops = any(u == v for u, v in graph.edges)
    unique_edges = len(graph.edges) == len({tuple(sorted(edge)) for edge in graph.edges})
    bipartite = graph.bipartition is not None and all(
        (u in graph.bipartition[0]) != (v in graph.bipartition[0]) for u, v in graph.edges
    )
    return {
        "order": graph.n,
        "size": graph.m,
        "simple": not loops and unique_edges,
        "connected": nx.is_connected(graph._nx),
        "bipartite": bipartite,
        "minimum_degree": min(degrees.values(), default=0),
        "meets_required_filters": graph.n == 18 and not loops and unique_edges and bipartite
        and nx.is_connected(graph._nx) and min(degrees.values(), default=0) >= 2,
    }


def first_queue_hashes() -> tuple[set[str], dict]:
    """Reconstruct only hash membership; alternate phase ranks are never matched to it."""
    import order18_targeted_search as prior

    class Args:
        lanes = "all"
        candidate_cap = 12_987
        rank_start = 0
        max_additions = 1
        max_deleted_degree = 3
        max_rewires = 750
        extension_limit = 18

    selected, raw_lanes, generated_lanes, selected_lanes, diagnostics = prior.generate_candidates(Args())
    hashes = {item[4] for item in selected}
    manifest = hashlib.sha256("".join(f"{rank}:{item[4]}\n" for rank, item in enumerate(selected, start=1)).encode("ascii")).hexdigest()
    published = json.loads(FIRST_QUEUE_LEDGER.read_text(encoding="utf-8"))
    expected_manifest = published["queue"]["rank_to_canonical_hash_manifest_sha256"]
    return hashes, {
        "reconstructed_count": len(selected),
        "unique_hash_count": len(hashes),
        "rank_hash_manifest_sha256": manifest,
        "published_manifest_matches": manifest == expected_manifest,
        "published_manifest_sha256": expected_manifest,
        "generation": {
            "generated_raw_by_lane": dict(sorted(raw_lanes.items())),
            "unique_ranked_by_lane": dict(sorted(generated_lanes.items())),
            "selected_by_lane": dict(sorted(selected_lanes.items())),
            **diagnostics,
        },
        "rank_mapping_policy": "membership-only reconstruction; no alternate event rank is aligned to a first-queue rank",
    }


def summarize_phase(name: str, path: Path, expected_count: int, parents: dict[str, Graph]) -> tuple[dict, list[dict], list[dict]]:
    source_rows, discrepancies = completed_rows(path)
    records: list[dict] = []
    hashes: dict[str, list[int]] = collections.defaultdict(list)
    ranks: dict[int, list[int]] = collections.defaultdict(list)
    statuses: collections.Counter[str] = collections.Counter()
    primary_statuses: collections.Counter[str] = collections.Counter()
    solver_statuses: collections.Counter[str] = collections.Counter()
    reconstructed_count = 0
    graph_filter_failures = 0
    mapping_failures = 0
    decision_failures = 0
    for line_number, row in source_rows:
        rank = row.get("rank")
        digest = row.get("canonical_sha256")
        candidate_id = row.get("candidate_id")
        status = row.get("status")
        primary_status = row.get("primary_status")
        solver_status = row.get("primary_solver_status")
        statuses[str(status)] += 1
        primary_statuses[str(primary_status)] += 1
        solver_statuses[str(solver_status)] += 1
        record = {
            "event_line": line_number,
            "candidate_id": candidate_id,
            "event_rank": rank,
            "canonical_sha256": digest,
            "status": status,
            "primary_status": primary_status,
            "primary_solver_status": solver_status,
            "primary_span": row.get("primary_span"),
            "source_parent": row.get("metadata", {}).get("parent") if isinstance(row.get("metadata"), dict) else None,
            "source_lane": row.get("metadata", {}).get("lane") if isinstance(row.get("metadata"), dict) else None,
            "rank_hash_mapping": "unresolved",
            "graph_verification": "unresolved",
            "decision_consistency": "unresolved",
        }
        if not isinstance(rank, int) or rank < 1:
            discrepancies.append({"kind": "invalid_event_rank", "phase": name, "line": line_number, "rank": rank})
        else:
            ranks[rank].append(line_number)
        if not isinstance(digest, str):
            discrepancies.append({"kind": "missing_canonical_hash", "phase": name, "line": line_number})
        else:
            hashes[digest].append(line_number)
        if status == "colorable" and primary_status == "colorable" and solver_status in {"OPTIMAL", "FEASIBLE"}:
            record["decision_consistency"] = "verified"
        else:
            decision_failures += 1
            discrepancies.append({
                "kind": "decision_status_inconsistent", "phase": name, "line": line_number,
                "status": status, "primary_status": primary_status, "primary_solver_status": solver_status,
            })
        try:
            graph = reconstruct(row, parents)
            checks = graph_check(graph)
            reconstructed_digest = nauty_canonical_hash(graph)
            record["reconstructed_canonical_sha256"] = reconstructed_digest
            record["graph_checks"] = checks
            reconstructed_count += 1
            if checks["meets_required_filters"]:
                record["graph_verification"] = "verified"
            else:
                graph_filter_failures += 1
                discrepancies.append({"kind": "graph_filter_failure", "phase": name, "line": line_number, "checks": checks})
            if isinstance(digest, str) and digest == reconstructed_digest:
                record["rank_hash_mapping"] = "verified_from_source_graph"
            else:
                mapping_failures += 1
                discrepancies.append({
                    "kind": "canonical_hash_reconstruction_mismatch", "phase": name, "line": line_number,
                    "recorded": digest, "reconstructed": reconstructed_digest,
                })
        except (KeyError, TypeError, ValueError) as exc:
            mapping_failures += 1
            discrepancies.append({
                "kind": "graph_reconstruction_unresolved", "phase": name, "line": line_number, "detail": str(exc),
            })
        records.append(record)
    duplicate_ranks = {rank: lines for rank, lines in ranks.items() if len(lines) > 1}
    duplicate_hashes = {digest: lines for digest, lines in hashes.items() if len(lines) > 1}
    for rank, lines in duplicate_ranks.items():
        discrepancies.append({"kind": "duplicate_phase_rank", "phase": name, "rank": rank, "lines": lines})
    for digest, lines in duplicate_hashes.items():
        discrepancies.append({"kind": "duplicate_phase_hash", "phase": name, "canonical_sha256": digest, "lines": lines})
    summary = {
        "name": name,
        "events_path": str(path.relative_to(ROOT)),
        "expected_completed_count": expected_count,
        "completed_event_count": len(source_rows),
        "unique_phase_rank_count": len(ranks),
        "unique_canonical_hash_count": len(hashes),
        "reconstructed_graph_count": reconstructed_count,
        "unresolved_graph_count": len(source_rows) - reconstructed_count,
        "graph_filter_failure_count": graph_filter_failures,
        "rank_hash_mapping_failure_count": mapping_failures,
        "decision_consistency_failure_count": decision_failures,
        "duplicate_phase_rank_count": len(duplicate_ranks),
        "duplicate_phase_ranks": sorted(duplicate_ranks),
        "duplicate_phase_hash_count": len(duplicate_hashes),
        "duplicate_phase_hashes": sorted(duplicate_hashes),
        "status_counts": dict(sorted(statuses.items())),
        "primary_status_counts": dict(sorted(primary_statuses.items())),
        "primary_solver_status_counts": dict(sorted(solver_statuses.items())),
        "rank_identity_policy": "event rank is phase-local; canonical hash is the cross-run identity",
        "records": records,
    }
    if len(source_rows) != expected_count:
        discrepancies.append({"kind": "phase_completed_count_mismatch", "phase": name, "expected": expected_count, "actual": len(source_rows)})
    return summary, records, discrepancies


def markdown(ledger: dict) -> str:
    coverage = ledger["coverage"]
    lines = [
        "# Order-18 Alternate-Family Ledger",
        "",
        "Scope: the alternate structured families through v2. This is coverage of the constructed family, not an exhaustive order-18 graph census.",
        "",
        f"Classified: {coverage['classified_count']}/{coverage['constructed_unique_count']} unique alternate candidates; residual at the final v2 bounds: {coverage['residual_unclassified_count']}.",
        f"Verified alternate canonical hashes: {coverage['unique_canonical_hash_count']}; classified overlap with the completed first queue: {coverage['classified_overlap_with_completed_first_queue_count']}.",
        "",
        "| Phase | Completed | Unique hashes | Reconstructed | Unresolved maps | Status |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for phase in ledger["phases"]:
        lines.append(
            f"| {phase['name']} | {phase['completed_event_count']} | {phase['unique_canonical_hash_count']} | {phase['reconstructed_graph_count']} | {phase['rank_hash_mapping_failure_count']} | {phase['status_counts']} |"
        )
    integrity = ledger["integrity"]
    lines.extend(["", "## Integrity", ""])
    if integrity["ok"]:
        lines.append("All serialized/reconstructed candidates satisfy the required graph filters; every recorded canonical hash matches reconstruction; phase-local ranks and solver decisions are consistent.")
    else:
        lines.append(f"Integrity issues: {integrity['issue_count']}. See discrepancies.jsonl for machine-readable details.")
    lines.extend([
        "",
        "## Rank Handling",
        "",
        "Candidate IDs and ranks restart in each phase. The ledger does not infer a portable global rank from a regenerated ordering. Each retained mapping is `(phase, event_rank) -> canonical_sha256`, verified from the serialized parent graph plus the recorded surgery metadata when reconstruction succeeds.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parents = {name: Graph.from_json(json.loads(path.read_text(encoding="utf-8"))) for name, path in PARENTS.items()}
    all_discrepancies: list[dict] = []
    phases: list[dict] = []
    all_records: list[tuple[str, dict]] = []
    for name, path, expected_count in PHASES:
        phase, records, discrepancies = summarize_phase(name, path, expected_count, parents)
        phases.append(phase)
        all_records.extend((name, record) for record in records)
        all_discrepancies.extend(discrepancies)

    hashes: dict[str, list[tuple[str, int]]] = collections.defaultdict(list)
    for phase_name, record in all_records:
        digest = record.get("canonical_sha256")
        if isinstance(digest, str):
            hashes[digest].append((phase_name, record["event_line"]))
    cross_phase_duplicates = {digest: locations for digest, locations in hashes.items() if len(locations) > 1}
    for digest, locations in cross_phase_duplicates.items():
        all_discrepancies.append({"kind": "duplicate_alternate_hash", "canonical_sha256": digest, "locations": locations})

    first_hashes, first_queue = first_queue_hashes()
    overlap = sorted(set(hashes) & first_hashes)
    for digest in overlap:
        all_discrepancies.append({"kind": "classified_overlap_with_completed_first_queue", "canonical_sha256": digest})
    if not first_queue["published_manifest_matches"]:
        all_discrepancies.append({"kind": "first_queue_manifest_mismatch", "details": first_queue})

    v2_report = json.loads((ROOT / "results/order18-alternate-family-v2/report.json").read_text(encoding="utf-8"))
    final_generation = v2_report["final_bound_generation"]
    constructed_unique = final_generation["globally_unique_after_first_queue_filter"]
    classified = len(all_records)
    residual = v2_report["completion_details"]["unclassified_new_unique_remaining_at_final_bound"]
    expected_total = classified + residual
    if constructed_unique != expected_total:
        all_discrepancies.append({
            "kind": "final_bound_coverage_mismatch", "constructed_unique": constructed_unique,
            "classified": classified, "residual": residual,
        })
    if final_generation["overlap_with_completed_first_queue"] != 839:
        all_discrepancies.append({
            "kind": "unexpected_reported_first_queue_overlap_count",
            "reported": final_generation["overlap_with_completed_first_queue"], "expected": 839,
        })

    integrity = {
        "ok": not all_discrepancies,
        "issue_count": len(all_discrepancies),
        "external_note": "The v1 spot-audit investigation is intentionally not reinterpreted by this ledger.",
    }
    ledger = {
        "schema_version": 1,
        "purpose": "Non-mutating cumulative ledger for the alternate order-18 structural families through v2; it does not rerun classification.",
        "sources": {
            "v1_events": str(PHASES[0][1].relative_to(ROOT)),
            "v2_report": "results/order18-alternate-family-v2/report.json",
            "parent_graphs": {name: str(path.relative_to(ROOT)) for name, path in PARENTS.items()},
            "completed_first_queue_ledger": str(FIRST_QUEUE_LEDGER.relative_to(ROOT)),
        },
        "rank_mapping_policy": {
            "portable_rank_order_assumed": False,
            "identity": "canonical_sha256",
            "mapping": "phase-local event rank matched to canonical hash by reconstructing the recorded source graph and surgery",
            "unresolved_mapping_policy": "record unresolved rather than infer a match",
        },
        "phases": phases,
        "coverage": {
            "constructed_unique_count": constructed_unique,
            "classified_count": classified,
            "covered_count": classified,
            "uncovered_count": residual,
            "residual_unclassified_count": residual,
            "unique_canonical_hash_count": len(hashes),
            "global_alternate_hash_duplicate_count": len(cross_phase_duplicates),
            "classified_overlap_with_completed_first_queue_count": len(overlap),
            "classified_overlap_with_completed_first_queue_hashes": overlap,
            "reported_final_bound_generation_overlap_with_completed_first_queue_count": final_generation["overlap_with_completed_first_queue"],
            "status_counts": dict(sorted(collections.Counter(str(r["status"]) for _, r in all_records).items())),
            "primary_status_counts": dict(sorted(collections.Counter(str(r["primary_status"]) for _, r in all_records).items())),
            "primary_solver_status_counts": dict(sorted(collections.Counter(str(r["primary_solver_status"]) for _, r in all_records).items())),
        },
        "first_queue_membership_reconstruction": first_queue,
        "integrity": integrity,
    }
    atomic_json(OUTPUT / "ledger.json", ledger)
    atomic_write(OUTPUT / "coverage.md", markdown(ledger))
    atomic_write(OUTPUT / "discrepancies.jsonl", "".join(json.dumps(item, sort_keys=True) + "\n" for item in all_discrepancies))
    atomic_json(OUTPUT / "status.json", {
        "schema_version": 1,
        "status": "complete" if integrity["ok"] else "integrity_issues_present",
        "classified_count": classified,
        "uncovered_count": residual,
        "unique_canonical_hash_count": len(hashes),
        "classified_first_queue_overlap_count": len(overlap),
        "integrity_issue_count": len(all_discrepancies),
    })
    print(json.dumps({"coverage": ledger["coverage"], "integrity": integrity}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
