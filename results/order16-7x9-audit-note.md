# Order16 7+9 audit and throughput note

Updated: 2026-08-25T06:18:15Z. Worker scope is validation, monitoring, and planning only; root owns array 228989.

## Source integrity

- Authoritative generation output: `data/order16-7x9-d2to11.g6`.
- Records: **3,604,370,591**.
- SHA256: `902e940b2af30043d2a757e2825c0b274a2dd769465313eba533e66fe2dc5270`.
- Two independent full line-count passes agreed. Generation job 228779 completed in 02:38:48 with exit code 0.

## Exact 8,192-chunk partition

`ceil(3,604,370,591 / 8,192) = 439,987`.

- Chunk 0: indices 0 through 439,986.
- Chunk 8191: indices 3,603,933,517 through 3,604,370,590.
- Final chunk contains 437,074 records.
- Covered records: **3,604,370,591**.
- Result: exact, contiguous, no overlap and no gap.

## Observed queue

At 06:12:18 UTC:

- Array 228989 (`order16-7x9`, batch 0–999): 1,000 queued elements, 0 running.
- Array 228788 (`order16-6x10`) was observed as 149 running and 177 pending raw elements. Root also reported a 333 running / 334 pending operational count; both are retained below.

Using a conservative 150-slot homogeneous model, batch 0 cannot start until enough existing tasks drain. Estimated delay before first start:

| Density factor | Observed snapshot | Root-reported count |
|---:|---:|---:|
| 1.00x | 3.9 h | 11.4 h |
| 1.25x | 4.8 h | 14.2 h |
| 1.50x conservative | 5.8 h | 17.1 h |
| 2.00x high uncertainty | 7.8 h | 22.8 h |

Estimated batch-0 completion including run time is 27.1–54.0 hours under the observed snapshot and 34.6–69.1 hours under the larger root-reported backlog.

## Throughput evidence

From job 228788, 187 completed elements were visible. Twelve evenly spaced completed chunks were sampled:

- Mean elapsed: 4.356 h; maximum sampled: 5.374 h.
- Mean rate: **133,052.9 rows/task-hour**; median 137,270.
- Mean JSONL size: **244.43 bytes/record**.
- Every sampled chunk had exactly 570,153 expected rows for its 6+10 boundary.

For one 439,987-record 7+9 chunk:

| Density factor | Runtime estimate |
|---:|---:|
| 1.00x direct baseline | 3.31 h |
| 1.25x | 4.13 h |
| 1.50x conservative | 4.96 h |
| 2.00x high uncertainty | 6.61 h |

The 24-hour walltime remains appropriate because density and solver behavior near the dense end are not yet directly measured.

## Storage and I/O

- Projected JSONL output for all records at 244.43 B/record: **820.51 GiB (0.80 TiB)**.
- Recommended reserve including negative-hit artifacts and rerun appendices: **1,026 GiB**.
- Filesystem free space was about **28 TiB** at assessment; storage is feasible.
- Because the planned jobs scan the contiguous source, all 8,192 tasks collectively read an estimated **295.37 TiB** while skipping prior records.
- Late 150-task stages add substantial aggregate scans: chunks 6000–6149 read roughly 8.02 TiB of skipped prefix, and chunks 7500–7649 roughly 10.00 TiB. At sustained shared throughput of 0.25–1 TiB/h this is 8–40 hours of aggregate service per stage; cache hits can reduce it, but contention can increase it.

## Feasibility for chunks 1000-8191

There are 7,192 remaining chunks after batch 0. At a 150-task throttle, serial-equivalent wall estimates are:

| Density factor | Chunks 1000-8191 wall time |
|---:|---:|
| 1.00x baseline | 158.55 h (6.61 d) |
| 1.25x | 198.19 h (8.26 d) |
| 1.50x conservative | 237.83 h (9.91 d) |
| 2.00x high uncertainty | 317.11 h (13.21 d) |

This excludes queue delay and assumes continuous access to 150 task slots.

## Conservative later-batch plan

Root owns submission. Keep 228989 intact and do not submit overlapping work.

1. Preserve exact boundaries: fixed size 439,987, final chunk 437,074.
2. After batch 0, submit chunks in stages of at most 1,000 with `--array=FIRST-LAST%150`.
3. Proposed boundaries: 1000–1999, 2000–2999, 3000–3999, 4000–4999, 5000–5999, 6000–6999, 7000–7999, then 8000–8191.
4. Keep `TIME_LIMIT=10`, memory 12G, CPUs/task 4, GRES `cpuonly:4`, partition `research_cpu`, QOS `researcher`, and walltime `24:00:00`.
5. Use a Slurm dependency or explicit audit gate so each stage starts only after the previous stage's completed chunks validate.
6. Expected conservative stage wall times are 34.72 h each for seven 1,000-task stages, plus 9.92 h for the final 192-task stage, excluding queue delay and storage contention.

A small authorized pilot from late/dense regions would materially reduce the density-factor uncertainty. Do not submit it without root approval.

## Failure, timeout, and negative policy

- A timeout is unresolved, never non-colorable.
- Rerun failed or timeout indices exactly with a 3,600 s per-record limit and identical boundaries.
- Resume logic skips already resolved non-timeout indices; appended rerun rows supersede earlier timeout rows during audit.
- Audit latest row per index, require exactly one resolved row per legal index, and reject malformed/schema/out-of-range rows.
- Independently confirm every primary negative across every legal span, delta through 15. Any UNKNOWN leaves it unresolved. Only all-span infeasibility confirms non-colorability.
