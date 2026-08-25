#!/usr/bin/env bash
set -euo pipefail

ssh -o BatchMode=yes hrant@cluster.ysu.am 'bash -s' <<'REMOTE'
cd /mnt/weka/hrant/interval-search
echo '---REFERENCES---'
if command -v rg >/dev/null 2>&1; then
  rg -n --hidden --glob '!Inkling/**' --glob '!data/**' --glob '!results/**' 'rerun_graph6_index|order16-6x10|228788' . || true
else
  grep -RInE --exclude-dir=Inkling --exclude-dir=data --exclude-dir=results 'rerun_graph6_index|order16-6x10|228788' . || true
fi
echo '---TOP_LEVEL---'
find . -maxdepth 2 -type f -not -path './data/*' -not -path './results/*' -not -path './Inkling/*' -printf '%p %s bytes\n' | sort | head -200
echo '---LOG_TAILS---'
for id in 0 1 118 119; do
  echo "[$id]"
  tail -8 "./slurm-228788_${id}.out" 2>/dev/null || true
done
REMOTE
