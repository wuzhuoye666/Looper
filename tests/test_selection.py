from __future__ import annotations

import pytest
from looper_core.analysis import (
    InsufficientEvidence,
    cluster_paired_bootstrap_improvement,
    paired_bootstrap_improvement,
)
from looper_core.contracts import (
    FrontierBlockEvidence,
    FrontierPointEvidence,
    GoodputPolicy,
    LoadSearchSpec,
    TailEvidenceSpec,
)
from looper_core.selection import analyze_slo_frontier, compare_frontier_intervals


def _point(load: float, *, passed: bool) -> FrontierPointEvidence:
    return FrontierPointEvidence(
        offered_load=load,
        blocks=[
            FrontierBlockEvidence(
                block_id=f"{load}-{index}",
                time_block_id=f"block-{index}",
                committed_goodput=load * 0.995,
                latency_p99=40 if passed else 65,
                latency_samples=120000,
                error_ratio=0.0001,
                abort_ratio=0.002,
                timeout_ratio=0,
                offered_load_achieved_ratio=0.995,
                rate_limiter_lag_ratio=0.001,
                client_headroom_ratio=0.30,
                correctness_passed=True,
                resource_valid=True,
            )
            for index in range(5)
        ],
    )


def _frontier(points: list[FrontierPointEvidence]) -> dict[str, object]:
    return analyze_slo_frontier(
        points,
        LoadSearchSpec(offered_load_metric="offered_tps", unit="transactions/second"),
        latency_p99_threshold=50,
        goodput=GoodputPolicy(metric="committed_tps", unit="transactions/second"),
        tail=TailEvidenceSpec(
            metric="transaction_latency",
            unit="ms",
            minimum_samples=100,
            histogram_format="raw",
        ),
    )


def test_paired_bootstrap_resamples_time_blocks_together() -> None:
    result = paired_bootstrap_improvement(
        [110, 210, 310, 410],
        [100, 200, 300, 400],
        "maximize",
        comparison="difference",
        aggregation="mean",
        resamples=500,
        seed=9,
    )
    assert result["estimate"] == 10
    assert result["lower"] == 10
    assert result["upper"] == 10
    assert result["pair_count"] == 4


def test_paired_bootstrap_rejects_unmatched_blocks() -> None:
    with pytest.raises(InsufficientEvidence, match="equally sized"):
        paired_bootstrap_improvement([1, 2], [1], "maximize")


def test_cluster_bootstrap_uses_placement_pairs_as_units() -> None:
    result = cluster_paired_bootstrap_improvement(
        {"wave-a": [110, 111], "wave-b": [205, 207], "wave-c": [309, 310]},
        {"wave-a": [100, 101], "wave-b": [200, 201], "wave-c": [300, 301]},
        "maximize",
        comparison="difference",
        aggregation="median",
        resamples=500,
        seed=12,
    )
    assert result["cluster_count"] == 3
    assert result["candidate_samples"] == 6
    assert result["lower"] > 0


def test_frontier_reports_bracket_and_next_binary_load() -> None:
    searching = _frontier([_point(100, passed=True), _point(110, passed=False)])
    assert searching["status"] == "searching"
    assert searching["next_offered_load"] == 105
    resolved = _frontier(
        [_point(100, passed=True), _point(102, passed=False), _point(110, passed=False)]
    )
    assert resolved["status"] == "resolved"
    assert resolved["confirmed_pass"] == 100
    assert resolved["confirmed_fail"] == 102


def test_frontier_stops_after_maximum_adaptive_points() -> None:
    result = analyze_slo_frontier(
        [_point(100, passed=True), _point(110, passed=False)],
        LoadSearchSpec(
            offered_load_metric="offered_tps",
            unit="transactions/second",
            maximum_adaptive_points=2,
        ),
        latency_p99_threshold=50,
        goodput=GoodputPolicy(metric="committed_tps", unit="transactions/second"),
        tail=TailEvidenceSpec(
            metric="transaction_latency",
            unit="ms",
            minimum_samples=100,
            histogram_format="raw",
        ),
        adaptive_points_used=2,
    )
    assert result["status"] == "frontier_unresolved"
    assert result["next_offered_load"] is None
    assert result["termination_reason"] == "maximum_adaptive_points_exhausted"


@pytest.mark.parametrize(
    ("field", "value"),
    [("timeout_ratio", 0.01), ("rate_limiter_lag_ratio", 0.01)],
)
def test_frontier_rejects_timeout_or_client_lag(field: str, value: float) -> None:
    point = _point(100, passed=True)
    for block in point.blocks:
        setattr(block, field, value)
    result = _frontier([point])
    assert result["decisions"][0]["status"] == "confirmed_fail"


def test_frontier_comparison_uses_conservative_interval_edges() -> None:
    baseline = _frontier([_point(100, passed=True), _point(102, passed=False)])
    candidate = _frontier([_point(120, passed=True), _point(122, passed=False)])
    result = compare_frontier_intervals(candidate, baseline, minimum_effect_ratio=0.05)
    assert result["distinguishable"] is True
    assert result["winner"] == "candidate"
    assert result["conservative_effect_ratio"] == pytest.approx((120 - 102) / 102)
