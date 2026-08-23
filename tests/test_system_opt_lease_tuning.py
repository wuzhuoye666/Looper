from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from looper_core.system_opt.demo import (
    SyntheticMeasurementAdapter,
    build_demo_manifest,
    build_demo_policy,
    resolve_demo_domains,
    run_full_demo,
)
from looper_core.system_opt.executor import ConfigSnapshot, OperationStatus, SnapshotEntry
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.lease import (
    FileTargetGuard,
    LeaseConflict,
    ReconciliationOutcome,
    TargetReconciliation,
    TargetRecoveryEvidence,
)
from looper_core.system_opt.policy import OptimizationMode
from looper_core.system_opt.safety import SafetyState
from looper_core.system_opt.tuning import StopReason, SystemOptimizationEngine


def _snapshot(target_id: str = "target-1", value: int = 10) -> ConfigSnapshot:
    return ConfigSnapshot(
        target_id=target_id,
        entries={
            "vm-swappiness": SnapshotEntry(
                item_id="vm-swappiness",
                target="vm.swappiness",
                status=OperationStatus.SUCCEEDED,
                value=value,
            )
        },
    )


def test_target_lease_conflict_and_expired_takeover_require_reconciliation(
    tmp_path: Path,
) -> None:
    guard = FileTargetGuard(tmp_path / "leases")
    now = datetime(2026, 8, 23, tzinfo=UTC)
    first = guard.acquire(
        "target-1",
        "owner-a",
        ttl_seconds=10,
        now=now,
        reconciliation=None,
    )

    with pytest.raises(LeaseConflict, match="leased"):
        guard.acquire(
            "target-1",
            "owner-b",
            ttl_seconds=10,
            now=now + timedelta(seconds=1),
            reconciliation=None,
        )
    with pytest.raises(LeaseConflict, match="reconciliation"):
        guard.acquire(
            "target-1",
            "owner-b",
            ttl_seconds=10,
            now=now + timedelta(seconds=11),
            reconciliation=None,
        )

    snapshot = _snapshot()
    reconciliation = TargetReconciliation(
        target_id="target-1",
        previous_lease_digest=first.digest,
        actual_snapshot=snapshot,
        expected_snapshot=snapshot,
        outcome=ReconciliationOutcome.MATCHED_SNAPSHOT,
        reason="actual target snapshot matches the recorded rollback snapshot",
        recorded_at=now + timedelta(seconds=11),
    )
    second = guard.acquire(
        "target-1",
        "owner-b",
        ttl_seconds=10,
        now=now + timedelta(seconds=11),
        reconciliation=reconciliation,
    )
    assert second.fencing_token == first.fencing_token + 1
    guard.release(second)


def test_needs_attention_blocks_future_writers(tmp_path: Path) -> None:
    guard = FileTargetGuard(tmp_path / "leases")
    guard.mark_needs_attention(
        "target-1",
        reason="rollback verification failed",
        evidence_digest="sha256:" + "b" * 64,
        now=datetime(2026, 8, 23, tzinfo=UTC),
    )

    with pytest.raises(LeaseConflict, match="needs attention"):
        guard.acquire(
            "target-1",
            "owner-a",
            ttl_seconds=10,
            now=datetime(2026, 8, 23, tzinfo=UTC),
            reconciliation=None,
        )


def test_attention_clear_requires_bound_matching_recovery_evidence(tmp_path: Path) -> None:
    guard = FileTargetGuard(tmp_path / "leases")
    attention = guard.mark_needs_attention(
        "target-1",
        reason="rollback verification failed",
        evidence_digest="sha256:" + "b" * 64,
        now=datetime(2026, 8, 23, tzinfo=UTC),
    )
    snapshot = _snapshot()
    recovery = TargetRecoveryEvidence(
        target_id="target-1",
        attention_evidence_digest=attention.evidence_digest,
        actual_snapshot=snapshot,
        approved_snapshot=snapshot,
        reason="operator approved the verified current state",
        recorded_at=datetime(2026, 8, 23, tzinfo=UTC),
    )

    guard.clear_attention("target-1", recovery=recovery)

    assert guard.current_attention("target-1") is None


def test_expired_takeover_rejects_unbound_reconciliation(tmp_path: Path) -> None:
    guard = FileTargetGuard(tmp_path / "leases")
    now = datetime(2026, 8, 23, tzinfo=UTC)
    guard.acquire(
        "target-1",
        "owner-a",
        ttl_seconds=1,
        now=now,
        reconciliation=None,
    )
    snapshot = _snapshot()
    unbound = TargetReconciliation(
        target_id="target-1",
        previous_lease_digest="sha256:" + "e" * 64,
        actual_snapshot=snapshot,
        expected_snapshot=snapshot,
        outcome=ReconciliationOutcome.MATCHED_SNAPSHOT,
        reason="syntactically valid but not bound to the expired lease",
        recorded_at=now + timedelta(seconds=2),
    )

    with pytest.raises(LeaseConflict, match="does not bind"):
        guard.acquire(
            "target-1",
            "owner-b",
            ttl_seconds=1,
            now=now + timedelta(seconds=2),
            reconciliation=unbound,
        )


def test_full_demo_runs_general_and_workload_closed_loops() -> None:
    result = run_full_demo()

    assert result.evidence_kind == "synthetic"
    assert result.general.stop_reason == StopReason.NO_IMPROVEMENT
    assert result.workload.stop_reason == StopReason.TARGET_ACHIEVED
    assert len(result.general.candidates) > 1
    assert len(result.general.baseline_history) > 1
    assert result.general.recommended_candidate_id is not None
    assert result.workload.recommended_candidate_id is not None
    assert result.workload.routed_components == ["cpu"]
    assert all(
        candidate.safety_state == SafetyState.ROLLED_BACK
        for candidate in [*result.general.candidates, *result.workload.candidates]
    )


def _run_budgeted_loop(*, max_candidates: int, max_attempts: int, baseline_every_n: int):
    manifest = build_demo_manifest()
    initial = {item.id: item.default for item in manifest.items}
    backend = SimulatedBackend(initial, target_id="synthetic-multi-round")
    policy = build_demo_policy(OptimizationMode.GENERAL)
    policy.search.max_candidates = max_candidates
    policy.search.max_attempts = max_attempts
    policy.search.no_improvement_limit = max_candidates + 1
    policy.search.target_improvement = None
    policy.statistics.baseline_every_n = baseline_every_n
    return SystemOptimizationEngine(policy, manifest, resolve_demo_domains(manifest), backend).run(
        baseline_parameters={item.parameter_id: item.default for item in manifest.items},
        measure=SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
        fencing_token=9,
    )


def test_engine_generates_multiple_candidates_and_refreshes_baseline() -> None:
    result = _run_budgeted_loop(max_candidates=4, max_attempts=8, baseline_every_n=2)

    assert result.stop_reason == StopReason.CANDIDATE_BUDGET
    assert len(result.candidates) == 4
    assert len(result.baseline_history) == 2
    assert result.attempt_count == 6
    assert result.candidates[0].comparison_baseline_digest == result.baseline_history[0].digest
    assert result.candidates[2].comparison_baseline_digest == result.baseline_history[1].digest
    assert len({candidate.candidate_id for candidate in result.candidates}) == 4


def test_periodic_baseline_and_candidates_share_attempt_budget() -> None:
    result = _run_budgeted_loop(max_candidates=10, max_attempts=4, baseline_every_n=1)

    assert result.stop_reason == StopReason.ATTEMPT_BUDGET
    assert len(result.candidates) == 2
    assert len(result.baseline_history) == 2
    assert result.attempt_count == 4
