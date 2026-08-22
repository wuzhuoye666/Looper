"""Variability analyzer service: DB run rows to RunSamples to a VGO report.

Reads already-normalized observations (never upstream benchmark files) for the
experiment's primary objective, groups repeated runs by target (selection) or
candidate (optimization) and workload, runs the shared analyzer, and persists
the result as an analysis snapshot keyed by the variability policy digest.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from statistics import fmean, pstdev
from typing import Any

from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.contracts import ExperimentMode, ExperimentSpec, GateKind
from looper_core.state import AttemptStatus
from looper_core.variability import (
    SYSTEM_METRIC_NAMES,
    VARIABILITY_ANALYZER_ID,
    VARIABILITY_CODE_VERSION,
    RunSample,
    VariabilityPolicy,
    analyze_variability,
    compare_distributions,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.analysis_service import _input_facts, _observation_value
from looper_api.models import (
    AnalysisSnapshotRecord,
    AttemptRecord,
    CandidateRecord,
    EvaluationRecord,
    ExperimentRecord,
    ObservationRecord,
    TargetRecord,
)

SYSTEM_METRIC_NAMES_SET = frozenset(SYSTEM_METRIC_NAMES)
DEFAULT_VARIABILITY_POLICY = VariabilityPolicy()


def _date_of(attempt: AttemptRecord) -> str | None:
    moment = attempt.started_at or attempt.completed_at or attempt.created_at
    if moment is None or not isinstance(moment, datetime):
        return None
    return moment.date().isoformat()


def _host_of(attempt: AttemptRecord, target_snapshot: dict[str, Any] | None) -> str | None:
    envelope = attempt.envelope_json if isinstance(attempt.envelope_json, dict) else {}
    for source in (envelope.get("target"), target_snapshot):
        if isinstance(source, dict):
            fingerprint = source.get("fingerprint")
            if isinstance(fingerprint, dict) and fingerprint.get("hostname"):
                return str(fingerprint["hostname"])
    return None


def _envelope_extensions(attempt: AttemptRecord) -> dict[str, Any]:
    envelope = attempt.envelope_json if isinstance(attempt.envelope_json, dict) else {}
    extensions = envelope.get("extensions")
    return extensions if isinstance(extensions, dict) else {}


def _placement_of(attempt: AttemptRecord) -> str | None:
    binding = _envelope_extensions(attempt).get("targetBinding")
    if not isinstance(binding, dict):
        return None
    for key in ("placementPairId", "placement_pair_id"):
        if binding.get(key):
            return str(binding[key])
    return None


def _time_block_of(attempt: AttemptRecord) -> str | None:
    block = _envelope_extensions(attempt).get("timeBlockId")
    return str(block) if block else None


def _run_sample(
    attempt: AttemptRecord,
    observations: list[ObservationRecord],
    *,
    metric: str,
    unit: str,
    target_snapshot: dict[str, Any] | None,
    target_id: str | None,
) -> RunSample | None:
    """Fold one attempt's observations into a single RunSample."""

    objective_values: list[float] = []
    system_values: dict[str, list[float]] = defaultdict(list)
    for observation in observations:
        value = _observation_value(observation)
        if observation.phase == "warmup" or isinstance(value, bool) or value is None:
            continue
        if not math.isfinite(float(value)):
            continue
        if observation.metric == metric and observation.unit == unit:
            objective_values.append(float(value))
        elif observation.metric in SYSTEM_METRIC_NAMES_SET:
            system_values[observation.metric].append(float(value))
    if not objective_values:
        return None
    return RunSample(
        runId=attempt.id,
        value=fmean(objective_values),
        withinRunStd=pstdev(objective_values) if len(objective_values) > 1 else None,
        targetId=target_id,
        hostId=_host_of(attempt, target_snapshot),
        placementPairId=_placement_of(attempt),
        date=_date_of(attempt),
        timeBlockId=_time_block_of(attempt),
        systemMetrics={name: fmean(values) for name, values in system_values.items() if values},
    )


def _collect_group_samples(
    session: Session,
    experiment_id: str,
    metric: str,
    unit: str,
    mode: ExperimentMode,
) -> dict[tuple[str, str], list[RunSample]]:
    """Return RunSamples keyed by (group_key, group_label).

    Selection mode groups by target; optimization mode groups by candidate.
    Each group label keeps the workload identity so a study with several
    workloads still yields one report per (group, workload).
    """

    attempts = list(
        session.scalars(
            select(AttemptRecord)
            .where(
                AttemptRecord.experiment_id == experiment_id,
                AttemptRecord.status == AttemptStatus.SUCCEEDED,
            )
            .order_by(AttemptRecord.id)
        )
    )
    if not attempts:
        return {}
    evaluations = {
        evaluation.id: evaluation
        for evaluation in session.scalars(
            select(EvaluationRecord).where(
                EvaluationRecord.id.in_({attempt.evaluation_id for attempt in attempts})
            )
        )
    }
    candidates = {
        candidate.id: candidate
        for candidate in session.scalars(
            select(CandidateRecord).where(
                CandidateRecord.id.in_(
                    {evaluation.candidate_id for evaluation in evaluations.values()}
                )
            )
        )
    }
    targets = {
        target.id: target
        for target in session.scalars(
            select(TargetRecord).where(
                TargetRecord.id.in_({evaluation.target_id for evaluation in evaluations.values()})
            )
        )
    }
    observations = list(
        session.scalars(
            select(ObservationRecord).where(
                ObservationRecord.attempt_id.in_([attempt.id for attempt in attempts])
            )
        )
    )
    observations_by_attempt: dict[str, list[ObservationRecord]] = defaultdict(list)
    for observation in observations:
        observations_by_attempt[observation.attempt_id].append(observation)

    groups: dict[tuple[str, str], list[RunSample]] = defaultdict(list)
    for attempt in attempts:
        evaluation = evaluations.get(attempt.evaluation_id)
        if evaluation is None:
            continue
        candidate = candidates.get(evaluation.candidate_id)
        target = targets.get(evaluation.target_id)
        if mode == ExperimentMode.SELECTION:
            group_key = evaluation.target_id
            label = target.name if target is not None else evaluation.target_id
        else:
            role = candidate.role if candidate is not None else "candidate"
            group_key = (
                f"{role}:{candidate.id}" if candidate is not None else evaluation.candidate_id
            )
            label = group_key
        group_label = f"{label} · {evaluation.workload_id}"
        sample = _run_sample(
            attempt,
            observations_by_attempt.get(attempt.id, []),
            metric=metric,
            unit=unit,
            target_snapshot=evaluation.target_snapshot_json,
            target_id=evaluation.target_id,
        )
        if sample is not None:
            groups.setdefault((group_key, group_label), []).append(sample)
    return groups


def _slo_threshold(spec: ExperimentSpec, metric: str) -> float | None:
    for gate in spec.gates:
        if (
            gate.kind == GateKind.SLO
            and gate.metric == metric
            and gate.threshold is not None
            and not isinstance(gate.threshold, bool)
        ):
            return float(gate.threshold)
    return None


def _variability_policy(spec: ExperimentSpec) -> VariabilityPolicy:
    objective = spec.objectives[0] if spec.objectives else None
    if objective is None:
        return DEFAULT_VARIABILITY_POLICY
    return VariabilityPolicy(
        minimumSamples=max(DEFAULT_VARIABILITY_POLICY.minimum_samples, objective.minimum_samples)
    )


def build_variability_report(
    session: Session, experiment_id: str, *, persist: bool = True
) -> dict[str, Any]:
    experiment = session.get(ExperimentRecord, experiment_id)
    if experiment is None:
        raise ValueError("experiment does not exist")
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    if not spec.objectives:
        raise ValueError("experiment declares no objectives; variability analysis is undefined")
    objective = spec.objectives[0]
    policy = _variability_policy(spec)
    facts = _input_facts(session, experiment_id)
    input_digest = canonical_digest(facts)
    policy_payload = {
        "analyzer": VARIABILITY_ANALYZER_ID,
        "analyzerVersion": VARIABILITY_CODE_VERSION,
        "policy": policy.model_dump(mode="json", by_alias=True),
        "objective": objective.model_dump(mode="json"),
        "mode": spec.mode,
    }
    policy_digest = canonical_digest(policy_payload)
    existing = session.scalar(
        select(AnalysisSnapshotRecord).where(
            AnalysisSnapshotRecord.experiment_id == experiment_id,
            AnalysisSnapshotRecord.input_digest == input_digest,
            AnalysisSnapshotRecord.policy_digest == policy_digest,
        )
    )
    if existing:
        return existing.result_json

    groups = _collect_group_samples(
        session, experiment_id, objective.metric, objective.unit, spec.mode
    )
    reports: list[dict[str, Any]] = []
    samples_by_group: dict[str, list[RunSample]] = {}
    for (group_key, group_label), samples in sorted(groups.items()):
        report = analyze_variability(
            samples,
            metric=objective.metric,
            unit=objective.unit,
            direction=objective.direction,
            group_label=group_label,
            policy=policy,
        )
        reports.append(report.model_dump(mode="json", by_alias=True))
        samples_by_group.setdefault(group_key, []).extend(samples)

    comparisons: list[dict[str, Any]] = []
    slo_threshold = _slo_threshold(spec, objective.metric)
    if spec.mode == ExperimentMode.SELECTION:
        # Pairwise target comparisons within the same workload.
        by_workload: dict[str, list[tuple[str, list[RunSample]]]] = defaultdict(list)
        for (_group_key, group_label), samples in groups.items():
            workload = group_label.rsplit(" · ", 1)[-1]
            by_workload[workload].append((group_label, samples))
        for _workload, entries in sorted(by_workload.items()):
            for (left_label, left_samples), (right_label, right_samples) in combinations(
                entries, 2
            ):
                comparison = compare_distributions(
                    left_samples,
                    right_samples,
                    metric=objective.metric,
                    unit=objective.unit,
                    direction=objective.direction,
                    baseline_label=left_label,
                    candidate_label=right_label,
                    slo_threshold=slo_threshold,
                    policy=policy,
                )
                comparisons.append(comparison.model_dump(mode="json", by_alias=True))
    else:
        baseline_key = next((key for key in samples_by_group if key.startswith("baseline:")), None)
        if baseline_key is not None:
            for group_key, samples in sorted(samples_by_group.items()):
                if group_key == baseline_key:
                    continue
                comparison = compare_distributions(
                    samples_by_group[baseline_key],
                    samples,
                    metric=objective.metric,
                    unit=objective.unit,
                    direction=objective.direction,
                    baseline_label=baseline_key,
                    candidate_label=group_key,
                    slo_threshold=slo_threshold,
                    policy=policy,
                )
                comparisons.append(comparison.model_dump(mode="json", by_alias=True))

    overall = "available" if reports else "insufficient_evidence"
    result = {
        "schema_version": "v1alpha1",
        "analyzer": VARIABILITY_ANALYZER_ID,
        "analyzer_version": VARIABILITY_CODE_VERSION,
        "experiment_id": experiment_id,
        "mode": spec.mode.value,
        "metric": objective.metric,
        "unit": objective.unit,
        "direction": objective.direction.value,
        "status": overall,
        "group_statuses": sorted({report["status"] for report in reports}),
        "groups": reports,
        "comparisons": comparisons,
        "policy": policy.model_dump(mode="json", by_alias=True),
        "input_digest": input_digest,
        "policy_digest": policy_digest,
        "evidence": {
            "attempt_count": len(facts),
            "run_group_count": len(reports),
            "system_metric_names": list(SYSTEM_METRIC_NAMES),
        },
    }
    if persist:
        session.add(
            AnalysisSnapshotRecord(
                id=new_id("ana"),
                experiment_id=experiment_id,
                policy_digest=policy_digest,
                input_digest=input_digest,
                code_version=VARIABILITY_CODE_VERSION,
                status=overall,
                result_json=result,
                created_at=utc_now(),
            )
        )
        session.flush()
    return result
