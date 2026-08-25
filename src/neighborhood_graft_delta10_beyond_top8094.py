#!/usr/bin/env python3
"""Classify ranks 8095--9094 of the Delta <= 10 neighborhood-graft family.

This continuation retains ranks 1--8094 as immutable, validated ledger
evidence and classifies precisely the next 1,000 canonical candidates.
"""

from pathlib import Path

import neighborhood_graft_delta10_beyond_top7094 as prior


# The preceding band has a complete append-only ledger.  The imported runner
# reconstructs the full family, validates every predecessor, and skips all
# prior decisions before evaluating ranks 8095--9094.
continuation = prior.continuation
continuation.DEFAULT_PREDECESSORS = (
    *continuation.DEFAULT_PREDECESSORS,
    Path("results/neighborhood-graft-delta10-agent/extension-beyond-top7094"),
)
continuation.DEFAULT_OUTPUT = Path(
    "results/neighborhood-graft-delta10-agent/extension-beyond-top8094"
)
continuation.EXPECTED_PREDECESSORS = (*continuation.EXPECTED_PREDECESSORS, 1000)
continuation.__doc__ = __doc__


if __name__ == "__main__":
    continuation.main()
