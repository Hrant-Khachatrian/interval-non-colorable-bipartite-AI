# Order-18 Final-Tail Spot Audit

- Conclusion: `pass`
- Reconstructed deterministic queue: `12987` candidates
- Audited final-tail window: `10501-12987`
- Deterministic sample: `36/36` rows (12 beginning, 12 middle, 12 end)
- Mismatches: `0`; structural mismatches: `0`
- Invalid certificates: `0`; solver disagreements: `0`
- Reported negatives/unresolved outcomes: `0`; replay negative observations: `0`
- Maximum sampled runtime: `0.736s`
- Solver replay: rank-potential plus fixed-span at each reported span, `2` workers (production used `1`)

The audit rechecks sample hashes, stored structural fields, connectivity, bipartiteness, bipartition validity, and the minimum-degree filter. It also reconciles the complete event log against report/status counts and all stated rank/hash coverage. Any negative observation triggers an all-legal-span replay before it is recorded.

| Third | Samples | Pass | Disagreement |
| --- | ---: | ---: | ---: |
| beginning | 12 | 12 | 0 |
| middle | 12 | 12 | 0 |
| end | 12 | 12 | 0 |
