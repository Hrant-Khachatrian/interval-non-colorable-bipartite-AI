# Order-18 Alternate-Family Ledger

Scope: the alternate structured families through v2. This is coverage of the constructed family, not an exhaustive order-18 graph census.

Classified: 2200/2231 unique alternate candidates; residual at the final v2 bounds: 31.
Verified alternate canonical hashes: 2200; classified overlap with the completed first queue: 0.

| Phase | Completed | Unique hashes | Reconstructed | Unresolved maps | Status |
| --- | ---: | ---: | ---: | ---: | --- |
| v1 | 1200 | 1200 | 1200 | 0 | {'colorable': 1200} |
| v2-v1-residual | 414 | 414 | 414 | 0 | {'colorable': 414} |
| v2-expanded-step1 | 377 | 377 | 377 | 0 | {'colorable': 377} |
| v2-expanded-step2 | 209 | 209 | 209 | 0 | {'colorable': 209} |

## Integrity

All serialized/reconstructed candidates satisfy the required graph filters; every recorded canonical hash matches reconstruction; phase-local ranks and solver decisions are consistent.

## Rank Handling

Candidate IDs and ranks restart in each phase. The ledger does not infer a portable global rank from a regenerated ordering. Each retained mapping is `(phase, event_rank) -> canonical_sha256`, verified from the serialized parent graph plus the recorded surgery metadata when reconstruction succeeds.
