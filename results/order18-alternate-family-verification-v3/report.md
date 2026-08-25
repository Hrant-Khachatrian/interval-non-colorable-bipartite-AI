# Order-18 Alternate-Family Verification Through v3

Checkpoint: **stable**. This reconciliation reads compact ledger and replay summaries only; it does not rerun classification or modify event logs.

Authoritative verified count: **3231** canonical graphs.
Remaining unverified rows at the v3 expanded-step3 bound: **1361**.
Integrity verdict: **verified**.

| Full replay phase | Source | Replayed | Span-valid | Mismatch | Timeout |
| --- | ---: | ---: | ---: | ---: | ---: |
| v1 | 1200 | 1200 | 1200 | 0 | 0 |
| v2 | 1000 | 1000 | 1000 | 0 | 0 |
| v3 | 1031 | 1031 | 1031 | 0 | 0 |

| Source campaign phase | Source | Replayed | Span-valid | Mismatch | Timeout | Missing evidence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| v1 | 1200 | 1200 | 1200 | 0 | 0 | 0 |
| v2-v1-residual | 414 | 414 | 414 | 0 | 0 | 0 |
| v2-expanded-step1 | 377 | 377 | 377 | 0 | 0 | 0 |
| v2-expanded-step2 | 209 | 209 | 209 | 0 | 0 | 0 |
| v3-v2-residual | 31 | 31 | 31 | 0 | 0 | 0 |
| v3-expanded-step3 | 1000 | 1000 | 1000 | 0 | 0 | 0 |

## Identity And Residual Accounting

The verification set has 3231 globally unique canonical hashes; duplicate hashes: 0; overlap with the completed first queue: 0.
At v2's final bound, 31 rows remained. v3 resolved 31 of those, then classified 1000 of 2361 newly unique expanded-step3 rows, leaving 1361 unverified.

Decision disagreements: 0; missing evidence: 0; replay mismatches: 0; timeouts: 0.
