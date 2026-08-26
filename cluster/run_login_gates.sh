#!/usr/bin/env bash
set -euo pipefail

ROOT=/mnt/weka/hrant/interval-search
export PATH="/opt/slurm/bin:${PATH}"
cd "$ROOT"

if [[ -z "${1:-}" ]]; then
  nohup nice -n 10 .venv/bin/python results/order16-6x10/.audit/incremental-pass-229248.py \
    > results/order16-6x10/.audit/incremental-pass-login.log 2>&1 &
  echo "incremental_pid=$!"

  nohup nice -n 10 .venv/bin/python cluster/audit_order16_6x10_strict.py \
    > results/order16-6x10/.audit/strict-120-143-login.log 2>&1 &
  echo "strict_pid=$!"

  nohup nice -n 5 bin/finalize_order16_8x8.sh \
    > results/generation-logs/o16-8x8-finalize-login.out \
    2> results/generation-logs/o16-8x8-finalize-login.err &
  echo "finalize_pid=$!"
fi

if [[ "${1:-}" == "restart-strict" ]]; then
  pkill -f 'python cluster/audit_order16_6x10_strict.py' || true
  nohup nice -n 10 .venv/bin/python cluster/audit_order16_6x10_strict.py \
    > results/order16-6x10/.audit/strict-120-143-login2.log 2>&1 &
  echo "strict_restart_pid=$!"
fi

if [[ "${1:-}" == "restart-incremental" ]]; then
  pkill -f 'python results/order16-6x10/.audit/incremental-pass-229248.py' || true
  nohup nice -n 10 .venv/bin/python results/order16-6x10/.audit/incremental-pass-229248.py \
    > results/order16-6x10/.audit/incremental-pass-login2.log 2>&1 &
  echo "incremental_restart_pid=$!"
fi

if [[ "${1:-}" == "restart-finalize" ]]; then
  pkill -f 'bash bin/finalize_order16_8x8.sh' || true
  nohup nice -n 5 bin/finalize_order16_8x8.sh \
    > results/generation-logs/o16-8x8-finalize-login2.out \
    2> results/generation-logs/o16-8x8-finalize-login2.err &
  echo "finalize_restart_pid=$!"
fi
