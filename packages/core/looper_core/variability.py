"""VGO-inspired variability analyzer: is performance stable, how does it vary?

The analyzer is a reusable component that every benchmark shares. It consumes
already-normalized run samples (one row per repeated run, plus optional system
metrics) and never parses upstream benchmark formats.

Analysis chain (VGO, Frachtenberg et al., ICPE '26, simplified for P0):

    repeated runs -> distribution statistics -> stability verdict
        -> outlier / fast-slow mode split -> system-metric association clues
        -> variance attribution across host / placement / date / time block
        -> control-variable A/B experiment recommendations

Everything the analyzer reports about causes is a *correlation clue*, never a
causal claim: mitigation suggestions are always phrased as experiments to run.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from statistics import mean, median, pstdev
from typing import Any, Literal

from pydantic import Field

from looper_core.analysis import cvar, quantile
from looper_core.contracts import Direction, StabilityMetric, StabilityObjectiveSpec, StrictModel

VARIABILITY_ANALYZER_ID = "looper.variability-analyzer"
VARIABILITY_CODE_VERSION = "1.1.0"

#: System metrics the analyzer knows how to interpret. Benchmarks and adapters
#: emit them as ordinary observations using these canonical names.
SYSTEM_METRIC_NAMES = (
    "cpu_migration_count",
    "context_switch_count",
    "page_fault_count",
    "dtlb_miss_count",
    "cache_miss_count",
    "numa_migration_count",
    "cpu_frequency_mhz",
    "cpu_utilization_percent",
    "iowait_percent",
    "run_queue_depth",
)

#: Metrics that are usually a *consequence* of slow runs rather than a cause
#: (VGO explicitly filters these out before interpreting associations).
CONSEQUENCE_METRICS = frozenset({"cycles", "instructions", "cpu_time", "elapsed_time", "wall_time"})

StabilityVerdict = Literal["stable", "warning", "unstable", "insufficient_evidence"]
RunLabel = Literal["normal", "slow_mode", "fast_mode", "slow_outlier", "fast_outlier"]
ComparisonVerdict = Literal[
    "dominant", "dominated", "mean_better_tail_worse", "mean_worse_tail_better", "inconclusive"
]


class VariabilityPolicy(StrictModel):
    """Thresholds that turn statistics into verdicts.

    All thresholds are part of the analysis policy digest, so changing them
    produces a new snapshot instead of silently mutating old conclusions.
    """

    minimum_samples: int = Field(default=5, alias="minimumSamples", ge=2)
    cv_stable: float = Field(default=0.05, alias="cvStable", gt=0)
    cv_unstable: float = Field(default=0.15, alias="cvUnstable", gt=0)
    slow_share_unstable: float = Field(default=0.30, alias="slowShareUnstable", gt=0, le=1)
    outlier_fence_k: float = Field(default=1.5, alias="outlierFenceK", gt=0)
    mode_min_r2: float = Field(default=0.5, alias="modeMinR2", ge=0, le=1)
    mode_min_cluster_share: float = Field(default=0.10, alias="modeMinClusterShare", gt=0, le=0.5)
    mode_gap_ratio: float = Field(
        default=2.5,
        alias="modeGapRatio",
        gt=0,
        description="Two modes are declared only when the gap between clusters "
        "exceeds this multiple of the combined within-cluster standard deviation.",
    )
    skew_threshold: float = Field(default=1.0, alias="skewThreshold", gt=0)
    association_min_correlation: float = Field(
        default=0.3, alias="associationMinCorrelation", ge=0, le=1
    )
    association_min_lift: float = Field(default=1.2, alias="associationMinLift", gt=1)
    attribution_min_eta_squared: float = Field(
        default=0.25, alias="attributionMinEtaSquared", ge=0, le=1
    )


class RunSample(StrictModel):
    """One repeated run of a benchmark under one configuration."""

    run_id: str = Field(alias="runId", min_length=1, max_length=120)
    value: float
    within_run_std: float | None = Field(default=None, alias="withinRunStd", ge=0)
    target_id: str | None = Field(default=None, alias="targetId", max_length=120)
    host_id: str | None = Field(default=None, alias="hostId", max_length=160)
    placement_pair_id: str | None = Field(default=None, alias="placementPairId", max_length=160)
    date: str | None = Field(default=None, max_length=32)
    time_block_id: str | None = Field(default=None, alias="timeBlockId", max_length=160)
    system_metrics: dict[str, float] = Field(default_factory=dict, alias="systemMetrics")


class DistributionStats(StrictModel):
    count: int = Field(ge=1)
    mean: float
    median: float
    standard_deviation: float = Field(alias="standardDeviation", ge=0)
    coefficient_of_variation: float | None = Field(default=None, alias="coefficientOfVariation")
    minimum: float
    maximum: float
    p05: float | None = None
    p95: float | None = None
    p99: float | None = None
    iqr: float | None = None
    mad: float | None = None
    tail_mean: float | None = Field(
        default=None, alias="tailMean", description="CVaR over the worst 5% tail"
    )
    skewness: float | None = None


class ModeSplit(StrictModel):
    """Suspected fast/slow modes found by a one-dimensional cutoff search."""

    cutoff: float
    fast_mode: dict[str, float | int] = Field(alias="fastMode")
    slow_mode: dict[str, float | int] = Field(alias="slowMode")


class RunClassification(StrictModel):
    run_id: str = Field(alias="runId")
    value: float
    label: RunLabel
    slow: bool


class AssociationClue(StrictModel):
    """A correlation between one system metric and slow runs. Not a cause."""

    metric: str
    correlation: float
    lift: float | None
    direction: Literal["elevated_in_slow", "reduced_in_slow"]
    slow_mean: float | None = Field(default=None, alias="slowMean")
    normal_mean: float | None = Field(default=None, alias="normalMean")
    likely_consequence: bool = Field(default=False, alias="likelyConsequence")
    note: str


class AttributionEntry(StrictModel):
    dimension: str
    eta_squared: float | None = Field(alias="etaSquared")
    group_count: int = Field(alias="groupCount")
    dominant: bool = False
    group_means: dict[str, float] = Field(default_factory=dict, alias="groupMeans")


class Recommendation(StrictModel):
    action: str
    rationale: str
    priority: Literal["high", "medium", "low"]
    kind: Literal["control_experiment", "data_collection", "design_change"]


class VariabilityReport(StrictModel):
    schema_version: Literal["v1alpha1"] = Field(default="v1alpha1", alias="schemaVersion")
    analyzer: str = VARIABILITY_ANALYZER_ID
    analyzer_version: str = Field(default=VARIABILITY_CODE_VERSION, alias="analyzerVersion")
    group_label: str = Field(alias="groupLabel")
    metric: str
    unit: str
    direction: str
    status: StabilityVerdict
    distribution: DistributionStats
    stability: dict[str, Any]
    modes: ModeSplit | None = None
    runs: list[RunClassification]
    outliers: dict[str, list[str]]
    association_clues: list[AssociationClue] = Field(default_factory=list, alias="associationClues")
    attribution: list[AttributionEntry]
    recommendations: list[Recommendation]
    selection_impact: dict[str, Any] = Field(default_factory=dict, alias="selectionImpact")
    evidence: dict[str, Any]


class DistributionComparison(StrictModel):
    schema_version: Literal["v1alpha1"] = Field(default="v1alpha1", alias="schemaVersion")
    analyzer: str = VARIABILITY_ANALYZER_ID
    metric: str
    unit: str
    direction: str
    baseline_label: str = Field(alias="baselineLabel")
    candidate_label: str = Field(alias="candidateLabel")
    baseline: DistributionStats
    candidate: DistributionStats
    mean_improvement: float | None = Field(default=None, alias="meanImprovement")
    median_improvement: float | None = Field(default=None, alias="medianImprovement")
    tail_improvement: float | None = Field(default=None, alias="tailImprovement")
    cv_ratio: float | None = Field(default=None, alias="cvRatio")
    slow_run_probability: dict[str, float | None] = Field(
        default_factory=dict, alias="slowRunProbability"
    )
    worst_host: dict[str, Any] = Field(default_factory=dict, alias="worstHost")
    slo_exceedance: dict[str, Any] = Field(default_factory=dict, alias="sloExceedance")
    tail_worsened: bool = Field(default=False, alias="tailWorsened")
    verdict: ComparisonVerdict
    summary: str
    recommendation: str


class VariabilityError(ValueError):
    pass


def _finite(values: Sequence[float]) -> list[float]:
    cleaned = [float(value) for value in values]
    if not cleaned:
        raise VariabilityError("at least one run is required")
    if any(not math.isfinite(value) for value in cleaned):
        raise VariabilityError("run values must be finite")
    return cleaned


def _badness(value: float, direction: Direction) -> float:
    """Map a value onto a scale where larger always means worse."""

    return -value if direction == Direction.MAXIMIZE else value


def _skewness(values: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    average = mean(values)
    deviation = pstdev(values)
    if deviation == 0:
        return 0.0
    third = sum((value - average) ** 3 for value in values) / len(values)
    return third / deviation**3


def _distribution_stats(values: Sequence[float], direction: Direction) -> DistributionStats:
    """Distribution statistics on the original value scale.

    Quantiles are computed on the badness scale (larger = worse) so ``p95``
    always means "95% of runs are at least this good", then mapped back to
    the original scale for reporting -- a MAXIMIZE metric must never show up
    as negative means or reversed comparisons.
    """

    cleaned = _finite(values)
    ordered = sorted(_badness(value, direction) for value in cleaned)
    count = len(ordered)
    average = mean(ordered)
    deviation = pstdev(ordered) if count > 1 else 0.0
    q1 = quantile(ordered, 0.25)
    q3 = quantile(ordered, 0.75)
    med = median(ordered)
    tail = cvar(ordered, 0.95, upper_tail=True)

    def original(value: float) -> float:
        return -value if direction == Direction.MAXIMIZE else value

    # On the badness scale the first element is the *best* run, so the
    # reported minimum/maximum swap for MAXIMIZE metrics.
    minimum = original(ordered[0]) if direction == Direction.MINIMIZE else original(ordered[-1])
    maximum = original(ordered[-1]) if direction == Direction.MINIMIZE else original(ordered[0])
    return DistributionStats(
        count=count,
        mean=original(average),
        median=original(med),
        standard_deviation=deviation,
        coefficient_of_variation=abs(deviation / average) if average else None,
        minimum=minimum,
        maximum=maximum,
        p05=original(quantile(ordered, 0.05)),
        p95=original(quantile(ordered, 0.95)),
        p99=original(quantile(ordered, 0.99)),
        iqr=q3 - q1,
        mad=median(sorted(abs(value - med) for value in ordered)),
        tail_mean=original(tail),
        skewness=original(_skewness(ordered)),
    )


def stability_statistic(
    values: Sequence[float], metric: StabilityMetric, direction: Direction
) -> float | None:
    """Original-scale distribution statistic used by stability objectives.

    p95/p99/tail_mean keep the badness-scale semantics of ``_distribution_stats``
    ("95%/99% of runs are at least this good" / mean of the worst runs), so for
    a MAXIMIZE metric they are lower bounds on the original scale and for a
    MINIMIZE metric they are upper bounds. CV is dimensionless.
    """

    if not values:
        return None
    stats = _distribution_stats(values, direction)
    if metric == StabilityMetric.CV:
        return stats.coefficient_of_variation
    if metric == StabilityMetric.P95:
        return stats.p95
    if metric == StabilityMetric.P99:
        return stats.p99
    return stats.tail_mean


def _limit_satisfied(
    value: float, limit: float, metric: StabilityMetric, direction: Direction
) -> bool:
    """Direction-aware absolute bound: CV is always a cap; p95/p99/tail_mean
    are caps for MINIMIZE metrics and floors for MAXIMIZE metrics."""

    if metric == StabilityMetric.CV:
        return value <= limit
    if direction == Direction.MAXIMIZE:
        return value >= limit
    return value <= limit


def _tolerance_satisfied(
    value: float,
    baseline_value: float,
    tolerance: float,
    metric: StabilityMetric,
    direction: Direction,
) -> bool:
    """Candidate must not degrade more than ``tolerance`` relative to the
    baseline's same statistic (0.0 = strictly not worse)."""

    if metric == StabilityMetric.CV or direction == Direction.MINIMIZE:
        return value <= baseline_value * (1.0 + tolerance)
    return value >= baseline_value * (1.0 - tolerance)


def evaluate_stability_objective(
    values: Sequence[float],
    baseline_values: Sequence[float],
    objective: StabilityObjectiveSpec,
    direction: Direction,
) -> dict[str, Any]:
    """Evaluate one stability objective for one candidate.

    Fail closed: too few samples, an uncomputable statistic, or missing
    baseline evidence for a baseline-relative constraint all yield
    ``status="insufficient_evidence"`` / ``passed=False`` -- never a silent
    pass. Callers treat hard failures as infeasibility; soft objectives with
    ``pareto_value=None`` are excluded from Pareto dominance.
    """

    result: dict[str, Any] = {
        "id": objective.id,
        "metric": objective.metric.value,
        "target_metric": objective.target_metric,
        "hard": objective.hard,
        "sample_count": len(values),
        "minimum_samples": objective.minimum_samples,
        "value": None,
        "baseline_value": None,
        "limit": objective.limit,
        "baseline_tolerance": objective.baseline_tolerance,
        "pareto_value": None,
        "status": "insufficient_evidence",
        "passed": False,
        "reason": None,
    }
    if len(values) < objective.minimum_samples:
        result["reason"] = (
            f"样本数 {len(values)} 低于稳定性目标 '{objective.id}' 要求的 "
            f"{objective.minimum_samples}（fail closed）"
        )
        return result
    value = stability_statistic(values, objective.metric, direction)
    if value is None:
        result["reason"] = f"稳定性指标 {objective.metric.value} 无法计算（fail closed）"
        return result
    result["value"] = value
    result["pareto_value"] = value

    checks: list[tuple[bool, str]] = []
    if objective.limit is not None:
        satisfied = _limit_satisfied(value, objective.limit, objective.metric, direction)
        checks.append(
            (
                satisfied,
                f"{objective.metric.value}={value:.6g} "
                f"{'未超过' if satisfied else '超过'}绝对界线 {objective.limit:.6g}",
            )
        )
    if objective.baseline_tolerance is not None:
        if len(baseline_values) < objective.minimum_samples:
            result["reason"] = (
                f"基线样本数 {len(baseline_values)} 不足，无法验证 '{objective.id}' 的"
                f"不劣于基线约束（fail closed）"
            )
            return result
        baseline_value = stability_statistic(
            baseline_values, objective.metric, direction
        )
        if baseline_value is None:
            result["reason"] = (
                f"基线的 {objective.metric.value} 无法计算，"
                f"无法验证 '{objective.id}'（fail closed）"
            )
            return result
        result["baseline_value"] = baseline_value
        satisfied = _tolerance_satisfied(
            value, baseline_value, objective.baseline_tolerance, objective.metric, direction
        )
        checks.append(
            (
                satisfied,
                f"{objective.metric.value}={value:.6g} 相对基线 {baseline_value:.6g} "
                f"{'未劣化超过' if satisfied else '劣化超过'} "
                f"{objective.baseline_tolerance:.0%}",
            )
        )

    if not checks:
        # Soft objective: no limits to enforce, it only feeds the Pareto ranking.
        result["status"] = "satisfied"
        result["passed"] = True
        return result
    failed = [reason for satisfied, reason in checks if not satisfied]
    if failed:
        result["status"] = "violated"
        result["reason"] = "; ".join(failed)
    else:
        result["status"] = "satisfied"
        result["passed"] = True
    return result


def _detect_modes(
    values: Sequence[float], direction: Direction, policy: VariabilityPolicy
) -> ModeSplit | None:
    """Search a one-dimensional cutoff that best separates fast/slow modes.

    Mirrors VGO's cutoff placement on bimodal distributions: try every split
    of the sorted (badness-scaled) values and keep the one maximizing the
    between-cluster share of variance, then require the gap between the two
    clusters to be large relative to the within-cluster spread, so jittery
    unimodal data is not mistaken for two modes.
    """

    ordered = sorted(_badness(value, direction) for value in values)
    count = len(ordered)
    if count < 4:
        return None
    grand = mean(ordered)
    total_ss = sum((value - grand) ** 2 for value in ordered)
    if total_ss <= 0:
        return None
    lower_bound = max(1, math.ceil(count * policy.mode_min_cluster_share))
    upper_bound = min(count - 1, math.floor(count * (1 - policy.mode_min_cluster_share)))
    best: tuple[float, float, int] | None = None  # (r2, gap, split index)
    for split in range(lower_bound, upper_bound + 1):
        left, right = ordered[:split], ordered[split:]
        left_mean, right_mean = mean(left), mean(right)
        between = len(left) * (left_mean - grand) ** 2 + len(right) * (right_mean - grand) ** 2
        r2 = between / total_ss
        gap = ordered[split] - ordered[split - 1]
        if best is None or (r2, gap) > (best[0], best[1]):
            best = (r2, gap, split)
    if best is None:
        return None
    r2, gap, split = best
    minimum_cluster = max(2, math.ceil(count * policy.mode_min_cluster_share))
    if r2 < policy.mode_min_r2:
        return None
    if min(split, count - split) < minimum_cluster:
        return None
    left, right = ordered[:split], ordered[split:]
    within_variances = [pstdev(cluster) if len(cluster) > 1 else 0.0 for cluster in (left, right)]
    within_std = math.sqrt(mean(variance**2 for variance in within_variances))
    if gap < policy.mode_gap_ratio * within_std:
        return None
    cutoff = (ordered[split - 1] + ordered[split]) / 2
    fast_values = right if direction == Direction.MAXIMIZE else left
    slow_values = left if direction == Direction.MAXIMIZE else right
    return ModeSplit(
        cutoff=_original_scale(cutoff, direction),
        fast_mode={"count": len(fast_values), "center": mean(fast_values)},
        slow_mode={"count": len(slow_values), "center": mean(slow_values)},
    )


def _original_scale(value: float, direction: Direction) -> float:
    return -value if direction == Direction.MAXIMIZE else value


def _classify_runs(
    samples: Sequence[RunSample],
    stats: DistributionStats,
    modes: ModeSplit | None,
    direction: Direction,
    policy: VariabilityPolicy,
) -> list[RunClassification]:
    ordered = sorted(_badness(sample.value, direction) for sample in samples)
    q1 = quantile(ordered, 0.25)
    q3 = quantile(ordered, 0.75)
    fence_low = q1 - policy.outlier_fence_k * (q3 - q1)
    fence_high = q3 + policy.outlier_fence_k * (q3 - q1)
    cutoff = _badness(modes.cutoff, direction) if modes is not None else None
    classifications: list[RunClassification] = []
    for sample in samples:
        badness = _badness(sample.value, direction)
        label: RunLabel = "normal"
        if badness > fence_high:
            label = "slow_outlier"
        elif badness < fence_low:
            label = "fast_outlier"
        elif cutoff is not None and badness > cutoff:
            label = "slow_mode"
        elif cutoff is not None and badness <= cutoff:
            label = "fast_mode"
        classifications.append(
            RunClassification(
                runId=sample.run_id,
                value=sample.value,
                label=label,
                slow=label in {"slow_outlier", "slow_mode"},
            )
        )
    return classifications


def _pearson(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    mx, my = mean(xs), mean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    denominator = math.sqrt(sum(item * item for item in dx) * sum(item * item for item in dy))
    if denominator == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy, strict=True)) / denominator


def _association_clues(
    samples: Sequence[RunSample],
    classifications: Sequence[RunClassification],
    policy: VariabilityPolicy,
) -> list[AssociationClue]:
    slow_by_id = {item.run_id: item.slow for item in classifications}
    metric_names = sorted(
        {
            name
            for sample in samples
            for name in sample.system_metrics
            if math.isfinite(float(sample.system_metrics[name]))
        }
    )
    clues: list[AssociationClue] = []
    minimum_coverage = max(2, len(samples) // 2)
    for name in metric_names:
        pairs = [
            (float(sample.system_metrics[name]), float(slow_by_id[sample.run_id]))
            for sample in samples
            if name in sample.system_metrics
        ]
        if len(pairs) < minimum_coverage:
            continue
        slow_values = [value for value, flag in pairs if flag > 0]
        normal_values = [value for value, flag in pairs if flag == 0]
        if len(slow_values) < 2 or len(normal_values) < 2:
            continue
        correlation = _pearson([(flag, value) for value, flag in pairs])
        if correlation is None:
            continue
        slow_mean, normal_mean = mean(slow_values), mean(normal_values)
        lift = (slow_mean / normal_mean) if normal_mean else None
        elevated = slow_mean > normal_mean
        passes = abs(correlation) >= policy.association_min_correlation and (
            (elevated and (lift is None or lift >= policy.association_min_lift))
            or (not elevated and (lift is None or lift <= 1 / policy.association_min_lift))
        )
        if not passes:
            continue
        likely_consequence = name in CONSEQUENCE_METRICS
        note = "该指标与慢运行同时变化，但只是关联线索，不构成因果结论；需要控制变量实验验证。"
        if likely_consequence:
            note += " 该指标更可能是波动的结果而非原因（随慢运行天然增加），解读时需谨慎。"
        clues.append(
            AssociationClue(
                metric=name,
                correlation=round(correlation, 4),
                lift=round(lift, 4) if lift is not None else None,
                direction="elevated_in_slow" if elevated else "reduced_in_slow",
                slow_mean=slow_mean,
                normal_mean=normal_mean,
                likely_consequence=likely_consequence,
                note=note,
            )
        )
    return sorted(clues, key=lambda clue: -abs(clue.correlation))


def _eta_squared(groups: Mapping[str, Sequence[float]]) -> float | None:
    values = [value for group in groups.values() for value in group]
    if len(groups) < 2 or len(values) < 3:
        return None
    grand = mean(values)
    total = sum((value - grand) ** 2 for value in values)
    if total <= 0:
        return 0.0
    between = sum(len(group) * (mean(group) - grand) ** 2 for group in groups.values())
    return between / total


def _attribution(
    samples: Sequence[RunSample],
    direction: Direction,
    policy: VariabilityPolicy,
) -> list[AttributionEntry]:
    dimensions = (
        ("host", "host_id"),
        ("placement", "placement_pair_id"),
        ("date", "date"),
        ("time_block", "time_block_id"),
        ("environment", "target_id"),
    )
    entries: list[AttributionEntry] = []
    for dimension, attribute in dimensions:
        groups: dict[str, list[float]] = {}
        for sample in samples:
            key = getattr(sample, attribute)
            if key:
                groups.setdefault(str(key), []).append(_badness(sample.value, direction))
        eta = _eta_squared(groups)
        if eta is None:
            continue
        entries.append(
            AttributionEntry(
                dimension=dimension,
                etaSquared=round(eta, 4),
                groupCount=len(groups),
                dominant=eta >= policy.attribution_min_eta_squared,
                groupMeans={key: round(mean(group), 4) for key, group in sorted(groups.items())},
            )
        )
    within_stds = [sample.within_run_std for sample in samples if sample.within_run_std]
    if within_stds:
        values = [_badness(sample.value, direction) for sample in samples]
        total_variance = pstdev(values) ** 2 if len(values) > 1 else 0.0
        within_variance = mean(std**2 for std in within_stds)
        share = within_variance / total_variance if total_variance > 0 else 0.0
        entries.append(
            AttributionEntry(
                dimension="within_run",
                etaSquared=round(min(share, 1.0), 4),
                groupCount=len(within_stds),
                dominant=False,
                groupMeans={},
            )
        )
    return sorted(
        entries,
        key=lambda entry: -(entry.eta_squared if entry.eta_squared is not None else -1),
    )


def _recommend(
    clues: Sequence[AssociationClue],
    attribution: Sequence[AttributionEntry],
    modes: ModeSplit | None,
    stats: DistributionStats,
    has_system_metrics: bool,
    policy: VariabilityPolicy,
) -> list[Recommendation]:
    recommendations: list[Recommendation] = []
    clue_metrics = {clue.metric for clue in clues}
    rules: list[tuple[bool, Recommendation]] = [
        (
            "cpu_migration_count" in clue_metrics,
            Recommendation(
                action="固定 CPU affinity（taskset / numactl --cpunodebind）后重新 A/B",
                rationale="慢运行与 CPU migration 计数同时升高，优先验证调度迁移是否为波动来源。",
                priority="high",
                kind="control_experiment",
            ),
        ),
        (
            "numa_migration_count" in clue_metrics,
            Recommendation(
                action="关闭 NUMA balancing（sysctl kernel.numa_balancing=0）后重新 A/B",
                rationale="慢运行与 NUMA migration 同时升高，NUMA 远端内存访问是主要嫌疑。",
                priority="high",
                kind="control_experiment",
            ),
        ),
        (
            bool({"dtlb_miss_count", "page_fault_count"} & clue_metrics),
            Recommendation(
                action="启用透明大页（THP）或 hugetlbfs 后重新 A/B",
                rationale="慢运行与 dTLB miss / page fault 同时升高，TLB 覆盖不足是主要嫌疑。",
                priority="high",
                kind="control_experiment",
            ),
        ),
        (
            "context_switch_count" in clue_metrics,
            Recommendation(
                action="线程绑核并减少竞争线程（或 isolcpus 隔离）后重新 A/B",
                rationale="慢运行与 context switch 同时升高，调度干扰是主要嫌疑。",
                priority="high",
                kind="control_experiment",
            ),
        ),
        (
            "cache_miss_count" in clue_metrics,
            Recommendation(
                action="固定内存布局（numactl --membind）并检查 LLC 争用后重新 A/B",
                rationale="慢运行与 cache miss 同时升高，内存放置与缓存争用是主要嫌疑。",
                priority="medium",
                kind="control_experiment",
            ),
        ),
        (
            "cpu_frequency_mhz" in clue_metrics,
            Recommendation(
                action="固定 CPU 频率（governor=performance 或 cpupower frequency-set）后重测",
                rationale="慢运行与 CPU 频率变化相关，频率漂移/降频是主要嫌疑。",
                priority="high",
                kind="control_experiment",
            ),
        ),
        (
            any(entry.dimension == "host" and entry.dominant for entry in attribution),
            Recommendation(
                action="更换宿主机或在多台宿主机上重复实验",
                rationale="跨宿主机方差占比过高，波动可能来自特定宿主机状态。",
                priority="high",
                kind="design_change",
            ),
        ),
        (
            any(entry.dimension == "date" and entry.dominant for entry in attribution),
            Recommendation(
                action="增加跨日、跨时段重复次数",
                rationale="跨日方差占比过高，波动可能与时段性后台负载或维护相关。",
                priority="medium",
                kind="design_change",
            ),
        ),
        (
            any(entry.dimension == "placement" and entry.dominant for entry in attribution),
            Recommendation(
                action="更换 placement（可用区/宿主机组合）重复实验",
                rationale="跨 placement 方差占比过高，波动可能来自物理拓扑差异。",
                priority="medium",
                kind="design_change",
            ),
        ),
        (
            not has_system_metrics and stats.count >= policy.minimum_samples,
            Recommendation(
                action="补充 perf / 系统指标采集（migration、context switch、page fault、"
                "dTLB miss、NUMA、频率）后重新分析",
                rationale="当前运行缺少系统指标，无法把波动关联到具体资源，只能看到现象。",
                priority="high",
                kind="data_collection",
            ),
        ),
        (
            modes is not None,
            Recommendation(
                action="对疑似快/慢双模式做控制变量 A/B（先固定 affinity 与 NUMA，"
                "再固定频率），逐项排除",
                rationale="分布疑似双峰，存在 Fast/Slow 两种模式，需要逐项定位模式切换条件。",
                priority="high",
                kind="control_experiment",
            ),
        ),
        (
            has_system_metrics,
            Recommendation(
                action="对 profiler 开启/关闭各测一轮，排除采集开销引入的波动",
                rationale="系统指标采集本身可能影响性能，需要 A/B 确认波动不是采集引入的。",
                priority="low",
                kind="control_experiment",
            ),
        ),
    ]
    seen: set[str] = set()
    for triggered, recommendation in rules:
        if triggered and recommendation.action not in seen:
            seen.add(recommendation.action)
            recommendations.append(recommendation)
    return recommendations


def _selection_impact(
    verdict: StabilityVerdict,
    stats: DistributionStats,
    modes: ModeSplit | None,
    clues: Sequence[AssociationClue],
) -> dict[str, Any]:
    if verdict == "stable":
        summary = (
            "性能稳定，均值比较可信；该候选可直接进入选型排序，"
            "尾部风险低，容量规划可按均值加小余量。"
        )
        confidence = "high"
    elif verdict == "warning":
        summary = (
            "存在波动或异常运行，均值排序仅供参考；"
            "选型时应同时看 p99/尾部均值，并对 SLO 留出更大余量。"
        )
        confidence = "medium"
    elif verdict == "unstable":
        summary = (
            "波动过大，均值排序不可靠；建议先定位波动来源（见建议）"
            "再比较候选，否则选型结论可能翻转。"
        )
        confidence = "low"
    else:
        summary = "样本不足，无法判断稳定性；先增加重复次数再用于选型决策。"
        confidence = "insufficient"
    details: list[str] = []
    if stats.coefficient_of_variation is not None:
        details.append(f"CV={stats.coefficient_of_variation:.3f}")
    if modes is not None:
        details.append("疑似快/慢双模式")
    if clues:
        details.append(f"{len(clues)} 条系统指标关联线索")
    return {"summary": summary, "confidence": confidence, "details": details}


def analyze_variability(
    samples: Sequence[RunSample],
    *,
    metric: str,
    unit: str,
    direction: Direction | str,
    group_label: str,
    policy: VariabilityPolicy | None = None,
) -> VariabilityReport:
    """Run the full variability chain over one group of repeated runs."""

    selected_policy = policy or VariabilityPolicy()
    selected_direction = Direction(direction)
    if not samples:
        raise VariabilityError("variability analysis requires at least one run")
    values = [sample.value for sample in samples]
    if selected_direction == Direction.NONE:
        raise VariabilityError("variability analysis requires a min/max direction")

    stats = _distribution_stats(values, selected_direction)
    insufficient = stats.count < selected_policy.minimum_samples
    if insufficient:
        return VariabilityReport(
            groupLabel=group_label,
            metric=metric,
            unit=unit,
            direction=selected_direction.value,
            status="insufficient_evidence",
            distribution=stats,
            stability={
                "verdict": "insufficient_evidence",
                "reasons": [
                    f"样本数 {stats.count} 低于策略下限 "
                    f"{selected_policy.minimum_samples}，无法给出稳定性结论"
                ],
            },
            runs=[],
            outliers={"slow": [], "fast": []},
            attribution=[],
            recommendations=[
                Recommendation(
                    action=f"把重复次数增加到至少 {selected_policy.minimum_samples} 次",
                    rationale="当前样本量不足以区分真实波动与噪声。",
                    priority="high",
                    kind="design_change",
                )
            ],
            selection_impact=_selection_impact("insufficient_evidence", stats, None, []),
            evidence={
                "sampleCount": stats.count,
                "hostCount": len({s.host_id for s in samples if s.host_id}),
                "distinctDates": len({s.date for s in samples if s.date}),
                "systemMetricCount": len({name for s in samples for name in s.system_metrics}),
            },
        )

    modes = _detect_modes(values, selected_direction, selected_policy)
    classifications = _classify_runs(samples, stats, modes, selected_direction, selected_policy)
    slow_ids = [item.run_id for item in classifications if item.label == "slow_outlier"]
    fast_ids = [item.run_id for item in classifications if item.label == "fast_outlier"]
    slow_share = sum(1 for item in classifications if item.slow) / len(classifications)
    clues = _association_clues(samples, classifications, selected_policy)
    attribution = _attribution(samples, selected_direction, selected_policy)
    has_system_metrics = any(sample.system_metrics for sample in samples)

    reasons: list[str] = []
    cv = stats.coefficient_of_variation
    if cv is not None and cv > selected_policy.cv_unstable:
        reasons.append(f"CV={cv:.3f} 超过不稳定阈值 {selected_policy.cv_unstable}")
    if slow_share > selected_policy.slow_share_unstable:
        reasons.append(f"慢运行占比 {slow_share:.0%} 超过阈值")
    verdict: StabilityVerdict
    if (cv is not None and cv > selected_policy.cv_unstable) or (
        slow_share > selected_policy.slow_share_unstable
    ):
        verdict = "unstable"
    elif (
        (cv is not None and cv > selected_policy.cv_stable)
        or slow_ids
        or fast_ids
        or modes is not None
        or (stats.skewness is not None and abs(stats.skewness) > selected_policy.skew_threshold)
    ):
        verdict = "warning"
        if cv is not None and selected_policy.cv_stable < cv <= selected_policy.cv_unstable:
            reasons.append(f"CV={cv:.3f} 高于稳定阈值 {selected_policy.cv_stable}")
        if slow_ids or fast_ids:
            reasons.append(f"检测到 {len(slow_ids) + len(fast_ids)} 个 IQR 异常运行")
        if modes is not None:
            reasons.append("分布疑似双峰（快/慢模式）")
        if stats.skewness is not None and abs(stats.skewness) > selected_policy.skew_threshold:
            reasons.append(f"偏度 {stats.skewness:.2f} 显著")
    else:
        verdict = "stable"
        reasons.append(f"CV={(cv if cv is not None else 0.0):.3f}，无异常运行、无双峰迹象")

    recommendations = _recommend(
        clues, attribution, modes, stats, has_system_metrics, selected_policy
    )
    return VariabilityReport(
        groupLabel=group_label,
        metric=metric,
        unit=unit,
        direction=selected_direction.value,
        status=verdict,
        distribution=stats,
        stability={
            "verdict": verdict,
            "cv": cv,
            "slow_run_share": slow_share,
            "skewed": (
                stats.skewness is not None and abs(stats.skewness) > selected_policy.skew_threshold
            ),
            "suspected_multimodal": modes is not None,
            "reasons": reasons,
        },
        modes=modes,
        runs=classifications,
        outliers={"slow": slow_ids, "fast": fast_ids},
        associationClues=clues,
        attribution=attribution,
        recommendations=recommendations,
        selectionImpact=_selection_impact(verdict, stats, modes, clues),
        evidence={
            "sampleCount": stats.count,
            "hostCount": len({s.host_id for s in samples if s.host_id}),
            "distinctDates": len({s.date for s in samples if s.date}),
            "systemMetricCount": len({name for s in samples for name in s.system_metrics}),
        },
    )


def _slow_run_probability(
    samples: Sequence[RunSample],
    direction: Direction,
    policy: VariabilityPolicy,
) -> float:
    if not samples:
        return 0.0
    stats = _distribution_stats([sample.value for sample in samples], direction)
    modes = _detect_modes([sample.value for sample in samples], direction, policy)
    classifications = _classify_runs(samples, stats, modes, direction, policy)
    return sum(1 for item in classifications if item.slow) / len(classifications)


def _relative_improvement(candidate: float, baseline: float, direction: Direction) -> float | None:
    if baseline == 0:
        return None
    delta = candidate - baseline
    if direction == Direction.MAXIMIZE:
        return delta / abs(baseline)
    return -delta / abs(baseline)


def _worst_host(samples: Sequence[RunSample], direction: Direction) -> dict[str, Any] | None:
    groups: dict[str, list[float]] = {}
    for sample in samples:
        if sample.host_id:
            groups.setdefault(sample.host_id, []).append(sample.value)
    if len(groups) < 2:
        return None
    per_host = {host: median(values) for host, values in sorted(groups.items())}
    worst = (
        max(per_host, key=lambda host: per_host[host])
        if direction == Direction.MINIMIZE
        else min(per_host, key=lambda host: per_host[host])
    )
    return {"host": worst, "median": per_host[worst], "per_host": per_host}


def compare_distributions(
    baseline_samples: Sequence[RunSample],
    candidate_samples: Sequence[RunSample],
    *,
    metric: str,
    unit: str,
    direction: Direction | str,
    baseline_label: str,
    candidate_label: str,
    slo_threshold: float | None = None,
    policy: VariabilityPolicy | None = None,
) -> DistributionComparison:
    """Compare two configurations on the whole distribution, not just the mean."""

    selected_policy = policy or VariabilityPolicy()
    selected_direction = Direction(direction)
    if not baseline_samples or not candidate_samples:
        raise VariabilityError("distribution comparison requires both groups")
    baseline_stats = _distribution_stats(
        [sample.value for sample in baseline_samples], selected_direction
    )
    candidate_stats = _distribution_stats(
        [sample.value for sample in candidate_samples], selected_direction
    )
    mean_improvement = _relative_improvement(
        candidate_stats.mean, baseline_stats.mean, selected_direction
    )
    median_improvement = _relative_improvement(
        candidate_stats.median, baseline_stats.median, selected_direction
    )
    baseline_tail = (
        baseline_stats.tail_mean if baseline_stats.tail_mean is not None else baseline_stats.p95
    )
    candidate_tail = (
        candidate_stats.tail_mean if candidate_stats.tail_mean is not None else candidate_stats.p95
    )
    tail_improvement = (
        _relative_improvement(candidate_tail, baseline_tail, selected_direction)
        if baseline_tail is not None and candidate_tail is not None
        else None
    )
    cv_ratio = (
        candidate_stats.coefficient_of_variation / baseline_stats.coefficient_of_variation
        if candidate_stats.coefficient_of_variation is not None
        and baseline_stats.coefficient_of_variation is not None
        and baseline_stats.coefficient_of_variation > 0
        else None
    )
    tail_worsened = tail_improvement is not None and tail_improvement < -0.05
    mean_better = mean_improvement is not None and mean_improvement > 0.01
    mean_worse = mean_improvement is not None and mean_improvement < -0.01
    tail_better = tail_improvement is not None and tail_improvement > 0.05
    if mean_better and tail_worsened:
        verdict: ComparisonVerdict = "mean_better_tail_worse"
    elif mean_worse and tail_better:
        verdict = "mean_worse_tail_better"
    elif mean_better and tail_better:
        verdict = "dominant"
    elif mean_worse and tail_worsened:
        verdict = "dominated"
    else:
        verdict = "inconclusive"

    baseline_slow_probability = _slow_run_probability(
        baseline_samples, selected_direction, selected_policy
    )
    candidate_slow_probability = _slow_run_probability(
        candidate_samples, selected_direction, selected_policy
    )
    baseline_worst = _worst_host(baseline_samples, selected_direction)
    candidate_worst = _worst_host(candidate_samples, selected_direction)
    slo: dict[str, Any] = {}
    if slo_threshold is not None:
        baseline_exceed = sum(
            1
            for sample in baseline_samples
            if (
                sample.value > slo_threshold
                if selected_direction == Direction.MINIMIZE
                else sample.value < slo_threshold
            )
        ) / len(baseline_samples)
        candidate_exceed = sum(
            1
            for sample in candidate_samples
            if (
                sample.value > slo_threshold
                if selected_direction == Direction.MINIMIZE
                else sample.value < slo_threshold
            )
        ) / len(candidate_samples)
        slo = {
            "threshold": slo_threshold,
            "baseline_exceedance": baseline_exceed,
            "candidate_exceedance": candidate_exceed,
        }

    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value * 100:+.1f}%"

    if verdict == "mean_better_tail_worse":
        summary = (
            f"{candidate_label} 平均性能更好（均值 {pct(mean_improvement)}），"
            f"但尾部更差（尾部均值 {pct(tail_improvement)}）；"
            f"存在“均值改善但尾部恶化”的权衡。"
        )
        recommendation = (
            f"不要仅凭均值选择 {candidate_label}：若业务重视尾延迟/SLO，"
            f"应优先 {baseline_label}；若只关心平均吞吐且 SLO 余量大，可选 {candidate_label}。"
        )
    elif verdict == "mean_worse_tail_better":
        summary = (
            f"{candidate_label} 平均性能略差（均值 {pct(mean_improvement)}），"
            f"但尾部明显更好（尾部均值 {pct(tail_improvement)}），稳定性占优。"
        )
        recommendation = (
            f"若重视稳定性和尾延迟，优先 {candidate_label}；"
            f"若均值吞吐是唯一目标，可保持 {baseline_label}。"
        )
    elif verdict == "dominant":
        summary = (
            f"{candidate_label} 在均值（{pct(mean_improvement)}）和尾部"
            f"（{pct(tail_improvement)}）都更好，分布整体占优。"
        )
        recommendation = f"分布层面 {candidate_label} 优于 {baseline_label}，推荐采用。"
    elif verdict == "dominated":
        summary = (
            f"{candidate_label} 在均值（{pct(mean_improvement)}）和尾部"
            f"（{pct(tail_improvement)}）都更差，分布整体劣势。"
        )
        recommendation = f"分布层面 {baseline_label} 更优，不建议切换到 {candidate_label}。"
    else:
        summary = (
            f"两组分布在均值（{pct(mean_improvement)}）与尾部（{pct(tail_improvement)}）"
            f"上均无显著差异。"
        )
        recommendation = "当前证据不足以区分两者，增加重复次数或提升负载强度后再比较。"

    return DistributionComparison(
        metric=metric,
        unit=unit,
        direction=selected_direction.value,
        baselineLabel=baseline_label,
        candidateLabel=candidate_label,
        baseline=baseline_stats,
        candidate=candidate_stats,
        meanImprovement=mean_improvement,
        medianImprovement=median_improvement,
        tailImprovement=tail_improvement,
        cvRatio=cv_ratio,
        slowRunProbability={
            "baseline": baseline_slow_probability,
            "candidate": candidate_slow_probability,
        },
        worstHost={"baseline": baseline_worst, "candidate": candidate_worst},
        sloExceedance=slo,
        tailWorsened=tail_worsened,
        verdict=verdict,
        summary=summary,
        recommendation=recommendation,
    )
