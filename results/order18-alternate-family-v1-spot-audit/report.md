# Alternate Family v1 Spot-Audit Discrepancy Analysis

## Corrected verdict

campaign graph identities and sampled colorability evidence remain valid after joining by stored generation-time digest; the v1 spot-audit fail verdict is not a valid basis to reject the campaign because rank and rehash flags were interpreted as graph mismatches.

## Identity and ordering

- Completed event rows: 1200
- Reconstructed selected graphs: 1200
- Exact stored-digest set match: True
- Historical spot-audit rank mismatches: 790
- Current reconstruction rank/order mapping differences: 840
- Cross-semantic-key movements: 0
- Exact float-variance differences against event rows: 456 (maximum 1.66533453693773481e-16)
- Event-to-matched-graph field mismatches: 0

## Canonicalization

- Historical spot-audit sample rehash flags: 42
- Current all-row stored digest rehash matches: 1200
- Current all-row stored digest rehash mismatches: 0
- The historical flags are non-reproduced. In either case, the stored generation-time digests map the event and reconstructed selection sets one-to-one.

## Sampled decision evidence

- Spot samples: 60
- Samples with valid rank-potential certificates: 60
- Samples with valid fixed-span certificates: 60
- Spot solver disagreements: 0
- Spot invalid certificates: 0
- Mismatched spot samples individually joined by stored digest: 42

The JSON companion contains all corrected rank mappings and the field-level comparison for every matched event, including every mismatched spot sample.
