from __future__ import annotations

from pathlib import Path

import pytest
from looper_api.models import Base
from looper_api.system_optimization import (
    CapacityTaskObservation,
    CapacityTaskStatus,
    SimulatedCapacityDriver,
    SimulatedStudyEvaluator,
    StudyEvaluationResult,
    SystemOptimizationError,
    SystemOptimizationStatus,
    apply_prepared_optimization_activation,
    approve_optimization_study,
    create_system_optimization_study,
    prepare_optimization_activation,
    reconcile_system_optimization_study,
    record_optimization_hypothesis,
    recover_interrupted_system_optimization_studies,
    request_optimization_approval,
    rollback_activated_optimization,
    system_optimization_view,
)
from looper_api.system_optimization_models import SystemOptimizationStudyRecord
from looper_api.system_optimization_worker import (
    ReconciliationResources,
    run_reconciliation_cycle,
)
from looper_core.canonical import canonical_digest
from looper_core.cas import FileSystemCAS
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.executor.simulated import SimulatedBackend, SimulatedFailurePlan
from looper_core.system_opt.hypothesis import (
    HYPOTHESIS_SCHEMA,
    HypothesisEvidence,
    HypothesisState,
    OptimizationHypothesis,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker


def _digest(seed: str) -> str:
    return canonical_digest({"seed": seed})


def _hypothesis() -> OptimizationHypothesis:
    return OptimizationHypothesis(
        schema_version=HYPOTHESIS_SCHEMA,
        hypothesis_id="code-driven.storage.test",
        statement="A scheduler change may move the measured capacity frontier.",
        state=HypothesisState.SUPPORTED_HYPOTHESIS,
        context_digest=_digest("capacity-context"),
        affected_components=["storage"],
        candidate_parameters={"system.storage-scheduler": "none"},
        evidence=[
            HypothesisEvidence(
                kind="runtime-profile",
                digest=_digest("runtime"),
                locator="evidence://runtime/profile",
                claim="Measured diagnostics route pressure to storage.",
            ),
            HypothesisEvidence(
                kind="source-code",
                digest=_digest("source"),
                locator="src/storage.py",
                line_start=10,
                line_end=10,
                claim="The workload commits storage requests synchronously.",
            ),
            HypothesisEvidence(
                kind="configuration-contract",
                digest=_digest("configuration"),
                locator="evidence://configuration/contract",
                claim="The target authorizes the scheduler value.",
            ),
        ],
    )


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _approved(
    session: Session, cas: FileSystemCAS
) -> SystemOptimizationStudyRecord:
    record = create_system_optimization_study(
        session,
        baseline_capacity_study_id="capacity-baseline",
        target_id="target-a",
        network="internal",
        minimum_effect=0.05,
        authorization_profile_digest=_digest("authorization"),
    )
    record_optimization_hypothesis(
        session, cas, record, _hypothesis(), expected_revision=record.revision
    )
    request_optimization_approval(record, expected_revision=record.revision)
    approve_optimization_study(
        record,
        hypothesis_digest=_hypothesis().digest,
        expected_revision=record.revision,
    )
    session.commit()
    return record


def _evaluator(outcome: str = "accepted") -> SimulatedStudyEvaluator:
    return SimulatedStudyEvaluator(
        StudyEvaluationResult(
            outcome=outcome,  # type: ignore[arg-type]
            decision={
                "schemaVersion": "looper.simulated-capacity-decision/v1alpha1",
                "simulated": True,
                "outcome": outcome,
                "status": outcome,
                "rollbackVerified": True,
                "measurementIdentity": {
                    "environment_digest": _digest("environment")
                },
            },
        )
    )


def test_persistent_simulated_loop_is_stepwise_idempotent_and_crash_recoverable(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    backend = SimulatedBackend(
        {"storage-scheduler": "mq-deadline"}, target_id="target-a"
    )
    driver = SimulatedCapacityDriver("capacity-candidate")
    evaluator = _evaluator()
    manifest = build_demo_manifest()

    with session_factory() as session:
        record = _approved(session, cas)
        first = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert first.external_action == "snapshot"
        assert first.status == SystemOptimizationStatus.APPLYING
        assert record.snapshot_digest is not None
        assert backend.state()["storage-scheduler"] == "mq-deadline"
        session.commit()
        study_id = record.id

    # Simulate a process crash after the external apply but before the DB state changed.
    item = manifest.item("storage-scheduler")
    external_apply = backend.apply(item, "none", fencing_token=1)
    assert external_apply.succeeded

    with session_factory() as session:
        record = session.get(SystemOptimizationStudyRecord, study_id)
        assert record is not None
        recovered_apply = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert recovered_apply.external_action == "verify-existing-apply"
        assert recovered_apply.status == SystemOptimizationStatus.MEASURING
        session.commit()

        # Simulate a crash after idempotent external capacity submission.
        driver.submit_candidate(
            baseline_capacity_study_id=record.baseline_capacity_study_id,
            target_id=record.target_id,
            network=record.network,
            hypothesis_digest=record.hypothesis_digest or "",
            idempotency_key=f"system-optimization:{record.id}",
        )
        submitted = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert submitted.external_action == "submit-capacity"
        assert len(driver.submissions) == 1
        session.commit()

        pending = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert pending.changed is False
        assert pending.status == SystemOptimizationStatus.MEASURING

        driver.set_observation(
            CapacityTaskObservation(
                capacity_study_id="capacity-candidate",
                status=CapacityTaskStatus.COMPLETED,
                report_digest=_digest("candidate-report"),
            )
        )
        terminal = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert terminal.status == SystemOptimizationStatus.ROLLING_BACK
        session.commit()

        rollback = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert rollback.external_action == "rollback-and-verify"
        assert rollback.status == SystemOptimizationStatus.EVALUATING
        assert backend.state()["storage-scheduler"] == "mq-deadline"
        assert record.rollback_verified is True
        session.commit()

        evaluated = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert evaluated.status == SystemOptimizationStatus.COMPLETED
        assert record.decision_digest is not None
        assert evaluator.calls == 1
        session.commit()

        complete = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert complete.changed is False
        view = system_optimization_view(session, record)
        assert {artifact["role"] for artifact in view["artifacts"]} == {
            "hypothesis",
            "snapshot",
            "decision",
        }


def test_apply_failure_still_rolls_back_and_completes_as_blocked(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    backend = SimulatedBackend(
        {"storage-scheduler": "mq-deadline"},
        target_id="target-a",
        failure_plan=SimulatedFailurePlan(apply_failures={"storage-scheduler"}),
    )
    driver = SimulatedCapacityDriver("capacity-candidate")
    evaluator = _evaluator()
    manifest = build_demo_manifest()

    with session_factory() as session:
        record = _approved(session, cas)
        reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        session.commit()
        failed = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert failed.status == SystemOptimizationStatus.ROLLING_BACK
        assert record.problem_json["code"] == "apply_or_verify_failed"
        session.commit()

        rolled_back = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert rolled_back.status == SystemOptimizationStatus.EVALUATING
        assert record.rollback_verified is True
        session.commit()

        reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert record.status == SystemOptimizationStatus.COMPLETED.value
        assert record.orchestration_json["evaluation"]["outcome"] == "blocked"
        assert evaluator.calls == 0


def test_rollback_failure_enters_needs_attention_and_blocks_writes(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    backend = SimulatedBackend(
        {"storage-scheduler": "mq-deadline"},
        target_id="target-a",
        failure_plan=SimulatedFailurePlan(rollback_failures={"storage-scheduler"}),
    )
    driver = SimulatedCapacityDriver("capacity-candidate")
    evaluator = _evaluator()
    manifest = build_demo_manifest()

    with session_factory() as session:
        record = _approved(session, cas)
        for _ in range(2):
            reconcile_system_optimization_study(
                session,
                cas,
                record,
                backend=backend,
                manifest=manifest,
                capacity_driver=driver,
                evaluator=evaluator,
            )
            session.commit()
        reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        session.commit()
        driver.set_observation(
            CapacityTaskObservation(
                capacity_study_id="capacity-candidate",
                status=CapacityTaskStatus.FAILED,
                error_code="capacity_failed",
            )
        )
        reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        session.commit()
        failed_rollback = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )

        assert failed_rollback.status == SystemOptimizationStatus.NEEDS_ATTENTION
        assert record.rollback_verified is False
        assert record.problem_json["code"] == "rollback_verification_failed"
        assert backend.state()["storage-scheduler"] == "none"
        no_more_writes = reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        assert no_more_writes.changed is False

        with pytest.raises(SystemOptimizationError) as blocked:
            request_optimization_approval(record, expected_revision=record.revision)
        assert blocked.value.problem.code == "writes_blocked_after_rollback_failure"


def test_recovery_increments_fencing_token_without_guessing_next_step(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    with session_factory() as session:
        record = _approved(session, cas)
        original_status = record.status
        original_token = record.fencing_token

        assert recover_interrupted_system_optimization_studies(session) == 1
        assert record.status == original_status
        assert record.fencing_token == original_token + 1

        record.status = SystemOptimizationStatus.COMPLETED.value
        session.commit()
        assert recover_interrupted_system_optimization_studies(session) == 0


def test_revision_and_hypothesis_digest_are_required_for_approval(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    with session_factory() as session:
        record = create_system_optimization_study(
            session,
            baseline_capacity_study_id="capacity-baseline",
            target_id="target-a",
            network="external",
            minimum_effect=0.1,
            authorization_profile_digest=_digest("authorization"),
        )
        record_optimization_hypothesis(
            session, cas, record, _hypothesis(), expected_revision=1
        )
        request_optimization_approval(record, expected_revision=2)

        with pytest.raises(SystemOptimizationError) as stale:
            approve_optimization_study(
                record,
                hypothesis_digest=_hypothesis().digest,
                expected_revision=2,
            )
        assert stale.value.problem.code == "revision_conflict"

        with pytest.raises(SystemOptimizationError) as wrong_digest:
            approve_optimization_study(
                record,
                hypothesis_digest=_digest("different-hypothesis"),
                expected_revision=record.revision,
            )
        assert wrong_digest.value.problem.code == "hypothesis_digest_mismatch"

        records = list(session.scalars(select(SystemOptimizationStudyRecord)))
        assert records == [record]


def test_independent_worker_commits_one_step_and_stops_with_structured_problem(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    backend = SimulatedBackend(
        {"storage-scheduler": "mq-deadline"}, target_id="target-a"
    )
    driver = SimulatedCapacityDriver("capacity-candidate")
    evaluator = _evaluator()
    manifest = build_demo_manifest()
    with session_factory() as session:
        record = _approved(session, cas)
        study_id = record.id

    report = run_reconciliation_cycle(
        session_factory,
        cas,
        lambda _record: ReconciliationResources(
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        ),
    )
    assert [result.external_action for result in report.reconciled] == ["snapshot"]
    with session_factory() as session:
        persisted = session.get(SystemOptimizationStudyRecord, study_id)
        assert persisted is not None
        assert persisted.status == SystemOptimizationStatus.APPLYING.value
        assert persisted.snapshot_digest is not None

    stopped = run_reconciliation_cycle(
        session_factory,
        cas,
        lambda _record: (_ for _ in ()).throw(RuntimeError("injected provider failure")),
    )
    assert stopped.stopped_study_id == study_id
    assert stopped.problem is not None
    assert stopped.problem.code == "reconciliation_unhandled_error"
    assert stopped.problem.evidence_summary == {"errorType": "RuntimeError"}
    with session_factory() as session:
        persisted = session.get(SystemOptimizationStudyRecord, study_id)
        assert persisted is not None
        assert persisted.status == SystemOptimizationStatus.NEEDS_ATTENTION.value


def _completed_accepted(
    session: Session,
    cas: FileSystemCAS,
    backend: SimulatedBackend,
) -> tuple[SystemOptimizationStudyRecord, SimulatedCapacityDriver]:
    record = _approved(session, cas)
    driver = SimulatedCapacityDriver("capacity-candidate")
    evaluator = _evaluator()
    manifest = build_demo_manifest()
    reconcile_system_optimization_study(
        session,
        cas,
        record,
        backend=backend,
        manifest=manifest,
        capacity_driver=driver,
        evaluator=evaluator,
    )
    session.commit()
    reconcile_system_optimization_study(
        session,
        cas,
        record,
        backend=backend,
        manifest=manifest,
        capacity_driver=driver,
        evaluator=evaluator,
    )
    session.commit()
    reconcile_system_optimization_study(
        session,
        cas,
        record,
        backend=backend,
        manifest=manifest,
        capacity_driver=driver,
        evaluator=evaluator,
    )
    session.commit()
    driver.set_observation(
        CapacityTaskObservation(
            capacity_study_id="capacity-candidate",
            status=CapacityTaskStatus.COMPLETED,
            report_digest=_digest("candidate-report"),
        )
    )
    for _ in range(3):
        reconcile_system_optimization_study(
            session,
            cas,
            record,
            backend=backend,
            manifest=manifest,
            capacity_driver=driver,
            evaluator=evaluator,
        )
        session.commit()
    assert record.status == SystemOptimizationStatus.COMPLETED.value
    assert record.rollback_verified is True
    return record, driver


def test_explicit_activation_is_fenced_committed_runtime_only_and_reversible(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    backend = SimulatedBackend(
        {"storage-scheduler": "mq-deadline"}, target_id="target-a"
    )
    manifest = build_demo_manifest()
    with session_factory() as session:
        record, _driver = _completed_accepted(session, cas, backend)
        assert record.decision_digest is not None
        initial_token = record.fencing_token

        # First call only persists a new fence. No remote read or write happens.
        prepare_optimization_activation(
            session,
            cas,
            record,
            decision_digest=record.decision_digest,
            expected_revision=record.revision,
            current_environment_digest=_digest("environment"),
            current_authorization_profile_digest=_digest("authorization"),
            backend=backend,
            manifest=manifest,
        )
        assert record.activation_json["phase"] == "fenced"
        assert record.fencing_token == initial_token + 1
        session.commit()

        prepare_optimization_activation(
            session,
            cas,
            record,
            decision_digest=record.decision_digest,
            expected_revision=record.revision,
            current_environment_digest=_digest("environment"),
            current_authorization_profile_digest=_digest("authorization"),
            backend=backend,
            manifest=manifest,
        )
        assert record.activation_json["phase"] == "snapshot-ready"
        assert backend.state()["storage-scheduler"] == "mq-deadline"
        session.commit()

        # Simulate apply succeeding immediately before a process crash.
        item = manifest.item("storage-scheduler")
        assert backend.apply(item, "none", fencing_token=record.fencing_token).succeeded
        apply_prepared_optimization_activation(
            session,
            cas,
            record,
            expected_revision=record.revision,
            current_environment_digest=_digest("environment"),
            current_authorization_profile_digest=_digest("authorization"),
            backend=backend,
            manifest=manifest,
        )
        assert record.activation_json["status"] == "active"
        assert record.activation_json["runtimeOnly"] is True
        assert record.activation_json["persistentConfigurationWritten"] is False
        assert backend.state()["storage-scheduler"] == "none"
        session.commit()

        rollback_activated_optimization(
            session,
            cas,
            record,
            expected_revision=record.revision,
            backend=backend,
            manifest=manifest,
        )
        assert record.activation_json["rollbackPhase"] == "fenced"
        session.commit()
        rollback_activated_optimization(
            session,
            cas,
            record,
            expected_revision=record.revision,
            backend=backend,
            manifest=manifest,
        )
        assert record.activation_json["status"] == "rolled-back"
        assert backend.state()["storage-scheduler"] == "mq-deadline"


def test_activation_stops_on_environment_authorization_or_configuration_drift(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    cas = FileSystemCAS(tmp_path / "cas")
    backend = SimulatedBackend(
        {"storage-scheduler": "mq-deadline"}, target_id="target-a"
    )
    manifest = build_demo_manifest()
    with session_factory() as session:
        record, _driver = _completed_accepted(session, cas, backend)
        assert record.decision_digest is not None
        with pytest.raises(SystemOptimizationError) as environment:
            prepare_optimization_activation(
                session,
                cas,
                record,
                decision_digest=record.decision_digest,
                expected_revision=record.revision,
                current_environment_digest=_digest("changed-environment"),
                current_authorization_profile_digest=_digest("authorization"),
                backend=backend,
                manifest=manifest,
            )
        assert environment.value.problem.code == "activation_environment_drift"

        with pytest.raises(SystemOptimizationError) as authorization:
            prepare_optimization_activation(
                session,
                cas,
                record,
                decision_digest=record.decision_digest,
                expected_revision=record.revision,
                current_environment_digest=_digest("environment"),
                current_authorization_profile_digest=_digest("changed-authorization"),
                backend=backend,
                manifest=manifest,
            )
        assert authorization.value.problem.code == "activation_authorization_drift"

        prepare_optimization_activation(
            session,
            cas,
            record,
            decision_digest=record.decision_digest,
            expected_revision=record.revision,
            current_environment_digest=_digest("environment"),
            current_authorization_profile_digest=_digest("authorization"),
            backend=backend,
            manifest=manifest,
        )
        session.commit()
        backend.inject_drift("storage-scheduler", "none")
        with pytest.raises(SystemOptimizationError) as configuration:
            prepare_optimization_activation(
                session,
                cas,
                record,
                decision_digest=record.decision_digest,
                expected_revision=record.revision,
                current_environment_digest=_digest("environment"),
                current_authorization_profile_digest=_digest("authorization"),
                backend=backend,
                manifest=manifest,
            )
        assert configuration.value.problem.code == "activation_configuration_drift"
