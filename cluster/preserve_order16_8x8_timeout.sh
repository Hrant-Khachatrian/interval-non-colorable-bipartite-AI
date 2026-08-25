#!/usr/bin/env bash
set -euo pipefail

readonly ROOT=/mnt/weka/hrant/interval-search
readonly SOURCE="$ROOT/data/order16-8x8-d2to11.g6"
readonly DESTINATION="$ROOT/data/order16-8x8-d2to11.incomplete-229085.g6"
readonly GENERATION_JOB=229085

state=$(sacct -X -j "$GENERATION_JOB" --noheader --format=State | head -n1 | tr -d ' ')
if [[ "$state" == COMPLETED ]]; then
  echo "generation_completed_no_preservation_needed state=$state"
  exit 0
fi

if [[ ! -f "$SOURCE" ]]; then
  echo "source_missing source=$SOURCE" >&2
  exit 10
fi
if [[ -e "$DESTINATION" ]]; then
  echo "destination_already_exists destination=$DESTINATION" >&2
  exit 11
fi

size=$(stat -c '%s' "$SOURCE")
mtime=$(stat -c '%Y' "$SOURCE")
mv -n "$SOURCE" "$DESTINATION"
printf 'state=%s bytes=%s mtime=%s preserved_as=%s\n' \
  "$state" "$size" "$mtime" "$DESTINATION"
