from __future__ import annotations

from datetime import UTC, datetime

import pytest
from looper_core.system_opt.executor import ConfigSnapshot, OperationStatus, SnapshotEntry
from looper_core.system_opt.executor.simulated import SimulatedBackend, SimulatedFailurePlan
from looper_core.system_opt.result_vector import GeneralResultVector, PromotionEvidence
from looper_core.system_opt.rollback import RollbackStatus
from looper_core.system_opt.rollback.regression import (
    LastGoodCheckpoint,
    RegressionRecoveryOutcome,
    RegressionRecoveryRequest,
    RegressionRecoveryStatus,
    execute_regression_recovery,
)
from looper_core.system_opt.safety import SafetyController, SafetyPolicy
from system_opt_support import integer_item, manifest

AT = datetime(2026, 8, 24, 13, 0, tzinfo=UTC)
NORM = "sha256:" + "a" * 64
TRIGGER_EVIDENCE = "sha256:" + "b" * 64
CANDIDATE = "validated-profile-a"
TARGET = "regression-target"


def _vector(*, u_regression: float, candidate_id: str = CANDIDATE, norm: str = NORM):
    return GeneralResultVector(
        candidate_id=candidate_id,
        u_cpu=0.5,
        u_memory=0.5,
        u_storage=0.5,
        u_network=0.5,
        u_stability=0.5,
        u_regression=u_regression,
        normalization_digest=norm,
    )


def _promotion(*, promoted: bool = True, candidate_id: str = CANDIDATE):
    return PromotionEvidence(
        candidate_id=candidate_id,
        promoted=promoted,
        reason="S9 fixture decision",
        observation_count=3,
        distinct_time_blocks=2,
        distinct_environments=1,
        failed_observations=[],
    )


def _checkpoint(backend: SimulatedBackend, item) -> LastGoodCheckpoint:
    snapshot = backend.snapshot([item], fencing_token=1)
    return LastGoodCheckpoint(
        target_id=TARGET,
        candidate_id=CANDIDATE,
        snapshot=snapshot,
        promotion_evidence=_promotion(),
        validated_vector=_vector(u_regression=0.8),
        recorded_at=AT,
    )


def _request(
    checkpoint: LastGoodCheckpoint,
    *,
    u_regression: float = 0.1,
    threshold: float = 0.3,
    candidate_id: str = CANDIDATE,
    norm: str = NORM,
) -> RegressionRecoveryRequest:
    return RegressionRecoveryRequest(
        checkpoint=checkpoint,
        current_vector=_vector(
            u_regression=u_regression,
            candidate_id=candidate_id,
            norm=norm,
        ),
        regression_threshold=threshold,
        trigger_evidence_digests=[TRIGGER_EVIDENCE],
        evaluated_at=AT,
    )


def _controller() -> SafetyController:
    return SafetyController(SafetyPolicy(allow_keep=True))


def test_checkpoint_requires_complete_snapshot_and_promoted_s9_evidence() -> None:
    item = integer_item()
    complete = ConfigSnapshot(
        target_id=TARGET,
        entries={
            item.id: SnapshotEntry(
                item_id=item.id,
                target=item.target,
                status=OperationStatus.SUCCEEDED,
                value=60,
            )
        },
    )
    incomplete = complete.model_copy(
        update={
            "entries": {
                item.id: complete.entries[item.id].model_copy(
                    update={"status": OperationStatus.FAILED}
                )
            }
        }
    )

    with pytest.raises(ValueError, match="complete and non-empty"):
        LastGoodCheckpoint(
            target_id=TARGET,
            candidate_id=CANDIDATE,
            snapshot=incomplete,
            promotion_evidence=_promotion(),
            validated_vector=_vector(u_regression=0.8),
            recorded_at=AT,
        )
    with pytest.raises(ValueError, match="promoted S9"):
        LastGoodCheckpoint(
            target_id=TARGET,
            candidate_id=CANDIDATE,
            snapshot=complete,
            promotion_evidence=_promotion(promoted=False),
            validated_vector=_vector(u_regression=0.8),
            recorded_at=AT,
        )


def test_request_rejects_candidate_or_normalization_drift() -> None:
    item = integer_item()
    backend = SimulatedBackend({item.id: 60}, target_id=TARGET)
    checkpoint = _checkpoint(backend, item)

    with pytest.raises(ValueError, match="last-good candidate"):
        _request(checkpoint, candidate_id="other-profile")
    with pytest.raises(ValueError, match="different normalization"):
        _request(checkpoint, norm="sha256:" + "c" * 64)


def test_non_triggered_evaluation_performs_no_write_and_allows_continuation() -> None:
    item = integer_item()
    config = manifest(item)
    backend = SimulatedBackend({item.id: 60}, target_id=TARGET)
    checkpoint = _checkpoint(backend, item)
    backend.inject_drift(item.id, 10)

    outcome = execute_regression_recovery(
        _request(checkpoint, u_regression=0.4, threshold=0.3),
        manifest=config,
        controller=_controller(),
        backend=backend,
        fencing_token=2,
        recorded_at=AT,
    )

    assert outcome.status is RegressionRecoveryStatus.NOT_TRIGGERED
    assert not outcome.stop_required
    assert outcome.rollback_record is None
    assert backend.state()[item.id] == 10
    assert backend.operations == []


def test_triggered_regression_restores_exact_s9_checkpoint_and_stops() -> None:
    item = integer_item()
    config = manifest(item)
    backend = SimulatedBackend({item.id: 60}, target_id=TARGET)
    checkpoint = _checkpoint(backend, item)
    backend.inject_drift(item.id, 10)

    outcome = execute_regression_recovery(
        _request(checkpoint),
        manifest=config,
        controller=_controller(),
        backend=backend,
        fencing_token=2,
        recorded_at=AT,
    )

    assert outcome.status is RegressionRecoveryStatus.RESTORED
    assert outcome.stop_required
    assert backend.state()[item.id] == 60
    assert outcome.rollback_record is not None
    assert outcome.rollback_record.status is RollbackStatus.COMPLETED
    assert outcome.rollback_record.verified
    assert (
        outcome.rollback_record.final_snapshot_digest
        == outcome.rollback_record.baseline_snapshot_digest
        == checkpoint.snapshot.digest
    )
    assert outcome.rollback_record.checkpoint_digest == checkpoint.digest
    assert outcome.rollback_record.regression_vector_digest == _request(
        checkpoint
    ).current_vector.digest
    assert RegressionRecoveryOutcome.model_validate_json(outcome.model_dump_json()).digest == (
        outcome.digest
    )


def test_l1_verify_failure_is_needs_attention_and_does_not_claim_restoration() -> None:
    item = integer_item()
    config = manifest(item)
    backend = SimulatedBackend(
        {item.id: 60},
        target_id=TARGET,
        failure_plan=SimulatedFailurePlan(verify_failures={item.id}),
    )
    checkpoint = _checkpoint(backend, item)
    backend.inject_drift(item.id, 10)

    outcome = execute_regression_recovery(
        _request(checkpoint),
        manifest=config,
        controller=_controller(),
        backend=backend,
        fencing_token=2,
        recorded_at=AT,
    )

    assert outcome.status is RegressionRecoveryStatus.NEEDS_ATTENTION
    assert outcome.stop_required
    assert backend.state()[item.id] == 10
    assert outcome.rollback_record is not None
    assert outcome.rollback_record.status is RollbackStatus.NEEDS_ATTENTION
    assert not outcome.rollback_record.verified
    assert outcome.rollback_record.final_snapshot_digest != checkpoint.snapshot.digest


def test_binding_error_is_evidence_backed_needs_attention_without_backend_writes() -> None:
    item = integer_item()
    config = manifest(item)
    checkpoint_backend = SimulatedBackend({item.id: 60}, target_id=TARGET)
    checkpoint = _checkpoint(checkpoint_backend, item)
    other_backend = SimulatedBackend({item.id: 10}, target_id="other-target")

    outcome = execute_regression_recovery(
        _request(checkpoint),
        manifest=config,
        controller=_controller(),
        backend=other_backend,
        fencing_token=2,
        recorded_at=AT,
    )

    assert outcome.status is RegressionRecoveryStatus.NEEDS_ATTENTION
    assert outcome.execution_evidence is not None
    assert outcome.execution_evidence.execution_error_type == "ValueError"
    assert "executor target" in str(outcome.execution_evidence.execution_error_message)
    assert other_backend.operations == []


def test_snapshot_item_key_tampering_is_rejected_before_checkpoint_creation() -> None:
    item = integer_item()
    snapshot = ConfigSnapshot(
        target_id=TARGET,
        entries={
            "wrong-key": SnapshotEntry(
                item_id=item.id,
                target=item.target,
                status=OperationStatus.SUCCEEDED,
                value=60,
            )
        },
    )

    with pytest.raises(ValueError, match="keys must match"):
        LastGoodCheckpoint(
            target_id=TARGET,
            candidate_id=CANDIDATE,
            snapshot=snapshot,
            promotion_evidence=_promotion(),
            validated_vector=_vector(u_regression=0.8),
            recorded_at=AT,
        )
