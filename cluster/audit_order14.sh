#!/bin/bash
set -euo pipefail

cd /mnt/weka/hrant/interval-search

for dir in order14-2x12 order14-3x11 order14-4x10 order14-5x9 order14-6x8; do
  pattern="results/${dir}/slice-*.jsonl"
  output="results/${dir}-audit.txt"
  PYTHONPATH=pydeps .venv/bin/python src/stream_audit_search.py "$pattern" > "$output"
done
