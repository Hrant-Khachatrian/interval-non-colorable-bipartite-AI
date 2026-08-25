# order16 6+10 audit note

- Scheduler evidence queried at: `2026-08-25T06:41:40.627834Z` (UTC wall clock captured immediately after sacct array-state query).
- Scheduler: COMPLETED=363, RUNNING=149; array total=512.

## Completed-only (rigorous)

- Terminal COMPLETED chunks: 363; present files=204.
- Integrity pass this run: 183; valid=24; invalid=159; backlog after batch=180.
- Validated rows in audited completed chunks: 13,683,672 / 291,917,907 (4.68751%).
- Confirmed contiguous prefix: 0 rows.
- Terminal integrity: malformed JSON=90,653,898, duplicate indices=90,653,898, duplicate hashes=0, holes inside audited completed chunks=90,653,898.
- Exact terminal timeout rows: 0; reruns submitted this pass: none required.
- Primary non-colorable candidates: 0; independently confirmed discoveries: 0.

## Live append-stable telemetry (not completion)

- Scope: last at most 8,388,608 bytes per present non-terminal chunk; running tails do not count toward completion.
- Files: scanned=149, stable=149, unstable/excluded=0; parsed bytes=1,213,160,998.
- Validated tail rows: 4,939,723; unique indices=4,939,723; duplicate index rows=0.
- Tail hashes: unique=4,939,723; duplicate hash rows=0; malformed JSON=0.
- Tail unresolved timeouts=0; candidate negatives=0 (candidates still require independent confirmation).
- Observed tail index span: 70602460 through 200704778.

- Rerun decision: no exact timeout rows; no resubmission needed.
