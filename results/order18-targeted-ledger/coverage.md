# Order-18 Targeted Coverage Ledger

Queue: 12987 unique ranked graphs. Authoritative coverage: 8500; authoritative uncovered: 4487. Durable provisional coverage: 1228.

| Slice | Window | State | Durable rows | Unresolved | Hash check |
| --- | --- | --- | ---: | ---: | --- |
| v3 | 1-500 | complete | 500 | 0 | ok |
| v4 | 501-2500 | complete | 2000 | 0 | ok |
| v5 | 2501-4500 | complete | 2000 | 0 | ok |
| v6 | 4501-6500 | complete | 2000 | 0 | ok |
| v7 | 6501-8500 | complete | 2000 | 0 | ok |
| v8 | 8501-10500 | provisional_active | 1228 | 772 | ok |
| final-tail | 10501-12987 | prepared_unstarted | 0 | 2487 | ok |

## Integrity

No rank, canonical-hash, overlap, duplicate, or completed-slice sample-check integrity issues.

## Next Disjoint Windows

- v8 (active_provisional): 9729-10500
- final-tail (prepared_unstarted): 10501-12987
