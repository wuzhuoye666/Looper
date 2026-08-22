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


def reference_validity_rate(values: Sequence[bool]) -> float | None:
    return sum(1 for value in values if value) / len(values) if values else None


def kendall_tau(first: Sequence[str], second: Sequence[str]) -> float | None:
    common = [item for item in first if item in set(second)]
    if len(common) < 2:
        return None
    first_position = {item: index for index, item in enumerate(first)}
    second_position = {item: index for index, item in enumerate(second)}
    concordant = 0
    discordant = 0
    for left_index, left in enumerate(common):
        for right in common[left_index + 1 :]:
            first_delta = first_position[left] - first_position[right]
            second_delta = second_position[left] - second_position[right]
            if first_delta * second_delta > 0:
                concordant += 1
            elif first_delta * second_delta < 0:
                discordant += 1
    pairs = concordant + discordant
    return (concordant - discordant) / pairs if pairs else None


def rank_stability(rankings: Sequence[Sequence[str]]) -> dict[str, float | int | None]:
    taus: list[float] = []
    for index, first in enumerate(rankings):
        for second in rankings[index + 1 :]:
            tau = kendall_tau(first, second)
            if tau is not None:
                taus.append(tau)
    return {
        "comparison_count": len(taus),
        "mean_kendall_tau": mean(taus) if taus else None,
        "stability_0_to_1": mean((tau + 1) / 2 for tau in taus) if taus else None,
    }


def environment_sensitivity(groups: Mapping[str, Sequence[float]]) -> dict[str, float | int | None]:
    cleaned = {name: _clean(values) for name, values in groups.items() if values}
    total_count = sum(len(values) for values in cleaned.values())
    if len(cleaned) < 2 or total_count < 3:
        return {"environment_count": len(cleaned), "sample_count": total_count, "eta_squared": None}
    all_values = [value for values in cleaned.values() for value in values]
    grand_mean = mean(all_values)
    between = sum(len(values) * (mean(values) - grand_mean) ** 2 for values in cleaned.values())
    total = sum((value - grand_mean) ** 2 for value in all_values)
    return {
        "environment_count": len(cleaned),
        "sample_count": total_count,
        "eta_squared": between / total if total else 0.0,
    }


def task_leverage(
    scores: Mapping[str, Mapping[str, float]], weights: Mapping[str, float] | None = None
) -> dict[str, Any]:
    if len(scores) < 2:
        return {"max_rank_shift": None, "dominant_task": None, "task_shifts": {}}
    tasks = sorted({task for candidate in scores.values() for task in candidate})
    selected_weights = {task: float((weights or {}).get(task, 1.0)) for task in tasks}

    def ranking(excluded: str | None = None) -> list[str]:
        totals: dict[str, float] = {}
        for candidate_id, candidate_scores in scores.items():
            numerator = 0.0
            denominator = 0.0
            for task, value in candidate_scores.items():
                if task == excluded:
                    continue
                weight = selected_weights[task]
                numerator += weight * value
                denominator += weight
            if denominator:
                totals[candidate_id] = numerator / denominator
        return sorted(totals, key=lambda item: (-totals[item], item))

    full = ranking()
    full_positions = {candidate: index for index, candidate in enumerate(full)}
    shifts: dict[str, int] = {}
    for task in tasks:
        reduced = ranking(task)
        reduced_positions = {candidate: index for index, candidate in enumerate(reduced)}
        comparable = set(full_positions) & set(reduced_positions)
        if len(comparable) < 2:
            continue
        shifts[task] = max(
            abs(full_positions[candidate] - reduced_positions[candidate])
            for candidate in comparable
        )
    dominant = max(shifts, key=lambda item: shifts[item]) if shifts else None
    return {
        "max_rank_shift": shifts.get(dominant) if dominant else None,
        "dominant_task": dominant,
        "task_shifts": shifts,
    }
