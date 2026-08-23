#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

ssh -o BatchMode=yes hrant@cluster.ysu.am 'bash -s' <<'REMOTE'
set -euo pipefail
cd /mnt/weka/hrant/interval-search

tmpdir=$(mktemp -d /tmp/order15-hashes.XXXXXX)
trap 'rm -rf "$tmpdir"' EXIT

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

for pattern in "${patterns[@]}"; do
  .venv/bin/python - "$pattern" "$tmpdir" <<'PY'
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

cat "$tmpdir"/hashes-*.txt | LC_ALL=C sort -u | wc -l
REMOTE
