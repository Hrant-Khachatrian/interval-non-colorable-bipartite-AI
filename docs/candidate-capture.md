# Order-16 candidate capture

`src/capture_order16_candidate.py` turns negative rows in the exhaustive
order-16 result chunks into independently checked, certificate-ready bundles.
It connects only to `hrant@cluster.ysu.am`, scans `results/<dataset>` there, and
does not copy a result chunk to this workstation.

## Capture contract

A candidate is a valid row whose status is exactly `non-colorable` or
`non_colorable`. The status `timeout` is recorded as an unresolved timeout; it
is never promoted to a candidate. The composite capture key is the zero-based
source index together with `canonical_sha256`. Repeated identical keys are
collapsed and their source locations retained. An index with different hashes,
a hash at multiple indices, inconsistent duplicate fields, or a malformed row
is a hard input-integrity failure.

For each unique key, the tool reads the graph6 line selected by the zero-based
index from the remote canonical input. The default input is
`data/<dataset>-d2to11.g6`; override it with `--input` using a path relative to
the remote checkout. The reconstructed graph must round-trip to the same
graph6 line.

The invariant gate requires order 16, a simple connected bipartite graph,
bipartition side sizes matching the dataset name, minimum degree at least 2,
agreement with stored order/size/delta/minimum-degree fields, and equality of
the reconstructed bipartition-canonical hash with the source row hash.

## Verification

After the invariants pass, the workflow runs:

1. rank-potential CP-SAT and accepts only an exact non-colorable result;
2. independent fixed-span CP-SAT for every legal span, `delta` through `n-1`,
   and requires every model to be infeasible;
3. one deletion check for every edge and one for every vertex using
   rank-potential CP-SAT, with every returned coloring independently checked;
4. recording of edge-critical, vertex-critical, and combined minimality.

A bundle is marked certificate-ready only when all invariants pass, both global
and fixed-span proofs prove non-colorability, and every deletion check is
conclusive. A non-colorable graph can still be verified when it is not minimal;
the minimality fields then record that outcome explicitly. A solver timeout is
inconclusive and prevents a certificate-ready mark.

Each accepted or checked candidate is written atomically to
`results/candidates/ORDER16-<CLASS>-index-<INDEX>/`. The bundle contains the
reconstructed `graph.json`, exact `source.graph6`, source and duplicate
information, full `verification.json`, human-readable `SUMMARY.md`, artifact
hashes in `manifest.json`, and a `.certificate-ready.json` marker when the
decision qualifies. Existing bundles are never overwritten.

## Usage

Audit the completed all-colorable 4x12 class and create the required compact
status report:

```sh
.venv/bin/python src/capture_order16_candidate.py \
  --dataset order16-4x12 \
  --expected 26330 \
  --output-status results/order16-4x12-capture-status.json \
  --live --poll-seconds 30 --workers 8 --solver-time-limit 30
```

Do a cheap, bounded smoke test without verifying candidates:

```sh
.venv/bin/python src/capture_order16_candidate.py \
  --dataset order16-5x11 \
  --chunk-limit 1 \
  --candidate-limit 0 \
  --output-status results/order16-5x11-first-chunk-capture-status.json
```

With `--live`, point-in-time scans are repeated until two consecutive scans are
stable or an unchanged scan reaches `--expected`. `--stable-scans`,
`--poll-seconds`, and `--deadline-seconds` control the stopping behavior.
`--candidate-limit 0` is useful for scan-only validation; a positive limit
verifies candidates in ascending index order after deduplication.

The compact status JSON records rows scanned, malformed rows, the complete
status histogram, timeout count, deduplication counts, expected-row comparison,
selected count, per-candidate decisions, certificate-ready count, and scan
stability. It does not embed the raw chunk inventory or raw candidate rows.

Exit codes are `0` when the requested workflow completed (including zero
candidates), `2` for input, remote, integrity, or expected-count failure, and
`3` when verification finished with an inconclusive or erroneous candidate.
