# Order-18 Alternate-Family Cumulative Coverage Through v3

Scope: coverage of the constructed alternate structural families, not an exhaustive order-18 graph census. Canonical SHA-256 is the cross-phase identity; phase ranks are local.

Checkpoint: **stable**. Covered stable hashes: 3231/4592; residual constructed candidates: 1361.
Unique covered hashes: 3231; duplicate hashes: 0; overlap with the completed first queue: 0.

| Phase | State | Completed | Unique hashes | Duplicates | Decision failures | Missing graph evidence |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| v1 | stable | 1200 | 1200 | 0 | 0 | 0 |
| v2-v1-residual | stable | 414 | 414 | 0 | 0 | 0 |
| v2-expanded-step1 | stable | 377 | 377 | 0 | 0 | 0 |
| v2-expanded-step2 | stable | 209 | 209 | 0 | 0 | 0 |
| v3-v2-residual | stable | 31 | 31 | 0 | 0 | 0 |
| v3-expanded-step3 | stable | 1000 | 1000 | 0 | 0 | 0 |

## Residual Accounting

The 31 candidates residual at v2's final bound are all covered by `v3-v2-residual`. At the larger v3 expanded-step3 bound, 2,361 candidates are newly available after prior solutions; 1,000 are covered and 1,361 remain unclassified at that bound.

## Integrity

No integrity mismatches were found.

Rows from a phase without matching complete report and status files are retained only as provisional and are excluded from stable coverage until the next atomic refresh.
