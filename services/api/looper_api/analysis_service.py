from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from looper_core.analysis import (
    BENCHTRUST_METHOD_VERSION,
    InsufficientEvidence,
    aggregate,
    bootstrap_improvement,
    cluster_paired_bootstrap_improvement,
    environment_sensitivity,
    gate_passes,
    paired_bootstrap_improvement,
    pareto_ranks,
    rank_stability_by_axes,
    ranking_groups,
    reference_validity_rate,
    summarize,
    task_leverage,
)
from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.contracts import (
    Aggregation,
    Direction,
    ExperimentMode,
    ExperimentSpec,
    GateKind,
    StabilityMetric,
)
from looper_core.selection import compare_frontier_intervals
from looper_core.state import AttemptStatus
from looper_core.variability import evaluate_stability_objective
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from looper_api.models import (
    AnalysisSnapshotRecord,
    ArtifactLinkRecord,
    AttemptRecord,
    CandidateRecord,
    CheckRecord,
    EvaluationRecord,
    ExperimentRecord,
    ObservationRecord,
    SelectionLoadPointRecord,
    TargetRecord,
)

ANALYSIS_CODE_VERSION = "0.5.0"


def _observation_value(record: ObservationRecord) -> float | bool | None:
    return record.value_boolean if record.value_boolean is not None else record.value_number


def _input_facts(session: Session, experiment_id: str) -> list[dict[str, Any]]:
    attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(AttemptRecord.experiment_id == experiment_id)
            .order_by(AttemptRecord.id)
        )
    )
    facts: list[dict[str, Any]] = []
    for attempt in attempts:
        evaluation = session.get(EvaluationRecord, attempt.evaluation_id)
        observations = list(
            session.scalars(
                select(ObservationRecord)
                .where(ObservationRecord.attempt_id == attempt.id)
                .order_by(ObservationRecord.id)
            )
        )
        artifacts = list(
            session.scalars(
                select(ArtifactLinkRecord)
                .where(ArtifactLinkRecord.attempt_id == attempt.id)
                .order_by(ArtifactLinkRecord.digest, ArtifactLinkRecord.role)
            )
        )
        facts.append(
            {
                "attempt_id": attempt.id,
                "evaluation_id": attempt.evaluation_id,
                "selection_load_point_id": attempt.selection_load_point_id,
                "target_id": evaluation.target_id if evaluation else None,
                "workload_id": evaluation.workload_id if evaluation else None,
                "repeat_index": attempt.repeat_index,
                "retry_index": attempt.retry_index,
                "queue_sequence": attempt.queue_sequence,
                "status": attempt.status,
                "error_message": attempt.error_message,
                "fencing_token": attempt.fencing_token,
                "envelope_digest": attempt.envelope_digest,
                "observations": [
                    {
                        "id": item.id,
                        "metric": item.metric,
                        "value": _observation_value(item),
                        "unit": item.unit,
                        "phase": item.phase,
                        "sample_index": item.sample_index,
                        "sample_count": item.sample_count,
                        "statistic": item.statistic,
                        "attributes": item.attributes_json,
                    }
                    for item in observations
                ],
                "artifacts": [item.digest for item in artifacts],
            }
        )
    return facts


def _candidate_observations(
    session: Session, candidate: CandidateRecord
) -> tuple[list[AttemptRecord], list[ObservationRecord], list[CheckRecord]]:
    evaluations = list(
        session.scalars(
            select(EvaluationRecord).where(EvaluationRecord.candidate_id == candidate.id)
        )
    )
    evaluation_ids = [item.id for item in evaluations]
    attempts = list(
        session.scalars(
            select(AttemptRecord).where(
                AttemptRecord.evaluation_id.in_(evaluation_ids),
                AttemptRecord.status == AttemptStatus.SUCCEEDED,
            )
        )
    )
    attempt_ids = [item.id for item in attempts]
    if not attempt_ids:
        return attempts, [], []
    observations = list(
        session.scalars(
            select(ObservationRecord).where(ObservationRecord.attempt_id.in_(attempt_ids))
        )
    )
    checks = list(
        session.scalars(select(CheckRecord).where(CheckRecord.attempt_id.in_(attempt_ids)))
    )
    return attempts, observations, checks


def _metric_values(observations: list[ObservationRecord], metric: str, unit: str) -> list[float]:
    values: list[float] = []
    for item in observations:
        value = _observation_value(item)
        if item.metric != metric or item.unit != unit or isinstance(value, bool) or value is None:
            continue
        if item.phase == "warmup":
            continue
        values.append(float(value))
    return values


def _boolean_values(observations: list[ObservationRecord], metric: str) -> list[bool]:
    return [
        bool(item.value_boolean)
        for item in observations
        if item.metric == metric and item.value_boolean is not None and item.phase != "warmup"
    ]


def _objective_result(
    values: list[float],
    baseline_values: list[float],
    objective: Any,
    design: Any,
    *,
    is_baseline: bool,
    seed: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "metric": objective.metric,
        "unit": objective.unit,
        "direction": objective.direction,
        "aggregation": objective.aggregation,
        "comparison": objective.comparison,
        "sample_count": len(values),
        "raw": None,
        "improvement": None,
        "lower": None,
        "upper": None,
        "status": "insufficient_evidence",
        "summary": None,
    }
    if len(values) < objective.minimum_samples:
        return result
    if (
        objective.aggregation in {Aggregation.P95, Aggregation.P99, Aggregation.CVAR99}
        and len(values) < design.tail_min_samples
    ):
        return result
    result["raw"] = aggregate(values, objective.aggregation)
    result["summary"] = summarize(values, design.tail_min_samples)
    if is_baseline:
        result.update({"improvement": 0.0, "lower": 0.0, "upper": 0.0, "status": "available"})
        return result
    if len(baseline_values) < objective.minimum_samples:
        return result
    try:
        confidence = bootstrap_improvement(
            values,
            baseline_values,
            objective.direction,
            objective.aggregation,
            objective.comparison,
            design.confidence_level,
            design.bootstrap_resamples,
            seed,
        )
    except InsufficientEvidence as error:
        result["reason"] = str(error)
        return result
    result.update(
        {
            "improvement": confidence["estimate"],
            "lower": confidence["lower"],
            "upper": confidence["upper"],
            "status": "available",
        }
    )
    return result


def _candidate_gate_results(
    spec: ExperimentSpec,
    observations: list[ObservationRecord],
    checks: list[CheckRecord],
    objective_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for check in checks:
        results.append(
            {
                "id": check.check_id,
                "kind": check.kind,
                "scope": check.scope,
                "hard": True,
                "passed": check.passed,
                "value": check.passed,
                "message": check.message,
                "source": "benchmark",
            }
        )
    objectives = {item["metric"]: item for item in objective_results}
    for gate in spec.gates:
        if gate.metric:
            boolean_values = _boolean_values(observations, gate.metric)
            if boolean_values:
                value: float | bool | None = all(boolean_values)
            else:
                matching_objective = objectives.get(gate.metric)
                if gate.kind == GateKind.STATISTICAL and matching_objective:
                    value = matching_objective.get("lower")
                else:
                    numeric = [
                        float(value)
                        for item in observations
                        if item.metric == gate.metric
                        and not isinstance((value := _observation_value(item)), bool)
                        and value is not None
                        and item.phase != "warmup"
                    ]
                    value = aggregate(numeric, Aggregation.MEDIAN) if numeric else None
        else:
            value = all(check.passed for check in checks) if checks else None
        results.append(
            {
                "id": gate.id,
                "kind": gate.kind,
                "scope": gate.scope,
                "hard": gate.hard,
                "passed": gate_passes(gate, value),
                "value": value,
                "threshold": gate.threshold,
                "operator": gate.operator,
                "message": gate.message,
                "source": "experiment-policy",
            }
        )
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for item in results:
        deduplicated[(str(item["source"]), str(item["id"]))] = item
    return list(deduplicated.values())


def _selection_block_value(
    observations: list[ObservationRecord], metric: str, unit: str, aggregation: Aggregation
) -> float | None:
    values = _metric_values(observations, metric, unit)
    return aggregate(values, aggregation) if values else None


def _selection_analysis(
    session: Session,
    experiment: ExperimentRecord,
    spec: ExperimentSpec,
    *,
    input_digest: str,
    policy_digest: str,
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    if spec.scenario is None or spec.selection is None:
        raise ValueError("selection analysis requires scenario contracts")
    evaluations = list(
        session.scalars(
            select(EvaluationRecord).where(EvaluationRecord.experiment_id == experiment.id)
        )
    )
    bindings = {item.target_id: item for item in spec.selection.target_bindings}
    blocks: list[dict[str, Any]] = []
    for evaluation in evaluations:
        binding = bindings[evaluation.target_id]
        attempts = list(
            session.scalars(
                select(AttemptRecord).where(
                    AttemptRecord.evaluation_id == evaluation.id,
                    AttemptRecord.status == AttemptStatus.SUCCEEDED,
                )
            )
        )
        for attempt in attempts:
            observations = list(
                session.scalars(
                    select(ObservationRecord).where(ObservationRecord.attempt_id == attempt.id)
                )
            )
            checks = list(
                session.scalars(select(CheckRecord).where(CheckRecord.attempt_id == attempt.id))
            )
            metric_values: dict[str, float] = {}
            for objective in spec.objectives:
                value = _selection_block_value(
                    observations, objective.metric, objective.unit, objective.aggregation
                )
                if value is not None:
                    metric_values[objective.metric] = value
            scenario_gates: list[dict[str, Any]] = []
            for gate in spec.scenario.slo_gates:
                numeric = [
                    float(value)
                    for item in observations
                    if item.metric == gate.metric
                    and not isinstance((value := _observation_value(item)), bool)
                    and value is not None
                    and item.phase != "warmup"
                ]
                value = aggregate(numeric, Aggregation.MEDIAN) if numeric else None
                scenario_gates.append(
                    {
                        "id": gate.id,
                        "passed": gate_passes(gate, value),
                        "value": value,
                        "operator": gate.operator,
                        "threshold": gate.threshold,
                    }
                )
            benchmark_checks_passed = all(check.passed for check in checks)
            gates_passed = all(item["passed"] for item in scenario_gates)
            extensions = (attempt.envelope_json or {}).get("extensions", {})
            time_block_id = str(
                extensions.get("timeBlockId") or f"{evaluation.workload_id}:{attempt.repeat_index}"
            )
            blocks.append(
                {
                    "attempt_id": attempt.id,
                    "target_id": evaluation.target_id,
                    "variant_id": binding.variant_id,
                    "placement_pair_id": binding.placement_pair_id,
                    "workload_id": evaluation.workload_id,
                    "time_block_id": time_block_id,
                    "repeat_index": attempt.repeat_index,
                    "selection_load_point_id": attempt.selection_load_point_id,
                    "offered_load": extensions.get("offeredLoad"),
                    "metrics": metric_values,
                    "valid": benchmark_checks_passed and gates_passed,
                    "benchmark_checks": [
                        {
                            "id": check.check_id,
                            "passed": check.passed,
                            "kind": check.kind,
                        }
                        for check in checks
                    ],
                    "scenario_gates": scenario_gates,
                }
            )

    load_points = list(
        session.scalars(
            select(SelectionLoadPointRecord)
            .where(SelectionLoadPointRecord.experiment_id == experiment.id)
            .order_by(SelectionLoadPointRecord.sequence)
        )
    )
    analyzed_point = load_points[-1] if load_points else None
    frontier_summary = analyzed_point.analysis_json if analyzed_point is not None else {}
    persisted_frontiers = (
        frontier_summary.get("target_frontiers", {}) if analyzed_point is not None else {}
    )
    facts_by_point: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        point_id = fact.get("selection_load_point_id")
        if isinstance(point_id, str):
            facts_by_point[point_id].append(fact)
    search_trajectory = [
        {
            "load_point_id": point.id,
            "sequence": point.sequence,
            "workload_id": point.workload_id,
            "offered_load": float(point.offered_load),
            "origin": point.origin,
            "status": point.status,
            "required_repeats": point.required_repeats,
            "reason": (point.analysis_json or {}).get("reason"),
            "attempts": [
                {
                    "attempt_id": fact["attempt_id"],
                    "target_id": fact["target_id"],
                    "repeat_index": fact["repeat_index"],
                    "retry_index": fact["retry_index"],
                    "queue_sequence": fact["queue_sequence"],
                    "status": fact["status"],
                    "error_message": fact["error_message"],
                }
                for fact in sorted(
                    facts_by_point.get(point.id, []), key=lambda item: item["queue_sequence"]
                )
            ],
        }
        for point in load_points
    ]
    target_results: list[dict[str, Any]] = []
    for binding in spec.selection.target_bindings:
        target_blocks = [item for item in blocks if item["target_id"] == binding.target_id]
        valid_blocks = [item for item in target_blocks if item["valid"]]
        metrics: list[dict[str, Any]] = []
        for objective in spec.objectives:
            values = [
                item["metrics"][objective.metric]
                for item in valid_blocks
                if objective.metric in item["metrics"]
            ]
            available = len(values) >= objective.minimum_samples
            metrics.append(
                {
                    "metric": objective.metric,
                    "unit": objective.unit,
                    "direction": objective.direction,
                    "aggregation": objective.aggregation,
                    "block_count": len(values),
                    "raw": aggregate(values, objective.aggregation) if available else None,
                    "block_summary": summarize(values, tail_min_samples=20) if values else None,
                    "status": "available" if available else "insufficient_evidence",
                }
            )
        frontiers = {
            workload_id: persisted_frontiers.get(f"{workload_id}:{binding.target_id}")
            for workload_id in spec.workload_ids
            if persisted_frontiers.get(f"{workload_id}:{binding.target_id}") is not None
        }
        frontier_resolved = bool(frontiers) and all(
            frontier.get("status") == "resolved" for frontier in frontiers.values()
        )
        frontier_capacities = [
            float(frontier["confirmed_pass"])
            for frontier in frontiers.values()
            if frontier.get("confirmed_pass") is not None
        ]
        primary_value = (
            min(frontier_capacities)
            if spec.scenario.load_search and frontier_resolved and frontier_capacities
            else metrics[0]["raw"]
            if metrics and not spec.scenario.load_search
            else None
        )
        primary_unit = (
            spec.scenario.load_search.unit
            if spec.scenario.load_search
            else metrics[0]["unit"]
            if metrics
            else "capacity"
        )
        hourly_amount = float(binding.price.hourly_amount) if binding.price else None
        price_efficiency = (
            {
                "value": primary_value / hourly_amount,
                "unit": f"{primary_unit}/{binding.price.currency}/hour",
                "price_snapshot_digest": binding.price.quote_digest,
            }
            if primary_value is not None
            and hourly_amount is not None
            and hourly_amount > 0
            and binding.price is not None
            else None
        )
        target_results.append(
            {
                "target_id": binding.target_id,
                "variant_id": binding.variant_id,
                "label": binding.label,
                "placement_pair_id": binding.placement_pair_id,
                "price": binding.price.model_dump(mode="json") if binding.price else None,
                "price_efficiency": price_efficiency,
                "status": (
                    "available"
                    if frontier_resolved
                    else "frontier_unresolved"
                    if spec.scenario.load_search
                    else "available"
                    if metrics and all(item["status"] == "available" for item in metrics)
                    else "insufficient_evidence"
                ),
                "attempt_count": len(target_blocks),
                "valid_block_count": len(valid_blocks),
                "invalid_block_count": len(target_blocks) - len(valid_blocks),
                "metrics": metrics,
                "frontiers": frontiers,
            }
        )

    variants = sorted({binding.variant_id for binding in spec.selection.target_bindings})
    comparisons: list[dict[str, Any]] = []
    if len(variants) == 2 and spec.scenario.load_search and persisted_frontiers:
        baseline_variant, candidate_variant = variants
        minimum_effect = spec.scenario.load_search.minimum_effect_ratio
        for workload_id in spec.workload_ids:
            by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
            for binding in spec.selection.target_bindings:
                frontier = persisted_frontiers.get(f"{workload_id}:{binding.target_id}")
                if frontier is not None:
                    by_pair[binding.placement_pair_id][binding.variant_id] = frontier
            interval_comparisons = []
            for placement_pair_id, pair in sorted(by_pair.items()):
                if baseline_variant not in pair or candidate_variant not in pair:
                    continue
                comparison = compare_frontier_intervals(
                    pair[candidate_variant],
                    pair[baseline_variant],
                    minimum_effect_ratio=minimum_effect,
                )
                interval_comparisons.append({"placement_pair_id": placement_pair_id, **comparison})
            winners = {
                item.get("winner")
                for item in interval_comparisons
                if item.get("distinguishable") and item.get("winner") is not None
            }
            all_available = bool(interval_comparisons) and all(
                item["status"] == "available" for item in interval_comparisons
            )
            all_distinguishable = all_available and all(
                item.get("distinguishable") is True for item in interval_comparisons
            )
            winner = next(iter(winners)) if all_distinguishable and len(winners) == 1 else None
            pair_count = len(interval_comparisons)
            comparisons.append(
                {
                    "metric": spec.scenario.primary_metric,
                    "unit": spec.scenario.load_search.unit,
                    "workload_id": workload_id,
                    "baseline_variant": baseline_variant,
                    "candidate_variant": candidate_variant,
                    "inference_unit": "frontier_interval",
                    "placement_pair_count": pair_count,
                    "estimate": None,
                    "lower": None,
                    "upper": None,
                    "minimum_effect_ratio": minimum_effect,
                    "distinguishable": winner is not None,
                    "winner": winner,
                    "status": "available" if all_available else "frontier_unresolved",
                    "reason": None if all_available else "frontier interval is unresolved",
                    "conclusion_strength": (
                        "single-placement-provisional"
                        if pair_count <= 1
                        else "multi-placement-exploratory"
                        if pair_count < 5
                        else "procurement-candidate"
                    ),
                    "interval_comparisons": interval_comparisons,
                }
            )
    elif len(variants) == 2:
        baseline_variant, candidate_variant = variants
        for objective in spec.objectives:
            valid_metric_blocks = [
                item for item in blocks if item["valid"] and objective.metric in item["metrics"]
            ]
            placement_ids = sorted(
                {
                    item["placement_pair_id"]
                    for item in valid_metric_blocks
                    if item["variant_id"] == baseline_variant
                }
                & {
                    item["placement_pair_id"]
                    for item in valid_metric_blocks
                    if item["variant_id"] == candidate_variant
                }
            )
            confidence: dict[str, Any] | None = None
            inference_unit: str | None = None
            reason: str | None = None
            try:
                if len(placement_ids) >= 2:
                    baseline_clusters = {
                        placement_id: [
                            item["metrics"][objective.metric]
                            for item in valid_metric_blocks
                            if item["placement_pair_id"] == placement_id
                            and item["variant_id"] == baseline_variant
                        ]
                        for placement_id in placement_ids
                    }
                    candidate_clusters = {
                        placement_id: [
                            item["metrics"][objective.metric]
                            for item in valid_metric_blocks
                            if item["placement_pair_id"] == placement_id
                            and item["variant_id"] == candidate_variant
                        ]
                        for placement_id in placement_ids
                    }
                    confidence = cluster_paired_bootstrap_improvement(
                        candidate_clusters,
                        baseline_clusters,
                        objective.direction,
                        objective.aggregation,
                        objective.comparison,
                        spec.design.confidence_level,
                        spec.design.bootstrap_resamples,
                        spec.selection.random_seed,
                    )
                    inference_unit = "placement_pair"
                else:
                    keyed: dict[str, dict[str, float]] = defaultdict(dict)
                    for item in valid_metric_blocks:
                        key = (
                            f"{item['placement_pair_id']}:{item['workload_id']}:"
                            f"{item['time_block_id']}"
                        )
                        keyed[key][item["variant_id"]] = item["metrics"][objective.metric]
                    common = [
                        value
                        for _, value in sorted(keyed.items())
                        if baseline_variant in value and candidate_variant in value
                    ]
                    confidence = paired_bootstrap_improvement(
                        [item[candidate_variant] for item in common],
                        [item[baseline_variant] for item in common],
                        objective.direction,
                        objective.aggregation,
                        objective.comparison,
                        spec.design.confidence_level,
                        spec.design.bootstrap_resamples,
                        spec.selection.random_seed,
                    )
                    inference_unit = "time_block"
            except InsufficientEvidence as error:
                reason = str(error)

            minimum_effect = (
                spec.scenario.load_search.minimum_effect_ratio
                if spec.scenario.load_search
                else 0.05
            )
            estimate = confidence.get("estimate") if confidence else None
            lower = confidence.get("lower") if confidence else None
            upper = confidence.get("upper") if confidence else None
            winner = None
            if estimate is not None and abs(float(estimate)) >= minimum_effect:
                if lower is not None and float(lower) > 0:
                    winner = candidate_variant
                elif upper is not None and float(upper) < 0:
                    winner = baseline_variant
            pair_count = len(placement_ids)
            conclusion_strength = (
                "single-placement-provisional"
                if pair_count <= 1
                else "multi-placement-exploratory"
                if pair_count < 5
                else "procurement-candidate"
            )
            comparisons.append(
                {
                    "metric": objective.metric,
                    "unit": objective.unit,
                    "baseline_variant": baseline_variant,
                    "candidate_variant": candidate_variant,
                    "inference_unit": inference_unit,
                    "placement_pair_count": pair_count,
                    "estimate": estimate,
                    "lower": lower,
                    "upper": upper,
                    "minimum_effect_ratio": minimum_effect,
                    "distinguishable": winner is not None,
                    "winner": winner,
                    "status": "available" if confidence else "insufficient_evidence",
                    "reason": reason,
                    "conclusion_strength": conclusion_strength,
                }
            )

    artifact_count = int(
        session.scalar(
            select(func.count(ArtifactLinkRecord.id))
            .join(AttemptRecord, AttemptRecord.id == ArtifactLinkRecord.attempt_id)
            .where(AttemptRecord.experiment_id == experiment.id)
        )
        or 0
    )
    return {
        "schema_version": "v1alpha1",
        "mode": "selection",
        "experiment_id": experiment.id,
        "input_digest": input_digest,
        "policy_digest": policy_digest,
        "code_version": ANALYSIS_CODE_VERSION,
        "status": "available" if target_results else "insufficient_evidence",
        "scenario": spec.scenario.model_dump(mode="json"),
        "frontier": {
            "status": frontier_summary.get("frontier_status"),
            "termination_reason": frontier_summary.get("termination_reason"),
            "adaptive_points_used": sum(point.origin == "adaptive" for point in load_points),
            "trajectory": search_trajectory,
        }
        if spec.scenario.load_search
        else None,
        "targets": target_results,
        "comparisons": comparisons,
        "blocks": blocks,
        "candidates": [],
        "pareto": [],
        "benchtrust": _build_benchtrust(
            session,
            experiment,
            spec,
            input_digest=input_digest,
            policy_digest=policy_digest,
        ),
        "evidence": {
            "attempt_count": len(facts),
            "observation_count": sum(len(item["observations"]) for item in facts),
            "artifact_count": artifact_count,
            "all_artifacts_content_addressed": True,
        },
    }


def _benchtrust_samples(
    session: Session, experiment: ExperimentRecord, spec: ExperimentSpec
) -> list[dict[str, Any]]:
    first = spec.objectives[0]
    targets = {item.id: item for item in session.scalars(select(TargetRecord))}
    evaluations = list(
        session.scalars(
            select(EvaluationRecord).where(EvaluationRecord.experiment_id == experiment.id)
        )
    )
    candidate_by_id = {
        item.id: item
        for item in session.scalars(
            select(CandidateRecord).where(CandidateRecord.experiment_id == experiment.id)
        )
    }
    bindings = {
        item.target_id: item
        for item in (spec.selection.target_bindings if spec.selection else [])
    }
    variants = sorted(
        {item.variant_id for item in (spec.selection.target_bindings if spec.selection else [])}
    )
    baseline_variant = variants[0] if variants else None
    candidate_variant = variants[1] if len(variants) > 1 else None
    samples: list[dict[str, Any]] = []
    for evaluation in evaluations:
        attempts = list(
            session.scalars(
                select(AttemptRecord).where(
                    AttemptRecord.evaluation_id == evaluation.id,
                    AttemptRecord.status == AttemptStatus.SUCCEEDED,
                )
            )
        )
        target = targets.get(evaluation.target_id)
        fingerprint = (target.fingerprint_json or {}) if target else {}
        candidate = candidate_by_id.get(evaluation.candidate_id)
        binding = bindings.get(evaluation.target_id)
        variant_id = binding.variant_id if binding else None
        placement_id = binding.placement_pair_id if binding else None
        if binding is not None:
            is_baseline = variant_id == baseline_variant
            is_reference = (
                variant_id == candidate_variant
                if candidate_variant is not None
                else not is_baseline
            )
        else:
            is_baseline = bool(candidate and candidate.role == "baseline")
            is_reference = bool(candidate and candidate.role != "baseline")
        for attempt in attempts:
            observations = list(
                session.scalars(
                    select(ObservationRecord).where(ObservationRecord.attempt_id == attempt.id)
                )
            )
            values = [
                float(_observation_value(item))
                for item in observations
                if item.metric == first.metric
                and item.unit == first.unit
                and item.phase != "warmup"
                and not isinstance(_observation_value(item), bool)
                and _observation_value(item) is not None
            ]
            if not values:
                continue
            checks = list(
                session.scalars(
                    select(CheckRecord).where(CheckRecord.attempt_id == attempt.id)
                )
            )
            validity = all(check.passed for check in checks) if checks else True
            envelope = attempt.envelope_json or {}
            extensions = envelope.get("extensions", {})
            samples.append(
                {
                    "target_id": evaluation.target_id,
                    "candidate_id": evaluation.candidate_id,
                    "variant_id": variant_id,
                    "placement_pair_id": placement_id,
                    "is_baseline": is_baseline,
                    "is_reference": is_reference,
                    "workload_id": evaluation.workload_id,
                    "value": aggregate(values, first.aggregation),
                    "date": attempt.created_at.date().isoformat() if attempt.created_at else None,
                    "time_block_id": extensions.get("timeBlockId")
                    or extensions.get("time_block_id"),
                    "validity": validity,
                    "environment_fingerprint": fingerprint,
                }
            )
    return samples


def _environment_factors(fingerprint: Mapping[str, Any], target_id: str) -> dict[str, Any]:
    return {
        "cpu_model": fingerprint.get("processor") or fingerprint.get("instance_type"),
        "kernel": fingerprint.get("release"),
        "virtualization": fingerprint.get("system"),
        "host": fingerprint.get("host_key_sha256") or fingerprint.get("processor") or target_id,
    }


def _reference_validity_records(
    samples: Sequence[dict[str, Any]], spec: ExperimentSpec
) -> list[dict[str, Any]]:
    first = spec.objectives[0]
    minimum_effect = (
        spec.scenario.load_search.minimum_effect_ratio
        if spec.scenario is not None and spec.scenario.load_search is not None
        else 0.05
    )
    by_target: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"baseline": [], "reference": []}
    )
    validity_by_target: dict[str, list[bool]] = defaultdict(list)
    fingerprint_by_target: dict[str, Mapping[str, Any]] = {}
    for sample in samples:
        target_id = sample["target_id"]
        if sample["is_baseline"]:
            by_target[target_id]["baseline"].append(sample["value"])
        elif sample["is_reference"]:
            by_target[target_id]["reference"].append(sample["value"])
        validity_by_target[target_id].append(bool(sample["validity"]))
        fingerprint_by_target.setdefault(
            target_id, sample.get("environment_fingerprint") or {"target_id": target_id}
        )
    records: list[dict[str, Any]] = []
    for target_id in sorted(by_target):
        baseline_values = by_target[target_id]["baseline"]
        reference_values = by_target[target_id]["reference"]
        fingerprint = fingerprint_by_target[target_id]
        if not baseline_values or not reference_values:
            records.append(
                {
                    "environment_id": target_id,
                    "environment_fingerprint": fingerprint,
                    "eligible": False,
                    "excluded_reason": "缺少 Reference 或 Baseline 的配对结果",
                    "valid": None,
                    "invalid_reason": None,
                    "reference_value": None,
                    "baseline_value": None,
                    "benefit": None,
                    "benefit_lower": None,
                    "benefit_upper": None,
                    "repeat_count": len(reference_values),
                }
            )
            continue
        valid_gate = all(validity_by_target[target_id])
        try:
            confidence = bootstrap_improvement(
                reference_values,
                baseline_values,
                first.direction,
                first.aggregation,
                first.comparison,
                spec.design.confidence_level,
                spec.design.bootstrap_resamples,
                spec.design.random_seed,
            )
        except InsufficientEvidence as error:
            records.append(
                {
                    "environment_id": target_id,
                    "environment_fingerprint": fingerprint,
                    "eligible": False,
                    "excluded_reason": str(error),
                    "valid": None,
                    "invalid_reason": None,
                    "reference_value": None,
                    "baseline_value": None,
                    "benefit": None,
                    "benefit_lower": None,
                    "benefit_upper": None,
                    "repeat_count": len(reference_values),
                }
            )
            continue
        estimate = confidence["estimate"]
        lower = confidence["lower"]
        direction_consistent = estimate is not None and lower is not None and lower > minimum_effect
        valid = bool(
            direction_consistent
            and valid_gate
            and len(reference_values) >= spec.design.min_repeats
        )
        if valid:
            invalid_reason = None
        elif not valid_gate:
            invalid_reason = "有效性/正确性门禁未通过"
        elif len(reference_values) < spec.design.min_repeats:
            invalid_reason = f"重复数 {len(reference_values)} 低于 {spec.design.min_repeats}"
        else:
            invalid_reason = "参考收益方向与声明方向不一致或未达到最小效果"
        records.append(
            {
                "environment_id": target_id,
                "environment_fingerprint": fingerprint,
                "eligible": True,
                "excluded_reason": None,
                "valid": valid,
                "invalid_reason": invalid_reason,
                "reference_value": aggregate(reference_values, first.aggregation),
                "baseline_value": aggregate(baseline_values, first.aggregation),
                "benefit": estimate,
                "benefit_lower": lower,
                "benefit_upper": confidence["upper"],
                "repeat_count": len(reference_values),
            }
        )
    return records


def _build_benchtrust(
    session: Session,
    experiment: ExperimentRecord,
    spec: ExperimentSpec,
    *,
    task_scores: Mapping[str, Mapping[str, float]] | None = None,
    task_weights: Mapping[str, float] | None = None,
    input_digest: str,
    policy_digest: str,
) -> dict[str, Any]:
    first = spec.objectives[0]
    maximize = first.direction.value == "maximize"
    samples = _benchtrust_samples(session, experiment, spec)

    environment_records = []
    for sample in samples:
        factors = _environment_factors(sample["environment_fingerprint"], sample["target_id"])
        environment_records.append(
            {
                "value": sample["value"],
                "workload": sample["workload_id"],
                "candidate": sample["variant_id"] or sample["candidate_id"],
                "cpu_model": factors["cpu_model"],
                "kernel": factors["kernel"],
                "virtualization": factors["virtualization"],
                "host": factors["host"],
                "placement": sample["placement_pair_id"],
                "date": sample["date"],
                "time_block": sample["time_block_id"],
            }
        )
    environment_sensitivity_result = environment_sensitivity(
        environment_records,
        ["cpu_model", "kernel", "virtualization", "host", "placement", "date", "time_block"],
        controls=("workload", "candidate"),
    )

    def _rank_unit(sample: Mapping[str, Any]) -> str:
        return str(sample["variant_id"] or sample["candidate_id"])

    def _slices_for(group_key: Callable[[Mapping[str, Any]], Any]) -> list[list[list[str]]]:
        sliced: list[list[list[str]]] = []
        keys = sorted({group_key(sample) for sample in samples if group_key(sample) is not None})
        for key in keys:
            scored: dict[str, list[float]] = defaultdict(list)
            for sample in samples:
                if group_key(sample) == key:
                    scored[_rank_unit(sample)].append(sample["value"])
            if len(scored) >= 2:
                aggregate_map = {
                    unit: aggregate(values, first.aggregation) for unit, values in scored.items()
                }
                sliced.append(ranking_groups(aggregate_map, maximize=maximize))
        return sliced

    rank_axes = [
        {
            "axis": "machine",
            "scoring_formula_ids": None,
            "rankings": _slices_for(lambda sample: sample["target_id"]),
            "limitations": [],
        },
        {
            "axis": "day",
            "scoring_formula_ids": None,
            "rankings": _slices_for(lambda sample: sample["date"]),
            "limitations": [],
        },
        {
            "axis": "scoring_formula",
            "scoring_formula_ids": ["looper.v1alpha1:objectives"],
            "rankings": [],
            "limitations": ["单一计分公式，缺少跨计分公式的排名切片"],
        },
    ]
    rank_stability_result = rank_stability_by_axes(rank_axes)

    reference_validity_result = reference_validity_rate(
        _reference_validity_records(samples, spec),
        expected_direction=first.direction.value,
        minimum_effect=(
            spec.scenario.load_search.minimum_effect_ratio
            if spec.scenario is not None and spec.scenario.load_search is not None
            else 0.05
        ),
        min_repeats=spec.design.min_repeats,
    )

    if task_scores is not None:
        task_leverage_result = task_leverage(
            task_scores,
            task_weights or {},
            scoring_formula="objectives weighted-sum (relative improvement vs baseline)",
            aggregation_method="weighted-sum",
            decomposable=True,
        )
    else:
        task_leverage_result = task_leverage(
            {}, scoring_formula=None, aggregation_method=None, decomposable=False
        )

    statuses = {
        reference_validity_result["status"],
        rank_stability_result["status"],
        task_leverage_result["status"],
        environment_sensitivity_result["status"],
    }
    if any(status == "available" for status in statuses):
        overall = "available"
    elif any(status == "partial" for status in statuses):
        overall = "partial"
    elif statuses == {"unavailable"}:
        overall = "unavailable"
    else:
        overall = "insufficient_evidence"

    return {
        "schemaVersion": "v1alpha1",
        "methodVersion": BENCHTRUST_METHOD_VERSION,
        "status": overall,
        "referenceValidityRate": reference_validity_result,
        "rankStability": rank_stability_result,
        "taskLeverage": task_leverage_result,
        "environmentSensitivity": environment_sensitivity_result,
        "evidence": {
            "sample_count": len(samples),
            "target_count": len({item["target_id"] for item in samples}),
            "distinct_dates": len({item["date"] for item in samples if item["date"]}),
            "distinct_workloads": len({item["workload_id"] for item in samples}),
        },
        "limitations": [
            "BenchTrust 元指标是证据，不作为硬门禁",
            "仅当 audit policy 声明阈值时才输出 pass/warning/fail；当前未声明阈值",
            "单项指标不可互相补偿（如 Task Leverage 高不被 Reference Validity 高抵消）",
        ],
        "inputDigest": input_digest,
        "policyDigest": policy_digest,
    }


def build_analysis_snapshot(
    session: Session, experiment_id: str, *, persist: bool = True
) -> dict[str, Any]:
    experiment = session.get(ExperimentRecord, experiment_id)
    if experiment is None:
        raise ValueError("experiment does not exist")
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    facts = _input_facts(session, experiment_id)
    input_digest = canonical_digest(facts)
    policy = {
        "mode": spec.mode,
        "objectives": [item.model_dump(mode="json") for item in spec.objectives],
        "stability_objectives": [
            item.model_dump(mode="json") for item in spec.stability_objectives
        ],
        "gates": [item.model_dump(mode="json") for item in spec.gates],
        "design": spec.design.model_dump(mode="json"),
        "scenario": spec.scenario.model_dump(mode="json") if spec.scenario else None,
        "selection": spec.selection.model_dump(mode="json") if spec.selection else None,
        "code_version": ANALYSIS_CODE_VERSION,
        "benchtrust_method_version": BENCHTRUST_METHOD_VERSION,
    }
    policy_digest = canonical_digest(policy)
    existing = session.scalar(
        select(AnalysisSnapshotRecord).where(
            AnalysisSnapshotRecord.experiment_id == experiment_id,
            AnalysisSnapshotRecord.input_digest == input_digest,
            AnalysisSnapshotRecord.policy_digest == policy_digest,
        )
    )
    if existing:
        return existing.result_json

    if spec.mode == ExperimentMode.SELECTION:
        result = _selection_analysis(
            session,
            experiment,
            spec,
            input_digest=input_digest,
            policy_digest=policy_digest,
            facts=facts,
        )
        if persist:
            session.add(
                AnalysisSnapshotRecord(
                    id=new_id("ana"),
                    experiment_id=experiment_id,
                    policy_digest=policy_digest,
                    input_digest=input_digest,
                    code_version=ANALYSIS_CODE_VERSION,
                    status=result["status"],
                    result_json=result,
                    created_at=utc_now(),
                )
            )
            session.flush()
        return result

    candidates = list(
        session.scalars(
            select(CandidateRecord)
            .where(CandidateRecord.experiment_id == experiment_id)
            .order_by(CandidateRecord.sequence)
        )
    )
    baseline = next((item for item in candidates if item.role == "baseline"), None)
    baseline_observations: list[ObservationRecord] = []
    if baseline:
        _, baseline_observations, _ = _candidate_observations(session, baseline)

    rendered_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        attempts, observations, checks = _candidate_observations(session, candidate)
        objective_results: list[dict[str, Any]] = []
        for objective_index, objective in enumerate(spec.objectives):
            values = _metric_values(observations, objective.metric, objective.unit)
            baseline_values = _metric_values(
                baseline_observations, objective.metric, objective.unit
            )
            objective_results.append(
                _objective_result(
                    values,
                    baseline_values,
                    objective,
                    spec.design,
                    is_baseline=candidate.role == "baseline",
                    seed=spec.design.random_seed + candidate.sequence * 101 + objective_index,
                )
            )
        gates = _candidate_gate_results(spec, observations, checks, objective_results)
        stability_results: list[dict[str, Any]] = []
        for stability_objective in spec.stability_objectives:
            target = next(
                item for item in spec.objectives if item.metric == stability_objective.target_metric
            )
            stability_results.append(
                evaluate_stability_objective(
                    _metric_values(observations, target.metric, target.unit),
                    _metric_values(baseline_observations, target.metric, target.unit),
                    stability_objective,
                    target.direction,
                )
            )
        failed_gates = [item for item in gates if item["hard"] and not item["passed"]]
        failed_stability = [
            item for item in stability_results if item["hard"] and item["status"] != "satisfied"
        ]
        objective_missing = any(item["status"] != "available" for item in objective_results)
        has_successful_attempts = bool(attempts)
        feasible = (
            has_successful_attempts
            and not failed_gates
            and not failed_stability
            and not objective_missing
        )
        if not has_successful_attempts:
            status = "inconclusive"
            reason = "no successful attempts"
        elif objective_missing:
            status = "inconclusive"
            reason = "one or more objectives have insufficient evidence"
        elif failed_gates:
            status = "infeasible"
            reason = "; ".join(str(item["id"]) for item in failed_gates)
        elif failed_stability:
            status = "infeasible"
            reason = "; ".join(
                f"stability:{item['id']}({item['status']})" for item in failed_stability
            )
        else:
            status = "feasible"
            reason = None
        rendered_candidates.append(
            {
                "id": candidate.id,
                "sequence": candidate.sequence,
                "role": candidate.role,
                "parameters": candidate.parameters_json,
                "config_digest": candidate.config_digest,
                "status": status,
                "reason": reason,
                "feasible": feasible,
                "attempt_count": len(attempts),
                "objectives": objective_results,
                "stability": stability_results,
                "gates": gates,
                "pareto_rank": None,
            }
        )

    def _pareto_dimensions(candidate: dict[str, Any]) -> dict[str, float]:
        """Performance objective raw values plus soft stability dimensions.

        Hard stability objectives never reach this mapping: violations already
        made the candidate infeasible, and constraints must not double as
        ranking dimensions. Soft dimensions with insufficient evidence are
        omitted, which blocks dominance on that axis (fail closed).
        """

        dimensions = {
            objective["metric"]: objective["raw"]
            for objective in candidate["objectives"]
            if objective["raw"] is not None
        }
        for item in candidate.get("stability", []):
            if not item["hard"] and item["pareto_value"] is not None:
                dimensions[f"stability:{item['id']}"] = item["pareto_value"]
        return dimensions

    points = [
        {
            "id": candidate["id"],
            "feasible": candidate["feasible"],
            "objectives": _pareto_dimensions(candidate),
        }
        for candidate in rendered_candidates
    ]
    objective_directions = {objective.metric: objective.direction for objective in spec.objectives}
    objective_epsilons = {objective.metric: objective.epsilon for objective in spec.objectives}
    for stability_objective in spec.stability_objectives:
        if stability_objective.hard:
            continue
        key = f"stability:{stability_objective.id}"
        if stability_objective.metric == StabilityMetric.CV:
            # CV is dimensionless and always smaller-better, independent of the
            # target metric's direction.
            objective_directions[key] = Direction.MINIMIZE
        else:
            target = next(
                item for item in spec.objectives if item.metric == stability_objective.target_metric
            )
            objective_directions[key] = target.direction
        objective_epsilons[key] = 0.0
    ranks = pareto_ranks(points, objective_directions, objective_epsilons)
    for candidate in rendered_candidates:
        candidate["pareto_rank"] = ranks.get(candidate["id"])

    objective_weights = {objective.metric: objective.weight for objective in spec.objectives}
    task_scores: dict[str, dict[str, float]] = {}
    for candidate in rendered_candidates:
        row: dict[str, float] = {}
        for objective in candidate["objectives"]:
            improvement = objective.get("improvement")
            if improvement is None:
                row = {}
                break
            row[objective["metric"]] = float(improvement)
        if row:
            task_scores[candidate["id"]] = row
    artifact_count = int(
        session.scalar(
            select(func.count(ArtifactLinkRecord.id))
            .join(AttemptRecord, AttemptRecord.id == ArtifactLinkRecord.attempt_id)
            .where(AttemptRecord.experiment_id == experiment_id)
        )
        or 0
    )
    observation_count = sum(len(item["observations"]) for item in facts)
    result = {
        "schema_version": "v1alpha1",
        "experiment_id": experiment_id,
        "input_digest": input_digest,
        "policy_digest": policy_digest,
        "code_version": ANALYSIS_CODE_VERSION,
        "status": "available" if candidates else "insufficient_evidence",
        "candidates": rendered_candidates,
        "pareto": [
            {
                "candidate_id": candidate["id"],
                "rank": candidate["pareto_rank"],
                "feasible": candidate["feasible"],
                "objectives": _pareto_dimensions(candidate),
            }
            for candidate in rendered_candidates
        ],
        "benchtrust": _build_benchtrust(
            session,
            experiment,
            spec,
            task_scores=task_scores,
            task_weights=objective_weights,
            input_digest=input_digest,
            policy_digest=policy_digest,
        ),
        "evidence": {
            "attempt_count": len(facts),
            "observation_count": observation_count,
            "artifact_count": artifact_count,
            "all_artifacts_content_addressed": True,
        },
    }
    if persist:
        session.add(
            AnalysisSnapshotRecord(
                id=new_id("ana"),
                experiment_id=experiment_id,
                policy_digest=policy_digest,
                input_digest=input_digest,
                code_version=ANALYSIS_CODE_VERSION,
                status=result["status"],
                result_json=result,
                created_at=utc_now(),
            )
        )
        session.flush()
    return result
