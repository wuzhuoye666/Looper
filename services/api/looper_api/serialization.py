from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from looper_core.contracts import Direction, ExperimentSpec
from looper_core.state import AttemptStatus, CandidateStatus, ExperimentStatus
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from looper_api.analysis_service import build_analysis_snapshot
from looper_api.models import (
    ArtifactLinkRecord,
    AttemptRecord,
    BenchmarkRecord,
    BenchmarkRegistrationRecord,
    CandidateRecord,
    EvaluationRecord,
    ExperimentRecord,
    TargetRecord,
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def benchmark_view(
    record: BenchmarkRecord,
    registration: BenchmarkRegistrationRecord | None = None,
) -> dict[str, Any]:
    manifest = record.manifest_json
    metadata = manifest["metadata"]
    spec = manifest["spec"]
    scenario = spec.get("scenario")
    adapter = spec.get("adapter") or {}
    extensions = spec.get("x-extensions", {})
    execution_status = extensions.get("executionStatus", "executable")
    metadata_extensions = metadata.get("x-extensions") or {}
    explicit_category = metadata_extensions.get("category") or extensions.get("category")
    return {
        "id": record.benchmark_id,
        "key": record.key,
        "name": record.name,
        "description": record.description,
        "category": explicit_category or ("scenario" if scenario else "unclassified"),
        "executionModel": adapter.get("executionModel", "custom"),
        "inputs": adapter.get("inputs", []),
        "executionPolicy": spec.get("runtime", {}).get("executionPolicy"),
        "version": record.version,
        "license": record.license,
        "manifestDigest": record.manifest_digest,
        "metrics": list(manifest["spec"]["metrics"]),
        "cases": len(manifest["spec"]["workloads"]),
        "updatedAt": _iso(record.installed_at),
        "tags": manifest["spec"].get("capabilities", []),
        "trusted": record.trusted,
        "executionStatus": execution_status,
        "runnable": execution_status == "executable"
        and (record.trusted or manifest["spec"]["runtime"].get("type") == "container"),
        "registrationId": registration.id if registration else None,
        "registrationStatus": registration.status if registration else None,
        "auditStatus": "registered-not-admitted" if registration else "legacy-unreviewed",
        "scenario": scenario,
        "decisionQuestion": scenario.get("decision_question") if scenario else None,
        "primaryMetric": (
            scenario.get("primary_metric") if scenario else adapter.get("primaryMetric")
        ),
    }


def target_view(record: TargetRecord) -> dict[str, Any]:
    status_map = {
        "available": "online",
        "inventory-only": "unknown",
        "degraded": "degraded",
        "offline": "offline",
    }
    inventory = record.inventory_json
    provider_state = str(inventory.get("instance_state") or inventory.get("status") or "").upper()
    status = status_map.get(record.status, "unknown")
    if record.lifecycle_status in {"missing", "archived"}:
        status = "offline"
    if record.status == "inventory-only" and provider_state == "RUNNING":
        status = "inventory" if record.lifecycle_status == "active" else "offline"
    elif record.status == "inventory-only" and provider_state in {"STOPPED", "TERMINATED"}:
        status = "offline"
    fingerprint = record.fingerprint_json
    hardware_parts = [
        fingerprint.get("processor") or fingerprint.get("instance_type"),
        f"{fingerprint.get('logical_cpu_count')} vCPU"
        if fingerprint.get("logical_cpu_count")
        else None,
        f"{fingerprint.get('memory_gib'):g} GiB"
        if fingerprint.get("memory_gib")
        else None,
    ]
    return {
        "id": record.id,
        "name": record.name,
        "type": record.provider,
        "provider": record.provider,
        "endpoint": inventory.get("endpoint") or inventory.get("private_ip") or "local",
        "status": status,
        "framework": inventory.get("framework") or fingerprint.get("system"),
        "version": fingerprint.get("release"),
        "hardware": " · ".join(str(item) for item in hardware_parts if item),
        "lastSeenAt": _iso(record.last_inventory_seen_at or record.updated_at),
        "tags": record.capabilities_json,
        "runnable": record.runnable and record.lifecycle_status == "active",
        "lifecycleStatus": record.lifecycle_status,
        "lastInventorySeenAt": _iso(record.last_inventory_seen_at),
        "missingSince": _iso(record.inventory_missing_since),
        "inventoryMissCount": record.inventory_miss_count,
        "archivedAt": _iso(record.archived_at),
        "archiveReason": record.archive_reason,
        "snapshotDigest": record.snapshot_digest,
        "fingerprint": fingerprint,
    }


def _analysis_by_candidate(
    session: Session, experiment: ExperimentRecord
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    candidates_exist = session.scalar(
        select(CandidateRecord.id).where(CandidateRecord.experiment_id == experiment.id).limit(1)
    )
    if not candidates_exist:
        return {}, None
    analysis = build_analysis_snapshot(session, experiment.id, persist=False)
    return {item["id"]: item for item in analysis.get("candidates", [])}, analysis


def _artifact_views(session: Session, attempt_ids: list[str]) -> list[dict[str, Any]]:
    if not attempt_ids:
        return []
    links = list(
        session.scalars(
            select(ArtifactLinkRecord).where(ArtifactLinkRecord.attempt_id.in_(attempt_ids))
        )
    )
    return [
        {
            "name": item.name,
            "url": f"/api/v1/artifacts/{item.digest}",
            "type": item.media_type,
            "digest": item.digest,
            "role": item.role,
            "attemptId": item.attempt_id,
        }
        for item in links
    ]


def _best_primary_score(
    results: list[dict[str, Any]], metric: str, direction: Direction
) -> float | None:
    scores: list[float] = []
    for result in results:
        objective = next(
            (item for item in result.get("objectives", []) if item.get("metric") == metric),
            None,
        )
        raw = objective.get("raw") if objective else None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            scores.append(float(raw))
    if not scores:
        return None
    return max(scores) if direction == Direction.MAXIMIZE else min(scores)


def experiment_view(
    session: Session, record: ExperimentRecord, *, detail: bool = False
) -> dict[str, Any]:
    spec = ExperimentSpec.model_validate(record.spec_json)
    candidates = list(
        session.scalars(
            select(CandidateRecord)
            .where(CandidateRecord.experiment_id == record.id)
            .order_by(CandidateRecord.sequence)
        )
    )
    attempts = list(
        session.scalars(select(AttemptRecord).where(AttemptRecord.experiment_id == record.id))
    )
    terminal_attempts = sum(
        1
        for attempt in attempts
        if AttemptStatus(attempt.status)
        in {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.CANCELLED,
            AttemptStatus.LOST,
        }
    )
    budget_terminal_attempts = sum(
        1
        for attempt in attempts
        if attempt.retry_index == 0
        and AttemptStatus(attempt.status)
        in {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.CANCELLED,
            AttemptStatus.LOST,
        }
    )
    terminal_candidates = sum(
        1
        for candidate in candidates
        if CandidateStatus(candidate.status)
        in {
            CandidateStatus.FEASIBLE,
            CandidateStatus.INFEASIBLE,
            CandidateStatus.INCONCLUSIVE,
            CandidateStatus.FAILED,
        }
    )
    analysis_map, analysis = _analysis_by_candidate(session, record)
    is_selection = spec.mode.value == "selection"
    selection_targets = {item["target_id"]: item for item in (analysis or {}).get("targets", [])}
    baseline = next((item for item in candidates if item.role == "baseline"), None)
    baseline_result = analysis_map.get(baseline.id) if baseline else None
    feasible = [item for item in analysis_map.values() if item.get("feasible")]
    primary_objective = spec.objectives[0]
    best_score = (
        None
        if is_selection
        else _best_primary_score(
            feasible,
            primary_objective.metric,
            primary_objective.direction,
        )
    )

    benchmark = session.scalar(
        select(BenchmarkRecord).where(
            BenchmarkRecord.benchmark_id == spec.benchmark_id,
            BenchmarkRecord.version == spec.benchmark_version,
        )
    )
    target = session.get(TargetRecord, spec.target_ids[0]) if spec.target_ids else None
    response: dict[str, Any] = {
        "id": record.id,
        "name": record.name,
        "description": record.description,
        "status": record.status,
        "mode": spec.mode,
        "targetId": target.id if target else None,
        "targetName": target.name if target else None,
        "targetIds": spec.target_ids,
        "targetNames": [
            item.name
            for target_id in spec.target_ids
            if (item := session.get(TargetRecord, target_id)) is not None
        ],
        "benchmarkId": spec.benchmark_id,
        "benchmarkName": benchmark.name if benchmark else spec.benchmark_id,
        "progress": round(
            100
            * (
                budget_terminal_attempts / spec.budget.max_attempts
                if is_selection
                else terminal_candidates / spec.budget.max_candidates
            ),
            1,
        )
        if (spec.budget.max_attempts if is_selection else spec.budget.max_candidates)
        else 0,
        "bestScore": best_score,
        "baselineScore": baseline_result["objectives"][0].get("raw")
        if baseline_result and baseline_result.get("objectives")
        else None,
        "createdAt": _iso(record.created_at),
        "updatedAt": _iso(record.updated_at),
        "attempts": budget_terminal_attempts,
        "actualAttempts": terminal_attempts,
        "maxAttempts": spec.budget.max_attempts,
        "objective": spec.objectives[0].metric,
        "decisionQuestion": spec.scenario.decision_question if spec.scenario else None,
        "scenario": spec.scenario.model_dump(mode="json") if spec.scenario else None,
        "comparison": (analysis or {}).get("comparisons", [None])[0]
        if (analysis or {}).get("comparisons")
        else None,
        "config": record.spec_json,
        "candidateCount": len(candidates),
        "revision": record.revision,
        "analysisStatus": analysis.get("status") if analysis else None,
    }
    if not detail:
        return response

    evaluations = list(
        session.scalars(
            select(EvaluationRecord)
            .where(EvaluationRecord.experiment_id == record.id)
            .order_by(EvaluationRecord.created_at.desc())
        )
    )
    evaluation_views: list[dict[str, Any]] = []
    all_attempt_ids = [item.id for item in attempts]
    artifacts = _artifact_views(session, all_attempt_ids)
    artifacts_by_attempt: dict[str, list[dict[str, Any]]] = {}
    for item in artifacts:
        artifacts_by_attempt.setdefault(item["attemptId"], []).append(item)
    for evaluation in evaluations:
        candidate = session.get(CandidateRecord, evaluation.candidate_id)
        evaluation_attempts = [item for item in attempts if item.evaluation_id == evaluation.id]
        latest = max(
            evaluation_attempts,
            key=lambda item: (item.repeat_index, item.retry_index, item.created_at),
            default=None,
        )
        candidate_analysis = analysis_map.get(candidate.id, {}) if candidate else {}
        target_analysis = selection_targets.get(evaluation.target_id, {}) if is_selection else {}
        objective_rows = (
            target_analysis.get("metrics", [])
            if is_selection
            else candidate_analysis.get("objectives", [])
        )
        status_map = {
            CandidateStatus.PENDING: "queued",
            CandidateStatus.RUNNING: "running",
            CandidateStatus.FEASIBLE: "completed",
            CandidateStatus.INFEASIBLE: "failed",
            CandidateStatus.INCONCLUSIVE: "failed",
            CandidateStatus.FAILED: "failed",
        }
        metrics = [
            {
                "name": item["metric"],
                "value": item["raw"],
                "unit": item["unit"],
                "baseline": None,
                "direction": "max" if item["direction"] == "maximize" else "min",
            }
            for item in objective_rows
            if item.get("raw") is not None
        ]
        duration = None
        if latest and latest.started_at and latest.completed_at:
            duration = (latest.completed_at - latest.started_at).total_seconds()
        evaluation_views.append(
            {
                "id": evaluation.id,
                "attemptId": latest.id if latest else None,
                "candidate": target_analysis.get("label")
                if is_selection
                else _candidate_label(candidate),
                "candidateId": candidate.id if candidate else None,
                "parameters": candidate.parameters_json if candidate else {},
                "status": status_map[CandidateStatus(evaluation.status)],
                "score": objective_rows[0].get("raw") if objective_rows else None,
                "duration": duration,
                "createdAt": _iso(evaluation.created_at),
                "metrics": metrics,
                "artifacts": [
                    artifact
                    for attempt in evaluation_attempts
                    for artifact in artifacts_by_attempt.get(attempt.id, [])
                ],
                "error": latest.error_message if latest else None,
                "workload": evaluation.workload_id,
                "targetId": evaluation.target_id,
                "paretoRank": candidate_analysis.get("pareto_rank") if not is_selection else None,
                "gateResults": target_analysis.get("gates", [])
                if is_selection
                else candidate_analysis.get("gates", []),
            }
        )
    response["evaluations"] = evaluation_views
    response["artifacts"] = artifacts
    response["analysis"] = analysis
    return response


def _candidate_label(candidate: CandidateRecord | None) -> str:
    if candidate is None:
        return "Unknown candidate"
    if candidate.role == "baseline":
        return "Baseline"
    if candidate.role == "scenario":
        return "Scenario workload"
    pairs = [f"{name}={value}" for name, value in candidate.parameters_json.items()]
    return " · ".join(pairs)


def analysis_view(result: dict[str, Any]) -> dict[str, Any]:
    candidates = {item["id"]: item for item in result.get("candidates", [])}
    pareto = []
    for point in result.get("pareto", []):
        candidate = candidates.get(point["candidate_id"], {})
        objectives = candidate.get("objectives", [])
        pareto.append(
            {
                "id": point["candidate_id"],
                "candidate": _analysis_candidate_label(candidate),
                "score": objectives[0].get("raw", 0) if objectives else 0,
                "cost": objectives[1].get("raw", 0) if len(objectives) > 1 else 0,
                "latency": next(
                    (item.get("raw") for item in objectives if "latency" in item["metric"]), None
                ),
                "rank": point.get("rank"),
                "feasible": point.get("feasible"),
                "objectives": point.get("objectives", {}),
            }
        )
    evidence_summary = result.get("evidence", {})
    evidence = [
        {
            "id": "evidence-summary",
            "title": "Immutable experiment evidence",
            "kind": "content-addressed",
            "summary": (
                f"{evidence_summary.get('attempt_count', 0)} attempts, "
                f"{evidence_summary.get('observation_count', 0)} observations, "
                f"{evidence_summary.get('artifact_count', 0)} artifacts"
            ),
            "createdAt": None,
            "artifacts": [
                {
                    "name": "完整证据包",
                    "url": f"/api/v1/experiments/{result['experiment_id']}/evidence",
                    "type": "application/zip",
                }
            ],
        }
    ]
    return {
        **result,
        "pareto": pareto,
        "evidenceSummary": evidence_summary,
        "evidence": evidence,
    }


def _analysis_candidate_label(candidate: dict[str, Any]) -> str:
    if candidate.get("role") == "baseline":
        return "Baseline"
    return " · ".join(f"{key}={value}" for key, value in candidate.get("parameters", {}).items())


def dashboard_view(session: Session) -> dict[str, Any]:
    experiments = list(
        session.scalars(select(ExperimentRecord).order_by(ExperimentRecord.created_at.desc()))
    )
    counts = Counter(record.status for record in experiments)
    active = [
        record
        for record in experiments
        if record.status
        in {ExperimentStatus.QUEUED, ExperimentStatus.RUNNING, ExperimentStatus.PAUSED}
    ]
    terminal = [
        record
        for record in experiments
        if record.status in {ExperimentStatus.COMPLETED, ExperimentStatus.FAILED}
    ]
    success_count = sum(1 for record in terminal if record.status == ExperimentStatus.COMPLETED)
    attempt_seconds = session.scalar(
        select(
            func.sum(
                func.julianday(AttemptRecord.completed_at)
                - func.julianday(AttemptRecord.started_at)
            )
            * 86400
        ).where(AttemptRecord.started_at.is_not(None), AttemptRecord.completed_at.is_not(None))
    )
    trend = []
    for record in reversed(experiments[:12]):
        view = experiment_view(session, record)
        if view.get("bestScore") is not None:
            trend.append(
                {
                    "time": view.get("updatedAt") or view.get("createdAt"),
                    "score": view["bestScore"],
                    "baseline": view.get("baselineScore"),
                }
            )
    return {
        "counts": dict(counts),
        "activeExperiments": [experiment_view(session, item) for item in active[:6]],
        "recentExperiments": [experiment_view(session, item) for item in experiments[:8]],
        "trend": trend,
        "successRate": success_count / len(terminal) if terminal else None,
        "totalExperiments": len(experiments),
        "computeHours": float(attempt_seconds or 0) / 3600,
    }
