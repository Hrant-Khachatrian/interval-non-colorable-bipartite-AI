# Independent interval-solver audit

sound for this audit matrix: no resolved disagreement between rank-potential, fixed-span CP-SAT, and independently encoded PicoSAT CNF

- Cases: 33
- Fixed spans: 262
- Disagreements: 0
- Unresolved cases: 0
- Fixed-span false-negative/false-positive indicators: 0/0
- Validated SAT certificates (production/independent): 135/135
- Total elapsed: 92.062s

The audit checks every span from delta(G) through |V(G)| - 1. PicoSAT CNF uses separate edge-color and local-interval-start literals, pairwise incident-color exclusion, interval coverage, and global color use. All SAT assignments are validated as proper interval edge colorings.
