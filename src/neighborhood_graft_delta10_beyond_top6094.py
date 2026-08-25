#!/usr/bin/env python3
"""Classify ranks 6095--7094 of the Delta <= 10 neighborhood-graft family.

This continuation preserves the validated rank-1--6094 ledgers as immutable
evidence, selecting and classifying only the next 1,000 ranked candidates.
"""

from pathlib import Path

import neighborhood_graft_delta10_beyond_top5094 as prior


# The completed 5095--6094 continuation was written to its explicit resume
# output directory.  It is the sole new predecessor for this range.
continuation = prior.continuation
continuation.DEFAULT_PREDECESSORS = (
    *continuation.DEFAULT_PREDECESSORS,
    Path("results/neighborhood-graft-delta10-agent/extension-beyond-top5094-resume"),
)
continuation.DEFAULT_OUTPUT = Path(
    "results/neighborhood-graft-delta10-agent/extension-beyond-top6094"
)
continuation.EXPECTED_PREDECESSORS = (*continuation.EXPECTED_PREDECESSORS, 1000)
continuation.__doc__ = __doc__


if __name__ == "__main__":
    continuation.main()
