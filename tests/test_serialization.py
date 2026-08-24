from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from looper_api.serialization import _best_primary_score, _iso, _observation_metric_view
from looper_core.contracts import Direction


def test_iso_marks_database_timestamps_as_utc() -> None:
    timestamp = datetime(2026, 8, 24, 1, 53, 23, 940536)

    assert _iso(timestamp) == "2026-08-24T01:53:23.940536Z"
    assert _iso(timestamp.replace(tzinfo=UTC)) == "2026-08-24T01:53:23.940536Z"
    assert _iso(None) is None


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


def test_failed_attempt_observation_can_still_be_presented() -> None:
    observation = SimpleNamespace(
        metric="closed_loop_successful_rps",
        value_number=128.84,
        value_boolean=None,
        unit="requests/second",
    )

    assert _observation_metric_view(observation, {"direction": "maximize"}) == {
        "name": "closed_loop_successful_rps",
        "value": 128.84,
        "unit": "requests/second",
        "baseline": None,
        "direction": "max",
    }
