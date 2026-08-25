#!/usr/bin/env bash
set -euo pipefail

for shard_id in 307 1536 2764; do
  printf -v shard '%04d' "$shard_id"
  ssh -o BatchMode=yes hrant@cluster.ysu.am "sbatch --job-name=o16-79-p${shard} \
    --partition=research_cpu --qos=researcher \
    --gres=cpuonly:4 --cpus-per-task=4 --mem=12G --time=04:00:00 \
    --export=ALL,RUN=order16-7x9-pilot-shard${shard},N1=7,N2=9,MINEDGES=18,MAXEDGES=63,TOTAL=3604370591,SHARD_ID=${shard_id},LIMIT=2000,TIME_LIMIT=10 \
    /mnt/weka/hrant/interval-search/cluster/search_class_shard.sh"
done
