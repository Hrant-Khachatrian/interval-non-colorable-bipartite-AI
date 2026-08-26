#!/usr/bin/env bash
set -euo pipefail

# Slurm clients are not on the default PATH of every compute node.
export PATH="/opt/slurm/bin:${PATH}"

readonly ROOT=/mnt/weka/hrant/interval-search
readonly SOURCE="$ROOT/data/order16-8x8-d2to11.g6"
readonly RESULTS="$ROOT/results/order16-8x8"
readonly MANIFEST="$RESULTS/generation-manifest.json"
readonly MARKER="$RESULTS/.generation-complete"
readonly GENERATION_JOB=229085
readonly RECORD_WIDTH=22

mkdir -p "$RESULTS"

state=$(sacct -X -j "$GENERATION_JOB" --noheader --format=State | head -n1 | tr -d ' ')
if [[ "$state" != COMPLETED ]]; then
  echo "generation_is_not_completed state=$state" >&2
  exit 10
fi

stat_a=$(stat -c '%s:%Y' "$SOURCE")
sleep 30
stat_b=$(stat -c '%s:%Y' "$SOURCE")
if [[ "$stat_a" != "$stat_b" ]]; then
  echo "source_is_not_stable before=$stat_a after=$stat_b" >&2
  exit 11
fi

size=${stat_a%%:*}
mtime=${stat_a##*:}
if (( size <= 0 )); then
  echo "source_is_empty size=$size" >&2
  exit 12
fi
if (( size % RECORD_WIDTH != 0 )); then
  echo "byte_count_is_not_a_whole_record size=$size remainder=$((size % RECORD_WIDTH))" >&2
  exit 13
fi

records_from_bytes=$((size / RECORD_WIDTH))
line_count=$(wc -l < "$SOURCE")
if (( line_count != records_from_bytes )); then
  echo "line_count_disagrees_with_fixed_width_records lines=$line_count records=$records_from_bytes" >&2
  exit 14
fi

width_validation=$(python3 - "$SOURCE" "$RECORD_WIDTH" <<'PY'
import os, random, sys

path, width = sys.argv[1], int(sys.argv[2])
size = os.path.getsize(path)
count = size // width
rng = random.Random(1608)
positions = [0, count - 1] + [rng.randrange(count) for _ in range(255)]
bad = []
with open(path, 'rb') as handle:
    for position in positions:
        handle.seek(position * width)
        record = handle.read(width)
        if len(record) != width or not record.startswith(b'O') or not record.endswith(b'\n'):
            bad.append(position)
print(f"{len(positions)} {len(bad)}")
PY
)
read -r sampled bad <<<"$width_validation"
if (( bad != 0 )); then
  echo "graph6_record_validation_failed bad=$bad" >&2
  exit 15
fi

sha_line=$(sha256sum "$SOURCE")
read -r sha256 _ <<<"$sha_line"

sacct_fields=$(sacct -X -j "$GENERATION_JOB" --noheader --format=Elapsed,Start,End,NodeList | head -n1)
elapsed=$(awk '{print $1}' <<<"$sacct_fields")
started=$(awk '{print $2}' <<<"$sacct_fields")
ended=$(awk '{print $3}' <<<"$sacct_fields")
node=$(awk '{print $4}' <<<"$sacct_fields")

MANIFEST_TMP="${MANIFEST}.tmp.$$"
MARKER_TMP="${MARKER}.tmp.$$"
export MANIFEST_TMP SOURCE GENERATION_JOB RECORD_WIDTH size line_count sha256 \
  elapsed started ended node state sampled bad

python3 - <<'PY'
import datetime, json, os

manifest = {
    "schema_version": 1,
    "dataset": "order16-8x8-d2to11",
    "generator_command": "nauty-genbg -q -c -g -l -d2:2 -D11:11 8 8",
    "slurm_generation_job_id": int(os.environ["GENERATION_JOB"]),
    "generation_state": os.environ["state"],
    "generation_elapsed": os.environ["elapsed"],
    "generation_start": os.environ["started"],
    "generation_end": os.environ["ended"],
    "generation_node": os.environ["node"],
    "path": os.environ["SOURCE"],
    "bytes": int(os.environ["size"]),
    "record_width_bytes": int(os.environ["RECORD_WIDTH"]),
    "records": int(os.environ["line_count"]),
    "line_count_method": "wc -l cross-checked against byte count / record width",
    "sha256_algorithm": "sha256",
    "sha256": os.environ["sha256"],
    "width_validation": {
        "sampled_records": int(os.environ["sampled"]),
        "bad_records": int(os.environ["bad"]),
        "invariant": "every record begins with O, ends with LF, and is 22 bytes"
    },
    "stale_estimate": {
        "value": 159757218,
        "status": "invalid",
        "reason": "superseded by the verified generated record count"
    },
    "prior_partial_228780": {
        "records": 4707971072,
        "bytes": 103575363584,
        "outcome": "scheduler_timeout_preserved_not_final"
    },
    "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()
}

tmp = os.environ["MANIFEST_TMP"]
with open(tmp, 'w', encoding='utf-8') as handle:
    json.dump(manifest, handle, indent=2, sort_keys=True)
    handle.write('\n')
    handle.flush()
    os.fsync(handle.fileno())
os.replace(tmp, os.environ["MANIFEST"])

marker_tmp = os.environ["MARKER_TMP"]
with open(marker_tmp, 'w', encoding='ascii') as handle:
    handle.write(os.environ["sha256"] + '\n')
    handle.flush()
    os.fsync(handle.fileno())
os.replace(marker_tmp, os.environ["MARKER"])
PY

echo "finalized bytes=$size records=$line_count sha256=$sha256"
