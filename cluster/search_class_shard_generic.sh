#!/bin/bash
set -euo pipefail

: "${RUN:?Set RUN to a unique result name}"
: "${N1:?Set N1}"
: "${N2:?Set N2}"
: "${MINEDGES:?Set MINEDGES}"
: "${MAXEDGES:?Set MAXEDGES}"
: "${TOTAL:?Set TOTAL to the verified generated record count}"
: "${SHARD_ID:?Set SHARD_ID to the zero-based shard number}"
: "${SHARD_SIZE:?Set SHARD_SIZE to the verified records per full shard}"
: "${SHARD_DIR:?Set SHARD_DIR to the directory of physical shards}"
: "${TIME_LIMIT:=10}"
: "${SHARD_DIGITS:=5}"

start=$((SHARD_ID * SHARD_SIZE))
stop=$((start + SHARD_SIZE))
if (( stop > TOTAL )); then
  stop=$TOTAL
fi
expected_lines=$((stop - start))
if (( expected_lines <= 0 )); then
  echo "empty shard; exiting cleanly"
  exit 0
fi

cd /mnt/weka/hrant/interval-search
printf -v shard '%.*d' "$SHARD_DIGITS" "$SHARD_ID"
input="$SHARD_DIR/chunk-$shard.g6"
output="results/${RUN}/chunk-${shard}.jsonl"
[[ -f "$input" ]]

actual_lines=$(wc -l < "$input")
if (( actual_lines != expected_lines )); then
  echo "invalid shard line count: $input has $actual_lines, expected $expected_lines" >&2
  exit 10
fi

mkdir -p "results/${RUN}"
export PYTHONPATH=/mnt/weka/hrant/interval-search/pydeps
export OMP_NUM_THREADS=4

# A timeout rerun removes only unresolved rows. Successful indices remain done.
if [[ -f "$output" ]] && grep -q '"status": "timeout"' "$output"; then
  if (( TIME_LIMIT != 3600 )); then
    echo "timeout rows require TIME_LIMIT=3600: $output" >&2
    exit 11
  fi
  filtered="${output}.filtered.$$"
  grep -v '"status": "timeout"' "$output" > "$filtered"
  mv "$filtered" "$output"
fi

.venv/bin/python src/search_small_bipartite.py "$N1" "$N2" \
  --min-edges "$MINEDGES" --max-edges "$MAXEDGES" \
  --workers 4 --time-limit "$TIME_LIMIT" \
  --input "$input" \
  --index-offset "$start" \
  --output "$output"
