# New interval-non-colorable bipartite graphs

Status: verified candidates, 2026-08-22. These are **new graphs**, not new records: they have the same 19-vertex order as the previously known smallest examples and the same maximum degree 11 as the previous smallest known maximum degree.

## Candidate Q1-00012

- Simple, connected, bipartite graph with bipartition sizes 8 and 11.
- Order 19, size 34, maximum degree 11, minimum degree 3.
- Construction: identify connectors `C0` and `C1` in the reconstructed 20-vertex, maximum-degree-11 benchmark `hat_K34_prime_Delta11`.
- Bipartition-colored canonical hash:
  `8:11:eec834587010499acfe094698e6a75c05984de65c5a3d4e51289b2502aa75887`.
- Automorphism group order 8; 9 vertex orbits.

![Q1-00012 three-layer construction view](figures/Q1-00012-full.png)

**Figure 1.** Three-layer construction view: `u` is in the top layer, all eleven vertices adjacent to `u` are in the middle layer, and their core endpoints are in the bottom layer. Purple marks the merged connector.

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

![Q1-00014 three-layer construction view](figures/Q1-00014-full.png)

**Figure 4.** The second discovered graph in three-layer form: hub `u`, its eleven neighbors, and the core endpoints reached through them. Purple marks the merged connector `C0&C4`.

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

## Exhaustive small-order searches

On 2026-08-23, we extended the search for a graph with fewer than 19 vertices. Each connected candidate was classified by the exact rank-potential CP-SAT oracle over its full legal span range; regular bipartite graphs were skipped only when invoking their standard interval-coloring argument.

| class | generated | nonregular solved | regular skipped | negatives | timeouts |
|---|---:|---:|---:|---:|---:|
| connected 5+5 graphs with 14 edges | 558 | 558 | 0 | 0 | 0 |
| connected 5+6 graphs with minimum degree at least 2 | 5,969 | 5,962 | 7 | 0 | 0 |
| connected 6+6 graphs with minimum degree at least 2 | 71,945 | 71,932 | 13 | 0 | 0 |
| connected 6+7 graphs with minimum degree at least 2 | 706,201 | 706,201 | 0 | 0 | 0 |

Every processed graph has a distinct bipartition-colored canonical hash within its class. Thus no interval-non-colorable bipartite graph exists in these four exhaustively specified families. This rules out order-10 and order-11 counterexamples in the listed classes, and all minimum-degree-2, connected 6+6 and 6+7 graphs of orders 12 and 13.

The 6+7 class was processed on the YSU `research_cpu` partition as 128 deterministic nauty-residue slices, using four CPU slots per slice. Its slowest primary solve completed in 1.38 seconds under the two-second limit.

## Artifacts

- `results/candidates/Q1-00012/` — graph, CNFs, DRAT proofs, checker logs, certificate, manifest.
- `results/candidates/Q1-00014/` — same certificate bundle.
- `results/quotient-r1.json` — full 19-vertex quotient search.
- `results/quotient-r2.json` — 412 unique 18-vertex quotients, all colorable.
- `results/quotient-r3.json` — 5,066 unique 17-vertex quotients, all colorable.
- `results/logs/search-5x5.log` — exact classifications for all 558 connected 5+5, 14-edge graphs.
- `results/small-order-11-5x6.jsonl` — exact classifications for all connected 5+6, minimum-degree-2 graphs.
- `results/small-order-12-6x6.jsonl` — exact classifications for all connected 6+6, minimum-degree-2 graphs.
- `results/order11-5x6-audit.txt`, `results/order12-6x6-audit.txt`, and `results/order13-6x7-audit.txt` — completeness and uniqueness audits.
- `data/order13-6x7-d2.g6` — canonical nauty input used by the 128-way Slurm run.
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
