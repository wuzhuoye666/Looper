from __future__ import annotations

from looper_api.serialization import _best_primary_score
from looper_core.contracts import Direction

RESULTS = [
    {
        "feasible": True,
        "pareto_rank": 1,
        "sequence": 0,
        "objectives": [{"metric": "throughput", "raw": 958.0}],
    },
    {
        "feasible": True,
        "pareto_rank": 1,
        "sequence": 1,
        "objectives": [{"metric": "throughput", "raw": 3963.0}],
    },
]


def test_best_primary_score_uses_objective_direction() -> None:
    assert _best_primary_score(RESULTS, "throughput", Direction.MAXIMIZE) == 3963.0
    assert _best_primary_score(RESULTS, "throughput", Direction.MINIMIZE) == 958.0


def test_best_primary_score_ignores_missing_and_boolean_values() -> None:
    assert _best_primary_score(RESULTS, "missing", Direction.MAXIMIZE) is None
    assert (
        _best_primary_score(
            [{"objectives": [{"metric": "valid", "raw": True}]}],
            "valid",
            Direction.MAXIMIZE,
        )
        is None
    )
