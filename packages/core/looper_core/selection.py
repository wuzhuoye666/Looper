from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from looper_core.contracts import (
    FrontierBlockEvidence,
    FrontierPointEvidence,
    GoodputPolicy,
    LoadSearchSpec,
    TailEvidenceSpec,
)


class FrontierError(ValueError):
    pass


def frontier_block_from_scenario_result(
    result: Mapping[str, Any],
    *,
    block_id: str,
    time_block_id: str,
) -> FrontierBlockEvidence:
    raw_metrics = result.get("metrics")
    raw_checks = result.get("checks")
    if not isinstance(raw_metrics, Sequence) or isinstance(raw_metrics, (str, bytes)):
        raise FrontierError("scenario result metrics must be a list")
    if not isinstance(raw_checks, Sequence) or isinstance(raw_checks, (str, bytes)):
        raise FrontierError("scenario result checks must be a list")
    metrics: dict[str, Mapping[str, Any]] = {}
    for item in raw_metrics:
        if not isinstance(item, Mapping) or not isinstance(item.get("metric"), str):
            raise FrontierError("scenario result contains an invalid metric")
        name = item["metric"]
        if name in metrics:
            raise FrontierError(f"scenario result contains duplicate metric {name!r}")
        metrics[name] = item

    def metric_value(name: str) -> float:
        item = metrics.get(name)
        if item is None:
            raise FrontierError(f"scenario result is missing metric {name!r}")
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FrontierError(f"scenario metric {name!r} must be numeric")
        return float(value)

    latency_metric = metrics.get("latency_p99_ms")
    if latency_metric is None:
        raise FrontierError("scenario result is missing metric 'latency_p99_ms'")
    latency_samples = latency_metric.get("sample_count")
    if not isinstance(latency_samples, int) or isinstance(latency_samples, bool):
        raise FrontierError("latency_p99_ms must carry its underlying sample count")

    checks = [item for item in raw_checks if isinstance(item, Mapping)]
    correctness_checks = [
        item for item in checks if item.get("kind") in {"execution", "correctness"}
    ]
    resource_checks = [item for item in checks if item.get("kind") == "resource"]
    return FrontierBlockEvidence(
        block_id=block_id,
        time_block_id=time_block_id,
        committed_goodput=metric_value("committed_tps"),
        latency_p99=metric_value("latency_p99_ms"),
        latency_samples=latency_samples,
        error_ratio=metric_value("error_ratio"),
        abort_ratio=metric_value("abort_ratio"),
        timeout_ratio=metric_value("timeout_ratio"),
        offered_load_achieved_ratio=metric_value("offered_load_achieved_ratio"),
        rate_limiter_lag_ratio=metric_value("rate_limiter_lag_ratio"),
        client_headroom_ratio=metric_value("client_headroom_ratio"),
        correctness_passed=(
            result.get("status") == "succeeded"
            and bool(correctness_checks)
            and all(item.get("passed") is True for item in correctness_checks)
        ),
        resource_valid=(
            bool(resource_checks) and all(item.get("passed") is True for item in resource_checks)
        ),
    )


def block_passes_frontier_gates(
    block: FrontierBlockEvidence,
    *,
    latency_p99_threshold: float,
    goodput: GoodputPolicy,
    tail: TailEvidenceSpec,
) -> bool:
    return all(
        (
            block.correctness_passed,
            block.resource_valid,
            block.committed_goodput > 0,
            block.latency_samples >= tail.minimum_samples,
            block.latency_p99 <= latency_p99_threshold,
            block.error_ratio <= goodput.maximum_error_ratio,
            block.abort_ratio <= goodput.maximum_abort_ratio,
            block.timeout_ratio <= goodput.maximum_timeout_ratio,
            block.offered_load_achieved_ratio >= 0.99,
            block.rate_limiter_lag_ratio < 0.01,
            block.client_headroom_ratio >= 0.20,
        )
    )


def classify_frontier_point(
    point: FrontierPointEvidence,
    protocol: LoadSearchSpec,
    *,
    latency_p99_threshold: float,
    goodput: GoodputPolicy,
    tail: TailEvidenceSpec,
) -> dict[str, Any]:
    passes = [
        block_passes_frontier_gates(
            block,
            latency_p99_threshold=latency_p99_threshold,
            goodput=goodput,
            tail=tail,
        )
        for block in point.blocks
    ]
    pass_count = sum(passes)
    block_count = len(passes)
    if block_count < protocol.initial_repeats:
        status = "insufficient_blocks"
    elif block_count < protocol.boundary_repeats:
        required = max(1, round(protocol.required_passes * block_count / protocol.boundary_repeats))
        status = "provisional_pass" if pass_count >= required else "provisional_fail"
    else:
        status = "confirmed_pass" if pass_count >= protocol.required_passes else "confirmed_fail"
    return {
        "offered_load": point.offered_load,
        "status": status,
        "block_count": block_count,
        "pass_count": pass_count,
        "failed_block_ids": [
            block.block_id for block, passed in zip(point.blocks, passes, strict=True) if not passed
        ],
    }


def analyze_slo_frontier(
    points: Sequence[FrontierPointEvidence],
    protocol: LoadSearchSpec,
    *,
    latency_p99_threshold: float,
    goodput: GoodputPolicy,
    tail: TailEvidenceSpec,
    adaptive_points_used: int = 0,
) -> dict[str, Any]:
    if latency_p99_threshold <= 0:
        raise FrontierError("latency threshold must be positive")
    if not points:
        raise FrontierError("frontier analysis requires at least one load point")
    offered_loads = [point.offered_load for point in points]
    if len(offered_loads) != len(set(offered_loads)):
        raise FrontierError("frontier offered loads must be unique")

    decisions = [
        classify_frontier_point(
            point,
            protocol,
            latency_p99_threshold=latency_p99_threshold,
            goodput=goodput,
            tail=tail,
        )
        for point in sorted(points, key=lambda item: item.offered_load)
    ]
    confirmed_passes = [
        item["offered_load"] for item in decisions if item["status"] == "confirmed_pass"
    ]
    confirmed_failures = [
        item["offered_load"] for item in decisions if item["status"] == "confirmed_fail"
    ]
    monotonic_violations = [
        {"failed_load": failed, "higher_passing_load": passed}
        for failed in confirmed_failures
        for passed in confirmed_passes
        if failed < passed
    ]
    lower = max(confirmed_passes) if confirmed_passes else None
    upper_candidates = [value for value in confirmed_failures if lower is None or value > lower]
    upper = min(upper_candidates) if upper_candidates else None
    next_load: float | None = None
    width_ratio: float | None = None
    termination_reason: str | None = None

    if monotonic_violations:
        status = "non_monotonic"
        termination_reason = "non_monotonic_frontier"
    elif lower is None:
        status = "needs_lower_bracket"
        next_load = min(offered_loads) / protocol.expansion_factor
    elif upper is None:
        status = "needs_upper_bracket"
        next_load = max(offered_loads) * protocol.expansion_factor
    else:
        width_ratio = (upper - lower) / lower
        if width_ratio <= protocol.resolution_ratio:
            status = "resolved"
        else:
            status = "searching"
            next_load = (lower + upper) / 2

    if status in {"needs_lower_bracket", "needs_upper_bracket", "searching"} and (
        adaptive_points_used >= protocol.maximum_adaptive_points
    ):
        status = "frontier_unresolved"
        next_load = None
        termination_reason = "maximum_adaptive_points_exhausted"

    return {
        "status": status,
        "confirmed_pass": lower,
        "confirmed_fail": upper,
        "width_ratio": width_ratio,
        "next_offered_load": next_load,
        "adaptive_points_used": adaptive_points_used,
        "termination_reason": termination_reason,
        "decisions": decisions,
        "monotonic_violations": monotonic_violations,
    }


def compare_frontier_intervals(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    *,
    minimum_effect_ratio: float,
) -> dict[str, Any]:
    if candidate.get("status") != "resolved" or baseline.get("status") != "resolved":
        return {
            "status": "frontier_unresolved",
            "distinguishable": False,
            "conservative_effect_ratio": None,
        }
    candidate_lower = float(candidate["confirmed_pass"])
    candidate_upper = float(candidate["confirmed_fail"])
    baseline_lower = float(baseline["confirmed_pass"])
    baseline_upper = float(baseline["confirmed_fail"])
    if candidate_lower > baseline_upper:
        effect = (candidate_lower - baseline_upper) / baseline_upper
        winner = "candidate"
    elif baseline_lower > candidate_upper:
        effect = (baseline_lower - candidate_upper) / candidate_upper
        winner = "baseline"
    else:
        effect = 0.0
        winner = None
    return {
        "status": "available",
        "distinguishable": effect >= minimum_effect_ratio,
        "winner": winner if effect >= minimum_effect_ratio else None,
        "conservative_effect_ratio": effect,
        "minimum_effect_ratio": minimum_effect_ratio,
    }
