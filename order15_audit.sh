#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

ssh -o BatchMode=yes hrant@cluster.ysu.am 'bash -s' <<'REMOTE'
set -euo pipefail
cd /mnt/weka/hrant/interval-search

tmpdir=$(mktemp -d /tmp/order15-audit.XXXXXX)
trap 'rm -rf "$tmpdir"' EXIT

python=.venv/bin/python
patterns=(
  'chunk-[0-9].jsonl'
  'chunk-[1-9][0-9].jsonl'
  'chunk-1[0-9][0-9].jsonl'
  'chunk-2[0-9][0-9].jsonl'
  'chunk-3[0-9][0-9].jsonl'
  'chunk-4[0-9][0-9].jsonl'
  'chunk-50[0-9].jsonl'
  'chunk-51[0-1].jsonl'
)

rows=0
files=0
duplicates=0
unique_indices=0
declare -A statuses=()

for pattern in "${patterns[@]}"; do
  output=$(PYTHONPATH=pydeps "$python" -u src/stream_audit_search.py "results/order15-6x9/$pattern")
  pass_files=$(awk '$1 == "files" {print $2}' <<<"$output")
  pass_rows=$(awk '$1 == "rows" {print $2}' <<<"$output")
  pass_unique_indices=$(awk '$1 == "unique_indices" {print $2}' <<<"$output")
  pass_duplicates=$(awk '$1 == "duplicate_indices" {print $2}' <<<"$output")
  status_json=$(awk '$1 == "statuses" {sub(/^[^ ]+ /, ""); print}' <<<"$output")

  files=$((files + pass_files))
  rows=$((rows + pass_rows))
  unique_indices=$((unique_indices + pass_unique_indices))
  duplicates=$((duplicates + pass_duplicates))

  while read -r status count; do
    statuses[$status]=$(( ${statuses[$status]:-0} + count ))
  done < <("$python" -c 'import json,sys
for key, value in json.loads(sys.stdin.read()).items():
    print(key, str(value))' <<<"$status_json")

  echo "PASS pattern=$pattern files=$pass_files rows=$pass_rows duplicates=$pass_duplicates"
done

if (( unique_indices != rows || duplicates != 0 )); then
  echo "INDEX_FAILURE rows=$rows unique_indices=$unique_indices duplicates=$duplicates"
  exit 2
fi

for pattern in "${patterns[@]}"; do
  "$python" - "$pattern" "$tmpdir" <<'PY'
import glob
import json
import sys

pattern, tmpdir = sys.argv[1:]
with open(f"{tmpdir}/hashes-{pattern}.txt", "w") as out:
    for path in glob.glob(f"results/order15-6x9/{pattern}"):
        with open(path) as handle:
            for line in handle:
                out.write(json.loads(line)["canonical_sha256"] + "\n")
PY
done

unique_canonical=$(cat "$tmpdir"/hashes-*.txt | LC_ALL=C sort -u | wc -l)
status_json=$("$python" -c 'import json,sys
pairs=[]
for arg in sys.argv[1:]:
    key, value = arg.split("=", 1)
    pairs.append((key, int(value)))
print(json.dumps(dict(pairs), sort_keys=True))' "${statuses[@]}")

cat <<RESULTS
AUDIT files=$files
AUDIT rows=$rows
AUDIT unique_indices=$unique_indices
AUDIT duplicate_indices=$duplicates
AUDIT unique_canonical=$unique_canonical
AUDIT statuses=$status_json
RESULTS
REMOTE
