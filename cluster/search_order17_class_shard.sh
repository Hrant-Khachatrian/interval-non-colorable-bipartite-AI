#!/usr/bin/env bash
# Resume-safe classifier for one validated physical order-17 shard.
set -euo pipefail

: "${N1:?Set N1}"
: "${N2:?Set N2}"
if [[ -z ${SHARD_ID:-} ]]; then
  : "${SLURM_ARRAY_TASK_ID:?Set SHARD_ID or run inside a Slurm array}"
  SHARD_ID=$SLURM_ARRAY_TASK_ID
fi
: "${TIME_LIMIT:=10}"
: "${CONFIRM_TIME_LIMIT:=3600}"

readonly ROOT=/mnt/weka/hrant/interval-search
readonly DATASET="order17-${N1}x${N2}-d2"
readonly SHARD_DIR="$ROOT/data/${DATASET}-shards"
readonly RUN_DIR="$ROOT/results/order17-census/classification-${N1}x${N2}"
readonly MANIFEST="$SHARD_DIR/manifest.json"

read -r total shard_size shards <<<"$(python3 - "$MANIFEST" <<'PY'
import json, sys
item = json.load(open(sys.argv[1], encoding="ascii"))
print(item["records"], item["shard_size"], item["shards"])
PY
)"
if (( SHARD_ID < 0 || SHARD_ID >= shards )); then
  echo "invalid_shard_id id=$SHARD_ID shards=$shards" >&2
  exit 10
fi
printf -v shard "%0${#shards}d" "$SHARD_ID"
if (( ${#shards} < 5 )); then printf -v shard '%05d' "$SHARD_ID"; fi
input="$SHARD_DIR/chunk-${shard}.g6"
start=$((SHARD_ID * shard_size))
stop=$((start + shard_size))
if (( stop > total )); then stop=$total; fi
expected=$((stop - start))
if (( $(wc -l < "$input") != expected )); then
  echo "input_line_count_mismatch shard=$SHARD_ID" >&2
  exit 11
fi

mkdir -p "$RUN_DIR"
output="$RUN_DIR/chunk-${shard}.jsonl"
if [[ -f "$output" ]] && grep -q '"status": "timeout"' "$output"; then
  if (( TIME_LIMIT != 3600 )); then
    echo "timeouts_require_TIME_LIMIT_3600 shard=$SHARD_ID" >&2
    exit 12
  fi
  grep -v '"status": "timeout"' "$output" > "${output}.filtered.$$"
  mv "${output}.filtered.$$" "$output"
fi

cd "$ROOT"
export PYTHONPATH="$ROOT/pydeps"
export OMP_NUM_THREADS=4
.venv/bin/python src/search_small_bipartite.py "$N1" "$N2" \
  --min-edges "$((2 * N2))" --max-edges "$((N1 * N2))" \
  --workers 4 --time-limit "$TIME_LIMIT" --input "$input" \
  --index-offset "$start" --output "$output"

# A primary negative is only provisional. This writes either an independently
# fixed-span-confirmed result or an unresolved record.
.venv/bin/python src/confirm_primary_negative.py \
  --primary-jsonl "$output" --hits-dir "$RUN_DIR/hits" \
  --output "$RUN_DIR/confirmations/chunk-${shard}.json" \
  --time-limit "$CONFIRM_TIME_LIMIT" --workers 4
