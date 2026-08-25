#!/bin/bash
set -euo pipefail

: "${RUN:?Set RUN to a unique result name}"
: "${N1:?Set N1}"
: "${N2:?Set N2}"
: "${MINEDGES:?Set MINEDGES}"
: "${MAXEDGES:?Set MAXEDGES}"
: "${TOTAL:?Set TOTAL to the canonical input line count}"
: "${SHARD_ID:?Set SHARD_ID to the zero-based shard number}"
: "${TIME_LIMIT:=10}"
LIMIT=${LIMIT:-0}

chunk_size=1173298
start=$((SHARD_ID * chunk_size))
stop=$((start + chunk_size))
if (( stop > TOTAL )); then
  stop=$TOTAL
fi
if (( LIMIT > 0 )); then
  stop=$((start + LIMIT))
fi
if (( start >= stop )); then
  echo "empty shard range; exiting cleanly"
  exit 0
fi

cd /mnt/weka/hrant/interval-search
input=data/order16-7x9-d2to11-shards/chunk-$(printf '%04d' "$SHARD_ID").g6
output=results/${RUN}/chunk-${SHARD_ID}.jsonl

[ -f "$input" ]
actual_lines=$(wc -l < "$input")
if [ "$actual_lines" -ne 1173298 ] && [ "$SHARD_ID" -ne 3071 ]; then
  echo "invalid full shard line count: $input has $actual_lines" >&2
  exit 1
fi

mkdir -p "results/${RUN}"
export PYTHONPATH=/mnt/weka/hrant/interval-search/pydeps
export OMP_NUM_THREADS=4

if [ -f "$output" ] && grep -q '"status": "timeout"' "$output"; then
  if [ "$TIME_LIMIT" -ne 3600 ]; then
    echo "timeout rows require TIME_LIMIT=3600: $output" >&2
    exit 2
  fi
  filtered=${output}.filtered
  grep -v '"status": "timeout"' "$output" > "$filtered"
  mv "$filtered" "$output"
fi

.venv/bin/python src/search_small_bipartite.py "$N1" "$N2" \
  --min-edges "$MINEDGES" --max-edges "$MAXEDGES" \
  --workers 4 --time-limit "$TIME_LIMIT" \
  --input "$input" \
  --start-index "$start" --stop-index "$stop" \
  --output "$output"
