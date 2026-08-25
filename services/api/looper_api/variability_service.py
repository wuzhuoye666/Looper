"""Contract-driven variability diagnostics over normalized Benchmark observations."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.contracts import ExperimentMode, ExperimentSpec, GateKind
from looper_core.manifest import load_and_validate_manifest
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

from looper_api.analysis_service import _input_facts, _observation_value, build_analysis_snapshot
from looper_api.models import (
    AnalysisSnapshotRecord,
    AttemptRecord,
    BenchmarkRecord,
    CandidateRecord,
    EvaluationRecord,
    ExperimentRecord,
    ObservationRecord,
    TargetRecord,
)

SYSTEM_METRIC_NAMES_SET = frozenset(SYSTEM_METRIC_NAMES)


def _date_of(attempt: AttemptRecord) -> str | None:
    moment = attempt.started_at or attempt.completed_at or attempt.created_at
    return moment.date().isoformat() if isinstance(moment, datetime) else None


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
    value = binding.get("placementPairId") or binding.get("placement_pair_id")
    return str(value) if value else None


def _time_block_of(attempt: AttemptRecord) -> str | None:
    value = _envelope_extensions(attempt).get("timeBlockId")
    return str(value) if value else None


def _run_sample(
    attempt: AttemptRecord,
    observations: list[ObservationRecord],
    *,
    metric: str,
    unit: str,
    target_snapshot: dict[str, Any] | None,
    target_id: str | None,
) -> RunSample | None:
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


def _benchmark_contract(
    session: Session, spec: ExperimentSpec
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    benchmark = session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == spec.benchmark_id,
            BenchmarkRecord.version == spec.benchmark_version,
        )
    )
    if benchmark is None:
        return None, None
    manifest = benchmark.manifest_json if isinstance(benchmark.manifest_json, dict) else {}
    manifest_spec = manifest.get("spec") or {}
    contract = (manifest_spec.get("x-extensions") or {}).get("diagnosticRecommendations")
    if not isinstance(contract, dict):
        repository_root = Path(__file__).resolve().parents[3]
        for path in (repository_root / "benchmarks").glob("*/benchmark.yaml"):
            current, _ = load_and_validate_manifest(path)
            metadata = current.get("metadata") or {}
            if (
                metadata.get("id") == spec.benchmark_id
                and metadata.get("version") == spec.benchmark_version
            ):
                manifest_spec = current.get("spec") or manifest_spec
                contract = (manifest_spec.get("x-extensions") or {}).get(
                    "diagnosticRecommendations"
                )
                break
    return manifest_spec, contract if isinstance(contract, dict) else None


def _metric_declaration(
    manifest_spec: dict[str, Any], workload_id: str
) -> tuple[str, str, str] | None:
    workload = next(
        (item for item in manifest_spec.get("workloads", []) if str(item.get("id")) == workload_id),
        None,
    )
    name: str | None = None
    if workload:
        for metric_name, declaration in (workload.get("metrics") or {}).items():
            roles = (declaration.get("presentation") or {}).get("roles", [])
            if "primary_outcome" in roles:
                name = str(metric_name)
                break
    adapter = manifest_spec.get("adapter") or {}
    scenario = manifest_spec.get("scenario") or {}
    name = name or adapter.get("primaryMetric") or scenario.get("primary_metric")
    definition = (manifest_spec.get("metrics") or {}).get(name) if name else None
    if not name or not isinstance(definition, dict):
        return None
    direction = str(definition.get("direction") or "none")
    if direction not in {"minimize", "maximize"}:
        return None
    return str(name), str(definition.get("unit") or ""), direction


def _policy(contract: dict[str, Any]) -> VariabilityPolicy:
    payload = contract.get("policy") if isinstance(contract.get("policy"), dict) else {}
    return VariabilityPolicy.model_validate(payload)


def _validity_facts(
    session: Session, experiment_id: str, mode: ExperimentMode
) -> tuple[set[str] | None, dict[tuple[str, str], int]]:
    if mode != ExperimentMode.SELECTION:
        return None, {}
    analysis = build_analysis_snapshot(session, experiment_id, persist=False)
    blocks = analysis.get("blocks") if isinstance(analysis, dict) else []
    valid_ids = {str(item["attempt_id"]) for item in blocks if item.get("valid")}
    invalid_counts: dict[tuple[str, str], int] = defaultdict(int)
    for item in blocks:
        if not item.get("valid"):
            invalid_counts[(str(item.get("target_id")), str(item.get("workload_id")))] += 1
    return valid_ids, dict(invalid_counts)


def _collect_group_samples(
    session: Session,
    experiment_id: str,
    manifest_spec: dict[str, Any],
    mode: ExperimentMode,
    valid_attempt_ids: set[str] | None,
) -> dict[tuple[str, str, str, str, str, str, str], list[RunSample]]:
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
    if valid_attempt_ids is not None:
        attempts = [attempt for attempt in attempts if attempt.id in valid_attempt_ids]
    if not attempts:
        return {}
    evaluations = {
        item.id: item
        for item in session.scalars(
            select(EvaluationRecord).where(
                EvaluationRecord.id.in_({attempt.evaluation_id for attempt in attempts})
            )
        )
    }
    candidates = {
        item.id: item
        for item in session.scalars(
            select(CandidateRecord).where(
                CandidateRecord.id.in_({item.candidate_id for item in evaluations.values()})
            )
        )
    }
    targets = {
        item.id: item
        for item in session.scalars(
            select(TargetRecord).where(
                TargetRecord.id.in_({item.target_id for item in evaluations.values()})
            )
        )
    }
    observations_by_attempt: dict[str, list[ObservationRecord]] = defaultdict(list)
    for observation in session.scalars(
        select(ObservationRecord).where(
            ObservationRecord.attempt_id.in_([attempt.id for attempt in attempts])
        )
    ):
        observations_by_attempt[observation.attempt_id].append(observation)

    groups: dict[tuple[str, str, str, str, str, str, str], list[RunSample]] = defaultdict(list)
    for attempt in attempts:
        evaluation = evaluations.get(attempt.evaluation_id)
        if evaluation is None:
            continue
        declaration = _metric_declaration(manifest_spec, evaluation.workload_id)
        if declaration is None:
            continue
        metric, unit, direction = declaration
        target = targets.get(evaluation.target_id)
        candidate = candidates.get(evaluation.candidate_id)
        if mode == ExperimentMode.SELECTION:
            group_key = evaluation.target_id
            label = target.name if target is not None else evaluation.target_id
        else:
            role = candidate.role if candidate is not None else "candidate"
            group_key = (
                f"{role}:{candidate.id}"
                if candidate is not None
                else evaluation.candidate_id
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
            key = (
                group_key,
                group_label,
                evaluation.target_id,
                evaluation.workload_id,
                metric,
                unit,
                direction,
            )
            groups[key].append(sample)
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


def _rule_applies(rule: dict[str, Any], workload_id: str) -> bool:
    workloads = rule.get("workloadIds")
    return not workloads or workload_id in workloads


def _render_rule(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "ruleId": rule["id"],
        "source": "benchmark-contract",
        "action": rule["action"],
        "rationale": rule["rationale"],
        "priority": rule["priority"],
        "kind": rule["kind"],
    }


def _group_recommendations(
    contract: dict[str, Any], report: dict[str, Any], workload_id: str, invalid_count: int
) -> list[dict[str, Any]]:
    conditions = {str(report["status"])}
    if report.get("modes"):
        conditions.add("multimodal")
    outliers = report.get("outliers") or {}
    if outliers.get("slow") or outliers.get("fast"):
        conditions.add("outlier_present")
    if (report.get("evidence") or {}).get("systemMetricCount", 0) == 0:
        conditions.add("missing_system_metrics")
    if invalid_count:
        conditions.add("gate_failure")
    return [
        _render_rule(rule)
        for rule in contract.get("rules", [])
        if rule.get("scope") == "group"
        and rule.get("when") in conditions
        and _rule_applies(rule, workload_id)
    ]


def _study_recommendations(
    contract: dict[str, Any], *, target_ids: set[str], has_comparisons: bool
) -> list[dict[str, Any]]:
    conditions: set[str] = set()
    if len(target_ids) == 1:
        conditions.add("single_target")
    if not has_comparisons:
        conditions.add("no_comparison")
    return [
        _render_rule(rule)
        for rule in contract.get("rules", [])
        if rule.get("scope") == "study" and rule.get("when") in conditions
    ]


def _unavailable_result(
    experiment_id: str, spec: ExperimentSpec, input_digest: str, reason: str
) -> dict[str, Any]:
    return {
        "schema_version": "v1alpha1",
        "analyzer": VARIABILITY_ANALYZER_ID,
        "analyzer_version": VARIABILITY_CODE_VERSION,
        "experiment_id": experiment_id,
        "mode": spec.mode.value,
        "metric": "",
        "unit": "",
        "direction": "",
        "status": "unavailable",
        "reason": reason,
        "group_statuses": [],
        "groups": [],
        "comparisons": [],
        "studyRecommendations": [],
        "input_digest": input_digest,
    }


def build_variability_report(
    session: Session, experiment_id: str, *, persist: bool = True
) -> dict[str, Any]:
    experiment = session.get(ExperimentRecord, experiment_id)
    if experiment is None:
        raise ValueError("experiment does not exist")
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    facts = _input_facts(session, experiment_id)
    input_digest = canonical_digest(facts)
    manifest_spec, contract = _benchmark_contract(session, spec)
    if manifest_spec is None:
        return _unavailable_result(
            experiment_id, spec, input_digest, "无法读取该实验所用的 Benchmark 合同。"
        )
    if not contract or contract.get("enabled") is not True:
        return _unavailable_result(
            experiment_id, spec, input_digest, "该 Benchmark 未声明优化建议规则。"
        )

    policy = _policy(contract)
    diagnostic_contract_digest = canonical_digest(contract)
    policy_payload = {
        "analyzer": VARIABILITY_ANALYZER_ID,
        "analyzerVersion": VARIABILITY_CODE_VERSION,
        "policy": policy.model_dump(mode="json", by_alias=True),
        "diagnosticContract": contract,
        "diagnosticContractDigest": diagnostic_contract_digest,
        "workloadMetrics": {
            workload_id: _metric_declaration(manifest_spec, workload_id)
            for workload_id in spec.workload_ids
        },
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

    valid_attempt_ids, invalid_counts = _validity_facts(session, experiment_id, spec.mode)
    groups = _collect_group_samples(
        session, experiment_id, manifest_spec, spec.mode, valid_attempt_ids
    )
    reports: list[dict[str, Any]] = []
    report_samples: dict[tuple[str, str], tuple[list[RunSample], str, str, str]] = {}
    for key, samples in sorted(groups.items()):
        group_key, group_label, target_id, workload_id, metric, unit, direction = key
        report = analyze_variability(
            samples,
            metric=metric,
            unit=unit,
            direction=direction,
            group_label=group_label,
            policy=policy,
        ).model_dump(mode="json", by_alias=True)
        invalid_count = invalid_counts.get((target_id, workload_id), 0)
        report.update(
            {
                "targetId": target_id,
                "workloadId": workload_id,
                "invalidAttemptCount": invalid_count,
                "recommendations": _group_recommendations(
                    contract, report, workload_id, invalid_count
                ),
                "selectionImpact": {
                    "summary": "仅描述该工作负载的稳定性与证据质量，不形成性能优劣或采购结论。",
                    "confidence": report.get("selectionImpact", {}).get(
                        "confidence", "insufficient"
                    ),
                    "details": report.get("selectionImpact", {}).get("details", []),
                },
            }
        )
        reports.append(report)
        report_samples[(group_key, workload_id)] = (samples, metric, unit, direction)

    comparisons: list[dict[str, Any]] = []
    if spec.mode == ExperimentMode.SELECTION:
        by_workload: dict[
            str, list[tuple[str, tuple[list[RunSample], str, str, str]]]
        ] = defaultdict(list)
        for (group_key, workload_id), value in report_samples.items():
            by_workload[workload_id].append((group_key, value))
        for workload_id, entries in sorted(by_workload.items()):
            for (left_key, left), (right_key, right) in combinations(entries, 2):
                left_samples, metric, unit, direction = left
                comparison = compare_distributions(
                    left_samples,
                    right[0],
                    metric=metric,
                    unit=unit,
                    direction=direction,
                    baseline_label=f"{left_key} · {workload_id}",
                    candidate_label=f"{right_key} · {workload_id}",
                    slo_threshold=_slo_threshold(spec, metric),
                    policy=policy,
                )
                rendered = comparison.model_dump(mode="json", by_alias=True)
                rendered["workloadId"] = workload_id
                comparisons.append(rendered)
    else:
        by_workload: dict[
            str, list[tuple[str, tuple[list[RunSample], str, str, str]]]
        ] = defaultdict(list)
        for (group_key, workload_id), value in report_samples.items():
            by_workload[workload_id].append((group_key, value))
        for workload_id, entries in sorted(by_workload.items()):
            baseline = next((entry for entry in entries if entry[0].startswith("baseline:")), None)
            if baseline is None:
                continue
            baseline_key, baseline_values = baseline
            baseline_samples, metric, unit, direction = baseline_values
            for candidate_key, candidate_values in entries:
                if candidate_key == baseline_key:
                    continue
                comparison = compare_distributions(
                    baseline_samples,
                    candidate_values[0],
                    metric=metric,
                    unit=unit,
                    direction=direction,
                    baseline_label=f"{baseline_key} · {workload_id}",
                    candidate_label=f"{candidate_key} · {workload_id}",
                    slo_threshold=_slo_threshold(spec, metric),
                    policy=policy,
                )
                rendered = comparison.model_dump(mode="json", by_alias=True)
                rendered["workloadId"] = workload_id
                comparisons.append(rendered)

    study_recommendations = _study_recommendations(
        contract, target_ids=set(spec.target_ids), has_comparisons=bool(comparisons)
    )
    overall = "available" if reports else "insufficient_evidence"
    result = {
        "schema_version": "v1alpha1",
        "analyzer": VARIABILITY_ANALYZER_ID,
        "analyzer_version": VARIABILITY_CODE_VERSION,
        "experiment_id": experiment_id,
        "mode": spec.mode.value,
        "metric": "workload-specific",
        "unit": "varies",
        "direction": "varies",
        "status": overall,
        "group_statuses": sorted({report["status"] for report in reports}),
        "groups": reports,
        "comparisons": comparisons,
        "studyRecommendations": study_recommendations,
        "policy": policy.model_dump(mode="json", by_alias=True),
        "diagnosticContractDigest": diagnostic_contract_digest,
        "input_digest": input_digest,
        "policy_digest": policy_digest,
        "evidence": {
            "attempt_count": len(facts),
            "valid_attempt_count": (
                len(valid_attempt_ids) if valid_attempt_ids is not None else None
            ),
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
