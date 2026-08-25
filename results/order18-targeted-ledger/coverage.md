# Order-18 Targeted Coverage Ledger

Scope: exhaustive coverage of the constructed structured queue only; this is not an exhaustive order-18 graph census.

Queue: 12987 unique ranked graphs. Authoritative coverage: 12987; authoritative uncovered: 0. Covered outcomes: colorable: 12987; timeouts: 0.

| Slice | Window | State | Durable rows | Unresolved | Hash check |
| --- | --- | --- | ---: | ---: | --- |
| v3 | 1-500 | complete | 500 | 0 | ok |
| v4 | 501-2500 | complete | 2000 | 0 | ok |
| v5 | 2501-4500 | complete | 2000 | 0 | ok |
| v6 | 4501-6500 | complete | 2000 | 0 | ok |
| v7 | 6501-8500 | complete | 2000 | 0 | ok |
| v8 | 8501-10500 | complete | 2000 | 0 | ok |
| final-tail | 10501-12987 | complete | 2487 | 0 | ok |

## Global Reconciliation

Covered ranks: 12987/12987; uncovered ranges: []; rank overlaps: []; duplicate ranks: [].
Canonical hashes: 12987/12987 unique; duplicate hashes: []; rank/hash mismatch ranges: [].
Per-slice count total: 12987; global rank count: 12987; counts match: True. All decisions colorable: True; timeout count: 0.

## Sample Checks

- v3: ranks 1, 250, 500; passed: True.
- v4: ranks 501, 1500, 2500; passed: True.
- v5: ranks 2501, 3500, 4500; passed: True.
- v6: ranks 4501, 5500, 6500; passed: True.
- v7: ranks 6501, 7500, 8500; passed: True.
- v8: ranks 8501, 9500, 10500; passed: True.
- final-tail: ranks 10501, 11744, 12987; passed: True.

## Integrity

No rank, canonical-hash, overlap, duplicate, or completed-slice sample-check integrity issues.
