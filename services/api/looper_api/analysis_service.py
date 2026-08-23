from __future__ import annotations

from collections import defaultdict
from typing import Any

from looper_core.analysis import (
    InsufficientEvidence,
    aggregate,
    bootstrap_improvement,
    cluster_paired_bootstrap_improvement,
    environment_sensitivity,
    gate_passes,
    paired_bootstrap_improvement,
    pareto_ranks,
    rank_stability,
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
        "benchtrust": {
            "placement_pair_count": len(
                {binding.placement_pair_id for binding in spec.selection.target_bindings}
            ),
            "conclusion_strength": comparisons[0]["conclusion_strength"]
            if comparisons
            else "availability-only",
        },
        "evidence": {
            "attempt_count": len(facts),
            "observation_count": sum(len(item["observations"]) for item in facts),
            "artifact_count": artifact_count,
            "all_artifacts_content_addressed": True,
        },
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
    candidate_observations: dict[str, list[ObservationRecord]] = {}
    for candidate in candidates:
        attempts, observations, checks = _candidate_observations(session, candidate)
        candidate_observations[candidate.id] = observations
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

    first_objective = spec.objectives[0]
    rankings: list[list[str]] = []
    workload_scores: dict[str, dict[str, float]] = defaultdict(dict)
    environment_groups: dict[str, list[float]] = defaultdict(list)
    evaluations = list(
        session.scalars(
            select(EvaluationRecord).where(EvaluationRecord.experiment_id == experiment_id)
        )
    )
    for evaluation in evaluations:
        observations = candidate_observations.get(evaluation.candidate_id, [])
        scoped = [
            item
            for item in observations
            if item.workload == evaluation.workload_id
            and item.metric == first_objective.metric
            and item.unit == first_objective.unit
            and not isinstance(_observation_value(item), bool)
        ]
        values = [
            float(_observation_value(item))
            for item in scoped
            if _observation_value(item) is not None
        ]
        if values:
            raw = aggregate(values, first_objective.aggregation)
            normalized = raw if first_objective.direction.value == "maximize" else -raw
            workload_scores[evaluation.workload_id][evaluation.candidate_id] = normalized
            environment_groups[evaluation.target_id].extend(values)
    for scores in workload_scores.values():
        rankings.append(sorted(scores, key=lambda item: (-scores[item], item)))

    leverage_input: dict[str, dict[str, float]] = defaultdict(dict)
    for workload, scores in workload_scores.items():
        for candidate_id, value in scores.items():
            leverage_input[candidate_id][workload] = value
    validity = [
        bool(candidate["objectives"][0].get("lower", 0) > 0)
        for candidate in rendered_candidates
        if candidate["role"] != "baseline"
        and candidate["objectives"]
        and candidate["objectives"][0]["status"] == "available"
    ]
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
        "benchtrust": {
            "reference_validity_rate": reference_validity_rate(validity),
            "rank_stability": rank_stability(rankings),
            "task_leverage": task_leverage(leverage_input),
            "environment_sensitivity": environment_sensitivity(environment_groups),
        },
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
