#!/bin/bash
set -euo pipefail

: "${RUN:?Set RUN to a unique result name}"
: "${N1:?Set N1}"
: "${N2:?Set N2}"
: "${MINEDGES:?Set MINEDGES}"
: "${MAXEDGES:?Set MAXEDGES}"
: "${INPUT:?Set INPUT to the canonical graph6 file}"
: "${TOTAL:?Set TOTAL to the input line count}"
: "${CHUNKS:=256}"

chunk_size=$(((TOTAL + CHUNKS - 1) / CHUNKS))
start=$((SLURM_ARRAY_TASK_ID * chunk_size))
stop=$((start + chunk_size))
if (( stop > TOTAL )); then
  stop=$TOTAL
fi
if (( start >= stop )); then
  echo "empty chunk; exiting cleanly"
  exit 0
fi

cd /mnt/weka/hrant/interval-search
export PYTHONPATH=/mnt/weka/hrant/interval-search/pydeps
export OMP_NUM_THREADS=4

mkdir -p "results/${RUN}"
.venv/bin/python src/search_small_bipartite.py "$N1" "$N2" \
  --min-edges "$MINEDGES" --max-edges "$MAXEDGES" \
  --workers 4 --time-limit "${TIME_LIMIT:-5}" \
  --input "$INPUT" \
  --start-index "$start" --stop-index "$stop" \
  --output "results/${RUN}/chunk-${SLURM_ARRAY_TASK_ID}.jsonl"
