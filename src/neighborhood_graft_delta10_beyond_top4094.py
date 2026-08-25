#!/usr/bin/env python3
"""Classify ranks 4095--5094 of the Delta <= 10 neighborhood-graft family.

This continuation reuses the audited rank-3095--4094 runner, extending its
immutable predecessor ledger set before invoking the same append-only,
rank-potential and independently-confirmed-negative workflow.
"""

from pathlib import Path

import neighborhood_graft_delta10_beyond_top3094 as continuation


continuation.DEFAULT_PREDECESSORS = (
    *continuation.DEFAULT_PREDECESSORS,
    Path("results/neighborhood-graft-delta10-agent/extension-beyond-top3094"),
)
continuation.DEFAULT_OUTPUT = Path(
    "results/neighborhood-graft-delta10-agent/extension-beyond-top4094"
)
continuation.EXPECTED_PREDECESSORS = (94, 1000, 1000, 1000, 1000)
continuation.__doc__ = __doc__


if __name__ == "__main__":
    continuation.main()
