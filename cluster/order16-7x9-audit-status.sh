#!/usr/bin/env bash
set -euo pipefail

ssh -o BatchMode=yes hrant@cluster.ysu.am "python3 - --grace-seconds=\${1:-120}" \
  < "$(dirname "$0")/order16-7x9-audit.py"
