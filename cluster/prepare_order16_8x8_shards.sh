#!/usr/bin/env bash
set -euo pipefail

: "${SHARD_SIZE:=450000}"

readonly ROOT=/mnt/weka/hrant/interval-search
readonly SOURCE="$ROOT/data/order16-8x8-d2to11.g6"
readonly MANIFEST="$ROOT/results/order16-8x8/generation-manifest.json"
readonly MARKER="$ROOT/results/order16-8x8/.generation-complete"
readonly SHARD_DIR="$ROOT/data/order16-8x8-d2to11-shards"

if [[ ! -f "$MANIFEST" || ! -f "$MARKER" ]]; then
  echo "generation_is_not_finalized" >&2
  exit 10
fi

read -r total sha256 <<<"$(python3 - "$MANIFEST" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as handle:
    item = json.load(handle)
print(item['records'], item['sha256'])
PY
)"

if (( SHARD_SIZE <= 0 )); then
  echo "invalid_shard_size value=$SHARD_SIZE" >&2
  exit 11
fi

shards=$(((total + SHARD_SIZE - 1) / SHARD_SIZE))
digits=${#shards}
if (( digits < 5 )); then
  digits=5
fi

if [[ -f "$SHARD_DIR/.complete" ]]; then
  echo "shards_already_complete shards=$shards"
  exit 0
fi
if [[ -e "$SHARD_DIR" ]] && [[ -n "$(find "$SHARD_DIR" -maxdepth 1 -type f -print -quit)" ]]; then
  echo "refusing_nonempty_shard_directory dir=$SHARD_DIR" >&2
  exit 12
fi

source_bytes=$(stat -c '%s' "$SOURCE")
avail_bytes=$(df -B1 --output=avail "$ROOT" | tail -n1)
minimum_free=$((source_bytes * 2 + 2 * 1024 * 1024 * 1024 * 1024))
if (( avail_bytes < minimum_free )); then
  echo "insufficient_storage avail=$avail_bytes minimum=$minimum_free" >&2
  exit 13
fi

mkdir -p "$SHARD_DIR"
split -l "$SHARD_SIZE" -d -a "$digits" --additional-suffix=.g6 \
  "$SOURCE" "$SHARD_DIR/chunk-"

wc -l "$SHARD_DIR"/chunk-*.g6 > "$SHARD_DIR/manifest.wc"
read -r actual_files actual_total <<<"$(awk '$2 != "total" {n++; s += $1} END {print n, s}' "$SHARD_DIR/manifest.wc")"
if [[ "$actual_files" != "$shards" || "$actual_total" != "$total" ]]; then
  echo "shard_validation_failed files=$actual_files/$shards lines=$actual_total/$total" >&2
  exit 14
fi

bad_width=0
while IFS= read -r shard; do
  shard_bytes=$(stat -c '%s' "$shard")
  if (( shard_bytes % 22 != 0 )); then
    bad_width=$((bad_width + 1))
  fi
done < <(find "$SHARD_DIR" -maxdepth 1 -type f -name 'chunk-*.g6' | sort)
if (( bad_width != 0 )); then
  echo "invalid_record_width_in_shards bad=$bad_width" >&2
  exit 15
fi

export SHARD_DIR SHARD_SIZE total sha256 actual_files actual_total source_bytes SOURCE
python3 - <<'PY'
import datetime, json, os

manifest = {
    "schema_version": 1,
    "source": os.environ["SOURCE"] if "SOURCE" in os.environ else "/mnt/weka/hrant/interval-search/data/order16-8x8-d2to11.g6",
    "source_sha256": os.environ["sha256"],
    "records": int(os.environ["total"]),
    "record_width_bytes": 22,
    "shard_directory": os.environ["SHARD_DIR"],
    "shard_size": int(os.environ["SHARD_SIZE"]),
    "shards": int(os.environ["actual_files"]),
    "validated_records": int(os.environ["actual_total"]),
    "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()
}
path = os.path.join(os.environ["SHARD_DIR"], "manifest.json")
tmp = path + ".tmp"
with open(tmp, 'w', encoding='utf-8') as handle:
    json.dump(manifest, handle, indent=2, sort_keys=True)
    handle.write('\n')
os.replace(tmp, path)
PY

touch "$SHARD_DIR/.complete"
echo "shards_complete shards=$shards shard_size=$SHARD_SIZE records=$total"
