#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

ssh -o BatchMode=yes hrant@cluster.ysu.am 'bash -s' <<'REMOTE'
cd /mnt/weka/hrant/interval-search

timeout=0
noncolorable=0
colorable=0

for file in results/order15-6x9/chunk-*.jsonl; do
  timeout=$((timeout + $(grep -c -F '"status": "timeout"' "$file" || true)))
  noncolorable=$((noncolorable + $(grep -c -F '"status": "non-colorable"' "$file" || true)))
  colorable=$((colorable + $(grep -c -F '"status": "colorable"' "$file" || true)))
done

printf 'timeout %d\n' "$timeout"
printf 'non-colorable %d\n' "$noncolorable"
printf 'colorable %d\n' "$colorable"
REMOTE
