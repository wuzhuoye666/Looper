from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from looper_core.system_opt.demo import run_full_demo
from looper_core.system_opt.lease import FileTargetGuard, LeaseConflict
from looper_core.system_opt.safety import SafetyState
from looper_core.system_opt.tuning import StopReason


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
    assert result.general.stop_reason == StopReason.TARGET_ACHIEVED
    assert result.workload.stop_reason == StopReason.TARGET_ACHIEVED
    assert result.general.recommended_candidate_id is not None
    assert result.workload.recommended_candidate_id is not None
    assert result.workload.routed_components == ["cpu"]
    assert all(
        candidate.safety_state == SafetyState.ROLLED_BACK
        for candidate in [*result.general.candidates, *result.workload.candidates]
    )
