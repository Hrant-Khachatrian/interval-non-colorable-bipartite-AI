#!/usr/bin/env python3
"""Classify ranks 5095--6094 of the Delta <= 10 neighborhood-graft family.

This continuation extends the immutable rank-1--5094 ledger set while
preserving the established append-only, rank-potential, independently
confirmed-negative workflow.
"""

from pathlib import Path

import neighborhood_graft_delta10_beyond_top4094 as prior


# The rank-4094 module is deliberately a thin wrapper.  Its imported runner
# carries the accumulated predecessor configuration after the wrapper loads.
continuation = prior.continuation


continuation.DEFAULT_PREDECESSORS = (
    *continuation.DEFAULT_PREDECESSORS,
    Path("results/neighborhood-graft-delta10-agent/extension-beyond-top4094"),
)
continuation.DEFAULT_OUTPUT = Path(
    "results/neighborhood-graft-delta10-agent/extension-beyond-top5094"
)
continuation.EXPECTED_PREDECESSORS = (*continuation.EXPECTED_PREDECESSORS, 1000)
continuation.__doc__ = __doc__


if __name__ == "__main__":
    continuation.main()
