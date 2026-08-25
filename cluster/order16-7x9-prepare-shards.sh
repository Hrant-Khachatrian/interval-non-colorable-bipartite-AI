#!/usr/bin/env bash
set -euo pipefail

ssh -o BatchMode=yes hrant@cluster.ysu.am bash -s <<'REMOTE'
set -euo pipefail
cd /mnt/weka/hrant/interval-search
source=data/order16-7x9-d2to11.g6
shard_dir=data/order16-7x9-d2to11-shards
manifest=$shard_dir/manifest.wc
complete=$shard_dir/.complete
expected_count=3604370591
expected_files=3072

if [ -f "$complete" ]; then
  echo "shards_already_complete"
  exit 0
fi

files=$(find "$shard_dir" -maxdepth 1 -type f | wc -l)
if [ "$files" -ne 0 ]; then
  echo "refusing_nonempty_shard_directory_without_complete_marker" >&2
  exit 1
fi

split -l 1173298 -d -a 4 --additional-suffix=.g6 "$source" "$shard_dir/chunk-"
wc -l "$shard_dir"/chunk-*.g6 > "$manifest"

validation=$(awk '$2 != "total" {n++; s += $1} END {print n, s}' "$manifest")
read -r actual_files actual_count <<<"$validation"
if [ "$actual_files" -ne "$expected_files" ] || [ "$actual_count" -ne "$expected_count" ]; then
  echo "shard_validation_failed files=$actual_files lines=$actual_count" >&2
  exit 1
fi

touch "$complete"
echo "shards_complete files=$actual_files lines=$actual_count"
REMOTE
