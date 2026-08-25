#!/usr/bin/env bash
set -euo pipefail

while true; do
  printf '\n---SNAPSHOT %s---\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  ssh -o BatchMode=yes hrant@cluster.ysu.am 'bash -s' <<'REMOTE'
cd /mnt/weka/hrant/interval-search
sacct -j 228788 --array -X -n -o State | sed 's/[[:space:]]*$//' | sort | uniq -c |
  awk '{print $2 "=" $1}' | paste -sd,
squeue -j 228788 -h -t R -o '%i %M %L' | head -5
printf 'chunk_files='
find results/order16-6x10 -maxdepth 1 -type f -name 'chunk-*.jsonl' | wc -l
printf 'chunk_bytes='
du -cb results/order16-6x10/chunk-*.jsonl 2>/dev/null | tail -1 | cut -f1
REMOTE
  sleep 600
done
