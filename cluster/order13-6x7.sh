#!/bin/bash
# Slurm array runner for one canonical graph6 input.
# Submit from /mnt/weka/hrant/interval-search with: sbatch cluster/order13-6x7.sh
#SBATCH --partition=research_cpu
#SBATCH --gres=cpuonly:4
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --array=0-127%128
#SBATCH --output=logs/interval-13-6x7-%A_%a.out
#SBATCH --error=logs/interval-13-6x7-%A_%a.err

cd /mnt/weka/hrant/interval-search || exit 1
export PYTHONPATH=/mnt/weka/hrant/interval-search/pydeps
export OMP_NUM_THREADS=4

mkdir -p results/order13-6x7
.venv/bin/python src/search_small_bipartite.py 6 7 \
  --min-edges 14 --max-edges 42 \
  --res "$SLURM_ARRAY_TASK_ID" --mod 128 \
  --workers 4 --time-limit 2 \
  --input data/order13-6x7-d2.g6 \
  --output "results/order13-6x7/slice-$SLURM_ARRAY_TASK_ID.jsonl"
