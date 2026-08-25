# Order-18 spot audit

- Completion: `complete`
- Deterministic queue: `12987` candidates
- Sample: `108/108` claims (18 from each completed v3-v8 slice)
- Mismatches: `0`
- Invalid certificates: `0`
- Solver disagreements: `0`
- Max combined sample runtime: `0.519s`
- Recheck configuration: rank-potential and fixed-span CP-SAT, `2` workers, `20s` limits

Every sample reconstructs the production-ranked graph, compares its hash and stored structural fields, validates both extracted certificates, and confirms SAT at the reported primary span.

| Slice | Sampled ranks | Pass | Mismatch |
| --- | ---: | ---: | ---: |
| v3 | 18 | 18 | 0 |
| v4 | 18 | 18 | 0 |
| v5 | 18 | 18 | 0 |
| v6 | 18 | 18 | 0 |
| v7 | 18 | 18 | 0 |
| v8 | 18 | 18 | 0 |
