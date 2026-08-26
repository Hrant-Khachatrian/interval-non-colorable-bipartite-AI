#!/usr/bin/env bash
set -euo pipefail

ROOT=/mnt/weka/hrant/interval-search

for file in \
  "$ROOT/results/order16-6x10/.audit/incremental-pass-229248.py" \
  "$ROOT/cluster/audit_order16_6x10_strict.py"; do
  python3 - "$file" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
text = text.replace('"squeue"', '"/opt/slurm/bin/squeue"')
text = text.replace('"sacct"', '"/opt/slurm/bin/sacct"')
path.write_text(text)
PY
done

chmod +x "$ROOT/cluster/audit_order16_6x10_strict.py"
python3 -m py_compile "$ROOT/results/order16-6x10/.audit/incremental-pass-229248.py" "$ROOT/cluster/audit_order16_6x10_strict.py"
grep -nE '/opt/slurm/bin/(squeue|sacct)' "$ROOT/results/order16-6x10/.audit/incremental-pass-229248.py" "$ROOT/cluster/audit_order16_6x10_strict.py"
