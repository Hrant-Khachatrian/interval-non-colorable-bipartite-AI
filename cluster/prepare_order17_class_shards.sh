#!/usr/bin/env bash
# Make validated physical graph6 shards after a validated order-17 generation.
set -euo pipefail

: "${N1:?Set N1}"
: "${N2:?Set N2}"
: "${SHARD_SIZE:=10000}"

readonly ROOT=/mnt/weka/hrant/interval-search
readonly DATASET="order17-${N1}x${N2}-d2"
readonly SOURCE="$ROOT/data/${DATASET}.g6"
readonly GENERATION_MANIFEST="$ROOT/results/order17-census/generation-${N1}x${N2}.json"
readonly SHARD_DIR="$ROOT/data/${DATASET}-shards"

if (( SHARD_SIZE <= 0 )); then
  echo "invalid_shard_size value=$SHARD_SIZE" >&2
  exit 10
fi
read -r total source_sha width <<<"$(python3 - "$GENERATION_MANIFEST" <<'PY'
import json, sys
item = json.load(open(sys.argv[1], encoding="ascii"))
print(item["records"], item["sha256"], item["record_width_bytes"])
PY
)"
if [[ $(sha256sum "$SOURCE" | awk '{print $1}') != "$source_sha" ]]; then
  echo "source_sha256_mismatch" >&2
  exit 11
fi
if [[ -e "$SHARD_DIR/.complete" ]]; then
  echo "shards_already_complete directory=$SHARD_DIR"
  exit 0
fi
if [[ -e "$SHARD_DIR" ]] && [[ -n $(find "$SHARD_DIR" -maxdepth 1 -type f -print -quit) ]]; then
  echo "refusing_nonempty_shard_directory directory=$SHARD_DIR" >&2
  exit 12
fi

shards=$(((total + SHARD_SIZE - 1) / SHARD_SIZE))
digits=${#shards}
if (( digits < 5 )); then digits=5; fi
mkdir -p "$SHARD_DIR"
split -l "$SHARD_SIZE" -d -a "$digits" --additional-suffix=.g6 "$SOURCE" "$SHARD_DIR/chunk-"
wc -l "$SHARD_DIR"/chunk-*.g6 > "$SHARD_DIR/manifest.wc"
read -r files lines <<<"$(awk '$2 != "total" {n++; s += $1} END {print n, s}' "$SHARD_DIR/manifest.wc")"
if (( files != shards || lines != total )); then
  echo "shard_count_mismatch files=$files/$shards lines=$lines/$total" >&2
  exit 13
fi
bad_width=$(find "$SHARD_DIR" -maxdepth 1 -name 'chunk-*.g6' -type f -printf '%s\n' | awk -v width="$width" '$1 % width {bad++} END {print bad + 0}')
if (( bad_width != 0 )); then
  echo "shard_width_mismatch bad_files=$bad_width" >&2
  exit 14
fi

export SHARD_DIR SOURCE source_sha total SHARD_SIZE shards width
python3 - <<'PY'
import datetime
import json
import os

item = {
    "schema_version": 1,
    "source": os.environ["SOURCE"],
    "source_sha256": os.environ["source_sha"],
    "records": int(os.environ["total"]),
    "record_width_bytes": int(os.environ["width"]),
    "shard_directory": os.environ["SHARD_DIR"],
    "shard_size": int(os.environ["SHARD_SIZE"]),
    "shards": int(os.environ["shards"]),
    "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
path = os.path.join(os.environ["SHARD_DIR"], "manifest.json")
with open(path + ".tmp", "w", encoding="ascii") as handle:
    json.dump(item, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.replace(path + ".tmp", path)
PY
touch "$SHARD_DIR/.complete"
echo "shards_complete dataset=$DATASET shards=$shards records=$total"
