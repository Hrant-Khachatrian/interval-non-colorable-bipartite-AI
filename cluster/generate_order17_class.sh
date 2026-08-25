#!/usr/bin/env bash
# Generate one complete order-17 side class and publish it only after validation.
set -euo pipefail

: "${N1:?Set the smaller bipartition size N1}"
: "${N2:?Set the larger bipartition size N2}"

readonly ROOT=/mnt/weka/hrant/interval-search
readonly GENERATOR="$ROOT/bin/nauty-genbg"
readonly RESULTS="$ROOT/results/order17-census"
readonly DATASET="order17-${N1}x${N2}-d2"
readonly OUTPUT="$ROOT/data/${DATASET}.g6"
readonly MANIFEST="$RESULTS/generation-${N1}x${N2}.json"
readonly RECORD_WIDTH=25
readonly JOB_TAG="${SLURM_JOB_ID:-manual}"
readonly TEMPORARY="${OUTPUT}.partial-${JOB_TAG}"

if (( N1 < 2 || N1 > N2 || N1 + N2 != 17 )); then
  echo "invalid_order17_split n1=$N1 n2=$N2" >&2
  exit 10
fi
if [[ ! -x "$GENERATOR" ]]; then
  echo "missing_generator path=$GENERATOR" >&2
  exit 11
fi
if [[ -e "$OUTPUT" || -e "$MANIFEST" || -e "$TEMPORARY" ]]; then
  echo "refusing_existing_artifact output=$OUTPUT manifest=$MANIFEST temporary=$TEMPORARY" >&2
  exit 12
fi

mkdir -p "$RESULTS"
mine=$((2 * N2))
maxe=$((N1 * N2))
export LD_LIBRARY_PATH="$ROOT/bin${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# This is intentionally not a pipe: completion of the generator is a required
# condition before any count is considered authoritative.
"$GENERATOR" -q -c -g -l -d2:2 -D"${N2}:${N1}" "$N1" "$N2" \
  "${mine}:${maxe}" > "$TEMPORARY"

size=$(stat -c '%s' "$TEMPORARY")
if (( size <= 0 || size % RECORD_WIDTH != 0 )); then
  echo "invalid_output_width bytes=$size record_width=$RECORD_WIDTH" >&2
  exit 13
fi
records_from_bytes=$((size / RECORD_WIDTH))
line_count=$(wc -l < "$TEMPORARY")
if (( line_count != records_from_bytes )); then
  echo "line_count_disagrees lines=$line_count records_from_bytes=$records_from_bytes" >&2
  exit 14
fi

read -r sampled bad <<<"$(python3 - "$TEMPORARY" "$RECORD_WIDTH" <<'PY'
import os
import random
import sys

path, width = sys.argv[1], int(sys.argv[2])
count = os.path.getsize(path) // width
positions = sorted({0, count - 1, *(random.Random(1700).randrange(count) for _ in range(min(255, count)) )})
bad = 0
with open(path, "rb") as handle:
    for position in positions:
        handle.seek(position * width)
        record = handle.read(width)
        if len(record) != width or record[0:1] != b"P" or record[-1:] != b"\n":
            bad += 1
print(len(positions), bad)
PY
)"
if (( bad != 0 )); then
  echo "graph6_sample_validation_failed sampled=$sampled bad=$bad" >&2
  exit 15
fi

sha256=$(sha256sum "$TEMPORARY" | awk '{print $1}')
mv "$TEMPORARY" "$OUTPUT"

export MANIFEST OUTPUT N1 N2 mine maxe RECORD_WIDTH line_count size sha256 sampled JOB_TAG
python3 - <<'PY'
import datetime
import json
import os

manifest = {
    "schema_version": 1,
    "dataset": f"order17-{os.environ['N1']}x{os.environ['N2']}-d2",
    "generator_command": [
        "nauty-genbg", "-q", "-c", "-g", "-l", "-d2:2",
        f"-D{os.environ['N2']}:{os.environ['N1']}",
        os.environ['N1'], os.environ['N2'],
        f"{os.environ['mine']}:{os.environ['maxe']}",
    ],
    "degree_ranges": {
        "first_part": [2, int(os.environ['N2'])],
        "second_part": [2, int(os.environ['N1'])],
    },
    "edge_range": [int(os.environ['mine']), int(os.environ['maxe'])],
    "path": os.environ['OUTPUT'],
    "records": int(os.environ['line_count']),
    "bytes": int(os.environ['size']),
    "record_width_bytes": int(os.environ['RECORD_WIDTH']),
    "line_count_method": "independent wc -l cross-checked against byte_count / fixed_record_width",
    "sha256": os.environ['sha256'],
    "width_validation": {"sampled_records": int(os.environ['sampled']), "bad_records": 0},
    "slurm_generation_job_id": os.environ['JOB_TAG'],
    "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
temporary = os.environ['MANIFEST'] + ".tmp"
with open(temporary, "w", encoding="ascii") as handle:
    json.dump(manifest, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.replace(temporary, os.environ['MANIFEST'])
PY

echo "generation_complete dataset=$DATASET records=$line_count sha256=$sha256"
