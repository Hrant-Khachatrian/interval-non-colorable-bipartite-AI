#!/bin/bash
# Parameterized Slurm array runner.
# Example:
#   sbatch --array=0-127%128 --export=ALL,RUN=order13-5x8,N1=5,N2=8,MINEDGES=16,MAXEDGES=40,INPUT=data/order13-5x8-d2.g6 cluster/search_class.sh
#SBATCH --partition=research_cpu
#SBATCH --gres=cpuonly:4
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --output=logs/search-%x-%A_%a.out
#SBATCH --error=logs/search-%x-%A_%a.err

set -euo pipefail

: "${RUN:?Set RUN to a unique result name}"
: "${N1:?Set N1}"
: "${N2:?Set N2}"
: "${MINEDGES:?Set MINEDGES}"
: "${MAXEDGES:?Set MAXEDGES}"
: "${INPUT:?Set INPUT to the canonical graph6 file}"
: "${SLICES:=128}"

if (( SLURM_ARRAY_TASK_ID >= SLICES )); then
  echo "task id exceeds SLICES; exiting cleanly"
  exit 0
fi

cd /mnt/weka/hrant/interval-search || exit 1
export PYTHONPATH=/mnt/weka/hrant/interval-search/pydeps
export OMP_NUM_THREADS=4

mkdir -p "results/${RUN}"
.venv/bin/python src/search_small_bipartite.py "$N1" "$N2" \
  --min-edges "$MINEDGES" --max-edges "$MAXEDGES" \
  --res "$SLURM_ARRAY_TASK_ID" --mod "$SLICES" \
  --workers 4 --time-limit 2 \
  --input "$INPUT" \
  --output "results/${RUN}/slice-${SLURM_ARRAY_TASK_ID}.jsonl"
