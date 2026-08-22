#!/usr/bin/env python3
"""Exact tools for interval edge colorings of finite simple graphs."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import itertools
import json
import math
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import networkx as nx
from ortools.sat.python import cp_model
import pynauty


@dataclass(frozen=True)
class SolveResult:
    status: str
    encoding: str
    elapsed_seconds: float
    coloring: dict[str, int] | None = None
    span: int | None = None
    solver_status: str | None = None
    conflicts: int | None = None
    branches: int | None = None
    wall_time: float | None = None


class Graph:
    """A simple graph with stable named vertices and optional bipartition."""

    def __init__(
        self,
        vertices: Sequence[str],
        edges: Iterable[Sequence[str]],
        bipartition: Sequence[Sequence[str]] | None = None,
        metadata: dict | None = None,
    ):
        self.vertices = tuple(vertices)
        self.vertex_set = set(self.vertices)
        if len(self.vertex_set) != len(self.vertices):
            raise ValueError("vertex names are not unique")
        self.edges = []
        seen = set()
        for uv in edges:
            u, v = uv
            if u == v or u not in self.vertex_set or v not in self.vertex_set:
                raise ValueError(f"invalid edge {uv!r}")
            key = tuple(sorted((u, v)))
            if key in seen:
                continue
            seen.add(key)
            self.edges.append(key)
        if bipartition is not None:
            left, right = (set(bipartition[0]), set(bipartition[1]))
            if left | right != self.vertex_set or left & right:
                raise ValueError("bipartition does not partition the vertices")
            if any(u in left and v in left for u, v in self.edges) or any(
                u in right and v in right for u, v in self.edges
            ):
                raise ValueError("edge lies inside a claimed bipartition class")
            self.bipartition = (tuple(sorted(left)), tuple(sorted(right)))
        else:
            colors = nx.bipartite.color(nx.Graph(self.edges)) if self.edges else {}
            if len(colors) < len(self.vertices):
                raise ValueError("graph is not bipartite")
            self.bipartition = (
                tuple(sorted(v for v in self.vertices if colors[v] == 0)),
                tuple(sorted(v for v in self.vertices if colors[v] == 1)),
            )
        self.metadata = dict(metadata or {})
        self._nx = nx.Graph(self.edges)
        self._nx.add_nodes_from(self.vertices)

    @property
    def n(self) -> int:
        return len(self.vertices)

    @property
    def m(self) -> int:
        return len(self.edges)

    @property
    def degrees(self) -> dict[str, int]:
        return dict(self._nx.degree())

    @property
    def delta(self) -> int:
        return max(dict(self._nx.degree()).values(), default=0)

    def adjacency(self) -> dict[str, list[tuple[str, str]]]:
        adj = {v: [] for v in self.vertices}
        for u, v in self.edges:
            edge = (u, v)
            adj[u].append((v, edge))
            adj[v].append((u, edge))
        return adj

    def canonical_hash(self) -> str:
        # Stable serialization of the labelled construction. Isomorphism-aware
        # comparison is deliberately separate so certificates preserve labels.
        payload = json.dumps(
            {
                "vertices": list(self.vertices),
                "edges": [list(e) for e in sorted(self.edges)],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def ordered_for_bipartite_canonicalization(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return tuple(sorted(self.bipartition[0])), tuple(sorted(self.bipartition[1]))

    def isomorphic_to(self, other: "Graph") -> bool:
        g1 = nx.Graph(self.edges)
        g1.add_nodes_from(self.vertices)
        g2 = nx.Graph(other.edges)
        g2.add_nodes_from(other.vertices)
        return nx.vf2pp_is_isomorphic(g1, g2)

    def to_json(self) -> dict:
        return {
            "vertices": list(self.vertices),
            "edges": [list(e) for e in sorted(self.edges)],
            "bipartition": [list(x) for x in self.bipartition],
            "metadata": self.metadata,
            "sha256_labelled": self.canonical_hash(),
            "sha256_bipartition_canonical": nauty_canonical_hash(self),
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2) + "\n")

    @classmethod
    def from_json(cls, data: dict) -> "Graph":
        return cls(data["vertices"], data["edges"], data.get("bipartition"), data.get("metadata"))


def verify_coloring(graph: Graph, coloring: dict[tuple[str, str], int]) -> tuple[bool, str]:
    if set(coloring) != set(map(tuple, graph.edges)):
        missing = set(map(tuple, graph.edges)) - set(coloring)
        extra = set(coloring) - set(map(tuple, graph.edges))
        return False, f"edge set mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
    incident: dict[str, list[int]] = defaultdict(list)
    used_colors = set()
    for (u, v), color in coloring.items():
        if not isinstance(color, int):
            return False, f"non-integer color on {u}-{v}"
        if color < 1:
            return False, f"nonpositive color on {u}-{v}"
        incident[u].append(color)
        incident[v].append(color)
        used_colors.add(color)
    expected_colors = set(range(1, max(used_colors, default=0) + 1))
    if used_colors != expected_colors:
        return False, "not every color 1..t is used"
    for vertex, colors in incident.items():
        if len(colors) != len(set(colors)):
            return False, f"properness failure at {vertex}"
        lo, hi = min(colors), max(colors)
        if set(colors) != set(range(lo, hi + 1)):
            return False, f"interval failure at {vertex}"
    return True, "valid"


def _graph6_number(n: int) -> bytes:
    if n < 0:
        raise ValueError("negative graph order")
    if n <= 62:
        return bytes([63 + n])
    if n <= 258047:
        width = 3
    elif n <= 68719476735:
        width = 6
    else:
        raise ValueError("graph order too large for graph6")
    return bytes([126]) + bytes(((n >> (6 * i)) & 63) + 63 for i in range(width - 1, -1, -1))


def to_graph6(vertices: Sequence[str], edges: Iterable[Sequence[str]]) -> str:
    """Encode a graph whose vertex order is exactly ``vertices``."""

    n = len(vertices)
    index = {v: i for i, v in enumerate(vertices)}
    if len(index) != n:
        raise ValueError("duplicate vertices")
    bit_count = n * (n - 1) // 2
    padded_count = ((bit_count + 5) // 6) * 6
    bits = [0] * padded_count
    for u, v in edges:
        i, j = sorted((index[u], index[v]))
        if i == j:
            raise ValueError("graph6 cannot encode loops")
        bit = i * n - i * (i + 1) // 2 + j - i - 1
        bits[bit] = 1
    payload = bytearray()
    for start in range(0, padded_count, 6):
        value = 0
        for bit in bits[start:start + 6]:
            value = (value << 1) | bit
        payload.append(value + 63)
    return (_graph6_number(n) + bytes(payload) + b"\n").decode("ascii")


def from_graph6(text: str) -> tuple[int, list[tuple[int, int]]]:
    data = text.strip().encode("ascii")
    if not data:
        raise ValueError("empty graph6")
    if data.startswith(b">>graph6<<"):
        data = data[len(b">>graph6<<"):]
    pos = 0
    # graph6 stores adjacency in consecutive six-bit groups, not bytes.
    if data[pos] == 126:
        pos += 1
        if len(data) < pos + 3:
            raise ValueError("truncated extended graph6 header")
        n = 0
        for _ in range(3):
            n = (n << 6) | (data[pos] - 63)
            pos += 1
    else:
        n = data[pos] - 63
        pos += 1
    expected_bits = n * (n - 1) // 2
    expected_chars = ((expected_bits + 5) // 6)
    payload = data[pos:]
    if len(payload) != expected_chars:
        raise ValueError(f"bad graph6 payload length: expected {expected_chars}, got {len(payload)}")
    bit_values = []
    for char in payload:
        value = char - 63
        if not 0 <= value < 64:
            raise ValueError("invalid graph6 character")
        bit_values.extend((value >> shift) & 1 for shift in range(5, -1, -1))
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            bit = i * n - i * (i + 1) // 2 + j - i - 1
            if bit_values[bit]:
                edges.append((i, j))
    return n, edges


def pynauty_bipartite_certificate(graph: Graph) -> bytes:
    """Return Nauty's certificate with the bipartition as vertex colors."""

    left, right = graph.ordered_for_bipartite_canonicalization()
    ordered = list(left) + list(right)
    index = {v: i for i, v in enumerate(ordered)}
    adjacency = {i: [] for i in range(graph.n)}
    for u, v in graph.edges:
        i, j = sorted((index[u], index[v]))
        adjacency[i].append(j)
        adjacency[j].append(i)
    colored_graph = pynauty.Graph(
        number_of_vertices=graph.n,
        directed=False,
        adjacency_dict=adjacency,
        vertex_coloring=[set(range(len(left))), set(range(len(left), graph.n))],
    )
    return pynauty.certificate(colored_graph)


def nauty_canonical_hash(graph: Graph) -> str:
    digest = hashlib.sha256(pynauty_bipartite_certificate(graph)).hexdigest()
    return f"{len(graph.bipartition[0])}:{len(graph.bipartition[1])}:{digest}"


def rank_potential_solve(graph: Graph, time_limit: float = 60.0, workers: int = 8) -> SolveResult:
    """Primary exact oracle: potentials plus local incidence ranks."""

    started = time.perf_counter()
    model = cp_model.CpModel()
    adj = graph.adjacency()
    degrees = graph.degrees
    bound = sum(d - 1 for d in degrees.values())
    potential = {
        v: model.NewIntVar(-bound, bound, f"a_{i}") for i, v in enumerate(graph.vertices)
    }
    model.Add(potential[graph.vertices[0]] == 0)
    rank: dict[tuple[str, tuple[str, str]], cp_model.IntVar] = {}
    for vi, vertex in enumerate(graph.vertices):
        local = []
        for ni, (neighbor, edge) in enumerate(adj[vertex]):
            var = model.NewIntVar(0, degrees[vertex] - 1, f"p_{vi}_{ni}")
            rank[(vertex, edge)] = var
            local.append(var)
        model.AddAllDifferent(local)
    for u, v in graph.edges:
        edge = (u, v)
        model.Add(potential[u] + rank[(u, edge)] == potential[v] + rank[(v, edge)])
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_search_workers = workers
    solver.parameters.log_search_progress = False
    status_code = solver.Solve(model)
    status_name = solver.StatusName(status_code)
    elapsed = time.perf_counter() - started
    if status_name in ("OPTIMAL", "FEASIBLE"):
        raw = {
            (u, v): int(solver.Value(potential[u] + rank[(u, (u, v))]))
            for u, v in graph.edges
        }
        lo = min(raw.values())
        normalized = {e: value - lo + 1 for e, value in raw.items()}
        ok, reason = verify_coloring(graph, normalized)
        if not ok:
            raise AssertionError(f"solver coloring failed verification: {reason}")
        return SolveResult(
            "colorable",
            "rank-potential-cpsat",
            elapsed,
            normalized,
            max(normalized.values()),
            status_name,
            solver.NumConflicts(),
            solver.NumBranches(),
            solver.WallTime(),
        )
    if status_name in ("INFEASIBLE",):
        return SolveResult(
            "non-colorable", "rank-potential-cpsat", elapsed, None, None, status_name,
            solver.NumConflicts(), solver.NumBranches(), solver.WallTime(),
        )
    return SolveResult("timeout", "rank-potential-cpsat", elapsed, None, None, status_name,
                       solver.NumConflicts(), solver.NumBranches(), solver.WallTime())


def fixed_span_sat_solve(graph: Graph, t: int, time_limit: float = 30.0, workers: int = 8):
    """Independent logical encoding for one prescribed span."""

    model = cp_model.CpModel()
    adj = graph.adjacency()
    degrees = graph.degrees
    x = {}
    s = {}
    for ei, edge in enumerate(map(tuple, graph.edges)):
        for color in range(1, t + 1):
            x[(edge, color)] = model.NewBoolVar(f"x_{ei}_{color}")
        model.AddExactlyOne(x[(edge, c)] for c in range(1, t + 1))
    for vi, vertex in enumerate(graph.vertices):
        d = degrees[vertex]
        starts = range(1, t - d + 2)
        if not starts:
            return "INFEASIBLE", None
        for start in starts:
            s[(vertex, start)] = model.NewBoolVar(f"s_{vi}_{start}")
        model.AddExactlyOne(s[(vertex, k)] for k in starts)
        incident_edges = [(edge, neighbor) for neighbor, edge in adj[vertex]]
        for color in range(1, t + 1):
            vars_here = [x[(edge, color)] for edge, _ in incident_edges]
            if vars_here:
                model.AddAtMostOne(vars_here)
            allowed_starts = range(max(1, color - d + 1), min(color, t - d + 1) + 1)
            cover = [s[(vertex, k)] for k in allowed_starts]
            if vars_here and cover:
                model.Add(sum(cover) == sum(vars_here))
            elif vars_here:
                model.Add(sum(vars_here) == 0)
        for start in starts:
            for oi, (edge, _) in enumerate(incident_edges):
                allowed = [x[(edge, c)] for c in range(start, min(t, start + d - 1) + 1)]
                model.Add(sum(allowed) >= 1).OnlyEnforceIf(s[(vertex, start)])
        for start in starts:
            for offset in range(start, min(t, start + d - 1) + 1):
                active = [x[(edge, offset)] for edge, _ in incident_edges]
                if active:
                    model.Add(sum(active) == 1).OnlyEnforceIf(s[(vertex, start)])
                else:
                    model.Add(s[(vertex, start)] == 0)
    for color in range(1, t + 1):
        model.AddAtLeastOne(x[(tuple(edge), color)] for edge in graph.edges)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_search_workers = workers
    status_name = solver.StatusName(solver.Solve(model))
    if status_name in ("OPTIMAL", "FEASIBLE"):
        coloring = {}
        for edge in map(tuple, graph.edges):
            values = [c for c in range(1, t + 1) if solver.Value(x[(edge, c)])]
            if len(values) != 1:
                raise AssertionError("bad extracted coloring")
            coloring[edge] = values[0]
        ok, reason = verify_coloring(graph, coloring)
        if not ok:
            raise AssertionError(f"time-slot coloring failed verification: {reason}")
        return status_name, coloring
    return status_name, None


def all_spans_solve(
    graph: Graph, time_limit_per_span: float = 30.0, workers: int = 8, stop_on_timeout: bool = True
) -> dict:
    results = {}
    overall = "non-colorable"
    for t in range(graph.delta, max(graph.delta, graph.n - 1) + 1):
        name, coloring = fixed_span_sat_solve(graph, t, time_limit_per_span, workers)
        results[t] = {"status": name, "coloring": {str(e): c for e, c in (coloring or {}).items()}}
        if name in ("OPTIMAL", "FEASIBLE"):
            overall = "colorable"
            break
        if name == "UNKNOWN" and stop_on_timeout:
            overall = "timeout"
            break
    return {"encoding": "fixed-span-sat", "decision": overall, "spans": results}


def weighted_hub_statistics(graph: Graph) -> list[dict]:
    result = []
    degrees = graph.degrees
    for h in graph.vertices:
        neighbors = list(graph.adjacency()[h])
        if len(neighbors) < 2:
            continue
        weights = {v: degrees[v] - 1 for v in graph.vertices}
        best = {}
        for source_index, (source, _) in enumerate(neighbors):
            pq = [(weights[source], source)]
            dist = {source: weights[source]}
            while pq:
                distance, u = heapq.heappop(pq)
                if distance != dist[u]:
                    continue
                for v, _ in graph.adjacency()[u]:
                    if v == h or distance + weights[v] >= dist.get(v, math.inf):
                        continue
                    dist[v] = distance + weights[v]
                    heapq.heappush(pq, (dist[v], v))
            for target, _ in neighbors[source_index + 1:]:
                if target in dist:
                    best[frozenset((source, target))] = dist[target]
        diameter = max(best.values(), default=-math.inf)
        margin = degrees[h] - 1 - diameter
        result.append(
            {
                "hub": h,
                "degree": degrees[h],
                "weighted_neighbor_diameter": None if diameter == -math.inf else diameter,
                "margin": margin,
                "sufficient_obstruction": margin > 0,
            }
        )
    return sorted(result, key=lambda row: (-row["margin"], row["hub"]))


def modular_relaxation(graph: Graph, q: int, time_limit: float = 10.0, workers: int = 8) -> str:
    model = cp_model.CpModel()
    adj = graph.adjacency()
    degrees = graph.degrees
    potential = {v: model.NewIntVar(0, q - 1, f"a_{v}_{q}") for v in graph.vertices}
    model.Add(potential[graph.vertices[0]] == 0)
    rank = {}
    for v in graph.vertices:
        local = []
        for ni, (neighbor, edge) in enumerate(adj[v]):
            var = model.NewIntVar(0, degrees[v] - 1, f"rp_{v}_{ni}_{q}")
            rank[(v, edge)] = var
            local.append(var)
        model.AddAllDifferent(local)
    for u, v in graph.edges:
        residue = model.NewIntVar(0, q - 1, f"r_{u}_{v}_{q}")
        model.AddModuloEquality(
            residue,
            potential[u] + rank[(u, (u, v))] - potential[v] - rank[(v, (u, v))],
            q,
        )
        model.Add(residue == 0)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = workers
    name = solver.StatusName(solver.Solve(model))
    if name in ("OPTIMAL", "FEASIBLE"):
        return "satisfiable"
    if name == "INFEASIBLE":
        return "unsatisfiable"
    return "timeout"


def complete_bipartite(left: int, right: int, name: str | None = None) -> Graph:
    xs = [f"x{i}" for i in range(left)]
    ys = [f"y{i}" for i in range(right)]
    edges = [(x, y) for x in xs for y in ys]
    return Graph(xs + ys, edges, [xs, ys], {"family": "complete-bipartite", "name": name})


def path_tree(n: int) -> Graph:
    vs = [f"v{i}" for i in range(n)]
    return Graph(vs, [(vs[i], vs[i + 1]) for i in range(n - 1)], metadata={"family": "path"})


def random_regular_bipartite(n: int, degree: int, seed: int) -> Graph:
    rng = random.Random(seed)
    xs = [f"x{i}" for i in range(n)]
    ys = [f"y{i}" for i in range(n)]
    left_stubs = xs * degree
    right_stubs = ys * degree
    while True:
        rng.shuffle(left_stubs)
        rng.shuffle(right_stubs)
        pairs = list(zip(left_stubs, right_stubs))
        if len(set(pairs)) == n * degree:
            return Graph(xs + ys, pairs, [xs, ys], {"family": "random-regular", "seed": seed})


def delta_construction(r: int, s: int, t: int) -> Graph:
    """Delta(r,s,t) from Petrosyan-Khachatrian (2014)."""
    vertices = ["v", "x", "y", "z"]
    groups = [("a", r, ("v", "x", "y")), ("b", s, ("v", "x", "z")), ("c", t, ("v", "y", "z"))]
    middle = []
    edges = []
    for prefix, count, hubs in groups:
        for i in range(count):
            w = f"{prefix}{i}"
            middle.append(w)
            vertices.append(w)
            edges.extend((w, h) for h in hubs)
    return Graph(vertices, edges, [["v", "x", "y", "z"], middle],
                 {"family": "delta-construction", "parameters": [r, s, t], "name": f"M{r}" if r == s == t else None})


def erd_projective_plane(multiplicities: Sequence[int], plane_order: int = 2) -> Graph:
    """Erd(r_i) over an explicit Fano plane when plane_order=2."""
    if plane_order != 2 or len(multiplicities) != 7:
        raise NotImplementedError("only the order-two plane is implemented")
    points = [f"P{i}" for i in range(7)]
    lines = [
        {0, 1, 2}, {0, 3, 4}, {0, 5, 6},
        {1, 3, 5}, {1, 4, 6}, {2, 3, 6}, {2, 4, 5},
    ]
    vertices = ["u"] + points
    twins = []
    edges = []
    twin_number = 0
    for point_index, multiplicity in enumerate(multiplicities):
        for copy in range(multiplicity):
            twin = f"W{twin_number}"
            twin_number += 1
            twins.append(twin)
            vertices.append(twin)
            edges.append(("u", twin))
            for p_index, p in enumerate(points):
                if p_index in lines[point_index]:
                    edges.append((twin, p))
    return Graph(vertices, edges, [["u"] + points, twins],
                 {"family": "projective-plane", "multiplicities": list(multiplicities)})


def hat_complete_graph(parts: Sequence[Sequence[int]], delete_hub_edge: str | None = None) -> Graph:
    """Hat construction applied to a complete multipartite core."""
    core_vertices = []
    for part_index, part in enumerate(parts):
        core_vertices.extend(f"V{part_index}_{value}" for value in part)
    pairs = []
    for (pi, vi), (pj, vj) in itertools.combinations(enumerate(parts), 2):
        for a in vi:
            for b in vj:
                pairs.append((f"V{pi}_{a}", f"V{pj}_{b}"))
    excluded = set()
    if delete_hub_edge is not None:
        endpoints = delete_hub_edge.split("|")
        if len(endpoints) != 2:
            raise ValueError("delete_hub_edge must use endpoint|endpoint")
        excluded.add(tuple(sorted(endpoints)))
    connectors = []
    vertices = ["u"] + core_vertices
    edges = []
    for number, pair in enumerate(pairs):
        connector = f"C{number}"
        connectors.append(connector)
        vertices.append(connector)
        edges.extend([(connector, pair[0]), (connector, pair[1]), ("u", connector)])
    left = ["u"] + core_vertices
    right = connectors
    return Graph(vertices, edges, [left, right],
                 {"family": "hat-complete-multipartite", "deleted_hub_edge": delete_hub_edge})


def benchmark_graphs() -> dict[str, Graph]:
    graphs = {}
    graphs["M5_delta_555"] = delta_construction(5, 5, 5)
    multiplicities = [2, 2, 2, 2, 2, 2, 1]
    graphs["Erd_Fano_2222221"] = erd_projective_plane(multiplicities)
    graphs["hat_K34"] = hat_complete_graph([[1, 2, 3], [4, 5, 6, 7]])
    base = hat_complete_graph([[1, 2, 3], [4, 5, 6, 7]])
    hub_edge = next(edge for edge in base.edges if "u" in edge)
    removed = {(hub_edge[0], hub_edge[1]): True}
    # Rebuild with a marker that removes one connector's hub edge.
    vertices = list(base.vertices)
    edges = [edge for edge in base.edges if edge != tuple(sorted(hub_edge))]
    graphs["hat_K34_prime_Delta11"] = Graph(
        vertices, edges, base.bipartition,
        {"family": "hat-complete-multipartite", "deleted_hub_edge": "|".join(hub_edge)},
    )
    del removed
    graphs["hat_K222"] = hat_complete_graph(
        [[1, 2], [3, 4], [5, 6]]
    )
    return graphs


def positive_controls(seed: int = 1729) -> dict[str, Graph]:
    controls = {}
    controls["path_10"] = path_tree(10)
    controls["K_2_5"] = complete_bipartite(2, 5)
    controls["K_3_6"] = complete_bipartite(3, 6)
    controls["random_regular_3_10"] = random_regular_bipartite(10, 3, seed)
    controls["random_regular_5_10"] = random_regular_bipartite(10, 5, seed + 1)
    return controls


def run_benchmark(
    graphs: dict[str, Graph], time_limit_a: float = 60.0, workers: int = 8, save_dir: str | None = None
) -> list[dict]:
    rows = []
    for name, graph in graphs.items():
        result = rank_potential_solve(graph, time_limit=time_limit_a, workers=workers)
        stats = weighted_hub_statistics(graph)
        modular = {str(q): modular_relaxation(graph, q, min(20.0, time_limit_a), workers) for q in (2, 3, 5)}
        row = {
            "name": name,
            "order": graph.n,
            "size": graph.m,
            "delta": graph.delta,
            "degrees": graph.degrees,
            "sha256_labelled": graph.canonical_hash(),
            "rank_potential": {
                key: value for key, value in result.__dict__.items()
            },
            "weighted_hubs_best": stats[:3],
            "modular": modular,
        }
        if result.coloring:
            row["verified"] = verify_coloring(graph, {tuple(e): c for e, c in result.coloring.items()})
        rows.append(row)
        if save_dir:
            out = Path(save_dir)
            out.mkdir(parents=True, exist_ok=True)
            graph.save(out / f"{name}.graph.json")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["solve", "spans", "stats", "benchmark", "selftest"])
    parser.add_argument("input", nargs="?", help="JSON graph file")
    parser.add_argument("--time-limit", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--save-dir", default=None)
    args = parser.parse_args()

    if args.command == "benchmark":
        graphs = benchmark_graphs()
        print(json.dumps(run_benchmark(graphs, args.time_limit, args.workers, args.save_dir), indent=2))
        return
    if args.command == "selftest":
        controls = positive_controls()
        for name, graph in controls.items():
            result = rank_potential_solve(graph, args.time_limit, args.workers)
            print(name, result.status, result.span, f"{result.elapsed_seconds:.3f}s")
            if result.status != "colorable":
                raise SystemExit(1)
        benchmarks = benchmark_graphs()
        expected = {"M5_delta_555", "Erd_Fano_2222221", "hat_K34", "hat_K34_prime_Delta11", "hat_K222"}
        for name in sorted(expected):
            result = rank_potential_solve(benchmarks[name], args.time_limit, args.workers)
            print(name, result.status, result.solver_status, f"{result.elapsed_seconds:.3f}s",
                  result.conflicts, result.branches)
            if result.status != "non-colorable":
                raise SystemExit(1)
        return
    if not args.input:
        parser.error("this command requires a JSON graph input")
    graph = Graph.from_json(json.loads(Path(args.input).read_text()))
    if args.command == "stats":
        print(json.dumps(weighted_hub_statistics(graph), indent=2))
    elif args.command == "solve":
        result = rank_potential_solve(graph, args.time_limit, args.workers)
        print(json.dumps(result.__dict__, indent=2))
    elif args.command == "spans":
        print(json.dumps(all_spans_solve(graph, args.time_limit, args.workers), indent=2))


if __name__ == "__main__":
    main()
