#!/usr/bin/env bash
set -euo pipefail

ssh -o BatchMode=yes hrant@cluster.ysu.am 'bash -s' <<'REMOTE'
cd /mnt/weka/hrant/interval-search
sed -n '1,300p' src/search_small_bipartite.py
REMOTE
