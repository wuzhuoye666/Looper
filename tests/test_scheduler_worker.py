from __future__ import annotations

from pathlib import Path

import pytest
from looper_api.config import Settings
from looper_api.models import AttemptRecord, BenchmarkRecord
from looper_api.scheduler import (
    SchedulerError,
    create_demo_request,
    create_experiment,
    start_experiment,
)
from looper_api.worker_protocol import (
    AttemptCompletion,
    AttemptHeartbeat,
    AttemptStart,
    WorkerRegister,
)
from looper_api.worker_service import (
    WorkerError,
    claim_attempt,
    complete_attempt,
    heartbeat_attempt,
    register_worker,
    start_attempt,
)
from looper_core.contracts import BenchmarkInputBinding, MetricObservation
from looper_core.state import AttemptStatus, ExperimentStatus


def test_demo_start_creates_baseline_and_candidate(db_session: object) -> None:
    session = db_session
    experiment = create_experiment(session, create_demo_request())
    start_experiment(session, experiment)
    session.flush()
    attempts = session.query(AttemptRecord).filter_by(experiment_id=experiment.id).all()
    assert experiment.status == ExperimentStatus.QUEUED
    assert len(attempts) == 6


def test_fencing_token_rejects_stale_worker(db_session: object, tmp_path: Path) -> None:
    session = db_session
    settings = Settings(
        data_dir=tmp_path,
        database_url="sqlite://",
        local_worker_token="secret",
        lease_seconds=30,
    )
    experiment = create_experiment(session, create_demo_request())
    start_experiment(session, experiment)
    worker = register_worker(
        session,
        settings,
        WorkerRegister(
            workerId="worker-1",
            name="test worker",
            token="secret",
            capabilities=["python", "local-process"],
            fingerprint={},
        ),
    )
    claim = claim_attempt(session, settings, worker)
    assert claim and claim["fencingToken"] == 1
    assert claim["benchmarkRelativeRoot"] == "benchmarks/demo"
    attempt = session.get(AttemptRecord, claim["attemptId"])
    assert attempt and attempt.status == AttemptStatus.LEASED
    attempt.fencing_token = 2
    with pytest.raises(WorkerError, match="stale"):
        heartbeat_attempt(
            session,
            settings,
            attempt.id,
            AttemptHeartbeat(workerId=worker.id, fencingToken=1),
        )


def test_worker_does_not_claim_above_its_concurrency(
    db_session: object, tmp_path: Path
) -> None:
    session = db_session
    settings = Settings(
        data_dir=tmp_path,
        database_url="sqlite://",
        local_worker_token="secret",
        lease_seconds=30,
    )
    experiment = create_experiment(session, create_demo_request())
    start_experiment(session, experiment)
    worker = register_worker(
        session,
        settings,
        WorkerRegister(
            workerId="worker-single-slot",
            name="single slot worker",
            token="secret",
            capabilities=["python", "local-process"],
            fingerprint={},
            maxConcurrency=1,
        ),
    )

    first_claim = claim_attempt(session, settings, worker)

    assert first_claim is not None
    assert claim_attempt(session, settings, worker) is None


def test_worker_reregistration_requeues_its_interrupted_attempt(
    db_session: object, tmp_path: Path
) -> None:
    session = db_session
    settings = Settings(
        data_dir=tmp_path,
        database_url="sqlite://",
        local_worker_token="secret",
        lease_seconds=30,
    )
    experiment = create_experiment(session, create_demo_request())
    start_experiment(session, experiment)
    registration = WorkerRegister(
        workerId="worker-restarted",
        name="restarted worker",
        token="secret",
        capabilities=["python", "local-process"],
        fingerprint={},
        maxConcurrency=1,
    )
    worker = register_worker(session, settings, registration)
    first_claim = claim_attempt(session, settings, worker)
    assert first_claim is not None
    start_attempt(
        session,
        first_claim["attemptId"],
        AttemptStart(
            workerId=worker.id,
            fencingToken=first_claim["fencingToken"],
            envelope=first_claim["envelope"],
        ),
    )

    worker = register_worker(session, settings, registration)
    recovered = session.get(AttemptRecord, first_claim["attemptId"])
    second_claim = claim_attempt(session, settings, worker)

    assert recovered is not None
    assert second_claim is not None
    assert second_claim["attemptId"] == first_claim["attemptId"]
    assert second_claim["fencingToken"] == first_claim["fencingToken"] + 1


def test_completion_rejects_required_metrics_without_enough_samples(
    db_session: object, tmp_path: Path
) -> None:
    session = db_session
    settings = Settings(
        data_dir=tmp_path,
        database_url="sqlite://",
        local_worker_token="secret",
        lease_seconds=30,
    )
    experiment = create_experiment(session, create_demo_request())
    start_experiment(session, experiment)
    worker = register_worker(
        session,
        settings,
        WorkerRegister(
            workerId="worker-samples",
            name="sample evidence worker",
            token="secret",
            capabilities=["python", "local-process"],
            fingerprint={},
        ),
    )
    claim = claim_attempt(session, settings, worker)
    assert claim is not None
    start_attempt(
        session,
        claim["attemptId"],
        AttemptStart(
            workerId=worker.id,
            fencingToken=claim["fencingToken"],
            envelope=claim["envelope"],
        ),
    )
    observations = [
        MetricObservation(
            schemaVersion="v1alpha1",
            metric="throughput_mib_s",
            value=100.0,
            unit="MiB/s",
            phase="measurement",
            statistic="sample",
        ),
        MetricObservation(
            schemaVersion="v1alpha1",
            metric="latency_ms",
            value=10.0,
            unit="ms",
            phase="measurement",
            statistic="sample",
        ),
        MetricObservation(
            schemaVersion="v1alpha1",
            metric="compression_ratio",
            value=0.5,
            unit="ratio",
            phase="measurement",
            statistic="mean",
        ),
        MetricObservation(
            schemaVersion="v1alpha1",
            metric="roundtrip_ok",
            value=True,
            unit="bool",
            phase="validation",
            statistic="boolean",
        ),
    ]
    attempt = complete_attempt(
        session,
        claim["attemptId"],
        AttemptCompletion(
            workerId=worker.id,
            fencingToken=claim["fencingToken"],
            idempotencyKey="complete-insufficient-samples",
            status="succeeded",
            observations=observations,
        ),
    )
    assert attempt.status == AttemptStatus.FAILED
    assert attempt.error_message is not None
    assert "insufficient metric samples" in attempt.error_message
    assert "'required': 3" in attempt.error_message
    assert "'observed': 1" in attempt.error_message


def test_worker_claim_requires_implicit_runtime_capability(
    db_session: object, tmp_path: Path
) -> None:
    session = db_session
    settings = Settings(
        data_dir=tmp_path,
        database_url="sqlite://",
        local_worker_token="secret",
        lease_seconds=30,
    )
    experiment = create_experiment(session, create_demo_request())
    start_experiment(session, experiment)
    benchmark = session.query(BenchmarkRecord).filter_by(
        benchmark_id="looper.demo.compression"
    ).one()
    benchmark.manifest_json["spec"]["runtime"]["type"] = "container"
    benchmark.manifest_json["spec"]["capabilities"] = ["python"]
    benchmark.manifest_json["spec"]["runtime"]["provisioning"]["hostCapabilities"] = [
        "python"
    ]
    local_worker = register_worker(
        session,
        settings,
        WorkerRegister(
            workerId="worker-local-only",
            name="local-only worker",
            token="secret",
            capabilities=["python", "local-process"],
            fingerprint={},
        ),
    )
    assert claim_attempt(session, settings, local_worker) is None

    container_worker = register_worker(
        session,
        settings,
        WorkerRegister(
            workerId="worker-container",
            name="container worker",
            token="secret",
            capabilities=["python", "container"],
            targetIds=["local"],
            fingerprint={},
        ),
    )
    assert claim_attempt(session, settings, container_worker) is not None


def test_worker_claim_respects_declared_target_affinity(
    db_session: object, tmp_path: Path
) -> None:
    session = db_session
    settings = Settings(
        data_dir=tmp_path,
        database_url="sqlite://",
        local_worker_token="secret",
        lease_seconds=30,
    )
    experiment = create_experiment(session, create_demo_request())
    start_experiment(session, experiment)
    wrong_worker = register_worker(
        session,
        settings,
        WorkerRegister(
            workerId="worker-wrong-target",
            name="wrong target worker",
            token="secret",
            capabilities=["python", "local-process"],
            targetIds=["other-target"],
            fingerprint={},
        ),
    )
    assert claim_attempt(session, settings, wrong_worker) is None
    matching_worker = register_worker(
        session,
        settings,
        WorkerRegister(
            workerId="worker-local-target",
            name="local target worker",
            token="secret",
            capabilities=["python", "local-process"],
            targetIds=["local"],
            fingerprint={},
        ),
    )
    assert claim_attempt(session, settings, matching_worker) is not None


def test_required_benchmark_inputs_are_bound_and_propagated(
    db_session: object, tmp_path: Path
) -> None:
    session = db_session
    benchmark = session.query(BenchmarkRecord).filter_by(
        benchmark_id="looper.demo.compression"
    ).one()
    benchmark.manifest_json["spec"]["adapter"] = {
        "protocol": "looper-adapter/v1",
        "executionModel": "batch-suite",
        "primaryMetric": "throughput_mib_s",
        "requiredChecks": ["roundtrip-ok"],
        "inputs": [
            {
                "id": "dataset",
                "kind": "dataset",
                "required": True,
                "digestRequired": True,
            }
        ],
        "canonicalOutputs": {"metrics": "metrics.jsonl", "result": "result.json"},
    }
    benchmark.manifest_json["spec"]["runtime"]["dependencyLockDigest"] = (
        "sha256:" + "e" * 64
    )
    request = create_demo_request()
    with pytest.raises(SchedulerError, match="required benchmark inputs"):
        create_experiment(session, request)

    request.spec.input_bindings = {
        "dataset": BenchmarkInputBinding(
            kind="dataset",
            reference="cas://dataset-fixture",
            digest="sha256:" + "d" * 64,
        )
    }
    experiment = create_experiment(session, request)
    assert experiment.spec_json["input_bindings"]["dataset"]["reference"] == "cas://dataset-fixture"
    start_experiment(session, experiment)
    settings = Settings(
        data_dir=tmp_path,
        database_url="sqlite://",
        local_worker_token="secret",
        lease_seconds=30,
    )
    worker = register_worker(
        session,
        settings,
        WorkerRegister(
            workerId="worker-inputs",
            name="input binding worker",
            token="secret",
            capabilities=["python", "local-process"],
            fingerprint={},
        ),
    )
    claim = claim_attempt(session, settings, worker)
    assert claim is not None
    assert claim["envelope"]["inputs"]["dataset"]["digest"] == "sha256:" + "d" * 64
    assert claim["envelope"]["benchmark"]["dependencyLockDigest"] == "sha256:" + "e" * 64
