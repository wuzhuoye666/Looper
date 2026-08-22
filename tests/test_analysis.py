from __future__ import annotations

import pytest
from looper_core.analysis import (
    bootstrap_improvement,
    environment_sensitivity,
    pareto_ranks,
    rank_stability,
    summarize,
    task_leverage,
)
from looper_core.contracts import Direction


def test_bootstrap_improvement_is_positive_for_lower_latency() -> None:
    result = bootstrap_improvement(
        [8.0, 8.2, 7.9, 8.1],
        [10.0, 10.2, 9.9, 10.1],
        Direction.MINIMIZE,
        confidence=0.95,
        resamples=500,
        seed=7,
    )
    assert result["estimate"] == pytest.approx(2.0 / 10.05)
    assert result["lower"] > 0


def test_tail_summary_reports_insufficient_evidence() -> None:
    summary = summarize([1, 2, 3], tail_min_samples=20)
    assert summary["median"] == 2
    assert summary["p99"] is None
    assert summary["tail_status"] == "insufficient_evidence"


def test_pareto_excludes_infeasible_and_ranks_fronts() -> None:
    points = [
        {"id": "fast", "feasible": True, "objectives": {"speed": 10, "cost": 5}},
        {"id": "cheap", "feasible": True, "objectives": {"speed": 8, "cost": 2}},
        {"id": "bad", "feasible": False, "objectives": {"speed": 100, "cost": 1}},
        {"id": "dominated", "feasible": True, "objectives": {"speed": 7, "cost": 6}},
    ]
    ranks = pareto_ranks(points, {"speed": "maximize", "cost": "minimize"})
    assert ranks["fast"] == 1
    assert ranks["cheap"] == 1
    assert ranks["dominated"] == 2
    assert ranks["bad"] is None


def test_benchtrust_helpers_report_rank_and_environment_effects() -> None:
    stability = rank_stability([["a", "b", "c"], ["a", "c", "b"]])
    assert stability["comparison_count"] == 1
    sensitivity = environment_sensitivity({"env-a": [1, 1.1], "env-b": [3, 3.1]})
    assert sensitivity["eta_squared"] and sensitivity["eta_squared"] > 0.9
    leverage = task_leverage({"a": {"x": 10, "y": 2}, "b": {"x": 3, "y": 4}})
    assert leverage["dominant_task"] in {"x", "y"}


def test_task_leverage_is_unavailable_for_one_workload() -> None:
    leverage = task_leverage({"a": {"only": 10}, "b": {"only": 3}})
    assert leverage == {"max_rank_shift": None, "dominant_task": None, "task_shifts": {}}
