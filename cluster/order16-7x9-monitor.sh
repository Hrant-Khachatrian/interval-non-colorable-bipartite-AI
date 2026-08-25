#!/usr/bin/env bash
set -euo pipefail

interval=${1:-300}
checks=${2:-1}
file=/mnt/weka/hrant/interval-search/data/order16-7x9-d2to11.g6

for ((i = 1; i <= checks; i++)); do
  ssh -o BatchMode=yes hrant@cluster.ysu.am bash -s <<REMOTE
sacct -j 228779 --noheader --format=JobID,State,Elapsed,ExitCode -P | head -n 1
if [ -f "$file" ]; then
  stat -c 'bytes=%s mtime=%Y' "$file"
else
  echo 'missing'
fi
REMOTE
  ((i == checks)) || sleep "$interval"
done
