# Order-16 class audit

`src/audit_order16_class.py` audits the `chunk-*.jsonl` files for one order-16
result class on `hrant@cluster.ysu.am`. It reads each file sequentially at a byte
snapshot boundary, emits only compact audit facts, and uses remote GNU `sort`
for canonical-hash uniqueness. Raw JSONL chunks are neither copied locally nor
held whole in memory.

## Snapshot audit

```sh
python3 src/audit_order16_class.py \
  --dataset order16-4x12 \
  --expected 26330 \
  --output results/order16-4x12-tool-audit.json
```

For the larger completed class, use a larger remote sort budget:

```sh
python3 src/audit_order16_class.py \
  --dataset order16-5x11 \
  --expected 5158975 \
  --sort-memory 512M \
  --output results/order16-5x11-tool-audit.json
```

The JSON records row and malformed-row counts, unique/repeated zero-based
indices, unique/repeated bipartition canonical hashes, status histogram,
minimum/maximum index, missing ranges inside the observed range, exact timeout
indices, negative candidates (`non-colorable`, `non_colorable`, and
`confirmed_non_colorable`), a bounded list of invalid-row examples, per-file
inventory, and comparison with `--expected`. `duplicate_index_rows` counts extra
occurrences; `duplicate_indices` counts distinct index values that occur more
than once. The same distinction applies to hashes.

## Live snapshots

Add `--live` to repeat point-in-time scans until two consecutive scans have the
same core fingerprint and no file changed during either scan:

```sh
python3 src/audit_order16_class.py \
  --dataset order16-5x11 \
  --expected 5158975 \
  --output results/order16-5x11-live.json \
  --live --poll-seconds 60 --stable-scans 2
```

Use `--deadline-seconds` to bound polling. The default SSH destination is
`hrant@cluster.ysu.am`; override it with `--remote`, and point at another
checkout with `--remote-root`. Increase `--sort-memory` if the cluster has more
memory available; temporary sorted data is created under `/tmp` and removed on
exit.
