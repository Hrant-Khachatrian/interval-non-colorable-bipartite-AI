#!/usr/bin/env python3
"""Generate PicoSAT RUP proofs, convert them to DRAT, and check with DRAT-Trim."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_cnf(cnf: Path, picosat: str, drat_trim: str) -> dict:
    raw_rup = cnf.with_suffix(cnf.suffix + ".picosat.rup")
    drat = cnf.with_suffix(cnf.suffix + ".drat")
    log = cnf.with_suffix(cnf.suffix + ".drat-check.log")
    started = time.time()
    generate = subprocess.run(
        [picosat, "-n", "-r", str(raw_rup), str(cnf)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if not raw_rup.exists():
        raise RuntimeError(f"PicoSAT did not create proof for {cnf}: {generate.stderr}")
    # PicoSAT writes an ASCII format header followed directly by text DRAT.
    with raw_rup.open("rb") as source, drat.open("wb") as target:
        source.readline()
        while chunk := source.read(1024 * 1024):
            target.write(chunk)
    check = subprocess.run(
        [drat_trim, str(cnf), str(drat)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    verified = "s VERIFIED" in check.stdout
    log.write_text(check.stdout + check.stderr)
    if not verified:
        raise RuntimeError(f"DRAT verification failed for {cnf}: {check.stdout}{check.stderr}")
    return {
        "cnf_sha256": sha256(cnf),
        "drat_sha256": sha256(drat),
        "checker": "drat-trim",
        "status": "VERIFIED",
        "elapsed_seconds": time.time() - started,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate_dirs", nargs="+")
    parser.add_argument(
        "--picosat", default="tools/picosat-965/picosat"
    )
    parser.add_argument("--drat-trim", default="tools/drat-trim/drat-trim")
    args = parser.parse_args()
    report = []
    for directory_name in args.candidate_dirs:
        directory = Path(directory_name)
        cnf_directory = next(directory.glob("cnf"))
        proofs = {}
        for cnf in sorted(cnf_directory.glob("span-*.cnf")):
            span = int(cnf.stem.split("-")[1])
            proofs[str(span)] = verify_cnf(cnf, args.picosat, args.drat_trim)
            print(cid := directory.name, span, proofs[str(span)]["status"], flush=True)
        output = directory / "formal-proof-report.json"
        output.write_text(json.dumps(proofs, indent=2) + "\n")
        report.append({"candidate_id": directory.name, "proofs": proofs})
    Path("results/formal-proof-summary.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
