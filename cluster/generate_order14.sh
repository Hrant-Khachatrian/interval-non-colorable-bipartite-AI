#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p data

for sides in "2 12" "3 11" "4 10" "5 9" "6 8" "7 7"; do
  read -r n1 n2 <<<"$sides"
  minimum=$((2 * (n1 > n2 ? n1 : n2)))
  maximum=$((n1 * n2))
  output="data/order14-${n1}x${n2}-d2.g6"
  /usr/bin/nauty-genbg -q -g -c -l -d2 "$n1" "$n2" "${minimum}:${maximum}" > "$output"
done
