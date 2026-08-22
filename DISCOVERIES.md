# New interval-non-colorable bipartite graphs

Status: verified candidates, 2026-08-22. These are **new graphs**, not new records: they have the same 19-vertex order as the previously known smallest examples and the same maximum degree 11 as the previous smallest known maximum degree.

## Candidate Q1-00012

- Simple, connected, bipartite graph with bipartition sizes 8 and 11.
- Order 19, size 34, maximum degree 11, minimum degree 3.
- Construction: identify connectors `C0` and `C1` in the reconstructed 20-vertex, maximum-degree-11 benchmark `hat_K34_prime_Delta11`.
- Bipartition-colored canonical hash:
  `8:11:eec834587010499acfe094698e6a75c05984de65c5a3d4e51289b2502aa75887`.
- Automorphism group order 8; 9 vertex orbits.

![Q1-00012 full bipartite graph](figures/Q1-00012-full.png)

**Figure 1.** The complete graph with its two bipartition classes. The red hub must receive a block of eleven consecutive colors; purple marks the vertex produced by identifying connectors `C0` and `C1`.

![Q1-00012 hub-constraint view](figures/Q1-00012-hub-constraint.png)

**Figure 2.** A hub-centered reading of the same graph. Thick spokes are the hub incidences. Thin paths through the core couple those color choices, preventing every span from 11 through 18.

![Q1-00012 verification dashboard](figures/Q1-00012-verification.png)

**Figure 3.** Independent verification outcomes for every possible span, together with exhaustive single-edge and single-vertex minimality checks.

## Candidate Q1-00014

- Simple, connected, bipartite graph with bipartition sizes 8 and 11.
- Order 19, size 34, maximum degree 11, minimum degree 2.
- Construction: identify connectors `C0` and `C4` in `hat_K34_prime_Delta11`.
- Bipartition-colored canonical hash:
  `8:11:2412ce4d084d92cdaa41c7e1a009e8011de4be9e281a3fe6349eb7fc057ef9a0`.
- Automorphism group order 12; 9 vertex orbits.

![Q1-00014 full bipartite graph](figures/Q1-00014-full.png)

**Figure 4.** The second discovered graph in bipartite-column form. Purple marks the merged connector `C0&C4`; red marks the degree-11 hub.

![Q1-00014 hub-constraint view](figures/Q1-00014-hub-constraint.png)

**Figure 5.** Hub-centered view of Q1-00014. This layout shows why a low-degree local structure can still force a long interval at one vertex: all eleven outer spokes interact through the inner core paths.

![Q1-00014 verification dashboard](figures/Q1-00014-verification.png)

**Figure 6.** Verification and minimality summary for Q1-00014. Every legal span has four independent exact checks, while every deletion check returns to interval-colorable status.

## Exact verification

For both graphs and every span `t=11,...,18`:

1. the rank-potential CP-SAT model is infeasible;
2. the independent fixed-start/fixed-color CP-SAT model is infeasible;
3. the exported DIMACS instance is unsatisfiable under MiniSat;
4. PicoSAT produces a DRAT proof, and DRAT-Trim reports `s VERIFIED`.

The legal span range is exhaustive because a connected bipartite graph on 19 vertices has span at most 18.

Controls used for the encodings include `K(3,5)`: MiniSat reports UNSAT at spans 5 and 6 and SAT at span 7. The published rosette `M(5,5,5)` is UNSAT at all legal spans.

## Minimality

For each candidate, the exact rank-potential oracle checked:

- all 34 single-edge deletions: all colorable;
- all 19 single-vertex deletions: all colorable;
- no deletion solve timed out.

Thus both candidates are edge-minimal and vertex-minimal under deletion checks.

## Novelty scope

The two graphs are non-isomorphic to each other and to the five reconstructed published benchmarks: `Delta(5,5,5)`, `Erd(2,2,2,2,2,2,1)`, hat `K(3,4)`, its degree-11 edge deletion, and hat `K(2,2,2)`. A same-day arXiv/OpenAlex literature recheck found no direct newer hit for interval-non-colorable bipartite graphs. The novelty claim is therefore narrowly to the documented 2026-08-22 baseline and these reconstructed benchmarks, not an exhaustive claim over every unpublished or hard-to-index source.

## Artifacts

- `results/candidates/Q1-00012/` — graph, CNFs, DRAT proofs, checker logs, certificate, manifest.
- `results/candidates/Q1-00014/` — same certificate bundle.
- `results/quotient-r1.json` — full 19-vertex quotient search.
- `results/quotient-r2.json` — 412 unique 18-vertex quotients, all colorable.
- `results/quotient-r3.json` — 5,066 unique 17-vertex quotients, all colorable.
- `results/literature-recheck-2026-08-22.json` — literature-query snapshot.
- `results/literature-targeted-openalex-2026-08-22.json` — targeted exact-phrase OpenAlex query.

## Tool versions

- Python 3.10.12 (WSL Ubuntu 22.04).
- OR-Tools CP-SAT 9.15.6755.
- MiniSat 2.2.
- PicoSAT 965, built with trace generation.
- DRAT-Trim commit `2e3b2dc0ecf938addbd779d42877b6ed69d9a985`.
- Nauty/pynauty 2.7.3 for bipartition-colored canonical certificates and automorphism groups.

## Final bundle audit

On 2026-08-22, an automated audit reloaded both candidate bundles and confirmed:

- graph order/size/degree and bipartition invariants;
- agreement of stored colored canonical hashes;
- CP-SAT fixed-span exclusion of every span 11 through 18;
- MiniSat UNSAT status for every span 11 through 18;
- PicoSAT/DRAT-Trim verification of every span 11 through 18;
- zero negative edge or vertex deletions and zero minimality timeouts;
- pairwise non-isomorphism to all five reconstructed benchmarks;
- every non-manifest file hash listed in each manifest.
