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
MIDDLE_GROUP_COLORS = {
    "V0_1": "#3D6FA8",
    "V0_2": "#C86A1D",
    "V0_3": "#3E8452",
}
BOTTOM_GROUP_COLORS = {
    "V0": "#8FBBD9",
    "V1": "#E8C564",
}


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


def layered_positions(graph: Graph):
    """Place u, N(u), and the remaining core endpoints in three wide rows."""

    middle = sorted(graph._nx.neighbors("u"))
    bottom = sorted(v for v in graph.bipartition[0] if v != "u")
    pos = {"u": (0.5, 0.96)}
    middle_x = np.linspace(0.035, 0.965, len(middle))
    pos.update((vertex, (x, 0.58)) for vertex, x in zip(middle, middle_x))
    bottom_x = np.linspace(0.05, 0.95, len(bottom))
    pos.update((vertex, (x, 0.09)) for vertex, x in zip(bottom, bottom_x))
    return pos


def middle_group(vertex: str, graph: Graph, merged_name: str) -> str:
    if vertex == merged_name:
        return "merged"
    core_neighbors = [
        neighbor for neighbor in graph._nx.neighbors(vertex)
        if neighbor != "u" and neighbor.startswith("V0_")
    ]
    return sorted(core_neighbors)[0]


def draw_full_graph(ax: plt.Axes, graph: Graph, merged_name: str) -> None:
    pos = layered_positions(graph)
    middle = sorted(graph._nx.neighbors("u"))
    bottom = sorted(v for v in graph.bipartition[0] if v != "u")
    colors = {"u": HUB_COLOR}
    for vertex in middle:
        group = middle_group(vertex, graph, merged_name)
        colors[vertex] = MERGED_COLOR if group == "merged" else MIDDLE_GROUP_COLORS[group]
    for vertex in bottom:
        colors[vertex] = BOTTOM_GROUP_COLORS[vertex.rsplit("_", 1)[0]]

    ax.axhspan(0.82, 1.03, facecolor="#F4DAD8", alpha=0.42, zorder=-3)
    ax.axhspan(0.40, 0.74, facecolor="#E8EFF5", alpha=0.48, zorder=-3)
    ax.axhspan(-0.02, 0.25, facecolor="#F1EBDA", alpha=0.48, zorder=-3)
    ax.text(0.005, 0.995, "HUB", fontsize=8.5, weight="bold", color="#8E332F", va="top")
    ax.text(0.005, 0.715, "ELEVEN VERTICES ADJACENT TO u", fontsize=8.5,
            weight="bold", color="#37536A", va="top")
    ax.text(0.005, 0.225, "CORE ENDPOINTS REACHED THROUGH THOSE ELEVEN",
            fontsize=8.5, weight="bold", color="#6C5A29", va="top")

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
        edge_color=EDGE_COLOR, width=1.05, alpha=0.46
    )
    nx.draw_networkx_edges(
        nx_graph, pos, edgelist=edge_sets["hub"], ax=ax,
        edge_color=HUB_COLOR, width=2.1, alpha=0.84
    )
    nx.draw_networkx_edges(
        nx_graph, pos, edgelist=edge_sets["merged"], ax=ax,
        edge_color=MERGED_COLOR, width=2.5, alpha=1.0
    )
    nx.draw_networkx_nodes(
        nx_graph, pos, ax=ax,
        node_size=[1300 if v == "u" else 650 for v in graph.vertices],
        node_color=[colors[v] for v in graph.vertices], edgecolors="white", linewidths=1.1
    )
    ux, uy = pos["u"]
    ax.add_patch(plt.Circle((ux, uy), 0.024, facecolor=HUB_COLOR, edgecolor="white",
                            linewidth=1.5, zorder=4))
    for vertex, (x, y) in pos.items():
        above = y > 0.7
        below = y < 0.25
        if vertex == "u":
            ax.text(x, y - 0.075, "u", ha="center", va="center", fontsize=9.0,
                    weight="bold", color="#7C2723")
            continue
        ax.text(
            x, y + (0.065 if above else -0.072 if below else 0),
            vertex, ha="center", va="center", fontsize=8.0, color="#25313D",
            bbox={"boxstyle": "round,pad=0.17", "fc": "white", "ec": "#D5DCE2", "lw": 0.5},
        )
    ax.set_title("Three-layer construction view", fontsize=16, weight="bold", loc="left")
    legend = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor=HUB_COLOR, markersize=9, label="Hub u"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=MERGED_COLOR, markersize=9, label=f"Merged vertex {merged_name}"),
        *[Line2D([0], [0], marker="o", color="none", markerfacecolor=color, markersize=8,
                 label=f"Connector via {group}") for group, color in MIDDLE_GROUP_COLORS.items()],
        Patch(facecolor=BOTTOM_GROUP_COLORS["V0"], edgecolor="#9BA9B4", label="V0 core endpoint"),
        Patch(facecolor=BOTTOM_GROUP_COLORS["V1"], edgecolor="#9BA9B4", label="V1 core endpoint"),
        Line2D([0], [0], color=HUB_COLOR, lw=2, label="Hub incidence"),
        Line2D([0], [0], color=MERGED_COLOR, lw=2, label="Merged incidence"),
    ]
    ax.text(
        1.0, 0.995,
        f"{graph.n} vertices, {graph.m} edges, maximum degree {graph.delta}. The graph is bipartite; "
        "this drawing separates the hub, its neighbors, and their core endpoints.",
        transform=ax.transAxes, fontsize=9.0, color="#43535F", ha="right", va="top",
    )
    ax.legend(handles=legend, loc="upper center", bbox_to_anchor=(0.5, -0.035),
              frameon=True, fontsize=7.8, ncols=5)
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.13, 1.05)
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
