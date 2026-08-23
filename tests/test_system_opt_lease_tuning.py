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
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.lease import FileTargetGuard, LeaseConflict
from looper_core.system_opt.policy import OptimizationMode
from looper_core.system_opt.safety import SafetyState
from looper_core.system_opt.tuning import StopReason, SystemOptimizationEngine


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
        reconciliation_digest=None,
    )

    with pytest.raises(LeaseConflict, match="leased"):
        guard.acquire(
            "target-1",
            "owner-b",
            ttl_seconds=10,
            now=now + timedelta(seconds=1),
            reconciliation_digest=None,
        )
    with pytest.raises(LeaseConflict, match="reconciliation"):
        guard.acquire(
            "target-1",
            "owner-b",
            ttl_seconds=10,
            now=now + timedelta(seconds=11),
            reconciliation_digest=None,
        )

    second = guard.acquire(
        "target-1",
        "owner-b",
        ttl_seconds=10,
        now=now + timedelta(seconds=11),
        reconciliation_digest="sha256:" + "a" * 64,
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
            reconciliation_digest=None,
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
