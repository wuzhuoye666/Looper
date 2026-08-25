from __future__ import annotations

import json
from pathlib import Path

import pytest
from looper_core.system_opt.config_manifest import ConfigComponent, RiskLevel
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.hypothesis import ComponentHypothesis, InterventionExperiment
from looper_core.system_opt.intervention import (
    INTERVENTION_RISK_SOURCE_SCHEMA,
    InterventionExecutionReceiptV2,
    InterventionOutcome,
    InterventionPlan,
    ReceiptOperation,
    ReceiptStageV2,
    RiskSource,
    RiskSourceItem,
    RiskSourceKind,
    receipt_stage_for,
)
from looper_core.system_opt.intervention_receipt import (
    DurableReceiptStore,
    ReceiptStoreError,
)
from looper_core.system_opt.safety import (
    SafetyController,
    SafetyPolicy,
    SafetyProgressEvent,
    SafetyProgressStage,
    SafetyState,
)
from system_opt_support import integer_item, manifest

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


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


def _candidate_safety_terminal(
    store: DurableReceiptStore, plan: InterventionPlan, execution_id: str
) -> InterventionExecutionReceiptV2:
    receipt = store.start(
        plan=plan,
        execution_id=execution_id,
        operation=ReceiptOperation.CANDIDATE,
    )
    receipt = store.advance(
        receipt,
        ReceiptStageV2.PREFLIGHT_COMPLETED,
        safety_state=SafetyState.PREFLIGHT,
    )
    receipt = store.advance(
        receipt,
        ReceiptStageV2.APPLY_STARTED,
        safety_state=SafetyState.APPLY,
    )
    return store.advance(
        receipt,
        ReceiptStageV2.SAFETY_TERMINAL,
        safety_state=SafetyState.KEPT,
        evidence_digest=DIGEST_A,
    )


def test_candidate_and_recovery_chains_have_independent_pointers(tmp_path: Path) -> None:
    plan = _plan()
    store = DurableReceiptStore(tmp_path / "receipts")
    candidate = _candidate_safety_terminal(store, plan, "window-1")

    recovery = store.start(
        plan=plan,
        execution_id="window-1",
        operation=ReceiptOperation.RECOVERY,
        parent_receipt_digest=candidate.digest,
    )
    recovery = store.advance(
        recovery,
        ReceiptStageV2.PREFLIGHT_COMPLETED,
        safety_state=SafetyState.PREFLIGHT,
    )
    recovery = store.advance(
        recovery,
        ReceiptStageV2.APPLY_STARTED,
        safety_state=SafetyState.APPLY,
    )
    recovery = store.advance(
        recovery,
        ReceiptStageV2.SAFETY_TERMINAL,
        safety_state=SafetyState.KEPT,
        evidence_digest=DIGEST_B,
    )
    recovery = store.advance(
        recovery,
        ReceiptStageV2.OPERATION_TERMINAL,
        safety_state=SafetyState.KEPT,
        evidence_digest=DIGEST_B,
    )
    experiment = InterventionExperiment(
        measurement_batch_digest="sha256:" + "c" * 64,
        business_metric_id="business.throughput",
        accepted=False,
    )
    outcome = InterventionOutcome(
        plan_digest=plan.digest,
        write_attempted=True,
        apply_started=True,
        rollback_attempted=True,
        rollback_verified=True,
        experiment=experiment,
        safety_state=SafetyState.KEPT,
        evidence_digest=DIGEST_B,
    )
    candidate = store.advance(
        candidate,
        ReceiptStageV2.OPERATION_TERMINAL,
        safety_state=SafetyState.KEPT,
        evidence_digest=DIGEST_B,
        outcome=outcome,
    )

    assert store.verify_chain(
        plan.digest, "window-1", ReceiptOperation.CANDIDATE
    ) == candidate
    assert store.verify_chain(
        plan.digest, "window-1", ReceiptOperation.RECOVERY
    ) == recovery
    pointers = sorted(path.name for path in store.root.glob("*.current.json"))
    assert len(pointers) == 2
    assert pointers[0].split(".")[0] == pointers[1].split(".")[0]


def test_safety_observer_persists_a_complete_candidate_safety_chain(
    tmp_path: Path,
) -> None:
    plan = _plan()
    item = integer_item()
    store = DurableReceiptStore(tmp_path / "receipts")
    current = store.start(
        plan=plan,
        execution_id="window-observed",
        operation=ReceiptOperation.CANDIDATE,
    )

    def observer(event: SafetyProgressEvent) -> None:
        nonlocal current
        fields: dict[str, object] = {"safety_state": event.safety_state}
        if event.stage is SafetyProgressStage.SAFETY_TERMINAL:
            fields["evidence_digest"] = event.evidence_digest
        current = store.advance(current, receipt_stage_for(event.stage), **fields)

    observed = SafetyController(SafetyPolicy(allow_keep=True)).execute_observed(
        manifest(item),
        {item.parameter_id: 10},
        SimulatedBackend({item.id: 60}),
        fencing_token=1,
        keep=True,
        keep_authorized=True,
        progress_observer=observer,
    )

    assert observed.progress_failures == []
    assert current.stage is ReceiptStageV2.SAFETY_TERMINAL
    assert current.evidence_digest == observed.result.digest
    assert store.verify_chain(
        plan.digest, "window-observed", ReceiptOperation.CANDIDATE
    ) == current


def test_same_plan_can_execute_in_distinct_windows_but_not_restart_same_window(
    tmp_path: Path,
) -> None:
    plan = _plan()
    store = DurableReceiptStore(tmp_path / "receipts")

    first = store.start(
        plan=plan,
        execution_id="window-1",
        operation=ReceiptOperation.CANDIDATE,
    )
    second = store.start(
        plan=plan,
        execution_id="window-2",
        operation=ReceiptOperation.CANDIDATE,
    )

    assert first.execution_digest != second.execution_digest
    with pytest.raises(ReceiptStoreError, match="already exists"):
        store.start(
            plan=plan,
            execution_id="window-1",
            operation=ReceiptOperation.CANDIDATE,
        )


def test_store_rejects_illegal_stage_skip_and_stale_head(tmp_path: Path) -> None:
    plan = _plan()
    store = DurableReceiptStore(tmp_path / "receipts")
    root = store.start(
        plan=plan,
        execution_id="window-1",
        operation=ReceiptOperation.CANDIDATE,
    )

    with pytest.raises(ReceiptStoreError, match="illegal receipt stage transition"):
        store.advance(
            root,
            ReceiptStageV2.OPERATION_TERMINAL,
            safety_state=SafetyState.REJECTED,
            evidence_digest=DIGEST_A,
            outcome=InterventionOutcome(
                plan_digest=plan.digest,
                write_attempted=False,
                apply_started=False,
                rollback_attempted=False,
                rollback_verified=False,
                safety_state=SafetyState.REJECTED,
                evidence_digest=DIGEST_A,
            ),
        )

    successor = store.advance(
        root,
        ReceiptStageV2.PREFLIGHT_COMPLETED,
        safety_state=SafetyState.PREFLIGHT,
    )
    with pytest.raises(ReceiptStoreError, match="stale receipt head"):
        store.advance(
            root,
            ReceiptStageV2.SAFETY_TERMINAL,
            safety_state=SafetyState.REJECTED,
            evidence_digest=DIGEST_A,
        )
    assert store.verify_chain(
        plan.digest, "window-1", ReceiptOperation.CANDIDATE
    ) == successor


def test_store_detects_fork_from_same_predecessor(tmp_path: Path) -> None:
    plan = _plan()
    store = DurableReceiptStore(tmp_path / "receipts")
    root = store.start(
        plan=plan,
        execution_id="window-1",
        operation=ReceiptOperation.CANDIDATE,
    )
    store.advance(
        root,
        ReceiptStageV2.PREFLIGHT_COMPLETED,
        safety_state=SafetyState.PREFLIGHT,
    )
    forged = InterventionExecutionReceiptV2(
        plan_digest=plan.digest,
        execution_id="window-1",
        operation=ReceiptOperation.CANDIDATE,
        stage=ReceiptStageV2.SAFETY_TERMINAL,
        sequence=1,
        predecessor_receipt_digest=root.digest,
        safety_state=SafetyState.REJECTED,
        evidence_digest=DIGEST_A,
        error="forged competing successor",
    )
    forged_path = store.root / f"{forged.digest.removeprefix('sha256:')}.json"
    forged_path.write_text(forged.model_dump_json(), encoding="utf-8")

    with pytest.raises(ReceiptStoreError, match="forked"):
        store.verify_chain(plan.digest, "window-1", ReceiptOperation.CANDIDATE)


def test_store_rejects_terminal_successor_and_cross_execution_predecessor(
    tmp_path: Path,
) -> None:
    plan = _plan()
    store = DurableReceiptStore(tmp_path / "receipts")
    terminal = _candidate_safety_terminal(store, plan, "window-1")
    outcome = InterventionOutcome(
        plan_digest=plan.digest,
        write_attempted=True,
        apply_started=True,
        rollback_attempted=False,
        rollback_verified=False,
        experiment=InterventionExperiment(
            measurement_batch_digest=DIGEST_B,
            business_metric_id="business.throughput",
            accepted=True,
        ),
        safety_state=SafetyState.KEPT,
        evidence_digest=DIGEST_B,
    )
    operation_terminal = store.advance(
        terminal,
        ReceiptStageV2.OPERATION_TERMINAL,
        safety_state=SafetyState.KEPT,
        evidence_digest=DIGEST_B,
        outcome=outcome,
    )
    with pytest.raises(ReceiptStoreError, match="illegal receipt stage transition"):
        store.advance(
            operation_terminal,
            ReceiptStageV2.OPERATION_TERMINAL,
            safety_state=SafetyState.KEPT,
            evidence_digest=DIGEST_B,
            outcome=outcome,
        )

    identity_store = DurableReceiptStore(tmp_path / "identity-receipts")
    identity_terminal = _candidate_safety_terminal(
        identity_store, plan, "window-identity"
    )
    forged = InterventionExecutionReceiptV2(
        plan_digest=plan.digest,
        execution_id="window-forged",
        operation=ReceiptOperation.CANDIDATE,
        stage=ReceiptStageV2.OPERATION_TERMINAL,
        sequence=identity_terminal.sequence + 1,
        predecessor_receipt_digest=identity_terminal.digest,
        safety_state=SafetyState.KEPT,
        evidence_digest=DIGEST_B,
        outcome=outcome,
    )
    forged_path = identity_store.root / f"{forged.digest.removeprefix('sha256:')}.json"
    forged_path.write_text(forged.model_dump_json(), encoding="utf-8")
    with pytest.raises(ReceiptStoreError, match="identity drifted"):
        identity_store.verify_chain(
            plan.digest, "window-identity", ReceiptOperation.CANDIDATE
        )


def test_content_before_pointer_crash_recovers_unique_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan()
    store = DurableReceiptStore(tmp_path / "receipts")
    root = store.start(
        plan=plan,
        execution_id="window-1",
        operation=ReceiptOperation.CANDIDATE,
    )
    original = store._atomic_write

    def fail_pointer(path: Path, payload: dict[str, object]) -> None:
        if path.name.endswith(".current.json"):
            raise OSError("pointer replace failed")
        original(path, payload)

    monkeypatch.setattr(store, "_atomic_write", fail_pointer)
    with pytest.raises(OSError, match="pointer replace failed"):
        store.advance(
            root,
            ReceiptStageV2.PREFLIGHT_COMPLETED,
            safety_state=SafetyState.PREFLIGHT,
        )
    monkeypatch.setattr(store, "_atomic_write", original)

    recovered = store.verify_chain(
        plan.digest, "window-1", ReceiptOperation.CANDIDATE
    )
    assert recovered.stage is ReceiptStageV2.PREFLIGHT_COMPLETED
    assert recovered.predecessor_receipt_digest == root.digest


def test_missing_recovery_parent_and_tampered_content_fail_closed(tmp_path: Path) -> None:
    plan = _plan()
    store = DurableReceiptStore(tmp_path / "receipts")

    with pytest.raises(ReceiptStoreError, match="parent receipt is missing"):
        store.start(
            plan=plan,
            execution_id="window-1",
            operation=ReceiptOperation.RECOVERY,
            parent_receipt_digest=DIGEST_A,
        )

    root = store.start(
        plan=plan,
        execution_id="window-1",
        operation=ReceiptOperation.CANDIDATE,
    )
    path = store.root / f"{root.digest.removeprefix('sha256:')}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["execution_id"] = "forged-window"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReceiptStoreError, match="filename digest mismatch"):
        store.verify_chain(plan.digest, "window-1", ReceiptOperation.CANDIDATE)
