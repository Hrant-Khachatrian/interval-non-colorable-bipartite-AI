#!/usr/bin/env python3
"""Run one independent verification workflow for a candidate graph.

The workflow combines the global rank-potential CP-SAT model, fixed-span CP-SAT,
exported DIMACS, MiniSat when installed, and PicoSAT/DRAT-Trim proofs when the
bundled tools are available.  It writes evidence and a JSON manifest under a
separate workflow directory so existing candidate bundles remain untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from formal_proof_verify import verify_cnf as generate_and_check_drat
from interval_edge_coloring import (
    Graph,
    SolveResult,
    complete_bipartite,
    fixed_span_sat_solve,
    nauty_canonical_hash,
    rank_potential_solve,
    verify_coloring,
)
from minisat_verify import fixed_span_cnf, write_dimacs


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "workflow-manifest.json"


@dataclass(frozen=True)
class ToolPaths:
    minisat: Path | None
    picosat: Path | None
    drat_trim: Path | None


@dataclass(frozen=True)
class RunLimits:
    rank_time_limit: float
    span_time_limit: float
    minisat_time_limit: float
    workers: int


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def resolve_tool(specifier: str, root: Path = ROOT) -> Path | None:
    candidate = Path(specifier)
    if not candidate.is_absolute():
        rooted = root / candidate
        if rooted.is_file():
            return rooted.resolve()
    if candidate.is_file():
        return candidate.resolve()
    located = shutil.which(specifier)
    return Path(located).resolve() if located else None


def graph_is_connected(graph: Graph) -> bool:
    if not graph.vertices:
        return False
    adjacency = graph.adjacency()
    seen = {graph.vertices[0]}
    pending = [graph.vertices[0]]
    while pending:
        vertex = pending.pop()
        for neighbor, _ in adjacency[vertex]:
            if neighbor not in seen:
                seen.add(neighbor)
                pending.append(neighbor)
    return len(seen) == graph.n


def validate_graph(graph: Graph, raw_graph: dict[str, Any], input_path: Path) -> dict[str, Any]:
    if not graph.edges:
        raise ValueError("graph has no edges; interval non-colorability is undefined")
    if not graph_is_connected(graph):
        raise ValueError("workflow requires a connected graph so n-1 is an exhaustive span bound")

    labelled_hash = graph.canonical_hash()
    canonical_hash = nauty_canonical_hash(graph)
    stored_labelled = raw_graph.get("sha256_labelled")
    stored_canonical = raw_graph.get("sha256_bipartition_canonical")
    if stored_labelled is not None and stored_labelled != labelled_hash:
        raise ValueError("stored sha256_labelled does not match the supplied graph")
    if stored_canonical is not None and stored_canonical != canonical_hash:
        raise ValueError("stored bipartition-canonical hash does not match the supplied graph")

    degrees = graph.degrees
    return {
        "order": graph.n,
        "size": graph.m,
        "maximum_degree": graph.delta,
        "minimum_degree": min(degrees.values()),
        "degrees": degrees,
        "bipartition_sizes": [len(graph.bipartition[0]), len(graph.bipartition[1])],
        "labelled_sha256": labelled_hash,
        "bipartition_canonical_sha256": canonical_hash,
        "input_file": str(input_path.resolve()),
        "input_file_sha256": sha256_path(input_path),
    }


def rank_report(graph: Graph, result: SolveResult) -> dict[str, Any]:
    coloring_verified = False
    if result.coloring is not None:
        coloring_verified = verify_coloring(graph, dict(result.coloring))[0]
        if not coloring_verified:
            raise AssertionError("rank-potential witness failed verify_coloring")
    return {
        "encoding": result.encoding,
        "status": result.status,
        "solver_status": result.solver_status,
        "witness_span": result.span,
        "coloring_verified": coloring_verified,
        "elapsed_seconds": result.elapsed_seconds,
        "covers_all_legal_spans": result.status == "non-colorable",
        "scope_explanation": (
            "An infeasible unbounded rank-potential model excludes every span at once; "
            "a feasible model supplies one witness span."
        ),
    }


def run_minisat(cnf: Path, executable: Path, time_limit: float) -> dict[str, Any]:
    result_path = Path(str(cnf) + ".result")
    started = time.monotonic()
    try:
        process = subprocess.run(
            [str(executable), "-verb=0", str(cnf), str(result_path)],
            capture_output=True,
            text=True,
            timeout=time_limit,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "TIMEOUT",
            "returncode": None,
            "result_file": str(result_path),
            "result_sha256": sha256_path(result_path) if result_path.exists() else None,
            "elapsed_seconds": time.monotonic() - started,
            "stderr_excerpt": (exc.stderr or "")[-2000:],
            "gap": True,
        }

    if process.returncode == 20:
        status = "UNSATISFIABLE"
    elif process.returncode == 10:
        status = "SATISFIABLE"
    else:
        status = f"ERROR_{process.returncode}"
    return {
        "status": status,
        "returncode": process.returncode,
        "result_file": str(result_path),
        "result_sha256": sha256_path(result_path) if result_path.exists() else None,
        "elapsed_seconds": time.monotonic() - started,
        "stderr_excerpt": process.stderr[-2000:],
        "gap": status not in {"SATISFIABLE", "UNSATISFIABLE"},
    }


def verify_minisat_witness(
    result_path: Path,
    variables: dict[str, int],
    edge_list: list[tuple[str, str]],
    span: int,
) -> tuple[bool, str]:
    if not result_path.exists():
        return False, "MiniSat model file is missing"
    assignments: dict[int, bool] = {}
    for line in result_path.read_text().splitlines():
        tokens = line.split()
        if tokens and tokens[0] == "SAT":
            continue
        for token in tokens:
            try:
                literal = int(token)
            except ValueError:
                continue
            if literal == 0:
                continue
            assignments[abs(literal)] = literal > 0

    coloring: dict[tuple[str, str], int] = {}
    for edge_index, edge in enumerate(edge_list):
        selected = []
        for color in range(1, span + 1):
            variable = variables[f"x_{edge_index}_{color}"]
            if assignments.get(variable, False):
                selected.append(color)
        if len(selected) != 1:
            return False, f"expected one color for edge {edge}, found {selected}"
        coloring[edge] = selected[0]
    ok, reason = verify_coloring_from_edges(edge_list, coloring)
    return ok, reason


def verify_coloring_from_edges(
    edge_list: list[tuple[str, str]],
    coloring: dict[tuple[str, str], int],
) -> tuple[bool, str]:
    vertices = sorted({vertex for edge in edge_list for vertex in edge})
    graph = Graph(vertices, edge_list)
    return verify_coloring(graph, coloring)


def drat_report(cnf: Path, picosat: Path, drat_trim: Path) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = generate_and_check_drat(cnf, str(picosat), str(drat_trim))
    except Exception as exc:  # The manifest must retain tool failures rather than lose them.
        return {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc)[-4000:],
            "elapsed_seconds": time.monotonic() - started,
            "gap": True,
        }
    expected_hash = sha256_path(cnf)
    if result.get("cnf_sha256") != expected_hash:
        result["status"] = "ERROR"
        result["gap"] = True
        result["error"] = "DRAT helper returned a CNF hash mismatch"
    result["elapsed_seconds"] = result.get("elapsed_seconds", time.monotonic() - started)
    return result


def add_gap(gaps: list[dict[str, Any]], code: str, detail: str) -> None:
    entry = {"code": code, "detail": detail}
    if entry not in gaps:
        gaps.append(entry)


def tracked_evidence(workdir: Path, paths: set[Path]) -> list[dict[str, str]]:
    evidence = []
    for path in sorted(paths):
        if path.is_file():
            evidence.append(
                {
                    "path": path.relative_to(workdir).as_posix(),
                    "sha256": sha256_path(path),
                }
            )
    return evidence


def run_workflow(
    input_path: Path,
    workdir: Path,
    limits: RunLimits,
    tools: ToolPaths,
) -> tuple[dict[str, Any], Path]:
    raw_graph = json.loads(input_path.read_text())
    graph = Graph.from_json(raw_graph)
    summary = validate_graph(graph, raw_graph, input_path)

    metadata_candidate = raw_graph.get("metadata", {}).get("candidate_id")
    stem = input_path.stem
    candidate_id = str(metadata_candidate or stem)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate_id)
    expected_workdir = ROOT / "results" / "candidate-workflows" / safe_id
    if workdir.resolve() == expected_workdir.resolve() and (workdir / MANIFEST_NAME).exists():
        raise FileExistsError(f"refusing to overwrite {workdir / MANIFEST_NAME} without --overwrite")
    workdir.mkdir(parents=True, exist_ok=True)

    legal_spans = list(range(graph.delta, graph.n))
    gaps: list[dict[str, Any]] = []
    if tools.minisat is None:
        add_gap(gaps, "minisat_unavailable", "MiniSat was not found on PATH.")
    if tools.picosat is None or tools.drat_trim is None:
        add_gap(gaps, "drat_tools_unavailable", "PicoSAT or DRAT-Trim was not found.")

    print(
        f"rank-potential CP-SAT: delta={graph.delta}, workers={limits.workers}, "
        f"time_limit={limits.rank_time_limit}s",
        flush=True,
    )
    rank_result = rank_potential_solve(graph, limits.rank_time_limit, limits.workers)
    rank_data = rank_report(graph, rank_result)
    print(f"rank-potential CP-SAT: {rank_result.status}", flush=True)

    spans: dict[str, dict[str, Any]] = {}
    evidence_paths: set[Path] = set()
    witness_span: int | None = None
    witness_verified = False
    solver_disagreements: list[str] = []
    stopped_reason = "all legal spans checked"
    cnf_dir = workdir / "cnf"
    cnf_dir.mkdir(parents=True, exist_ok=True)

    for span in legal_spans:
        cp_status, coloring = fixed_span_sat_solve(
            graph, span, limits.span_time_limit, limits.workers
        )
        row: dict[str, Any] = {"cp_sat_status": cp_status}
        edge_list = sorted(map(tuple, graph.edges))

        clauses, variables = fixed_span_cnf(graph, span)
        cnf_path = cnf_dir / f"span-{span}.cnf"
        write_dimacs(cnf_path, clauses, len(variables))
        evidence_paths.add(cnf_path)
        row.update(
            {
                "cnf_variables": len(variables),
                "cnf_clauses": len(clauses),
                "cnf_sha256": sha256_path(cnf_path),
            }
        )

        if tools.minisat is not None:
            row["minisat"] = run_minisat(cnf_path, tools.minisat, limits.minisat_time_limit)
            if row["minisat"]["status"] == "SATISFIABLE":
                witness_ok, witness_reason = verify_minisat_witness(
                    Path(row["minisat"]["result_file"]),
                    variables,
                    edge_list,
                    span,
                )
                row["minisat_witness_verified"] = witness_ok
                row["minisat_witness_reason"] = witness_reason
                if not witness_ok:
                    add_gap(gaps, "minisat_witness_invalid", f"span {span}: {witness_reason}")
            if row["minisat"]["gap"]:
                add_gap(gaps, "minisat_failure", f"span {span}: {row['minisat']['status']}")

        cp_sat = cp_status in {"OPTIMAL", "FEASIBLE"}
        minisat_data = row.get("minisat", {})
        minisat_sat = minisat_data.get("status") == "SATISFIABLE"
        minisat_witness_valid = minisat_data.get("witness_verified") is True
        cp_unsat = cp_status == "INFEASIBLE"
        minisat_unsat = minisat_data.get("status") == "UNSATISFIABLE"

        if (cp_sat and minisat_unsat) or (cp_unsat and minisat_sat):
            solver_disagreements.append(str(span))
            add_gap(gaps, "solver_disagreement", f"CP-SAT and MiniSat disagree at span {span}")

        if (cp_sat or minisat_sat) and tools.picosat is not None and tools.drat_trim is not None:
            row["drat"] = {
                "status": "SKIPPED_SATISFIABLE",
                "gap": False,
                "elapsed_seconds": 0.0,
            }
        elif (cp_unsat or minisat_unsat) and tools.picosat is not None and tools.drat_trim is not None:
            row["drat"] = drat_report(cnf_path, tools.picosat, tools.drat_trim)
            evidence_paths.update(
                [
                    Path(str(cnf_path) + ".picosat.rup"),
                    Path(str(cnf_path) + ".drat"),
                    Path(str(cnf_path) + ".drat-check.log"),
                ]
            )
            if row["drat"].get("gap"):
                add_gap(gaps, "drat_failure", f"span {span}: {row['drat']['status']}")
        else:
            row["drat"] = {
                "status": "NOT_RUN",
                "reason": "no solver established this span as UNSAT",
                "gap": cp_status != "INFEASIBLE" and not minisat_unsat,
            }
            if row["drat"]["gap"]:
                add_gap(gaps, "drat_not_established", f"span {span}: no UNSAT basis")

        if cp_sat or minisat_sat:
            if coloring is not None:
                ok, reason = verify_coloring(graph, coloring)
                row["coloring_verified"] = ok
                if not ok:
                    raise AssertionError(f"fixed-span CP-SAT witness failed at span {span}: {reason}")
            if cp_sat:
                witness_span = span
                witness_verified = True
            elif minisat_witness_valid:
                witness_span = span
                witness_verified = True
            else:
                stopped_reason = f"unverifiable MiniSat witness at span {span}"
                spans[str(span)] = row
                break
            spans[str(span)] = row
            stopped_reason = "satisfiable span found"
            break

        if cp_status == "MODEL_INVALID":
            stopped_reason = f"invalid CP-SAT model at span {span}"
            spans[str(span)] = row
            break

        spans[str(span)] = row
        print(
            f"span {span}: CP-SAT={cp_status}"
            + (f", MiniSat={row['minisat']['status']}" if "minisat" in row else ""),
            flush=True,
        )

    checked_spans = [int(key) for key in spans]
    all_spans_checked = set(checked_spans) == set(legal_spans)
    all_cp_unsat = bool(spans) and all(row["cp_sat_status"] == "INFEASIBLE" for row in spans.values())
    all_mini_present = bool(spans) and all("minisat" in row for row in spans.values())
    all_mini_unsat = all_mini_present and all(
        row["minisat"]["status"] == "UNSATISFIABLE" for row in spans.values()
    )
    all_drat_present = bool(spans) and all("drat" in row for row in spans.values())
    all_drat_verified = all_drat_present and all(
        row["drat"]["status"] == "VERIFIED" for row in spans.values()
    )

    mandatory_pass = (
        rank_result.status == "non-colorable"
        and all_spans_checked
        and all_cp_unsat
        and witness_span is None
        and not solver_disagreements
    )
    external_complete = tools.minisat is not None and tools.picosat is not None and tools.drat_trim is not None
    external_pass = all_mini_unsat and all_drat_verified

    if solver_disagreements:
        decision = "inconclusive_solver_disagreement"
    elif witness_span is not None or rank_result.status == "colorable":
        decision = "colorable_claim_rejected"
    elif mandatory_pass and external_pass:
        decision = "verified_non_colorable"
    elif mandatory_pass and not external_complete:
        decision = "provisional_non_colorable_external_checks_incomplete"
    elif mandatory_pass:
        decision = "provisional_non_colorable_external_check_failed"
    else:
        decision = "inconclusive"

    source_modules = {
        name: sha256_path(ROOT / "src" / name)
        for name in (
            "interval_edge_coloring.py",
            "minisat_verify.py",
            "formal_proof_verify.py",
            "verify_candidate_workflow.py",
        )
    }
    generated_at = datetime.now(timezone.utc).isoformat()
    manifest: dict[str, Any] = {
        "schema": "interval-candidate-independent-verification-v1",
        "candidate_id": candidate_id,
        "claim_under_test": "the supplied connected simple bipartite graph has no interval edge coloring",
        "generated_at_utc": generated_at,
        "input": summary,
        "legal_spans": {
            "values": legal_spans,
            "count": len(legal_spans),
            "rule": "delta through n-1 inclusive for a connected graph on n vertices",
        },
        "parameters": {
            "rank_potential_time_limit_seconds": limits.rank_time_limit,
            "fixed_span_time_limit_seconds": limits.span_time_limit,
            "minisat_time_limit_seconds": limits.minisat_time_limit,
            "cp_sat_workers": limits.workers,
        },
        "tools": {
            "minisat": str(tools.minisat) if tools.minisat else None,
            "picosat": str(tools.picosat) if tools.picosat else None,
            "drat_trim": str(tools.drat_trim) if tools.drat_trim else None,
            "python": platform.python_version(),
        },
        "source_modules_sha256": source_modules,
        "verification": {
            "rank_potential_cp_sat": rank_data,
            "fixed_span_cp_sat": {
                "encoding": "independent start/color model from minisat_verify.fixed_span_cnf and interval_edge_coloring.fixed_span_sat_solve",
                "spans": spans,
                "all_legal_spans_checked": all_spans_checked,
                "stopped_reason": stopped_reason,
                "satisfiable_witness_span": witness_span,
                "witness_verified": witness_span is not None and witness_verified,
            },
            "summary": {
                "rank_potential_non_colorable": rank_result.status == "non-colorable",
                "all_fixed_spans_cp_sat_infeasible": all_cp_unsat and all_spans_checked,
                "all_minisat_spans_unsatisfiable": all_mini_unsat,
                "all_drat_proofs_verified": all_drat_verified,
                "external_chain_complete": external_complete,
                "solver_disagreements": solver_disagreements,
            },
        },
        "decision": decision,
        "decision_rule": (
            "verified_non_colorable requires global rank-potential infeasibility, INFEASIBLE "
            "CP-SAT for every legal span, MiniSat UNSAT for every exported span, and VERIFIED "
            "DRAT for every such span. Missing external tools produce a provisional result."
        ),
        "gaps": gaps,
        "evidence_files": [],
    }

    manifest_path = workdir / MANIFEST_NAME
    manifest["evidence_files"] = tracked_evidence(workdir, evidence_paths)
    write_json_atomic(manifest_path, manifest)
    print(f"manifest: {manifest_path}", flush=True)
    print(f"decision: {decision}", flush=True)
    return manifest, manifest_path


def proof_tool_smoke_test(tools: ToolPaths) -> None:
    if tools.picosat is None or tools.drat_trim is None:
        raise RuntimeError("PicoSAT and DRAT-Trim are required for the proof smoke test")
    with tempfile.TemporaryDirectory(prefix="interval-proof-smoke-") as directory:
        cnf = Path(directory) / "trivial-unsat.cnf"
        cnf.write_text("p cnf 1 2\n1 0\n-1 0\n")
        report = generate_and_check_drat(cnf, str(tools.picosat), str(tools.drat_trim))
        if report["status"] != "VERIFIED":
            raise AssertionError(f"proof smoke test failed: {report}")


def self_test(limits: RunLimits, tools: ToolPaths) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="interval-workflow-selftest-") as directory:
        graph_path = Path(directory) / "K_3_5.graph.json"
        graph = complete_bipartite(3, 5)
        graph.save(graph_path)
        report, _ = run_workflow(graph_path, Path(directory) / "run", limits, tools)
        expected_fixed = {"5": "INFEASIBLE", "6": "INFEASIBLE", "7": "OPTIMAL"}
        actual_fixed = {
            span: row["cp_sat_status"] for span, row in report["verification"]["fixed_span_cp_sat"]["spans"].items()
        }
        if actual_fixed != expected_fixed and actual_fixed != {
            "5": "INFEASIBLE",
            "6": "INFEASIBLE",
            "7": "FEASIBLE",
        }:
            raise AssertionError(f"unexpected K(3,5) fixed-span statuses: {actual_fixed}")
        if report["verification"]["rank_potential_cp_sat"]["status"] != "colorable":
            raise AssertionError("K(3,5) rank-potential control did not find a coloring")
        if tools.minisat is not None:
            actual_mini = [
                row["minisat"]["status"]
                for row in report["verification"]["fixed_span_cp_sat"]["spans"].values()
            ]
            if actual_mini != ["UNSATISFIABLE", "UNSATISFIABLE", "SATISFIABLE"]:
                raise AssertionError(f"unexpected K(3,5) MiniSat statuses: {actual_mini}")
        if tools.picosat is not None and tools.drat_trim is not None:
            proof_tool_smoke_test(tools)
        return {
            "control": "K_3_5",
            "decision": report["decision"],
            "fixed_span_cp_sat_statuses": actual_fixed,
            "minisat_available": tools.minisat is not None,
            "drat_smoke_tested": tools.picosat is not None and tools.drat_trim is not None,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph_json", nargs="?", help="candidate graph JSON")
    parser.add_argument("--output-dir", help="directory for CNFs and workflow-manifest.json")
    parser.add_argument("--overwrite", action="store_true", help="replace a default-location manifest")
    parser.add_argument("--rank-time-limit", type=float, default=60.0)
    parser.add_argument("--span-time-limit", type=float, default=30.0)
    parser.add_argument("--minisat-time-limit", type=float, default=300.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--minisat", default="minisat")
    parser.add_argument("--picosat", default=str(ROOT / "tools/picosat-965/picosat"))
    parser.add_argument("--drat-trim", default=str(ROOT / "tools/drat-trim/drat-trim"))
    parser.add_argument("--accept-colorable", action="store_true", help="exit zero on a colorable control")
    parser.add_argument("--self-test", action="store_true", help="run harmless K(3,5) and proof-tool controls")
    args = parser.parse_args(argv)
    if not args.self_test and not args.graph_json:
        parser.error("graph_json is required unless --self-test is used")
    if args.workers < 1 or min(args.rank_time_limit, args.span_time_limit, args.minisat_time_limit) <= 0:
        parser.error("workers and all time limits must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tools = ToolPaths(
        minisat=resolve_tool(args.minisat),
        picosat=resolve_tool(args.picosat),
        drat_trim=resolve_tool(args.drat_trim),
    )
    limits = RunLimits(
        rank_time_limit=args.rank_time_limit,
        span_time_limit=args.span_time_limit,
        minisat_time_limit=args.minisat_time_limit,
        workers=args.workers,
    )

    try:
        if args.self_test:
            print(json.dumps(self_test(limits, tools), indent=2, sort_keys=True))
            return 0
        input_path = Path(args.graph_json)
        if args.output_dir:
            output_dir = Path(args.output_dir)
            manifest_path = output_dir / MANIFEST_NAME
            if manifest_path.exists() and not args.overwrite:
                raise FileExistsError(f"refusing to overwrite {manifest_path} without --overwrite")
        else:
            raw_graph = json.loads(input_path.read_text())
            candidate_id = raw_graph.get("metadata", {}).get("candidate_id") or input_path.stem
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(candidate_id))
            output_dir = ROOT / "results" / "candidate-workflows" / safe_id

        manifest, _ = run_workflow(input_path, output_dir, limits, tools)
        accepted = manifest["decision"] in {"verified_non_colorable"} or (
            args.accept_colorable and manifest["decision"] == "colorable_claim_rejected"
        )
        return 0 if accepted else 1
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
