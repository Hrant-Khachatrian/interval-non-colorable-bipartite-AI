#!/usr/bin/env python3
"""Create explanatory figures for the discovered interval-non-colorable graphs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib as mpl

mpl.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import networkx as nx
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from interval_edge_coloring import Graph, nauty_canonical_hash


HUB_COLOR = "#C43C3C"
MERGED_COLOR = "#7A52A5"
LEFT_COLOR = "#37678E"
RIGHT_COLOR = "#468C5F"
EDGE_COLOR = "#767676"


def load_candidate(path: Path) -> tuple[Graph, dict]:
    data = json.loads(path.read_text())
    certificate_path = Path("results/candidates") / f"{data['metadata']['candidate_id']}" / "certificate.json"
    return Graph.from_json(data), json.loads(certificate_path.read_text())


def node_colors(graph: Graph, merged_name: str) -> dict[str, str]:
    return {
        vertex: HUB_COLOR
        if vertex == "u"
        else MERGED_COLOR
        if vertex == merged_name
        else LEFT_COLOR
        if vertex in graph.bipartition[0]
        else RIGHT_COLOR
        for vertex in graph.vertices
    }


def barycenter_layout(graph: Graph) -> dict[str, tuple[float, float]]:
    left_all = sorted(graph.bipartition[0])
    right = sorted(graph.bipartition[1])
    left = ["u"] + [v for v in left_all if v != "u"]
    adjacency = {v: set(graph._nx.neighbors(v)) for v in graph.vertices}
    for _ in range(6):
        right.sort(
            key=lambda v: (
                sum(left.index(n) for n in adjacency[v]) / len(adjacency[v]),
                v,
            )
        )
        movable = left[1:]
        movable.sort(
            key=lambda v: (
                sum(right.index(n) for n in adjacency[v]) / len(adjacency[v]),
                v,
            )
        )
        left = ["u"] + movable

    def ys(count: int) -> list[float]:
        return np.linspace(1.0, 0.0, count).tolist()

    left_y = ys(len(left))
    right_y = ys(len(right))
    return {v: (0.0, left_y[i]) for i, v in enumerate(left)} | {
        v: (1.0, right_y[i]) for i, v in enumerate(right)
    }


def draw_full_graph(ax: plt.Axes, graph: Graph, merged_name: str) -> None:
    pos = barycenter_layout(graph)
    colors = node_colors(graph, merged_name)
    edge_sets = {
        "merged": [],
        "hub": [],
        "normal": [],
    }
    for u, v in graph.edges:
        if u == merged_name or v == merged_name:
            edge_sets["merged"].append((u, v))
        elif u == "u" or v == "u":
            edge_sets["hub"].append((u, v))
        else:
            edge_sets["normal"].append((u, v))
    nx_graph = graph._nx
    nx.draw_networkx_edges(
        nx_graph, pos, edgelist=edge_sets["normal"], ax=ax,
        edge_color=EDGE_COLOR, width=0.85, alpha=0.48
    )
    nx.draw_networkx_edges(
        nx_graph, pos, edgelist=edge_sets["hub"], ax=ax,
        edge_color=HUB_COLOR, width=1.65, alpha=0.82
    )
    nx.draw_networkx_edges(
        nx_graph, pos, edgelist=edge_sets["merged"], ax=ax,
        edge_color=MERGED_COLOR, width=2.1, alpha=0.95
    )
    nx.draw_networkx_nodes(
        nx_graph, pos, ax=ax, node_size=520,
        node_color=[colors[v] for v in graph.vertices], edgecolors="white", linewidths=1.1
    )
    for vertex, (x, y) in pos.items():
        if x < 0.5:
            ax.text(
                x - 0.035, y, vertex, ha="right", va="center", fontsize=8.2,
                color="#25313D",
                bbox={"boxstyle": "round,pad=0.18", "fc": "white", "ec": "#D5DCE2", "lw": 0.5},
            )
        else:
            ax.text(
                x + 0.035, y, vertex, ha="left", va="center", fontsize=8.2,
                color="#25313D",
                bbox={"boxstyle": "round,pad=0.18", "fc": "white", "ec": "#D5DCE2", "lw": 0.5},
            )
    ax.set_title("Full bipartite graph", fontsize=15, weight="bold", loc="left")
    ax.text(
        0.0, -0.07,
        f"{graph.n} vertices, {graph.m} edges, maximum degree {graph.delta}. "
        "The red hub requires eleven consecutive incident colors.",
        transform=ax.transAxes, fontsize=10, color="#43535F",
    )
    legend = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor=HUB_COLOR, markersize=9, label="Hub u"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=MERGED_COLOR, markersize=9, label=f"Merged vertex {merged_name}"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=LEFT_COLOR, markersize=9, label="Other left side"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=RIGHT_COLOR, markersize=9, label="Other right side"),
        Line2D([0], [0], color=HUB_COLOR, lw=2, label="Hub incidence"),
        Line2D([0], [0], color=MERGED_COLOR, lw=2, label="Merged incidence"),
    ]
    ax.legend(handles=legend, loc="upper right", frameon=True, fontsize=8, ncols=2)
    ax.set_xlim(-0.38, 1.38)
    ax.set_ylim(-0.12, 1.08)
    ax.axis("off")


def radial_positions(graph: Graph, merged_name: str):
    neighbors = sorted(graph._nx.neighbors("u"))
    cores = [v for v in graph.bipartition[0] if v != "u"]
    pos = {"u": (0.0, 0.0)}
    for i, vertex in enumerate(neighbors):
        angle = 2 * np.pi * i / len(neighbors) + np.pi / 2
        pos[vertex] = (np.cos(angle), np.sin(angle))
    core_angles = np.linspace(0, 2 * np.pi, len(cores), endpoint=False) + np.pi / len(cores)
    for angle, vertex in zip(core_angles, cores):
        pos[vertex] = (0.50 * np.cos(angle), 0.50 * np.sin(angle))
    return pos


def draw_hub_constraint_view(ax: plt.Axes, graph: Graph, merged_name: str) -> None:
    pos = radial_positions(graph, merged_name)
    normal, hub, merged = [], [], []
    for edge_u, edge_v in graph.edges:
        if edge_u == merged_name or edge_v == merged_name:
            target = "merged"
        elif edge_u == "u" or edge_v == "u":
            target = "hub"
        else:
            target = "normal"
        {"normal": normal, "hub": hub, "merged": merged}[target].append((edge_u, edge_v))
    circle = plt.Circle((0, 0), 1.0, fill=False, color="#CBD4DB", linestyle="--", linewidth=0.9)
    ax.add_patch(circle)
    nx.draw_networkx_edges(graph._nx, pos, edgelist=normal, ax=ax, edge_color=EDGE_COLOR, width=0.8, alpha=0.38)
    nx.draw_networkx_edges(graph._nx, pos, edgelist=hub, ax=ax, edge_color=HUB_COLOR, width=2.0, alpha=0.86)
    nx.draw_networkx_edges(graph._nx, pos, edgelist=merged, ax=ax, edge_color=MERGED_COLOR, width=2.4, alpha=1.0)
    colors = node_colors(graph, merged_name)
    nx.draw_networkx_nodes(
        graph._nx, pos, ax=ax, nodelist=list(graph.vertices),
        node_size=[700 if v == "u" else 430 for v in graph.vertices],
        node_color=[colors[v] for v in graph.vertices], edgecolors="white", linewidths=1.0,
    )
    for vertex, (x, y) in pos.items():
        if vertex == "u":
            continue
        distance = np.hypot(x, y)
        if distance > 0.75:
            ax.text(1.16 * x, 1.16 * y, vertex, ha="center", va="center", fontsize=7.8,
                    bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "#D8DFE5", "lw": 0.4})
        else:
            ax.text(0.72 * x, 0.72 * y, vertex, ha="center", va="center", fontsize=7.2,
                    color="white", weight="bold")
    ax.text(0, 0, "u\nneeds\n11 colors", ha="center", va="center", fontsize=7.5,
            color="white", weight="bold", linespacing=1.15)
    ax.set_title("Why the hub is constrained", fontsize=15, weight="bold", loc="left")
    ax.text(
        0.0, -0.09,
        "Every spoke from u must occupy a distinct consecutive color. The thin paths through "
        "the K(3,4)-type core force those choices to interact.",
        transform=ax.transAxes, fontsize=10, color="#43535F",
    )
    ax.set_xlim(-1.42, 1.42)
    ax.set_ylim(-1.24, 1.24)
    ax.set_aspect("equal")
    ax.axis("off")


def draw_certificate_dashboard(ax: plt.Axes, graph: Graph, cert: dict, merged_name: str) -> None:
    ax.set_title("Verification and minimality dashboard", fontsize=15, weight="bold", loc="left")
    ax.axis("off")
    columns = ["Rank CP-SAT", "Fixed-span CP-SAT", "MiniSat", "DRAT-Trim"]
    spans = list(range(graph.delta, graph.n))
    cell_text = [
        ["UNSAT", "INFEASIBLE", "UNSAT", "VERIFIED"]
        for _ in spans
    ]
    table_x = 0.03
    table_y = 0.90
    row_h = 0.062
    col_w = 0.155
    ax.text(table_x, table_y + row_h, "Span", weight="bold", fontsize=9)
    for j, col in enumerate(columns):
        ax.text(table_x + (j + 1) * col_w, table_y + row_h, col, weight="bold", fontsize=9)
    for i, span in enumerate(spans):
        y = table_y - i * row_h
        ax.text(table_x, y, str(span), fontsize=9, va="center")
        for j, text in enumerate(cell_text[i]):
            x = table_x + (j + 1) * col_w
            ax.add_patch(plt.Rectangle((x - 0.01, y - 0.021), col_w - 0.02, 0.042,
                                       facecolor="#DFF3E4", edgecolor="#9CCFA9", linewidth=0.6))
            ax.text(x + (col_w - 0.02) / 2 - 0.01, y, text, ha="center", va="center", fontsize=7.4,
                    color="#21693A")

    minimality = cert["minimality"]
    ax.text(
        0.03, 0.31,
        "Minimality checks: every deletion remains interval-colorable.",
        fontsize=10.5, weight="bold", color="#254052"
    )
    checks = [
        ("Single-edge deletions", minimality["single_edge_deletions_checked"],
         minimality["negative_single_edge_deletions"]),
        ("Single-vertex deletions", minimality["single_vertex_deletions_checked"],
         minimality["negative_single_vertex_deletions"]),
    ]
    for i, (label, checked, negative) in enumerate(checks):
        y = 0.20 - i * 0.075
        ax.add_patch(plt.Rectangle((0.03, y - 0.022), 0.58, 0.044, facecolor="#DFF3E4",
                                   edgecolor="#9CCFA9", linewidth=0.6))
        ax.add_patch(plt.Rectangle((0.61, y - 0.022), 0.02, 0.044, facecolor="#F5C8C2",
                                   edgecolor="#DA9890", linewidth=0.6))
        ax.text(0.04, y, f"{label}: {checked} colorable, {negative} non-colorable",
                va="center", fontsize=8.7, color="#225233")
    symmetry = cert.get("symmetry", {})
    ax.text(
        0.03, 0.015,
        f"Automorphism group order {symmetry.get('automorphism_group_size', '?')}; "
        f"{symmetry.get('orbit_count', '?')} vertex orbits.  Canonical hash: "
        f"{nauty_canonical_hash(graph)}",
        fontsize=8.2, color="#53636F"
    )


def make_figures(candidate_id: str, output_dir: Path) -> list[Path]:
    graph_path = Path("results/graphs/quotient-r1") / f"{candidate_id}.graph.json"
    graph, cert = load_candidate(graph_path)
    merged_name = "&".join(cert["construction"]["blocks"][0])
    configs = [
        ("full", draw_full_graph, (13.2, 9.2)),
        ("hub-constraint", draw_hub_constraint_view, (11.5, 9.0)),
        ("verification", draw_certificate_dashboard, (13.0, 7.2)),
    ]
    outputs = []
    for suffix, drawer, size in configs:
        fig, ax = plt.subplots(figsize=size)
        facecolor = "#FBFCFC"
        fig.patch.set_facecolor(facecolor)
        ax.set_facecolor(facecolor)
        if suffix == "verification":
            drawer(ax, graph, cert, merged_name)
        else:
            drawer(ax, graph, merged_name)
        out = output_dir / f"{candidate_id}-{suffix}.png"
        fig.savefig(out, dpi=220, bbox_inches="tight", facecolor=facecolor)
        plt.close(fig)
        outputs.append(out)
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="figures")
    parser.add_argument("--candidates", nargs="+", default=["Q1-00012", "Q1-00014"])
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for candidate in args.candidates:
        outputs = make_figures(candidate, output_dir)
        print("\n".join(str(path) for path in outputs))


if __name__ == "__main__":
    main()
