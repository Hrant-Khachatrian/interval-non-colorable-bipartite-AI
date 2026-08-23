# Problem Definition: A Smaller Non-Interval-Colorable Bipartite Graph

## Graphs under consideration

All graphs in this problem are finite, undirected, and simple: they have no loops and no multiple edges.

A graph `G = (V,E)` is **bipartite** if its vertex set can be partitioned into two disjoint sets `X` and `Y` such that every edge has one endpoint in `X` and the other in `Y`.

Two edges are **adjacent** if they share an endpoint.

## Proper edge coloring

A **proper edge coloring** of a graph `G` is a function

```text
alpha: E(G) -> {1,2,...,t}
```

such that adjacent edges receive different colors.

For a vertex `v`, let

```text
S(v,alpha) = { alpha(e) : e is incident with v }
```

be the set of colors appearing on the edges incident with `v`.

## Interval edge coloring

A proper edge coloring `alpha` is an **interval `t`-edge-coloring** if:

1. Every color in `{1,2,...,t}` is used on at least one edge.
2. For every vertex `v`, the set `S(v,alpha)` is an interval of consecutive integers.

Equivalently, for every vertex `v`, there is an integer `a_v` such that

```text
S(v,alpha) = {a_v, a_v+1, ..., a_v+d(v)-1},
```

where `d(v)` is the degree of `v`.

A graph is **interval colorable** if it has an interval `t`-edge-coloring for at least one positive integer `t`.

A graph is **non-interval-colorable** if it has no interval `t`-edge-coloring for any positive integer `t`.

The distinction between the following statements is essential:

- `G` has no interval `Delta(G)`-edge-coloring.
- `G` has no interval edge coloring at all.

The first statement does not imply the second. A valid counterexample for this project must exclude interval colorings for every possible number of colors, not only for `t = Delta(G)`.

## Research problem

Find a finite simple bipartite graph `G` that is non-interval-colorable and satisfies at least one of the following strict inequalities:

```text
|V(G)| < 19
```

or

```text
Delta(G) < 11.
```

Here, `|V(G)|` is the number of vertices of `G`, and `Delta(G)` is its maximum degree. The word **or** is inclusive: a graph solves the problem if it satisfies either inequality, and it may satisfy both.

Formally, the task is to construct a bipartite graph `G = (X,Y;E)` satisfying

```text
|V(G)| < 19 or Delta(G) < 11
```

and prove that there do not exist a positive integer `t` and a function

```text
alpha: E(G) -> {1,2,...,t}
```

such that:

1. `alpha` is a proper edge coloring;
2. every color `1,...,t` is used; and
3. `S(v,alpha)` is a set of consecutive integers for every vertex `v` of `G`.

## Goal

The goal is to improve at least one of the two known benchmark parameters by finding and proving the existence of:

- a non-interval-colorable bipartite graph on at most 18 vertices, regardless of its maximum degree; or
- a non-interval-colorable bipartite graph of maximum degree at most 10, regardless of its number of vertices.

Improving both parameters simultaneously would also solve the problem, but it is not required.

A graph is considered a successful result only when both its parameter bound and its non-interval-colorability are established exactly. Failure to find a coloring, a heuristic result, or a computational timeout is not a proof.

## Required final object

For every claimed solution, the final result must specify:

- the graph unambiguously, for example by an adjacency list or equivalent construction;
- its bipartition;
- its number of vertices and edges;
- its maximum degree; and
- a rigorous mathematical or independently checkable computational proof that no interval edge coloring exists for any number of colors.

The reported parameters must verify explicitly that

```text
|V(G)| <= 18 or Delta(G) <= 10.
```
