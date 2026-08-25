#!/usr/bin/env bash
set -u
cd /mnt/c/Users/hrant/Documents/ChatGPT/Science

status=results/order16-6x10-status.json
if [[ -f "$status" ]]; then
  echo "---LOCAL_CHECKPOINT---"
  cat "$status"
else
  echo "NO_LOCAL_CHECKPOINT"
fi

ssh -o BatchMode=yes -o ConnectTimeout=12 hrant@cluster.ysu.am 'bash -s' <<'REMOTE'
project=/mnt/weka/hrant/interval-search
cd "$project" || { echo MISSING_PROJECT; exit 0; }

echo "---REMOTE_STATE---"
printf 'project=%s\n' "$(pwd)"
dataset=data/order16-6x10-d2to11.g6
if [[ -f "$dataset" ]]; then
  printf 'dataset_bytes=%s\n' "$(stat -c %s "$dataset")"
else
  echo dataset_missing
fi

chunkdir=results/order16-6x10
mkdir -p "$chunkdir"
printf 'chunks=%s\n' "$(find "$chunkdir" -maxdepth 1 -type f -name 'chunk-*.jsonl' | wc -l)"
printf 'chunk_bytes=%s\n' "$(du -cb "$chunkdir"/chunk-*.jsonl 2>/dev/null | tail -1 | cut -f1)"

echo "---SQUEUE---"
squeue -j 228788 -h -o '%i|%T|%M|%L|%N'

echo "---SACCT_AGG---"
sacct -j 228788 --array -X -n -o State | sed 's/[[:space:]]*$//' | sort | uniq -c

echo "---NON_SUCCESS---"
sacct -j 228788 --array -X -n -o JobID%20,State%18,Elapsed%12,ExitCode%10,Reason%40 |
  awk '$2 !~ /^(COMPLETED|RUNNING|PENDING)/ {print}' | head -200

echo "---CHECKPOINT---"
if [[ -f "$chunkdir/status.json" ]]; then cat "$chunkdir/status.json"; else echo none; fi
REMOTE
