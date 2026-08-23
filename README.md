# Interval-non-colorable bipartite graphs

This repository contains two newly certified simple connected bipartite graphs with no interval edge coloring. Both have 19 vertices, 34 edges, and maximum degree 11. They are new examples, not new records.

Subsequent exhaustive searches covered every connected bipartite graph on 10--14 vertices with minimum degree at least 2: 21,599,256 canonical candidates, all interval-colorable after exact reruns of two sweep timeouts. No smaller counterexample was found.

Bounded maximum-degree-10 synchronization, transfer, and set-system searches found only colorable graphs. Ordinary edge subdivision cannot reduce maximum degree, because endpoint degrees are invariant under subdivision.

The discovery pipeline starts from known high-degree benchmarks, applies structure-guided constructions and transformations, removes isomorphs with a bipartition-colored Nauty certificate, and classifies promising candidates with an exact rank-potential CP-SAT model. Apparent counterexamples are then checked with an independent fixed-span model, MiniSat on DIMACS instances, and checked DRAT proofs.

See [DISCOVERIES.md](DISCOVERIES.md) for the graphs, figures, verification matrix, minimality results, and hashes. Longer experiment notes are in [`docs/research-log.md`](docs/research-log.md).

## Quick start

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.interval_edge_coloring selftest
```

On Ubuntu, install `python3-pynauty` from APT and create the environment with `python3 -m venv --system-site-packages .venv`. The checked DRAT proofs can be rechecked with [DRAT-Trim](https://github.com/marijnheule/drat-trim).

## Repository map

- `src/` — solvers, search generators, proof export, and figure generation.
- `cluster/` — Slurm array recipe used for the order-13 search.
- `data/` — canonical graph6 inputs for distributed searches.
- `results/candidates/` — graph JSON, CNF, DRAT proofs, checker logs, and manifests.
- `results/` — search reports and benchmark data.
- `figures/` — explanatory images embedded in `DISCOVERIES.md`.

The main result bundles include reproducible DIMACS inputs and independently checkable DRAT certificates for every possible color span.
