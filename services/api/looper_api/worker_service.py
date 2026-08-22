from __future__ import annotations

import hashlib
import hmac
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO

from looper_core.canonical import canonical_digest, new_id, utc_now
from looper_core.cas import FileSystemCAS, StoredArtifact
from looper_core.contracts import ExperimentSpec
from looper_core.manifest import validate_document
from looper_core.state import AttemptStatus, CandidateStatus, ExperimentStatus
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.config import Settings
from looper_api.events import append_event
from looper_api.models import (
    ArtifactLinkRecord,
    ArtifactRecord,
    AttemptRecord,
    BenchmarkRecord,
    CandidateRecord,
    CheckRecord,
    EvaluationRecord,
    EventRecord,
    ExperimentRecord,
    ObservationRecord,
    SelectionLoadPointRecord,
    TargetRecord,
    WorkerRecord,
)
from looper_api.scheduler import (
    advance_experiment,
    mark_experiment_started,
    retry_attempt,
)
from looper_api.seed import get_benchmark
from looper_api.worker_protocol import (
    ArtifactMetadata,
    AttemptCompletion,
    AttemptHeartbeat,
    AttemptStart,
    WorkerRegister,
)


class WorkerError(ValueError):
    pass


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _authorized(settings: Settings, token: str) -> bool:
    return hmac.compare_digest(_token_hash(token), _token_hash(settings.local_worker_token))


def register_worker(session: Session, settings: Settings, request: WorkerRegister) -> WorkerRecord:
    if not _authorized(settings, request.token):
        raise WorkerError("worker token is invalid")
    now = utc_now()
    worker = session.get(WorkerRecord, request.worker_id)
    capabilities = set(request.capabilities)
    capabilities.update(f"target.{target_id}" for target_id in request.target_ids)
    values = {
        "name": request.name,
        "token_hash": _token_hash(request.token),
        "capabilities_json": sorted(capabilities),
        "fingerprint_json": request.fingerprint,
        "status": "online",
        "max_concurrency": request.max_concurrency,
        "last_heartbeat_at": now,
    }
    if worker is None:
        worker = WorkerRecord(
            id=request.worker_id,
            registered_at=now,
            **values,
        )
        session.add(worker)
    else:
        for field, value in values.items():
            setattr(worker, field, value)
    session.flush()
    for target_id in request.target_ids:
        target = session.get(TargetRecord, target_id)
        if target is None:
            continue
        merged_capabilities = sorted(set(target.capabilities_json) | set(request.capabilities))
        target.status = "available"
        target.capabilities_json = merged_capabilities
        target.fingerprint_json = {**target.fingerprint_json, **request.fingerprint}
        target.runnable = True
        target.lifecycle_status = "active"
        target.last_inventory_seen_at = now
        target.updated_at = now
        target.snapshot_digest = canonical_digest(
            {
                "provider": target.provider,
                "capabilities": merged_capabilities,
                "fingerprint": target.fingerprint_json,
            }
        )
    return worker


def authenticate_worker(
    session: Session, settings: Settings, worker_id: str, token: str
) -> WorkerRecord:
    worker = session.get(WorkerRecord, worker_id)
    if worker is None or not _authorized(settings, token):
        raise WorkerError("worker authentication failed")
    if not hmac.compare_digest(worker.token_hash, _token_hash(token)):
        raise WorkerError("worker authentication failed")
    worker.last_heartbeat_at = utc_now()
    worker.status = "online"
    return worker


def expire_stale_leases(session: Session) -> list[str]:
    now = utc_now()
    stale = list(
        session.scalars(
            select(AttemptRecord).where(
                AttemptRecord.status.in_(
                    [AttemptStatus.LEASED, AttemptStatus.RUNNING, AttemptStatus.UPLOADING]
                ),
                AttemptRecord.lease_expires_at < now,
            )
        )
    )
    experiment_ids: set[str] = set()
    for attempt in stale:
        attempt.status = AttemptStatus.LOST
        attempt.error_message = "worker lease expired"
        attempt.completed_at = now
        experiment_ids.add(attempt.experiment_id)
        append_event(
            session,
            experiment_id=attempt.experiment_id,
            event_type="attempt.lost",
            entity_type="attempt",
            entity_id=attempt.id,
            idempotency_key=f"attempt.lost:{attempt.id}:{attempt.fencing_token}",
            payload={"fencing_token": attempt.fencing_token},
        )
        with suppress(ValueError):
            retry_attempt(session, attempt)
    for experiment_id in experiment_ids:
        advance_experiment(session, experiment_id)
    return sorted(experiment_ids)


def _claim_envelope(
    session: Session,
    attempt: AttemptRecord,
    experiment: ExperimentRecord,
    evaluation: EvaluationRecord,
    candidate: CandidateRecord,
    benchmark: BenchmarkRecord,
) -> dict[str, Any]:
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    workload = next(
        item
        for item in benchmark.manifest_json["spec"]["workloads"]
        if item["id"] == evaluation.workload_id
    )
    source = benchmark.manifest_json["metadata"].get("source", {})
    runtime = benchmark.manifest_json["spec"]["runtime"]
    load_point = (
        session.get(SelectionLoadPointRecord, attempt.selection_load_point_id)
        if attempt.selection_load_point_id is not None
        else None
    )
    target_binding = (
        next(
            binding
            for binding in spec.selection.target_bindings
            if binding.target_id == evaluation.target_id
        )
        if spec.selection is not None
        else None
    )
    return {
        "schemaVersion": "v1alpha1",
        "experimentId": experiment.id,
        "candidateId": candidate.id,
        "evaluationId": evaluation.id,
        "attemptId": attempt.id,
        "leaseToken": attempt.fencing_token,
        "benchmark": {
            "id": benchmark.benchmark_id,
            "version": benchmark.version,
            "manifestDigest": benchmark.manifest_digest,
            "sourceCommit": source.get("commit"),
            "sourceDigest": source.get("digest"),
            "dependencyLockDigest": runtime.get("dependencyLockDigest"),
        },
        "candidate": {
            "digest": candidate.config_digest,
            "parameters": candidate.parameters_json,
            "role": candidate.role,
        },
        "workload": {"id": workload["id"], "metadata": workload.get("metadata", {})},
        "target": evaluation.target_snapshot_json,
        "inputs": {
            input_id: binding.model_dump(mode="json")
            for input_id, binding in spec.input_bindings.items()
        },
        "paths": {"input": "", "output": "", "workspace": ""},
        "seed": spec.design.random_seed
        + candidate.sequence * 1009
        + (load_point.sequence * 100_003 if load_point is not None else 0)
        + attempt.repeat_index * 37
        + attempt.retry_index,
        "cacheMode": spec.design.cache_mode,
        "createdAt": utc_now().isoformat(),
        "executionPolicy": runtime.get("executionPolicy"),
        "extensions": {
            "repeatIndex": attempt.repeat_index,
            "retryIndex": attempt.retry_index,
            "queueSequence": attempt.queue_sequence,
            "warmupRuns": spec.design.warmup_runs,
            **(
                {
                    "scenario": spec.scenario.model_dump(mode="json"),
                    "selection": spec.selection.model_dump(mode="json"),
                    "timeBlockId": ":".join(
                        [
                            experiment.id,
                            evaluation.workload_id,
                            load_point.offered_load_key if load_point is not None else "fixed",
                            str(attempt.repeat_index),
                            target_binding.placement_pair_id
                            if target_binding is not None
                            else "placement",
                        ]
                    ),
                    "targetBinding": target_binding.model_dump(mode="json")
                    if target_binding is not None
                    else None,
                    **(
                        {
                            "selectionLoadPointId": load_point.id,
                            "offeredLoad": float(load_point.offered_load),
                            "offeredLoadMetric": spec.scenario.load_search.offered_load_metric,
                            "offeredLoadUnit": spec.scenario.load_search.unit,
                        }
                        if load_point is not None and spec.scenario.load_search is not None
                        else {}
                    ),
                }
                if spec.scenario is not None and spec.selection is not None
                else {}
            ),
        },
    }


def claim_attempt(
    session: Session, settings: Settings, worker: WorkerRecord
) -> dict[str, Any] | None:
    expire_stale_leases(session)
    queued = list(
        session.scalars(
            select(AttemptRecord)
            .join(ExperimentRecord, ExperimentRecord.id == AttemptRecord.experiment_id)
            .where(
                AttemptRecord.status == AttemptStatus.QUEUED,
                ExperimentRecord.status.in_([ExperimentStatus.QUEUED, ExperimentStatus.RUNNING]),
            )
            .order_by(
                ExperimentRecord.created_at,
                AttemptRecord.queue_sequence,
                AttemptRecord.created_at,
                AttemptRecord.id,
            )
            .limit(100)
        )
    )
    worker_capabilities = set(worker.capabilities_json)
    worker_targets = {
        capability.removeprefix("target.")
        for capability in worker_capabilities
        if capability.startswith("target.")
    }
    selected: (
        tuple[AttemptRecord, ExperimentRecord, EvaluationRecord, CandidateRecord, BenchmarkRecord]
        | None
    ) = None
    for attempt in queued:
        experiment = session.get(ExperimentRecord, attempt.experiment_id)
        evaluation = session.get(EvaluationRecord, attempt.evaluation_id)
        if experiment is None or evaluation is None:
            continue
        candidate = session.get(CandidateRecord, evaluation.candidate_id)
        spec = ExperimentSpec.model_validate(experiment.spec_json)
        benchmark = get_benchmark(session, spec.benchmark_id, spec.benchmark_version)
        if candidate is None or benchmark is None:
            continue
        if worker_targets and evaluation.target_id not in worker_targets:
            continue
        required = set(benchmark.manifest_json["spec"].get("capabilities", []))
        runtime = benchmark.manifest_json["spec"]["runtime"]
        required.add(str(runtime["type"]))
        policy = runtime.get("executionPolicy") or {}
        if policy:
            required.update(
                {
                    f"placement.{policy['placement']['mode']}",
                    f"network.{policy['network']['mode']}",
                    f"storage.{policy['storage']['mode']}",
                    f"evidence.{policy['environmentEvidence']['profile']}",
                }
            )
        if required <= worker_capabilities:
            selected = attempt, experiment, evaluation, candidate, benchmark
            break
    if selected is None:
        return None

    attempt, experiment, evaluation, candidate, benchmark = selected
    now = utc_now()
    attempt.status = AttemptStatus.LEASED
    attempt.fencing_token += 1
    attempt.worker_id = worker.id
    attempt.leased_at = now
    attempt.lease_expires_at = now + timedelta(seconds=settings.lease_seconds)
    candidate.status = CandidateStatus.RUNNING
    evaluation.status = CandidateStatus.RUNNING
    mark_experiment_started(session, experiment)
    envelope = _claim_envelope(session, attempt, experiment, evaluation, candidate, benchmark)
    append_event(
        session,
        experiment_id=experiment.id,
        event_type="attempt.leased",
        entity_type="attempt",
        entity_id=attempt.id,
        idempotency_key=f"attempt.leased:{attempt.id}:{attempt.fencing_token}",
        payload={"worker_id": worker.id, "fencing_token": attempt.fencing_token},
    )
    session.flush()
    return {
        "attemptId": attempt.id,
        "fencingToken": attempt.fencing_token,
        "leaseSeconds": settings.lease_seconds,
        "maxOutputBytes": settings.max_output_bytes,
        "envelope": envelope,
        "manifest": benchmark.manifest_json,
        "benchmarkRoot": str(Path(benchmark.manifest_path or ".").resolve().parent),
        "benchmarkRelativeRoot": (
            str(Path("benchmarks") / Path(benchmark.manifest_path).resolve().parent.name)
            if benchmark.manifest_path
            else None
        ),
    }


def _active_attempt(
    session: Session,
    attempt_id: str,
    worker_id: str,
    fencing_token: int,
    allowed: set[AttemptStatus],
) -> AttemptRecord:
    attempt = session.get(AttemptRecord, attempt_id)
    if attempt is None:
        raise WorkerError("attempt does not exist")
    if attempt.worker_id != worker_id or attempt.fencing_token != fencing_token:
        raise WorkerError("stale or foreign lease token")
    if AttemptStatus(attempt.status) not in allowed:
        raise WorkerError(f"attempt is not active: {attempt.status}")
    return attempt


def heartbeat_attempt(
    session: Session,
    settings: Settings,
    attempt_id: str,
    request: AttemptHeartbeat,
) -> dict[str, Any]:
    attempt = _active_attempt(
        session,
        attempt_id,
        request.worker_id,
        request.fencing_token,
        {AttemptStatus.LEASED, AttemptStatus.RUNNING, AttemptStatus.UPLOADING},
    )
    attempt.lease_expires_at = utc_now() + timedelta(seconds=settings.lease_seconds)
    experiment = session.get(ExperimentRecord, attempt.experiment_id)
    return {
        "leaseExpiresAt": attempt.lease_expires_at.isoformat(),
        "cancelRequested": bool(
            experiment and ExperimentStatus(experiment.status) == ExperimentStatus.CANCELLED
        ),
    }


def start_attempt(session: Session, attempt_id: str, request: AttemptStart) -> AttemptRecord:
    attempt = _active_attempt(
        session,
        attempt_id,
        request.worker_id,
        request.fencing_token,
        {AttemptStatus.LEASED},
    )
    validate_document(request.envelope, "run-envelope.schema.json")
    if request.envelope.get("attemptId") != attempt.id:
        raise WorkerError("envelope attempt id does not match the lease")
    if request.envelope.get("leaseToken") != attempt.fencing_token:
        raise WorkerError("envelope fencing token does not match the lease")
    attempt.envelope_json = request.envelope
    attempt.envelope_digest = canonical_digest(request.envelope)
    attempt.status = AttemptStatus.RUNNING
    attempt.started_at = utc_now()
    append_event(
        session,
        experiment_id=attempt.experiment_id,
        event_type="attempt.started",
        entity_type="attempt",
        entity_id=attempt.id,
        idempotency_key=f"attempt.started:{attempt.id}:{attempt.fencing_token}",
        payload={"envelope_digest": attempt.envelope_digest},
    )
    return attempt


def store_artifact(
    session: Session,
    cas: FileSystemCAS,
    attempt_id: str,
    request: ArtifactMetadata,
    stream: BinaryIO,
) -> tuple[ArtifactLinkRecord, StoredArtifact]:
    attempt = _active_attempt(
        session,
        attempt_id,
        request.worker_id,
        request.fencing_token,
        {AttemptStatus.RUNNING, AttemptStatus.UPLOADING},
    )
    name = Path(request.name).name
    if name != request.name or name in {"", ".", ".."}:
        raise WorkerError("artifact name must be a plain filename")
    stored = cas.put_stream(stream)
    artifact = session.get(ArtifactRecord, stored.digest)
    if artifact is None:
        artifact = ArtifactRecord(
            digest=stored.digest,
            size=stored.size,
            verified=True,
            created_at=utc_now(),
        )
        session.add(artifact)
        session.flush()
    existing = session.scalar(
        select(ArtifactLinkRecord).where(
            ArtifactLinkRecord.attempt_id == attempt.id,
            ArtifactLinkRecord.digest == stored.digest,
            ArtifactLinkRecord.role == request.role,
            ArtifactLinkRecord.name == name,
        )
    )
    if existing:
        return existing, stored
    link = ArtifactLinkRecord(
        id=new_id("alink"),
        attempt_id=attempt.id,
        digest=stored.digest,
        role=request.role,
        name=name,
        media_type=request.media_type,
        producer=request.producer,
        created_at=utc_now(),
    )
    session.add(link)
    append_event(
        session,
        experiment_id=attempt.experiment_id,
        event_type="artifact.committed",
        entity_type="artifact",
        entity_id=stored.digest,
        idempotency_key=f"artifact.committed:{attempt.id}:{request.role}:{name}:{stored.digest}",
        payload={"attempt_id": attempt.id, "size": stored.size, "role": request.role},
    )
    return link, stored


def complete_attempt(
    session: Session,
    attempt_id: str,
    request: AttemptCompletion,
) -> AttemptRecord:
    existing_event = session.scalar(
        select(EventRecord).where(EventRecord.idempotency_key == request.idempotency_key)
    )
    if existing_event:
        existing_attempt = session.get(AttemptRecord, attempt_id)
        if existing_attempt is None:
            raise WorkerError("attempt does not exist")
        return existing_attempt
    attempt = _active_attempt(
        session,
        attempt_id,
        request.worker_id,
        request.fencing_token,
        {AttemptStatus.RUNNING, AttemptStatus.UPLOADING},
    )
    experiment = session.get(ExperimentRecord, attempt.experiment_id)
    evaluation = session.get(EvaluationRecord, attempt.evaluation_id)
    if experiment is None or evaluation is None:
        raise WorkerError("attempt parents are missing")
    spec = ExperimentSpec.model_validate(experiment.spec_json)
    benchmark = get_benchmark(session, spec.benchmark_id, spec.benchmark_version)
    if benchmark is None:
        raise WorkerError("benchmark is missing")
    metric_specs = benchmark.manifest_json["spec"]["metrics"]

    attempt.status = AttemptStatus.UPLOADING
    units_seen: dict[str, str] = {}
    sample_evidence: dict[str, int] = {}
    for item in request.observations:
        declaration = metric_specs.get(item.metric)
        if declaration is None:
            raise WorkerError(f"metric {item.metric!r} is not declared")
        if declaration["unit"] != item.unit:
            raise WorkerError(
                f"metric {item.metric!r} has unit {item.unit!r}, expected {declaration['unit']!r}"
            )
        units_seen[item.metric] = item.unit
        if item.sample_count is not None:
            sample_evidence[item.metric] = max(
                sample_evidence.get(item.metric, 0), item.sample_count
            )
        else:
            sample_evidence[item.metric] = sample_evidence.get(item.metric, 0) + 1
        value_number = None if isinstance(item.value, bool) else float(item.value)
        value_boolean = item.value if isinstance(item.value, bool) else None
        session.add(
            ObservationRecord(
                id=new_id("obs"),
                attempt_id=attempt.id,
                metric=item.metric,
                value_number=value_number,
                value_boolean=value_boolean,
                unit=item.unit,
                phase=item.phase,
                workload=item.workload,
                sample_index=item.sample_index,
                sample_count=item.sample_count,
                statistic=item.statistic,
                timestamp_text=item.timestamp,
                attributes_json=item.attributes,
                created_at=utc_now(),
            )
        )
    for item in request.checks:
        session.add(
            CheckRecord(
                id=new_id("check"),
                attempt_id=attempt.id,
                check_id=item.id,
                passed=item.passed,
                scope=item.scope,
                kind=item.kind,
                message=item.message,
                details_json=item.details,
                created_at=utc_now(),
            )
        )

    required_metrics = {
        name for name, declaration in metric_specs.items() if declaration.get("required", False)
    }
    missing_metrics = sorted(required_metrics - set(units_seen))
    insufficient_metrics = {
        name: {
            "required": int(metric_specs[name].get("minimumSamples", 1)),
            "observed": sample_evidence.get(name, 0),
        }
        for name in sorted(required_metrics & set(units_seen))
        if sample_evidence.get(name, 0) < int(metric_specs[name].get("minimumSamples", 1))
    }
    links = list(
        session.scalars(
            select(ArtifactLinkRecord).where(ArtifactLinkRecord.attempt_id == attempt.id)
        )
    )
    linked_names = {item.name for item in links}
    required_artifacts = {
        item["path"]
        for item in benchmark.manifest_json["spec"]["outputs"]["artifacts"]
        if item["required"]
    }
    missing_artifacts = sorted(required_artifacts - linked_names)
    final_status = AttemptStatus(request.status)
    if final_status == AttemptStatus.SUCCEEDED and (
        missing_metrics or insufficient_metrics or missing_artifacts
    ):
        final_status = AttemptStatus.FAILED
        details = []
        if missing_metrics:
            details.append(f"missing metrics: {missing_metrics}")
        if insufficient_metrics:
            details.append(f"insufficient metric samples: {insufficient_metrics}")
        if missing_artifacts:
            details.append(f"missing artifacts: {missing_artifacts}")
        request.error_message = "; ".join(details)
    attempt.status = final_status
    attempt.exit_code = request.exit_code
    attempt.error_message = request.error_message
    attempt.completed_at = utc_now()
    attempt.lease_expires_at = None
    append_event(
        session,
        experiment_id=attempt.experiment_id,
        event_type=f"attempt.{final_status.value}",
        entity_type="attempt",
        entity_id=attempt.id,
        idempotency_key=request.idempotency_key,
        payload={
            "fencing_token": attempt.fencing_token,
            "exit_code": request.exit_code,
            "observation_count": len(request.observations),
            "missing_metrics": missing_metrics,
            "insufficient_metrics": insufficient_metrics,
            "missing_artifacts": missing_artifacts,
        },
    )
    session.flush()
    advance_experiment(session, attempt.experiment_id)
    return attempt
