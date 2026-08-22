# Research Playbook: Finding New Non-Interval-Colorable Bipartite Graphs

Status: strategy prepared on 2026-08-22. No computational search described below has yet been executed in this workspace.

## Mission

Find, certify, minimize, understand, and document new simple bipartite graphs that admit no interval edge coloring.

The highest-value outcomes are:

1. A bipartite counterexample with maximum degree at most 10.
2. A bipartite counterexample on at most 18 vertices.
3. A new structural or infinite family of counterexamples, even if it does not improve either record.
4. A classification of edge-minimal counterexamples near the known maximum-degree-11 boundary.

The project must produce mathematical evidence, not merely solver timeouts. Every claimed counterexample must receive independent exact verification and, preferably, a human-readable proof.

## Definition and conventions

For a graph `G`, an interval `t`-edge-coloring is a proper edge coloring with colors `1,...,t` such that:

- every color is used; and
- at every vertex `v`, the colors of the edges incident with `v` are consecutive integers.

Write `G in N` when `G` is interval colorable and `G notin N` otherwise.

Unless explicitly stated otherwise, graphs in this project are finite, undirected, simple, and connected. Disconnected graphs are not useful as minimal counterexamples because their components can be colored independently and their palettes overlaid after normalization.

For a connected bipartite graph on `n` vertices, any interval coloring has a span `t` satisfying

```text
Delta(G) <= t <= n - 1.
```

The upper bound follows from the triangle-free case of the Asratian-Kamalian bound.

## Literature baseline

The following facts define the search boundary.

- All bipartite graphs of maximum degree at most 3 are interval colorable.
- All bipartite graphs on at most 15 vertices are interval colorable.
- If one bipartition class has at most 3 vertices, the graph is interval colorable.
- Regular bipartite graphs are interval colorable.
- Trees, complete bipartite graphs, grids, doubly convex bipartite graphs, simple outerplanar bipartite graphs, and `(2,b)`-biregular bipartite graphs are interval colorable.
- Every `(5*,2*)`-bipartite graph is interval colorable with at most 6 colors.
- Every `(6,3)`-biregular graph has an interval 7-coloring, although some have no interval 6-coloring.
- The `(3,4)`-biregular case remains a prominent open case of the conjecture that all biregular bipartite graphs are interval colorable.
- In the literature located through 2025, the smallest known order of a non-interval-colorable bipartite graph is 19, and the smallest known maximum degree is 11. No later resolution was found during the 2026-08-22 literature check.

Known negative benchmarks include:

- the 19-vertex Malafiejski rosette with maximum degree 15;
- a 21-vertex finite-projective-plane construction with maximum degree 13;
- the 19-vertex graph derived from `K_(2,2,2)` with maximum degree 12;
- the 20-vertex graph derived from `K_(3,4)` after deleting one hub edge, with maximum degree 11.

Primary references:

1. P. A. Petrosyan and H. H. Khachatrian, "Interval non-edge-colorable bipartite graphs and multigraphs," Journal of Graph Theory 76 (2014), 200-216. Preprint: https://arxiv.org/abs/1301.3811
2. H. Khachatrian and T. Mamikonyan, "On interval edge-colorings of bipartite graphs of small order," 2015. https://arxiv.org/abs/1508.02851
3. A. Malafiejska, M. Malafiejski, K. M. Ocetkiewicz, and K. Pastuszak, "Interval Edge Coloring of Bipartite Graphs with Small Vertex Degrees," ISAAC 2021. https://doi.org/10.4230/LIPIcs.ISAAC.2021.26
4. A. M. Magomedov, "Bipartite (6,3)_6-biregular graphs which do not allow interval coloring," 2014. https://doi.org/10.31029/demr.1.3
5. C. J. Casselgren et al., "Near-interval edge colorings of graphs," Discrete Applied Mathematics, 2025. https://doi.org/10.1016/j.dam.2025.03.011
6. A. Hambardzumyan and L. Muradyan, "On interval edge-colorings of planar graphs," 2023. https://arxiv.org/abs/2303.11466

### Terminology warning

Never equate "not interval Delta-colorable" with "not interval colorable."

The Magomedov `(6,3)` examples illustrate the danger: they have no interval 6-coloring, but every `(6,3)`-biregular graph has an interval 7-coloring. A valid counterexample must rule out every possible span, not just `t = Delta(G)`.

## Central exact formulation: local ranks and vertex potentials

This should be the primary decision model.

For every vertex `v`, introduce an integer `a_v`, interpreted as the first color appearing at `v`.

For every incidence `(v,e)`, where edge `e` is incident with `v`, introduce a local rank

```text
p_(v,e) in {0,...,d(v)-1}.
```

At each vertex `v`, require the incident ranks to be a permutation of

```text
0,...,d(v)-1.
```

For every edge `e = uv`, impose

```text
a_u + p_(u,e) = a_v + p_(v,e).                 (1)
```

### Equivalence theorem

`G` is interval colorable if and only if this system has a solution.

Proof sketch:

- Given an interval coloring, set `a_v` to the minimum color at `v` and set `p_(v,e) = c(e)-a_v`. The ranks at each vertex are exactly `0,...,d(v)-1`, and equation (1) holds.
- Conversely, define `c(e)` to be the common value in equation (1). The local permutation constraints make the coloring proper and consecutive at every vertex.
- In a connected graph, vertex color intervals overlap along their shared edge colors, so their union is an interval. Shift the global minimum to 1.

### Why this model is preferable

- It decides all possible spans simultaneously.
- It avoids an arbitrary palette bound in the conceptual model.
- It exposes the obstruction as incompatibility among local permutations and cycle equations.
- It explains why trees are colorable: there are no cycle consistency constraints.
- It is naturally implementable in CP-SAT, SMT, or SAT.

For implementation, pin one potential, for example `a_root = 0`. Other potentials can safely be bounded using a path bound such as

```text
|a_v| <= sum over w in V(G) of (d(w)-1).
```

### Cycle-only interpretation

Orient an edge `e_i = v_i v_(i+1)` along a walk. Equation (1) gives

```text
a_(v_(i+1)) - a_(v_i)
  = p_(v_i,e_i) - p_(v_(i+1),e_i).
```

Summing around a cycle forces the sum of these local-rank differences to be zero. Thus interval colorability can also be seen as selecting one permutation at every vertex so that all fundamental-cycle equations are satisfied.

This interpretation should be used during proof extraction.

## Independent exact encodings

Every serious candidate should be checked by at least two logically independent encodings.

### Encoding A: rank-potential CP-SAT or SMT

Variables:

- integer `a_v` for each vertex;
- integer `p_(v,e)` for each incidence;
- `AllDifferent` on incident ranks at every vertex;
- equation (1) on every edge.

This is the primary fast oracle.

### Encoding B: time-slot SAT

For each possible span `t` in `Delta(G),...,n-1`, use Boolean variables:

```text
x_(e,c) = 1 iff edge e receives color c;
s_(v,k) = 1 iff the interval at v starts at k.
```

Impose:

- exactly one color per edge;
- at most one incident edge of a vertex per color;
- exactly one start per vertex;
- the active colors at `v` are exactly `k,...,k+d(v)-1`;
- every active color occurs once at `v`;
- every global color is used.

Run all possible `t`, or produce one combined bounded SAT encoding. For a final negative result, produce and check DRAT/LRAT-style certificates when practical.

### Verification policy

- A SAT result must be checked by a small independent coloring verifier.
- An UNSAT result must be reproduced with the second encoding.
- A timeout is never evidence of noncolorability.
- Store solver version, options, graph hash, and complete logs.

## A unifying weighted-hub obstruction

Many published constructions use the same underlying mechanism.

Let `h` be a vertex. For `x,y in N(h)`, let `P` be a path from `x` to `y` in `G-h`, and define its vertex weight

```text
w(P) = sum over v in V(P) of (d_G(v)-1).
```

In any interval coloring,

```text
|c(hx)-c(hy)| <= w(P).                          (2)
```

Reason: when moving between two edges incident with a vertex `v`, their colors differ by at most `d(v)-1`. Summing these local changes along the path gives (2).

Define

```text
rho_h(x,y) = minimum w(P) over x-y paths P in G-h.
D_h = maximum rho_h(x,y) over x,y in N(h).
```

The two extreme-colored edges at `h` differ by `d(h)-1`. Therefore:

```text
If D_h < d(h)-1, then G is not interval colorable.   (3)
```

Define the obstruction margin

```text
margin(h) = d(h)-1-D_h.
```

Search interpretation:

- `margin(h) > 0` gives a direct noncolorability certificate.
- `margin(h) = 0` is extremely promising: every transition on a shortest connector path must be tight. Tightness often forces exact local ranks and eventually a collision on a short cycle.
- `margin(h) = -1` or `-2` may still be promising when several connector paths overlap and jointly force inconsistent ranks.

The fat-triangle, projective-plane, tree-closure, and subdivision arguments are special cases or close relatives of this mechanism.

## Structural lesson

Noncolorability is generated by interacting cycle constraints, not simply by high maximum degree.

Promising graphs tend to have:

- many overlapping 4- and 6-cycles;
- a hub or a small separator whose incident colors must span a long interval;
- several short, low-weight connector paths between hub neighbors;
- equality cases that force the same local rank twice;
- few interval colorings, often modulo translation, reversal, and automorphism.

Sparse cactus-like structures and isolated cycles are less promising because they leave local permutations too independent.

## Candidate-generation lanes

Run the lanes below in the stated priority order.

### Lane 0: reproduce the benchmark boundary

Before any discovery run, reconstruct and verify the known examples listed above. Also test positive controls:

- trees;
- complete bipartite graphs;
- random regular bipartite graphs;
- random subcubic bipartite graphs;
- selected `(5*,2*)` graphs;
- known `(6,3)` graphs that require 7 rather than 6 colors.

The exact oracle must classify all controls correctly.

### Lane 1: mine the neighborhood of the known Delta=11 obstruction

Start with the graph obtained from the subdivided `K_(3,4)` construction after deleting one hub edge.

Generate all non-isomorphic nearby graphs under:

- further edge deletions;
- bipartite 2-switches;
- redistribution of hub adjacencies;
- replacement of degree-2 or degree-3 connectors;
- vertex splitting away from the hub;
- small changes to the underlying core before subdivision;
- one- or two-vertex gadget replacements.

For every negative graph, minimize it greedily and then exhaustively where feasible. Record all edge-minimal and vertex-minimal variants.

Expected outcome: new maximum-degree-11 critical graphs and insight into which part of the known proof is essential.

### Lane 2: partial-hub subdivisions under a degree cap

Generalize the published construction:

1. Choose a small core graph `H`; `H` itself need not be bipartite.
2. Subdivide some or all edges.
3. Add a hub adjacent to a chosen subset of subdivision vertices.
4. Optionally add a second hub or a small terminal gadget.
5. Require the final graph to be simple and bipartite.

Enumerate cores with roughly 7-12 edges first. Constrain either:

```text
Delta(G) <= 10
```

or

```text
|V(G)| <= 18.
```

Prioritize candidates with weighted-hub margins in `{0,-1,-2}`. A positive margin should be recognized immediately by the sufficient obstruction (3).

### Lane 3: pairwise-intersecting incidence designs

Generalize the finite-projective-plane construction using a small pairwise-intersecting hypergraph.

- Hypergraph elements become connector vertices.
- Each block gives one or more twin vertices adjacent to its elements and to the hub.
- Block multiplicities are variables.
- Mixed block sizes are allowed.

Search over small intersecting set systems and integer multiplicities while constraining maximum degree and order. Optimize the weighted-hub margin. Symmetric projective planes are only one point in this larger space; irregular systems may sit closer to the record boundary.

### Lane 4: perturb `(6,3)` near-misses

Use the known `(6,3)_6` graphs with no interval 6-coloring as near-obstruction seeds. Do not misclassify them: they have interval 7-colorings.

For each seed:

1. Enumerate or sample interval 7-colorings modulo graph automorphisms and global color reversal.
2. Identify forced local ranks, forced terminal offsets, and edges that occupy extreme colors in all colorings.
3. Apply degree-preserving switches and small irregular perturbations designed to destroy those colorings.
4. Check all spans with the rank-potential oracle.
5. Explore attaching small terminal gadgets that block the forced 7-coloring without raising the degree substantially.

This is a high-risk, high-reward route to counterexamples of maximum degree 6-10.

### Lane 5: targeted order 16-18 search

Do not initially enumerate all graphs. A historical 16-vertex search had more than 12 billion filtered candidates.

Mandatory filters:

- connected;
- minimum degree at least 2;
- both bipartition classes have size at least 4;
- maximum degree at least 4;
- nonregular;
- exclude known guaranteed-positive classes;
- prefer high short-cycle overlap;
- prefer small weighted hub diameter;
- prefer difficult or nearly unique interval-coloring behavior.

For order 16, prioritize bipartitions

```text
5+11, 6+10, 7+9, 8+8.
```

The `4+12` case is covered by the known small-part results.

Search degree sequences first, using canonical bipartite graph generation within promising sequences. Only after targeted searches should full distributed canonical enumeration be considered.

### Lane 6: automated terminal-gadget synthesis

Enumerate small bipartite gadgets with two or three terminals and compute the relation they impose on terminal starts and ranks.

Seek gadgets that force:

- equal starts;
- a fixed start offset;
- reversed local order;
- a terminal edge to be the minimum or maximum color;
- two local palettes to concatenate;
- several low-degree vertices to behave like one higher-degree hub.

The last relation is especially important. Simply splitting the degree-11 hub destroys its long forced interval. A synchronization gadget that preserves the effective span while lowering individual degrees could break the maximum-degree-10 barrier and would likely yield an infinite family.

Represent a gadget by its finite terminal signature rather than by every internal coloring. Compose signatures to search for an inconsistent cycle of relations.

## Graph-space filters and cautions

Safe filters for a smallest or critical counterexample include:

- connectedness;
- minimum degree at least 2, since a pendant vertex can be added to a coloring of the smaller graph using a new endpoint color;
- both sides of the bipartition have at least 4 vertices;
- maximum degree at least 4;
- nonregularity.

Do not assume interval colorability or noncolorability is monotone under arbitrary edge addition or deletion. It is not a standard forbidden-subgraph property. Use deletions only when the exact oracle rechecks the result.

Do not discard biregular graphs as logically impossible counterexamples; the general conjecture is open. However, searching them should be treated as a moonshot rather than the first computational lane.

## Near-obstruction scoring

Most generated graphs will be colorable. Retain those that are close to failure.

For a proper edge coloring `alpha`, define deficiency

```text
def(alpha)
  = sum over v of
      (max S(v,alpha) - min S(v,alpha) + 1 - d(v)).
```

An interval coloring has deficiency zero. A heuristic optimizer can seek the minimum deficiency subject to a reasonable palette bound.

Additional candidate scores:

- number of interval colorings modulo translation, reversal, and graph automorphism;
- number of incidences with forced ranks;
- number of forced start differences;
- solver conflicts or search-tree size;
- weighted-hub margin;
- number and overlap pattern of 4- and 6-cycles;
- size of the smallest separator carrying most cycle constraints;
- size of a minimized UNSAT core for negative instances.

Prefer graphs with very few interval colorings. They are better mutation seeds than typical colorable graphs with large solution spaces.

## Modular relaxations

Reduce the rank-potential equations modulo a small prime `q`.

At vertex `v`, the incidence ranks retain the residue multiset of

```text
0,...,d(v)-1 mod q.
```

For every edge `uv`, require

```text
a_u + p_(u,e) = a_v + p_(v,e) mod q.
```

If this relaxed system is already unsatisfiable, the original integer system is unsatisfiable. Test `q = 2,3,5` early.

Benefits:

- very cheap filtering;
- possible parity or residue proofs;
- a strong discovery objective: deliberately seek graphs that are modularly inconsistent.

If a candidate fails modulo 2 or 3, prioritize it because the eventual human proof may be short.

## Search mechanics

Use canonical graph representations and isomorphism rejection from the start.

Recommended components:

- nauty/Traces `genbg` or an equivalent canonical bipartite generator;
- graph6/bipartite6 plus an explicit bipartition-aware canonical form;
- a fast graph-invariant layer before full isomorphism tests;
- CP-SAT or SMT for discovery;
- a Boolean SAT encoding with proof logging for final certification.

Use graph mutations only after canonicalization, or canonicalize every child before adding it to the queue.

Store at least:

```text
candidate_id
canonical_encoding
sha256
bipartition
adjacency_list
degree_sequence_by_side
order
size
maximum_degree
minimum_degree
girth
cycle_counts
automorphism_group_size
generator_lane
parent_candidate_id
mutation
weighted_hub_statistics
solver_results
solver_versions
proof_artifacts
minimality_results
literature_comparison
```

## From computational hit to mathematical result

For every graph classified as non-interval-colorable:

### 1. Minimize

- Greedily delete edges and retain a deletion only if the graph remains noncolorable.
- Attempt vertex deletion when connectivity and the intended scope permit.
- Then check all single-edge and single-vertex deletions exactly.
- Explore degree-preserving switches that reduce symmetry or order without restoring colorability.

### 2. Verify independently

- Re-run the rank-potential model with a second solver.
- Run the independent time-slot encoding over every possible span.
- Check proof logs with an independent proof checker.
- Confirm that a deliberately corrupted candidate is rejected by the verification harness.

### 3. Analyze symmetry

- Compute automorphism orbits of vertices, edges, and incidences.
- Quotient equivalent cases in any proof.
- Treat global color reversal as a symmetry.
- Normalize a distinguished edge or vertex to remove translation and reflection.

### 4. Extract an UNSAT core

Find the smallest collection of:

- local permutation constraints;
- edge equalities;
- fundamental-cycle equations;
- modular constraints;
- weighted-path inequalities

that is already inconsistent.

### 5. Build a human proof

Prefer one of the following final forms:

1. Weighted-span contradiction: the extreme colors at a hub cannot be connected through the low-weight network.
2. Tightness contradiction: equality is forced along several paths and duplicates a local rank.
3. Modular contradiction: parity or residue counts cannot be satisfied.
4. Cycle-equation contradiction: a small symmetry-reduced case split makes the sum around some cycle nonzero.
5. Separator table: a small dynamic-programming table lists all possible terminal signatures, none of which extend globally.

The proof generator may initially output a decision DAG. Compress equivalent states under automorphisms and convert repeated subtrees into lemmas about terminal offsets or forced ranks.

### 6. Establish novelty

- Canonically compare against every reconstructed published example.
- Compare against all known members of the same parameterized family.
- Search the literature again using the graph's construction, invariants, and degree sequence.
- State novelty narrowly: new graph, new record, new critical graph, new family, or new structural restriction.
- Do not claim a record solely because the graph looks different from published drawings.

## Acceptance checklist for a claimed counterexample

A candidate is ready to report only when all applicable boxes are checked:

- [ ] Simple and bipartite.
- [ ] Connected.
- [ ] Canonical encoding and adjacency list saved.
- [ ] Rank-potential model proves UNSAT.
- [ ] Independent encoding also proves UNSAT.
- [ ] Every possible span has genuinely been excluded.
- [ ] Formal proof certificate saved and checked, when supported.
- [ ] All single-edge deletions checked.
- [ ] All single-vertex deletions checked.
- [ ] Automorphism group and orbit structure computed.
- [ ] Published examples reconstructed and canonical comparison completed.
- [ ] Human-readable proof or compact machine-verifiable certificate prepared.
- [ ] Reproducible script, solver versions, and random seeds saved.

## Recommended execution order

### Phase A: exact infrastructure

1. Implement a graph parser, canonical serializer, and coloring verifier.
2. Implement the rank-potential oracle.
3. Implement the independent time-slot encoding.
4. Reproduce all positive and negative benchmarks.
5. Add modular relaxations and weighted-hub calculations.

Exit condition: every benchmark is classified correctly, and deliberate encoding errors are caught by cross-verification.

### Phase B: attainable discoveries

1. Enumerate the local neighborhood of the known maximum-degree-11 graph.
2. Minimize and classify all negative variants.
3. Extract at least one human-readable proof automatically or semi-automatically.

Exit condition: a reproducible catalog of new or rediscovered critical maximum-degree-11 graphs, with exact certificates.

### Phase C: attack the record boundary

1. Run partial-hub and intersecting-design generators under `Delta <= 10`.
2. Run perturbations of `(6,3)` near-misses.
3. Prioritize candidates with zero weighted margin, modular inconsistency, or very few colorings.
4. Explore synchronization gadgets that emulate a high-degree hub.

Exit condition: either a certified record candidate or a documented negative search that yields new structural lemmas and substantially narrows promising degree patterns.

### Phase D: small-order search

1. Search selected degree sequences for orders 16-18.
2. Use canonical generation and exact screening.
3. Scale to broader distributed enumeration only after profiling the targeted runs.

Exit condition: a certified small-order candidate, or a reproducible partial/exhaustive result for clearly specified degree sequences.

## Guidance for future AI agents

When continuing this project:

1. Read this file before changing code or launching a search.
2. Check the workspace for prior experiment logs and do not restart completed searches.
3. Preserve exact graph encodings and solver artifacts.
4. Treat SAT colorings as easy to verify and UNSAT claims as requiring independent confirmation.
5. Never infer noncolorability from a timeout or from failure at `t = Delta`.
6. Prefer small, proof-friendly generators before brute-force enumeration.
7. Report intermediate mathematical observations, especially forced-rank and weighted-path lemmas.
8. Keep discovery, certification, minimization, and novelty checking as separate stages.
9. Do not silently change the graph model from simple graphs to multigraphs.
10. If literature status may have changed, re-check current primary sources before claiming a record.

## Immediate next task

The next implementation turn should build the exact rank-potential solver and a small independent verifier, then reconstruct the four negative benchmarks and a representative suite of positive controls. Do not begin the large graph search until those tests pass.
