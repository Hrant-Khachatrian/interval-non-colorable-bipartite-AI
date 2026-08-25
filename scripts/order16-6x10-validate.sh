#!/usr/bin/env bash
set -euo pipefail

ssh -o BatchMode=yes hrant@cluster.ysu.am 'bash -s' <<'REMOTE'
cd /mnt/weka/hrant/interval-search
python3 - <<'PY'
import json
from collections import Counter
from pathlib import Path

root = Path("results/order16-6x10")
files = sorted(root.glob("chunk-*.jsonl"), key=lambda p: int(p.stem.split("-")[1]))
rows = 0
malformed = 0
statuses = Counter()
spans = Counter()
indices = set()
hashes = Counter()
duplicate_indices = []
duplicate_hashes = {}
candidates = []
timeout_rows = []
per_chunk = []

for path in files:
    chunk_rows = 0
    first_index = None
    last_index = None
    with path.open() as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                index = int(row["index"])
                status = str(row.get("status", ""))
                span = row.get("span")
                digest = str(row.get("canonical_sha256", ""))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                malformed += 1
                continue
            chunk_rows += 1
            rows += 1
            first_index = index if first_index is None else min(first_index, index)
            last_index = index if last_index is None else max(last_index, index)
            statuses[status] += 1
            spans[span] += 1
            if index in indices:
                duplicate_indices.append(index)
            else:
                indices.add(index)
            hashes[digest] += 1
            if status == "non-colorable":
                candidates.append((index, path.name))
            if status == "timeout" or row.get("solver_status") == "timeout":
                timeout_rows.append((index, path.name))
    per_chunk.append({
        "chunk": int(path.stem.split("-")[1]),
        "rows": chunk_rows,
        "first_index": first_index,
        "last_index": last_index,
    })

for digest, count in hashes.items():
    if count > 1:
        duplicate_hashes[digest] = count

summary = {
    "chunks": len(files),
    "rows": rows,
    "malformed": malformed,
    "statuses": dict(statuses),
    "spans": {str(k): v for k, v in sorted(spans.items(), key=lambda item: str(item[0]))},
    "unique_indices": len(indices),
    "duplicate_indices": duplicate_indices[:100],
    "duplicate_index_count": len(duplicate_indices),
    "duplicate_hash_count": len(duplicate_hashes),
    "duplicate_hashes": list(duplicate_hashes.items())[:100],
    "non_colorable_candidates": candidates[:100],
    "non_colorable_candidate_count": len(candidates),
    "timeout_rows": timeout_rows[:100],
    "timeout_row_count": len(timeout_rows),
    "first_index_seen": min(indices) if indices else None,
    "last_index_seen": max(indices) if indices else None,
}
print(json.dumps(summary, sort_keys=True))
print("PER_CHUNK_FIRST_LAST")
print(json.dumps(per_chunk, separators=(",", ":")))
PY
REMOTE
