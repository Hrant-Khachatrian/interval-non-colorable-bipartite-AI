# Neighborhood-Graft Delta <= 10 Spot Audit

Completed deterministic audit of 72 colorable ledger rows across ranks 1--2094: 24 from initial top94, 24 beyond top94, and 24 beyond top1094.  The source ledgers were read only; graph reconstruction was filtered to the selected canonical hashes, with no bulk classification rerun.

All 72 sampled graphs are simple, connected, bipartite, have minimum degree at least 2 and maximum degree at most 10.  Canonical hashes and reported order/size/degree metadata agree in every row.  All rank-potential certificates were valid under a three-worker rerun, and independent three-worker fixed-span CP-SAT produced a valid coloring at every reported span.  There were no solver disagreements, invalid certificates, structural mismatches, hash mismatches, or non-colorable claims.  The maximum per-row audit runtime was 1.714 seconds.

Two rows have nonminimal reported spans: rank 37 (`9:14:6da7c2cf5f7ead798d86d65a3abe220a9e74e5703e6614ad7937bb032cccdb6e`) and rank 269 (`10:15:39f21aaae96f5f709c0cca6ac5d56d000f7a6ce3b9623a0091b198bec006376f`).  Each ledger reports span 11 from a four-worker rank-potential solve, while independently verified fixed-span CP-SAT colorings exist at span 10 with both three workers and serial one-worker confirmation.  This is a reported-span metadata discrepancy only: the `colorable` decisions and statuses remain supported.

Reproducible fixtures: `fixtures/rank-0037-reported-span-metadata.json` and `fixtures/rank-0269-reported-span-metadata.json`.  Full details are in `report.json` and the compact completion state is in `status.json`.
