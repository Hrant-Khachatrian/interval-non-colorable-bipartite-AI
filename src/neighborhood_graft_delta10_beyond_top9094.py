#!/usr/bin/env python3
"""Classify ranks 9095--10094 of the Delta <= 10 neighborhood-graft family.

This continuation treats the rank-1--9094 ledgers as immutable validated
evidence. It reconstructs the deterministic full-root family, skips every
prior classification, and appends only the next 1,000 ranked decisions.
"""

from pathlib import Path

import neighborhood_graft_delta10_beyond_top8094 as prior


# The preceding band has a complete append-only ledger. The imported runner
# validates all prior records without recomputing their solver decisions.
continuation = prior.continuation
continuation.DEFAULT_PREDECESSORS = (
    *continuation.DEFAULT_PREDECESSORS,
    Path("results/neighborhood-graft-delta10-agent/extension-beyond-top8094"),
)
continuation.DEFAULT_OUTPUT = Path(
    "results/neighborhood-graft-delta10-agent/extension-beyond-top9094"
)
continuation.EXPECTED_PREDECESSORS = (*continuation.EXPECTED_PREDECESSORS, 1000)
continuation.__doc__ = __doc__


if __name__ == "__main__":
    continuation.main()
