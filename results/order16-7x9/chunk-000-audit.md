# Order16 7+9 chunk 000 audit

Snapshot at 2026-08-25T09:58:10Z: Slurm array `228989` element `0` (job `229469`) is running.  It owns input indices `0` through `439986`, exactly 439,987 records, with output at `/mnt/weka/hrant/interval-search/results/order16-7x9/chunk-0.jsonl` and log at `/mnt/weka/hrant/interval-search/slurm-228989_0.out`.

The changing output contained 70,500 rows, all reported `colorable`; no timeout or primary negative had appeared.  Full acceptance is deferred until the job is terminal and the output is stable.  That validation will require exact coverage, unique indices and `7:9:`-prefixed canonical hashes, the declared strict row schema, plausible solver times, unresolved timeout handling, and independent all-span confirmation for every primary negative.
