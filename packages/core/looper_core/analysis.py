from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from statistics import mean, median, pstdev
from typing import Any

from looper_core.contracts import Aggregation, Comparison, Direction, GateSpec, Operator


class InsufficientEvidence(ValueError):
    pass


def _clean(values: Iterable[float]) -> list[float]:
    cleaned = [float(value) for value in values]
    if not cleaned or any(not math.isfinite(value) for value in cleaned):
        raise InsufficientEvidence("observations must contain finite values")
    return cleaned


def quantile(values: Sequence[float], probability: float) -> float:
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between zero and one")
    ordered = sorted(_clean(values))
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def cvar(values: Sequence[float], probability: float = 0.99, upper_tail: bool = True) -> float:
    ordered = sorted(_clean(values), reverse=upper_tail)
    count = max(1, math.ceil(len(ordered) * (1 - probability)))
    return mean(ordered[:count])


def aggregate(values: Sequence[float], method: Aggregation | str) -> float:
    cleaned = _clean(values)
    aggregation = Aggregation(method)
    if aggregation == Aggregation.MEAN:
        return mean(cleaned)
    if aggregation in {Aggregation.MEDIAN, Aggregation.P50}:
        return median(cleaned)
    if aggregation == Aggregation.P95:
        return quantile(cleaned, 0.95)
    if aggregation == Aggregation.P99:
        return quantile(cleaned, 0.99)
    if aggregation == Aggregation.P999:
        return quantile(cleaned, 0.999)
    if aggregation == Aggregation.MAXIMUM:
        return max(cleaned)
    return cvar(cleaned)


def summarize(values: Sequence[float], tail_min_samples: int = 100) -> dict[str, Any]:
    cleaned = _clean(values)
    average = mean(cleaned)
    deviation = pstdev(cleaned) if len(cleaned) > 1 else 0.0
    summary: dict[str, Any] = {
        "count": len(cleaned),
        "mean": average,
        "median": median(cleaned),
        "minimum": min(cleaned),
        "maximum": max(cleaned),
        "standard_deviation": deviation,
        "coefficient_of_variation": abs(deviation / average) if average else None,
        "p95": None,
        "p99": None,
        "p99.9": None,
        "cvar99": None,
        "tail_status": "insufficient_evidence",
    }
    if len(cleaned) >= tail_min_samples:
        summary.update(
            {
                "p95": quantile(cleaned, 0.95),
                "p99": quantile(cleaned, 0.99),
                "p99.9": quantile(cleaned, 0.999),
                "cvar99": cvar(cleaned),
                "tail_status": "available",
            }
        )
    return summary


def bootstrap_interval(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = median,
    confidence: float = 0.95,
    resamples: int = 1000,
    seed: int = 0,
) -> tuple[float, float]:
    cleaned = _clean(values)
    if len(cleaned) < 2:
        raise InsufficientEvidence("bootstrap requires at least two observations")
    generator = random.Random(seed)
    estimates = [
        statistic([generator.choice(cleaned) for _ in range(len(cleaned))])
        for _ in range(resamples)
    ]
    alpha = (1 - confidence) / 2
    return quantile(estimates, alpha), quantile(estimates, 1 - alpha)


def _benefit(
    candidate: float, baseline: float, direction: Direction, comparison: Comparison
) -> float:
    raw = candidate - baseline if direction == Direction.MAXIMIZE else baseline - candidate
    if comparison == Comparison.ABSOLUTE:
        return candidate
    if comparison == Comparison.DIFFERENCE:
        return raw
    if baseline == 0:
        raise InsufficientEvidence("relative comparison is undefined for a zero baseline")
    return raw / abs(baseline)


def bootstrap_improvement(
    candidate: Sequence[float],
    baseline: Sequence[float],
    direction: Direction | str,
    aggregation: Aggregation | str = Aggregation.MEDIAN,
    comparison: Comparison | str = Comparison.RELATIVE,
    confidence: float = 0.95,
    resamples: int = 1000,
    seed: int = 0,
) -> dict[str, float | int]:
    candidate_values = _clean(candidate)
    baseline_values = _clean(baseline)
    if len(candidate_values) < 2 or len(baseline_values) < 2:
        raise InsufficientEvidence("improvement confidence requires two samples per group")
    selected_direction = Direction(direction)
    selected_aggregation = Aggregation(aggregation)
    selected_comparison = Comparison(comparison)
    point = _benefit(
        aggregate(candidate_values, selected_aggregation),
        aggregate(baseline_values, selected_aggregation),
        selected_direction,
        selected_comparison,
    )
    generator = random.Random(seed)
    estimates: list[float] = []
    for _ in range(resamples):
        candidate_sample = [generator.choice(candidate_values) for _ in candidate_values]
        baseline_sample = [generator.choice(baseline_values) for _ in baseline_values]
        estimates.append(
            _benefit(
                aggregate(candidate_sample, selected_aggregation),
                aggregate(baseline_sample, selected_aggregation),
                selected_direction,
                selected_comparison,
            )
        )
    alpha = (1 - confidence) / 2
    return {
        "estimate": point,
        "lower": quantile(estimates, alpha),
        "upper": quantile(estimates, 1 - alpha),
        "candidate_samples": len(candidate_values),
        "baseline_samples": len(baseline_values),
    }


def paired_bootstrap_improvement(
    candidate: Sequence[float],
    baseline: Sequence[float],
    direction: Direction | str,
    aggregation: Aggregation | str = Aggregation.MEDIAN,
    comparison: Comparison | str = Comparison.RELATIVE,
    confidence: float = 0.95,
    resamples: int = 1000,
    seed: int = 0,
) -> dict[str, float | int]:
    candidate_values = _clean(candidate)
    baseline_values = _clean(baseline)
    if len(candidate_values) != len(baseline_values):
        raise InsufficientEvidence("paired confidence requires equally sized groups")
    if len(candidate_values) < 2:
        raise InsufficientEvidence("paired confidence requires at least two pairs")
    selected_direction = Direction(direction)
    selected_aggregation = Aggregation(aggregation)
    selected_comparison = Comparison(comparison)
    point = _benefit(
        aggregate(candidate_values, selected_aggregation),
        aggregate(baseline_values, selected_aggregation),
        selected_direction,
        selected_comparison,
    )
    generator = random.Random(seed)
    estimates: list[float] = []
    pair_indexes = list(range(len(candidate_values)))
    for _ in range(resamples):
        selected = [generator.choice(pair_indexes) for _ in pair_indexes]
        candidate_sample = [candidate_values[index] for index in selected]
        baseline_sample = [baseline_values[index] for index in selected]
        estimates.append(
            _benefit(
                aggregate(candidate_sample, selected_aggregation),
                aggregate(baseline_sample, selected_aggregation),
                selected_direction,
                selected_comparison,
            )
        )
    alpha = (1 - confidence) / 2
    return {
        "estimate": point,
        "lower": quantile(estimates, alpha),
        "upper": quantile(estimates, 1 - alpha),
        "pair_count": len(candidate_values),
    }


def cluster_paired_bootstrap_improvement(
    candidate_clusters: Mapping[str, Sequence[float]],
    baseline_clusters: Mapping[str, Sequence[float]],
    direction: Direction | str,
    aggregation: Aggregation | str = Aggregation.MEDIAN,
    comparison: Comparison | str = Comparison.RELATIVE,
    confidence: float = 0.95,
    resamples: int = 1000,
    seed: int = 0,
) -> dict[str, float | int]:
    if set(candidate_clusters) != set(baseline_clusters):
        raise InsufficientEvidence("cluster comparison requires identical placement pair ids")
    selected_aggregation = Aggregation(aggregation)
    cluster_ids = sorted(candidate_clusters)
    if len(cluster_ids) < 2:
        raise InsufficientEvidence("cluster confidence requires at least two placement pairs")
    candidate_values = [
        aggregate(_clean(candidate_clusters[cluster_id]), selected_aggregation)
        for cluster_id in cluster_ids
    ]
    baseline_values = [
        aggregate(_clean(baseline_clusters[cluster_id]), selected_aggregation)
        for cluster_id in cluster_ids
    ]
    result = paired_bootstrap_improvement(
        candidate_values,
        baseline_values,
        direction,
        selected_aggregation,
        comparison,
        confidence,
        resamples,
        seed,
    )
    result["cluster_count"] = len(cluster_ids)
    result["candidate_samples"] = sum(len(candidate_clusters[item]) for item in cluster_ids)
    result["baseline_samples"] = sum(len(baseline_clusters[item]) for item in cluster_ids)
    return result


def gate_passes(gate: GateSpec, value: float | bool | None) -> bool:
    if gate.operator == Operator.TRUE:
        return value is True
    if gate.operator == Operator.FALSE:
        return value is False
    if value is None or gate.threshold is None:
        return False
    left = float(value)
    right = float(gate.threshold)
    operations = {
        Operator.EQ: lambda: left == right,
        Operator.NE: lambda: left != right,
        Operator.LT: lambda: left < right,
        Operator.LTE: lambda: left <= right,
        Operator.GT: lambda: left > right,
        Operator.GTE: lambda: left >= right,
    }
    return operations[gate.operator]()


def pareto_ranks(
    points: Sequence[Mapping[str, Any]],
    objective_directions: Mapping[str, Direction | str],
    epsilon: Mapping[str, float] | None = None,
) -> dict[str, int | None]:
    eps = epsilon or {}
    feasible = [point for point in points if point.get("feasible") is True]
    ranks: dict[str, int | None] = {str(point["id"]): None for point in points}

    def dominates(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        no_worse = True
        strictly_better = False
        for metric, direction_value in objective_directions.items():
            if metric not in left["objectives"] or metric not in right["objectives"]:
                return False
            direction = Direction(direction_value)
            multiplier = 1 if direction == Direction.MAXIMIZE else -1
            left_value = multiplier * float(left["objectives"][metric])
            right_value = multiplier * float(right["objectives"][metric])
            tolerance = float(eps.get(metric, 0.0))
            if left_value < right_value - tolerance:
                no_worse = False
                break
            if left_value > right_value + tolerance:
                strictly_better = True
        return no_worse and strictly_better

    remaining = list(feasible)
    rank = 1
    while remaining:
        front = [
            point
            for point in remaining
            if not any(dominates(other, point) for other in remaining if other is not point)
        ]
        if not front:
            raise RuntimeError("could not resolve Pareto front")
        for point in front:
            ranks[str(point["id"])] = rank
        front_ids = {str(point["id"]) for point in front}
        remaining = [point for point in remaining if str(point["id"]) not in front_ids]
        rank += 1
    return ranks


BENCHTRUST_METHOD_VERSION = "1.0.0"

#: Valid BenchTrust statuses. ``available`` = measurable with sufficient evidence,
#: ``partial`` = some but not all declared axes/factors measurable,
#: ``insufficient_evidence`` = evidence exists but too few to claim meaning,
#: ``unavailable`` = the required input contract or scoring decomposition is absent.
BENCHTRUST_STATUSES = frozenset(
    {"available", "partial", "insufficient_evidence", "unavailable"}
)


def _z_score(confidence: float) -> float:
    values = {
        0.90: 1.6448536269514722,
        0.95: 1.959963984540054,
        0.99: 2.5758293035489004,
    }
    return values.get(confidence, 1.959963984540054)


def _wilson_interval(successes: int, total: int, confidence: float) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    z = _z_score(confidence)
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def ranking_groups(scores: Mapping[str, float], *, maximize: bool) -> list[list[str]]:
    """Turn a score mapping into a ranked order as tie-groups.

    Items with equal scores share a group; ties are never broken by item order.
    """
    by_value: dict[float, list[str]] = {}
    for item, value in scores.items():
        numeric = float(value)
        if not math.isfinite(numeric):
            continue
        by_value.setdefault(numeric, []).append(item)
    ordered = sorted(by_value.items(), key=lambda pair: pair[0], reverse=maximize)
    return [sorted(items) for _, items in ordered]


def kendall_tau_b(
    first_groups: Sequence[Sequence[str]], second_groups: Sequence[Sequence[str]]
) -> float | None:
    """Kendall tau-b between two rankings expressed as tie-groups (rank order).

    Ties are handled explicitly (tau-b), so a shared score is never resolved by
    touching the item id. Returns None when fewer than two items are comparable.
    """
    first_rank = {item: rank for rank, group in enumerate(first_groups) for item in group}
    second_rank = {item: rank for rank, group in enumerate(second_groups) for item in group}
    common = [item for item in first_rank if item in second_rank]
    if len(common) < 2:
        return None
    concordant = 0
    discordant = 0
    ties_first = 0
    ties_second = 0
    for index, left in enumerate(common):
        for right in common[index + 1 :]:
            delta_first = first_rank[left] - first_rank[right]
            delta_second = second_rank[left] - second_rank[right]
            if delta_first == 0 and delta_second == 0:
                continue
            if delta_first != 0 and delta_second != 0:
                if (delta_first > 0) == (delta_second > 0):
                    concordant += 1
                else:
                    discordant += 1
            elif delta_first == 0:
                ties_first += 1
            else:
                ties_second += 1
    denominator = math.sqrt(
        (concordant + discordant + ties_first) * (concordant + discordant + ties_second)
    )
    if denominator == 0:
        return 1.0
    return (concordant - discordant) / denominator


def pairwise_flip_rate(
    first_groups: Sequence[Sequence[str]], second_groups: Sequence[Sequence[str]]
) -> float | None:
    """Fraction of strictly-ordered pairs whose relative order flips between rankings.

    Pairs tied in either ranking are not "flips" and are excluded from the rate.
    """
    first_rank = {item: rank for rank, group in enumerate(first_groups) for item in group}
    second_rank = {item: rank for rank, group in enumerate(second_groups) for item in group}
    common = [item for item in first_rank if item in second_rank]
    comparable = 0
    flips = 0
    for index, left in enumerate(common):
        for right in common[index + 1 :]:
            delta_first = first_rank[left] - first_rank[right]
            delta_second = second_rank[left] - second_rank[right]
            if delta_first == 0 or delta_second == 0:
                continue
            comparable += 1
            if (delta_first > 0) != (delta_second > 0):
                flips += 1
    return flips / comparable if comparable else None


def rank_stability(rankings: Sequence[Sequence[Sequence[str]]]) -> dict[str, Any]:
    """Aggregate rank stability across ranking slices of one environment axis.

    ``rankings`` is a list of slices; each slice is itself a list of tie-groups in
    rank order. Only slices expressing the same comparable candidate collection are
    meaningful: callers must not conflate distinct workloads or candidate sets.
    """
    taus: list[float] = []
    flips: list[float] = []
    tie_group_count = 0
    candidate_ids: set[str] = set()
    for slice_ in rankings:
        for group in slice_:
            if len(group) > 1:
                tie_group_count += 1
            candidate_ids.update(group)
    for index, first in enumerate(rankings):
        for second in rankings[index + 1 :]:
            tau = kendall_tau_b(first, second)
            if tau is not None:
                taus.append(tau)
            flip = pairwise_flip_rate(first, second)
            if flip is not None:
                flips.append(flip)
    comparisons = len(taus)
    return {
        "slice_count": len(rankings),
        "candidate_count": len(candidate_ids),
        "comparison_count": comparisons,
        "method": "kendall_tau_b",
        "median_tau": median(taus) if taus else None,
        "minimum_tau": min(taus) if taus else None,
        "maximum_tau": max(taus) if taus else None,
        "pairwise_flip_rate": mean(flips) if flips else None,
        "tie_count": tie_group_count,
    }


def rank_stability_by_axes(axes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rank stability measured independently per declared environment axis."""
    rendered: list[dict[str, Any]] = []
    measurable = 0
    declared = len(axes)
    for axis in axes:
        metric = rank_stability(axis.get("rankings", []))
        scoreable = metric["comparison_count"] > 0
        if scoreable:
            measurable += 1
        rendered.append(
            {
                "axis": axis.get("axis"),
                "scoring_formula_ids": axis.get("scoring_formula_ids"),
                "limitations": axis.get("limitations", []),
                **metric,
            }
        )
    if declared == 0:
        status = "unavailable"
    elif measurable == 0:
        status = "insufficient_evidence"
    elif measurable < declared:
        status = "partial"
    else:
        status = "available"
    return {
        "status": status,
        "axes": rendered,
        "limitations": [
            "不同 workload 的排名不能自动视为环境重复排名",
            "只有同 Benchmark、版本、workload 与候选集合下的排名才可比较",
            "少于两个可比较排名切片时该轴返回 insufficient_evidence",
        ],
    }


def reference_validity_rate(
    environments: Sequence[Mapping[str, Any]],
    *,
    expected_direction: str,
    minimum_effect: float,
    min_repeats: int = 3,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    """Reference validity measured on the unit of *target environment*.

    Each environment record supplies ``eligible`` (has paired reference + baseline +
    correctness evidence) and ``valid`` (direction-consistent AND gate-passed AND
    meets minimum repeats), computed by the caller against the declared direction.
    """
    if not environments:
        return {
            "status": "unavailable",
            "method": (
                "proportion of eligible target environments in which the reference "
                "outperforms baseline with a direction-consistent signal"
            ),
            "valid_environment_count": 0,
            "eligible_environment_count": 0,
            "excluded_environment_count": 0,
            "rate": None,
            "confidence_interval": None,
            "expected_direction": expected_direction,
            "minimum_effect": minimum_effect,
            "environment_results": [],
            "criteria": [],
            "limitations": ["没有可评价的目标环境或引用/基线配对合同"],
        }
    eligible_count = sum(1 for env in environments if env.get("eligible"))
    valid_count = sum(
        1 for env in environments if env.get("eligible") and env.get("valid")
    )
    excluded_count = len(environments) - eligible_count
    rate = (valid_count / eligible_count) if eligible_count else None
    interval = (
        _wilson_interval(valid_count, eligible_count, confidence_level)
        if eligible_count
        else None
    )
    if eligible_count < 2:
        status = "insufficient_evidence"
    elif valid_count < eligible_count:
        status = "partial"
    else:
        status = "available"
    environment_results = [
        {
            "environment_id": env.get("environment_id"),
            "environment_fingerprint": env.get("environment_fingerprint"),
            "eligible": bool(env.get("eligible")),
            "excluded_reason": env.get("excluded_reason"),
            "reference_value": env.get("reference_value"),
            "baseline_value": env.get("baseline_value"),
            "benefit": env.get("benefit"),
            "benefit_lower": env.get("benefit_lower"),
            "benefit_upper": env.get("benefit_upper"),
            "repeat_count": env.get("repeat_count"),
            "valid": env.get("valid"),
            "invalid_reason": env.get("invalid_reason"),
        }
        for env in environments
    ]
    return {
        "status": status,
        "method": (
            "proportion of eligible target environments in which the reference "
            "outperforms baseline with a direction-consistent signal"
        ),
        "valid_environment_count": valid_count,
        "eligible_environment_count": eligible_count,
        "excluded_environment_count": excluded_count,
        "rate": rate,
        "confidence_interval": list(interval) if interval else None,
        "expected_direction": expected_direction,
        "minimum_effect": minimum_effect,
        "environment_results": environment_results,
        "criteria": [
            "环境具备可配对的 Reference 与 Baseline 结果",
            "通过正确性/有效性门禁",
            "满足最小重复数",
            "参考收益方向与声明方向一致且达到最小效果",
        ],
        "limitations": [
            "单个环境无法构成跨环境有效性，整体状态为 insufficient_evidence",
            "因缺失结果或门禁失败被排除的环境单独列出原因",
        ],
    }


def _eta_squared_groups(groups: Mapping[str, Sequence[float]]) -> float | None:
    values = [value for group in groups.values() for value in group]
    if len(groups) < 2 or len(values) < 3:
        return None
    grand = mean(values)
    total = sum((value - grand) ** 2 for value in values)
    if total <= 0:
        return 0.0
    between = sum(len(group) * (mean(group) - grand) ** 2 for group in groups.values())
    return between / total


def _bootstrap_eta_ci(
    groups: Mapping[str, Sequence[float]],
    confidence: float,
    resamples: int,
    seed: int,
) -> tuple[float, float] | None:
    point = _eta_squared_groups(groups)
    if point is None or len(groups) < 2:
        return None
    generator = random.Random(seed)
    estimates: list[float] = []
    for _ in range(resamples):
        resampled = {
            key: [generator.choice(values) for _ in values]
            for key, values in groups.items()
            if values
        }
        estimate = _eta_squared_groups(resampled)
        if estimate is not None:
            estimates.append(estimate)
    if len(estimates) < 20:
        return None
    alpha = (1 - confidence) / 2
    return quantile(estimates, alpha), quantile(estimates, 1 - alpha)


def _center_by_group(values: Sequence[float], keys: Sequence[object]) -> list[float]:
    groups: dict[str, list[float]] = {}
    for key, value in zip(keys, values, strict=True):
        if key is None:
            continue
        groups.setdefault(str(key), []).append(value)
    centers = {key: mean(group) for key, group in groups.items()}
    return [
        value - centers[str(key)]
        if key is not None and str(key) in centers
        else value
        for key, value in zip(keys, values, strict=True)
    ]


def environment_sensitivity(
    records: Sequence[Mapping[str, Any]],
    factors: Sequence[str],
    *,
    controls: Sequence[str] = ("workload", "candidate"),
    minimum_samples: int = 5,
    confidence: float = 0.95,
    resamples: int = 200,
    seed: int = 0,
) -> dict[str, Any]:
    """Statistical *association* between environment factors and residual variance.

    Input records are one metric/unit only (callers must not mix units). Workload
    and candidate fixed effects are removed by group-mean centering before each
    factor is assessed with a single-factor eta-squared. Reports are correlations,
    never causal claims (``association_only`` is always True).
    """
    present = [
        record for record in records if math.isfinite(float(record.get("value", math.nan)))
    ]
    if not factors:
        return _environment_result("unavailable", "没有声明环境因素", factors=[])
    if len(present) < minimum_samples:
        return _environment_result(
            "insufficient_evidence",
            f"样本数 {len(present)} 低于下限 {minimum_samples}",
            factors=factors,
            sample_count=len(present),
        )

    residual = [float(record["value"]) for record in present]
    used_controls = [control for control in controls if any(control in r for r in present)]
    for control in used_controls:
        residual = _center_by_group(
            residual, [record.get(control) for record in present]
        )

    rendered_factors: list[dict[str, Any]] = []
    warnings: list[str] = []
    factor_present = [name for name in factors if name in present[0]]
    for factor in factor_present:
        keys = [record.get(factor) for record in present]
        missing = sum(1 for key in keys if key is None)
        missing_rate = missing / len(present) if present else 0.0
        groups: dict[str, list[float]] = {}
        for key, value in zip(keys, residual, strict=True):
            if key is None:
                continue
            groups.setdefault(str(key), []).append(value)
        eta = _eta_squared_groups(groups)
        if missing_rate > 0.5:
            warnings.append(f"因素 '{factor}' 缺失率 {missing_rate:.0%}，结果不可靠")
            eta = None
        with_ci = (
            _bootstrap_eta_ci(groups, confidence, resamples, seed)
            if eta is not None
            else None
        )
        sizes = sorted(len(group) for group in groups.values())
        if sizes and sizes[0] < 2 and (sizes[-1] - sizes[0]) >= 8:
            warnings.append(
                f"因素 '{factor}' 样本严重不平衡，"
                f"最小组 {sizes[0]}、最大组 {sizes[-1]}"
            )
        rendered_factors.append(
            {
                "factor": factor,
                "group_count": len(groups),
                "sample_count": sum(len(group) for group in groups.values()),
                "associated_variance_ratio": round(eta, 4) if eta is not None else None,
                "confidence_interval": [round(x, 4) for x in with_ci] if with_ci else None,
                "missing_rate": round(missing_rate, 4),
            }
        )

    # Collinearity: one factor's levels map bijectively onto another's.
    for left_index, left in enumerate(factor_present):
        for right in factor_present[left_index + 1 :]:
            left_keys = [str(record.get(left)) for record in present]
            right_keys = [str(record.get(right)) for record in present]
            mapping: dict[str, str] = {}
            bijective = True
            for lk, rk in zip(left_keys, right_keys, strict=True):
                if lk in mapping and mapping[lk] != rk:
                    bijective = False
                    break
                mapping[lk] = rk
            if bijective and len(set(left_keys)) == len(set(right_keys)):
                warnings.append(f"因素 '{left}' 与 '{right}' 共线，无法区分各自关联")

    joint_groups: dict[str, list[float]] = {}
    for record, value in zip(present, residual, strict=True):
        key = tuple(record.get(factor) for factor in factor_present)
        if any(part is None for part in key):
            continue
        joint_groups.setdefault("|".join(str(part) for part in key), []).append(value)
    total_explained = _eta_squared_groups(joint_groups)
    residual_ratio = (1.0 - total_explained) if total_explained is not None else None

    measurable = sum(
        1
        for factor in rendered_factors
        if factor["associated_variance_ratio"] is not None
    )
    if measurable == len(factor_present) and measurable > 0:
        status = "available"
    elif measurable > 0:
        status = "partial"
    else:
        status = "insufficient_evidence"
    return {
        "status": status,
        "method": (
            "control workload/candidate by group-mean centering, then single-factor "
            "eta-squared per environment factor on the residuals"
        ),
        "analysis_unit": "per-observation residual (single metric and unit)",
        "sample_count": len(present),
        "controls": used_controls,
        "total_explained_ratio": round(total_explained, 4) if total_explained is not None else None,
        "factors": rendered_factors,
        "residual_ratio": round(residual_ratio, 4) if residual_ratio is not None else None,
        "warnings": warnings,
        "limitations": [
            "单因素 η² 之间可能重叠，不相加为总解释率",
            "总解释率采用全部因素的联合分组，与单个因素口径不同",
            "结果只表示统计关联，不代表因果关系",
        ],
        "association_only": True,
    }


def _environment_result(
    status: str, message: str, *, factors: Sequence[str], sample_count: int = 0
) -> dict[str, Any]:
    return {
        "status": status,
        "method": (
            "control workload/candidate by group-mean centering, then single-factor "
            "eta-squared per environment factor on the residuals"
        ),
        "analysis_unit": "per-observation residual (single metric and unit)",
        "sample_count": sample_count,
        "controls": [],
        "total_explained_ratio": None,
        "factors": [],
        "residual_ratio": None,
        "warnings": [message],
        "limitations": ["结果只表示统计关联，不代表因果关系"],
        "association_only": True,
    }


def task_leverage(
    scores: Mapping[str, Mapping[str, float]],
    weights: Mapping[str, float] | None = None,
    *,
    scoring_formula: str | None = None,
    aggregation_method: str | None = None,
    decomposable: bool = True,
) -> dict[str, Any]:
    """Single-task share of the total (weighted-additive) score.

    ``scores[candidate][task]`` is the normalized task value (higher = better).
    ``maximum_contribution_share = max_task sum(abs(w_t * v_{c,t}))
    / sum over all tasks of abs contributions``; leave-one-out ranking is a
    diagnostic. A non-additive scoring formula must declare ``decomposable=False``
    (returns unavailable rather than assuming an arithmetic mean).
    """

    def _result(
        status: str,
        *,
        maximum_contribution_share: float | None = None,
        dominant_task: str | None = None,
        top_contributors: list[dict[str, Any]] | None = None,
        maximum_rank_shift: float | None = None,
        winner_changed: bool | None = None,
        task_shifts: dict[str, int] | None = None,
        limitations: Sequence[str] = (),
    ) -> dict[str, Any]:
        return {
            "status": status,
            "scoring_formula": scoring_formula,
            "aggregation_method": aggregation_method,
            "maximum_contribution_share": maximum_contribution_share,
            "dominant_task": dominant_task,
            "top_contributors": top_contributors or [],
            "leave_one_out": {
                "maximum_rank_shift": maximum_rank_shift,
                "winner_changed": winner_changed,
                "task_shifts": task_shifts or {},
            },
            "limitations": list(limitations),
        }

    if not decomposable:
        return _result(
            "unavailable",
            limitations=["计分公式不可加性分解，未提供正式的任务贡献定义"],
        )
    if not scores:
        return _result("unavailable", limitations=["没有候选得分输入合同"])
    tasks = sorted({task for candidate in scores.values() for task in candidate})
    if len(tasks) < 2:
        return _result(
            "insufficient_evidence",
            limitations=["只有一个任务，无法评估任务集中度（不判定为 100% 风险）"],
        )
    selected_weights = {task: float((weights or {}).get(task, 1.0)) for task in tasks}

    absolute_by_task: dict[str, float] = {}
    for task in tasks:
        absolute_by_task[task] = sum(
            abs(selected_weights[task] * float(values.get(task, 0.0)))
            for values in scores.values()
        )
    abs_total = sum(absolute_by_task.values())
    if abs_total <= 0:
        return _result(
            "insufficient_evidence",
            limitations=["任务贡献绝对值总和接近零，已采用绝对贡献占比但无法计算"],
        )
    dominant_task = max(absolute_by_task, key=lambda task: absolute_by_task[task])
    maximum_contribution_share = absolute_by_task[dominant_task] / abs_total

    ranked = sorted(tasks, key=lambda task: -absolute_by_task[task])
    top_contributors = [
        {
            "task_id": task,
            "weight": selected_weights[task],
            "contribution": absolute_by_task[task],
            "contribution_share": absolute_by_task[task] / abs_total,
        }
        for task in ranked[:5]
    ]

    def ranking(excluded: str | None = None) -> list[list[str]]:
        totals = {
            candidate: sum(
                selected_weights[task] * float(value)
                for task, value in candidate_scores.items()
                if task != excluded
            )
            for candidate, candidate_scores in scores.items()
        }
        return ranking_groups(totals, maximize=True)

    full = ranking()
    full_positions = {item: rank for rank, group in enumerate(full) for item in group}
    task_shifts: dict[str, int] = {}
    for task in tasks:
        reduced = ranking(task)
        reduced_positions = {
            item: rank for rank, group in enumerate(reduced) for item in group
        }
        comparable = set(full_positions) & set(reduced_positions)
        if len(comparable) < 2:
            continue
        task_shifts[task] = max(
            abs(full_positions[candidate] - reduced_positions[candidate])
            for candidate in comparable
        )
    maximum_rank_shift = max(task_shifts.values()) if task_shifts else None
    winner_changed = None
    if full:
        original_top = set(full[0])
        winner_changed = any(set(ranking(task)[0]) != original_top for task in tasks)

    return _result(
        "available",
        maximum_contribution_share=maximum_contribution_share,
        dominant_task=dominant_task,
        top_contributors=top_contributors,
        maximum_rank_shift=maximum_rank_shift,
        winner_changed=winner_changed,
        task_shifts=task_shifts,
        limitations=[],
    )
