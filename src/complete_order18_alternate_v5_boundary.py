#!/usr/bin/env python3
"""Append any missing v5 structural-boundary classifications."""

from __future__ import annotations

from order18_alternate_family import (
    ROOT,
    append_event,
    atomic_json,
    classify,
    completed_hashes,
    generate,
)
from reconcile_order18_alternate_v5_expansion import (
    BOUNDS,
    CLASSIFIED_LIMIT,
    EVENTS,
    PRIOR_EVENTS,
    completed_rows,
)


def main() -> None:
    ranked, _ = generate(**BOUNDS)
    prior_hashes = set().union(*(completed_hashes(path) for path in PRIOR_EVENTS))
    selected = [entry for entry in ranked if entry[3] not in prior_hashes][:CLASSIFIED_LIMIT]
    atomic_json(ROOT / "results/order18-alternate-family-v5-expansion/selection-manifest.json", {
        "schema_version": 1,
        "purpose": "Durable v5 structural-ranking selection snapshot.",
        "construction_bounds": BOUNDS,
        "selection_size": len(selected),
        "entries": [
            {"structural_rank": index, "canonical_sha256": entry[3], "structural_score": list(entry[:3])}
            for index, entry in enumerate(selected, 1)
        ],
    })
    done = {row["canonical_sha256"] for row in completed_rows(EVENTS)}
    needed = [entry for entry in selected if entry[3] not in done]
    needed_hashes = {entry[3] for entry in needed}
    next_rank = max((row["rank"] for row in completed_rows(EVENTS)), default=0) + 1
    for structural_rank, entry in enumerate(selected, 1):
        if entry[3] not in needed_hashes:
            continue
        _, _, _, digest, graph, _ = entry
        row = classify({
            "rank": next_rank,
            "digest": digest,
            "graph": graph.to_json(),
            "primary_seconds": 2.0,
            "span_seconds": 4.0,
            "workers": 1,
        })
        row["structural_rank_at_v5_reconciliation"] = structural_rank
        append_event(EVENTS, {"event": "classification_completed", "row": row})
        next_rank += 1
    print(f"appended={len(needed)}")


if __name__ == "__main__":
    main()
