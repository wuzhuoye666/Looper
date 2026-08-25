"""RCP-02B legacy ``.guard`` explicit reconciliation tests.

Contract source: ``receipt-mutex-recovery-contract-2026-08-24.md`` §7 (evidence
schema), §8 (frozen 9-step order), §9 (target-level attention scope), §11
(RCP-02B matrix, 9 cases).

A legacy guard is an empty file whose only recoverable facts are the
filename-encoded identity/operation and its byte digest.  Reconciliation must
publish durable evidence BEFORE deleting the guard; deletion without evidence
is a hard-order violation.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from looper_core.system_opt.config_manifest import ConfigComponent, RiskLevel
from looper_core.system_opt.hypothesis import ComponentHypothesis
from looper_core.system_opt.intervention import (
    INTERVENTION_RISK_SOURCE_SCHEMA,
    InterventionPlan,
    ReceiptOperation,
    RiskSource,
    RiskSourceItem,
    RiskSourceKind,
)
from looper_core.system_opt.intervention_receipt import (
    DurableReceiptStore,
    GuardReconciliationOutcome,
    GuardWriterQuiescence,
    ReceiptStoreError,
)
from system_opt_support import integer_item, manifest

# ---------------------------------------------------------------- fixtures


def _plan() -> InterventionPlan:
    item = integer_item()
    config = manifest(item)
    return InterventionPlan(
        hypothesis=ComponentHypothesis(
            hypothesis_id="hyp-cpu",
            symptom_id="symptom-window-1",
            component=ConfigComponent.CPU,
            rank=1,
        ),
        change={item.parameter_id: 10},
        risk=RiskLevel.LOW,
        risk_source=RiskSource(
            schema_version=INTERVENTION_RISK_SOURCE_SCHEMA,
            kind=RiskSourceKind.MANIFEST_DERIVED,
            manifest_digest=config.digest,
            items=[RiskSourceItem(item_id=item.id, risk=item.risk)],
        ),
    )


def _write_legacy_guard(store: DurableReceiptStore, plan: InterventionPlan) -> Path:
    identity = store._execution_digest(plan.digest, "exec-1").removeprefix("sha256:")
    guard_path = store.root / f".{identity}.candidate.guard"
    store.root.mkdir(parents=True, exist_ok=True)
    guard_path.write_bytes(b"")
    return guard_path


def _quiescence(declared: bool = True) -> GuardWriterQuiescence:
    return GuardWriterQuiescence(
        declared=declared,
        statement="operator declares legacy writers stopped",
    )


@pytest.fixture()
def store(tmp_path: Path) -> DurableReceiptStore:
    return DurableReceiptStore(tmp_path / "receipts")


# ------------------------------------------------- matrix 1: discovery


class TestDiscovery:
    def test_legacy_guard_is_discovered_and_blocks_writes(self, store, tmp_path):
        plan = _plan()
        guard_path = _write_legacy_guard(store, plan)
        discovered = store.discover_legacy_guards()
        assert [path.name for path in discovered] == [guard_path.name]
        # While the guard exists, every write path fails closed.
        with pytest.raises(ReceiptStoreError, match="legacy receipt guard"):
            store.start(plan=plan, execution_id="exec-1", operation=ReceiptOperation.CANDIDATE)

    def test_store_without_guards_discovers_none(self, store):
        assert store.discover_legacy_guards() == []


# --------------------------------------- matrix 3: quiescence gate


class TestQuiescenceGate:
    def test_undeclared_quiescence_refuses_to_reconcile(self, store):
        plan = _plan()
        guard_path = _write_legacy_guard(store, plan)
        with pytest.raises(ReceiptStoreError, match="quiescence"):
            store.reconcile_legacy_guard(
                guard_path,
                target_id="target-1",
                operator_id="operator-1",
                writer_quiescence=_quiescence(declared=False),
            )
        # The guard is untouched.
        assert guard_path.exists()


# ------------------------------------- matrix 4: identity mismatch


class TestIdentityBinding:
    def test_supplied_identity_mismatch_fails_closed(self, store):
        plan = _plan()
        guard_path = _write_legacy_guard(store, plan)
        with pytest.raises(ReceiptStoreError, match="does not match"):
            store.reconcile_legacy_guard(
                guard_path,
                target_id="target-1",
                operator_id="operator-1",
                writer_quiescence=_quiescence(),
                plan_digest=plan.digest,
                execution_id="exec-OTHER",
            )
        assert guard_path.exists()


# -------------------------- matrix 5/6/7: order + crash seams


class TestEvidenceBeforeDeletion:
    def test_successful_reconciliation_publishes_evidence_then_deletes(
        self, store, tmp_path
    ):
        plan = _plan()
        guard_path = _write_legacy_guard(store, plan)
        now = datetime.now(UTC)
        evidence = store.reconcile_legacy_guard(
            guard_path,
            target_id="target-1",
            operator_id="operator-1",
            writer_quiescence=_quiescence(),
            plan_digest=plan.digest,
            execution_id="exec-1",
            now=now,
        )
        # Guard deleted, evidence durably published.
        assert not guard_path.exists()
        evidence_path = (
            store.root
            / "guard-reconciliations"
            / f"{evidence.digest.removeprefix('sha256:')}.json"
        )
        assert evidence_path.exists()
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert payload["guard_filename"] == guard_path.name
        assert payload["outcome"] == GuardReconciliationOutcome.ORPHAN_CONFIRMED.value
        assert payload["operator_id"] == "operator-1"
        assert payload["target_id"] == "target-1"
        assert payload["writer_quiescence"]["declared"] is True

    def test_retry_after_evidence_publish_is_idempotent(self, store):
        """Crash seam: evidence written, guard still present -> retry works."""
        plan = _plan()
        guard_path = _write_legacy_guard(store, plan)
        first = store.reconcile_legacy_guard(
            guard_path,
            target_id="target-1",
            operator_id="operator-1",
            writer_quiescence=_quiescence(),
            plan_digest=plan.digest,
            execution_id="exec-1",
        )
        evidence_path = (
            store.root
            / "guard-reconciliations"
            / f"{first.digest.removeprefix('sha256:')}.json"
        )
        assert evidence_path.exists()
        # Simulate the crash seam: recreate the guard, rerun the same
        # reconciliation.  Content-addressed evidence must not conflict.
        guard_path.write_bytes(b"")
        second = store.reconcile_legacy_guard(
            guard_path,
            target_id="target-1",
            operator_id="operator-1",
            writer_quiescence=_quiescence(),
            plan_digest=plan.digest,
            execution_id="exec-1",
        )
        assert second.guard_filename == first.guard_filename
        assert not guard_path.exists()

    def test_chain_verification_failure_keeps_guard_and_attention_outcome(
        self, store
    ):
        """Matrix 8: a corrupted chain must not delete the guard."""
        plan = _plan()
        guard_path = _write_legacy_guard(store, plan)
        # Corrupt the store: a content file with a malformed name makes
        # _all_receipts fail, so head() raises inside reconciliation.
        (store.root / "deadbeef.json").write_text("not a receipt", encoding="utf-8")
        evidence = store.reconcile_legacy_guard(
            guard_path,
            target_id="target-1",
            operator_id="operator-1",
            writer_quiescence=_quiescence(),
            plan_digest=plan.digest,
            execution_id="exec-1",
        )
        assert evidence.outcome is GuardReconciliationOutcome.NEEDS_ATTENTION
        assert "verification failed" in evidence.reason or "failed" in evidence.reason
        # Guard survives; evidence for the failure is still durable.
        assert guard_path.exists()
        evidence_path = (
            store.root
            / "guard-reconciliations"
            / f"{evidence.digest.removeprefix('sha256:')}.json"
        )
        assert evidence_path.exists()


# --------------------------------------------- evidence schema


class TestEvidenceSchema:
    def test_filename_must_encode_declared_identity(self):
        from looper_core.system_opt.intervention_receipt import (
            ReceiptGuardReconciliation,
        )

        quiescence = _quiescence()
        # Well-formed filename (passes the field pattern) whose hex identity
        # differs from the declared execution_digest: the model validator
        # must reject the mismatch.
        with pytest.raises(ValueError, match="does not encode"):
            ReceiptGuardReconciliation(
                guard_filename=f".{'c' * 64}.candidate.guard",
                execution_digest="sha256:" + "a" * 64,
                operation=ReceiptOperation.CANDIDATE,
                guard_sha256="sha256:" + "b" * 64,
                receipt_root="/tmp/receipts",
                discovered_at=datetime.now(UTC),
                target_id="t",
                operator_id="o",
                writer_quiescence=quiescence,
                chain_head_digest=None,
                outcome=GuardReconciliationOutcome.ORPHAN_CONFIRMED,
                reason="ok",
                recorded_at=datetime.now(UTC),
            )

    def test_naive_timestamps_are_rejected(self):
        from looper_core.system_opt.intervention_receipt import (
            ReceiptGuardReconciliation,
        )

        digest_hex = "a" * 64
        with pytest.raises(ValueError, match="timezone-aware"):
            ReceiptGuardReconciliation(
                guard_filename=f".{digest_hex}.candidate.guard",
                execution_digest=f"sha256:{digest_hex}",
                operation=ReceiptOperation.CANDIDATE,
                guard_sha256="sha256:" + "b" * 64,
                receipt_root="/tmp/receipts",
                discovered_at=datetime(2026, 8, 25),
                target_id="t",
                operator_id="o",
                writer_quiescence=_quiescence(),
                chain_head_digest=None,
                outcome=GuardReconciliationOutcome.ORPHAN_CONFIRMED,
                reason="ok",
                recorded_at=datetime(2026, 8, 25),
            )


# ------------------------------------- reconciliation unlocks writes


class TestStoreUnblocked:
    def test_after_reconciliation_writes_work_again(self, store):
        plan = _plan()
        guard_path = _write_legacy_guard(store, plan)
        with pytest.raises(ReceiptStoreError, match="legacy receipt guard"):
            store.start(plan=plan, execution_id="exec-1", operation=ReceiptOperation.CANDIDATE)
        store.reconcile_legacy_guard(
            guard_path,
            target_id="target-1",
            operator_id="operator-1",
            writer_quiescence=_quiescence(),
            plan_digest=plan.digest,
            execution_id="exec-1",
        )
        receipt = store.start(
            plan=plan, execution_id="exec-1", operation=ReceiptOperation.CANDIDATE
        )
        assert receipt.sequence == 0


# ------------------------------------------- pointer-only association


class TestPointerOnlyAssociation:
    def test_reconciliation_without_plan_execution_uses_pointer_lookup(self, store):
        """When plan/execution are unknown the head association is
        best-effort via the pointer filename (GAP-R02B-2), and the
        reconciliation still succeeds with a null head when no pointer
        exists."""
        plan = _plan()
        guard_path = _write_legacy_guard(store, plan)
        evidence = store.reconcile_legacy_guard(
            guard_path,
            target_id="target-1",
            operator_id="operator-1",
            writer_quiescence=_quiescence(),
        )
        assert evidence.chain_head_digest is None
        assert evidence.outcome is GuardReconciliationOutcome.ORPHAN_CONFIRMED
        assert not guard_path.exists()
