# order16 8+8 generation and classification plan

## Current gate

Generation job `229085` remains authoritative while it is `RUNNING`. The stale
`159,757,218` estimate is invalid and must not be used as `TOTAL`. Classification
must not be submitted until `results/order16-8x8/generation-manifest.json` and
`results/order16-8x8/.generation-complete` exist on YSU.

The graph6 record width was independently sampled: 257 records, including the
first and last complete records, all began with `O`, ended with newline, and were
22 bytes wide. This supports a **provisional** live estimate only:

```text
complete_records_in_snapshot = floor(output_bytes / 22)
provisional_upper_bound      = complete_records_in_snapshot + 1
```

At `2026-08-25T10:16:19+04:00`, output was `107,571,290,112` bytes, giving
`4,889,604,096` complete records and a provisional upper bound of
`4,889,604,097`. The writer was active, so this is not a census.

## Generation branches

Two dependent jobs are already queued and consume no compute until released:

- `229204` (`afterok:229085`) runs the exact finalizer.
- `229205` (`afternotok:229085`) preserves a failed or timed-out partial as
  `/mnt/weka/hrant/interval-search/data/order16-8x8-d2to11.incomplete-229085.g6`.

The success finalizer performs these steps before publishing anything:

1. Require scheduler state `COMPLETED`.
2. Require source size and mtime to remain unchanged for 30 seconds.
3. Require `size % 22 == 0`.
4. Run exact `wc -l` and require it to equal `size / 22`.
5. Revalidate first, last, and 255 deterministic random records for width.
6. Compute SHA256.
7. Atomically publish `generation-manifest.json` and `.generation-complete`.

It does not submit classification.

## Post-generation verification

After job `229204` succeeds, run:

```bash
ssh -o BatchMode=yes hrant@cluster.ysu.am '
  cat /mnt/weka/hrant/interval-search/results/order16-8x8/generation-manifest.json
  sacct -X -j 229085,229204 --format=JobID,JobName%24,State,Elapsed,ExitCode
'
```

Record the manifest in local status. `records`, not the stale estimate, becomes
classification `TOTAL`.

## Physical shard staging

Do not use contiguous logical chunks directly from the full stream: each distant
task would rescan all preceding records. Use fixed-width physical shards after
the finalized count is known.

A conservative target is 450,000 records per shard, close to the completed
`7+9` chunk scale. For example, if the verified total is near 4.89 billion,
`ceil(total / 450000)` yields roughly 10,900 shards. Exact values are computed
from the manifest.

Prepare and validate shards with:

```bash
ssh -o BatchMode=yes hrant@cluster.ysu.am \
  'SHARD_SIZE=450000 bash /mnt/weka/hrant/interval-search/bin/prepare_order16_8x8_shards.sh'
```

The preparation script checks free storage, requires at least the source size
plus shard duplication plus two TiB headroom, validates file count and aggregate
line count, rejects any non-22-byte shard, writes `manifest.json`, and only then
makes `.complete`. It does not delete the full source.

## Staged classification map

Use physical-shard runner `bin/search_class_shard_generic.sh` on YSU. Set:

```text
RUN=order16-8x8
N1=8
N2=8
MINEDGES=16
MAXEDGES=64
TIME_LIMIT=10
TOTAL=<verified manifest records>
SHARD_SIZE=450000
SHARD_DIR=/mnt/weka/hrant/interval-search/data/order16-8x8-d2to11-shards
partition=research_cpu
qos=researcher
gres=cpuonly:4
cpus-per-task=4
mem=12G
time=24:00:00
```

Submit batches of at most 1,000 array tasks with `%150` concurrency:

```text
batch 0: 0-999%150
batch 1: 1000-1999%150
...
last batch: <first>-<last inclusive>, capped by shards-1
```

Before each next batch, audit prior outputs for malformed rows, duplicate global
indices/hashes, missing ranges, timeouts, and negatives. A timeout is unresolved,
never negative; rerun exactly that timeout work with `TIME_LIMIT=3600`.

Any primary negative must be independently confirmed over every legal span using
the independent fixed-span SAT path. A confirmation timeout or unknown result
remains unresolved and cannot be reported as negative.
