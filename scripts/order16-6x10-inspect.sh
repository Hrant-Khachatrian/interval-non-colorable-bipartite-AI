#!/usr/bin/env bash
set -euo pipefail

ssh -o BatchMode=yes hrant@cluster.ysu.am 'bash -s' <<'REMOTE'
cd /mnt/weka/hrant/interval-search
echo '---FILES---'
ls -l src/rerun_graph6_index.py
echo '---CHUNK_NAMES---'
find results/order16-6x10 -maxdepth 1 -type f -name 'chunk-*.jsonl' -printf '%f %s bytes\n' | sort -V | head -5
find results/order16-6x10 -maxdepth 1 -type f -name 'chunk-*.jsonl' -printf '%f %s bytes\n' | sort -V | tail -5
find . -maxdepth 2 -type f \( -name '*228788*.sbatch' -o -name '*order16*6x10*' \) -printf '%p %s bytes\n' | sort | head -100
echo '---RERUN_USAGE---'
sed -n '1,240p' src/rerun_graph6_index.py
echo '---SAMPLE_HEAD---'
first=$(find results/order16-6x10 -maxdepth 1 -type f -name 'chunk-*.jsonl' | sort -V | head -1)
last=$(find results/order16-6x10 -maxdepth 1 -type f -name 'chunk-*.jsonl' | sort -V | tail -1)
head -3 "$first"
echo '---SAMPLE_TAIL---'
tail -3 "$last"
echo '---LOGS---'
find . -maxdepth 3 -type f -name '*228788_0.out' -o -name '*228788_119.out' | head -20
REMOTE
