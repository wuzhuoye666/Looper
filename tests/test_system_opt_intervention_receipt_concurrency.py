"""RCP-02A real concurrency tests for the DurableReceiptStore advisory lock.

These tests exercise genuine thread and process contention against the OS
advisory lock.  The lock file (``.<digest>.<operation>.lock``) is a permanent
anchor: it is allowed to exist after a test, but must never hold an active lock
after the holder exits (including a forced process kill).

Platform discipline: these tests run on the platform that executes them.  They
never claim the other platform passed.
"""
from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest
from looper_core.system_opt.config_manifest import ConfigComponent, RiskLevel
from looper_core.system_opt.hypothesis import ComponentHypothesis
from looper_core.system_opt.intervention import (
    INTERVENTION_RISK_SOURCE_SCHEMA,
    InterventionPlan,
    ReceiptOperation,
    ReceiptStageV2,
    RiskSource,
    RiskSourceItem,
    RiskSourceKind,
)
from looper_core.system_opt.intervention_receipt import (
    DurableReceiptStore,
    ReceiptStoreError,
    ReceiptStoreUnavailable,
)
from looper_core.system_opt.safety import SafetyState
from system_opt_support import integer_item, manifest

# Test-only liveness guards (NOT a production contract): these timeouts exist
# only to stop a wedged test thread/process from hanging the suite.  They never
# gate lock acquisition in production code.
_THREAD_JOIN_TIMEOUT = 10.0
_PROCESS_JOIN_TIMEOUT = 30.0
_EVENT_WAIT_TIMEOUT = 30.0


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


def _lock_file(
    store: DurableReceiptStore,
    plan: InterventionPlan,
    execution_id: str,
    operation: ReceiptOperation,
) -> Path:
    identity = store._execution_digest(plan.digest, execution_id).removeprefix("sha256:")
    return store.root / f".{identity}.{operation.value}.lock"


def _worker_hold_lock(root, plan, execution_id, operation, entered, release):
    """Module-level spawn worker: hold one scope's lock until released."""
    store = DurableReceiptStore(Path(root))
    with store._mutex(plan.digest, execution_id, operation):
        entered.set()
        release.wait(timeout=_EVENT_WAIT_TIMEOUT)


def _worker_hold_lock_forever(root, plan, execution_id, operation, entered):
    """Module-level spawn worker: hold the lock until force-killed."""
    store = DurableReceiptStore(Path(root))
    with store._mutex(plan.digest, execution_id, operation):
        entered.set()
        while True:
            time.sleep(0.05)


def _assert_scope_can_enter_while_other_is_held(
    store: DurableReceiptStore,
    plan: InterventionPlan,
    *,
    held_execution_id: str,
    held_operation: ReceiptOperation,
    other_execution_id: str,
    other_operation: ReceiptOperation,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store._mutex(plan.digest, held_execution_id, held_operation):
            entered.set()
            release.wait(timeout=_EVENT_WAIT_TIMEOUT)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert entered.wait(timeout=_EVENT_WAIT_TIMEOUT)
        with store._mutex(plan.digest, other_execution_id, other_operation):
            assert thread.is_alive()
    finally:
        release.set()
        thread.join(timeout=_THREAD_JOIN_TIMEOUT)
        assert not thread.is_alive()


def test_same_scope_two_threads_contend(tmp_path: Path) -> None:
    store = DurableReceiptStore(tmp_path / "receipts")
    plan = _plan()
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store._mutex(plan.digest, "window-1", ReceiptOperation.CANDIDATE):
            entered.set()
            release.wait(timeout=_EVENT_WAIT_TIMEOUT)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert entered.wait(timeout=_EVENT_WAIT_TIMEOUT)
        with (
            pytest.raises(ReceiptStoreError, match="busy"),
            store._mutex(plan.digest, "window-1", ReceiptOperation.CANDIDATE),
        ):
            pass
    finally:
        release.set()
        thread.join(timeout=_THREAD_JOIN_TIMEOUT)


def test_same_scope_two_processes_contend(tmp_path: Path) -> None:
    plan = _plan()
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    process = context.Process(
        target=_worker_hold_lock,
        args=(
            str(tmp_path / "receipts"),
            plan,
            "window-1",
            ReceiptOperation.CANDIDATE,
            entered,
            release,
        ),
    )
    process.start()
    try:
        assert entered.wait(timeout=_EVENT_WAIT_TIMEOUT)
        store = DurableReceiptStore(tmp_path / "receipts")
        with (
            pytest.raises(ReceiptStoreError, match="busy"),
            store._mutex(plan.digest, "window-1", ReceiptOperation.CANDIDATE),
        ):
            pass
    finally:
        release.set()
        process.join(timeout=_PROCESS_JOIN_TIMEOUT)
        if process.is_alive():
            process.terminate()
            process.join(timeout=_PROCESS_JOIN_TIMEOUT)


def test_lock_holder_terminate_releases_lock(tmp_path: Path) -> None:
    plan = _plan()
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    process = context.Process(
        target=_worker_hold_lock_forever,
        args=(
            str(tmp_path / "receipts"),
            plan,
            "window-1",
            ReceiptOperation.CANDIDATE,
            entered,
        ),
    )
    process.start()
    try:
        assert entered.wait(timeout=_EVENT_WAIT_TIMEOUT)
        process.terminate()
        process.join(timeout=_PROCESS_JOIN_TIMEOUT)
        assert not process.is_alive()
        # The OS releases the lock on process exit; the same scope must be
        # reacquirable and the lock file must hold no active lock.
        store = DurableReceiptStore(tmp_path / "receipts")
        with store._mutex(plan.digest, "window-1", ReceiptOperation.CANDIDATE):
            pass
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=_PROCESS_JOIN_TIMEOUT)


def test_different_executions_do_not_block(tmp_path: Path) -> None:
    store = DurableReceiptStore(tmp_path / "receipts")
    plan = _plan()
    _assert_scope_can_enter_while_other_is_held(
        store,
        plan,
        held_execution_id="window-1",
        held_operation=ReceiptOperation.CANDIDATE,
        other_execution_id="window-2",
        other_operation=ReceiptOperation.CANDIDATE,
    )


def test_candidate_and_recovery_do_not_block(tmp_path: Path) -> None:
    store = DurableReceiptStore(tmp_path / "receipts")
    plan = _plan()
    _assert_scope_can_enter_while_other_is_held(
        store,
        plan,
        held_execution_id="window-1",
        held_operation=ReceiptOperation.CANDIDATE,
        other_execution_id="window-1",
        other_operation=ReceiptOperation.RECOVERY,
    )


def test_concurrent_advance_same_current_is_single_head(tmp_path: Path) -> None:
    store = DurableReceiptStore(tmp_path / "receipts")
    plan = _plan()
    root = store.start(plan=plan, execution_id="window-1", operation=ReceiptOperation.CANDIDATE)
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def worker() -> None:
        barrier.wait(timeout=_EVENT_WAIT_TIMEOUT)
        try:
            store.advance(
                root, ReceiptStageV2.PREFLIGHT_COMPLETED, safety_state=SafetyState.PREFLIGHT
            )
        except ReceiptStoreError as error:
            errors.append(str(error))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_THREAD_JOIN_TIMEOUT)

    # No fork: the unique content head is a single PREFLIGHT_COMPLETED node.
    head = store.verify_chain(plan.digest, "window-1", ReceiptOperation.CANDIDATE)
    assert head.stage is ReceiptStageV2.PREFLIGHT_COMPLETED
    assert head.sequence == 1
    assert all("fork" not in message for message in errors)


def test_stale_current_is_rejected_under_lock(tmp_path: Path) -> None:
    store = DurableReceiptStore(tmp_path / "receipts")
    plan = _plan()
    root = store.start(plan=plan, execution_id="window-1", operation=ReceiptOperation.CANDIDATE)
    store.advance(root, ReceiptStageV2.PREFLIGHT_COMPLETED, safety_state=SafetyState.PREFLIGHT)
    # Advancing the stale root to a *different* stage is non-idempotent and must
    # be rejected as a stale head (the unique chain head is already advanced).
    with pytest.raises(ReceiptStoreError, match="stale"):
        store.advance(
            root,
            ReceiptStageV2.SAFETY_TERMINAL,
            safety_state=SafetyState.KEPT,
            evidence_digest="sha256:" + "a" * 64,
        )


def test_pointer_deleted_rebuilds_from_content_chain(tmp_path: Path) -> None:
    store = DurableReceiptStore(tmp_path / "receipts")
    plan = _plan()
    root = store.start(plan=plan, execution_id="window-1", operation=ReceiptOperation.CANDIDATE)
    advanced = store.advance(
        root, ReceiptStageV2.PREFLIGHT_COMPLETED, safety_state=SafetyState.PREFLIGHT
    )
    pointer_path = store._pointer_path(plan.digest, "window-1", ReceiptOperation.CANDIDATE)
    pointer_path.unlink()
    assert store.verify_chain(plan.digest, "window-1", ReceiptOperation.CANDIDATE) == advanced


def test_pointer_to_ancestor_recovers_unique_head(tmp_path: Path) -> None:
    store = DurableReceiptStore(tmp_path / "receipts")
    plan = _plan()
    root = store.start(plan=plan, execution_id="window-1", operation=ReceiptOperation.CANDIDATE)
    advanced = store.advance(
        root, ReceiptStageV2.PREFLIGHT_COMPLETED, safety_state=SafetyState.PREFLIGHT
    )
    # Rewrite the pointer back to the root (content-before-pointer crash seam).
    pointer_path = store._pointer_path(plan.digest, "window-1", ReceiptOperation.CANDIDATE)
    pointer_path.write_text(
        (
            '{"schema_version": "looper.intervention-receipt-pointer/v1alpha1", '
            f'"execution_digest": "{root.execution_digest}", '
            f'"plan_digest": "{root.plan_digest}", '
            f'"execution_id": "{root.execution_id}", '
            f'"operation": "candidate", '
            f'"receipt_digest": "{root.digest}", '
            f'"receipt_filename": "{root.digest.removeprefix("sha256:")}.json"}}'
        ),
        encoding="utf-8",
    )
    assert store.verify_chain(plan.digest, "window-1", ReceiptOperation.CANDIDATE) == advanced


def test_lock_file_persists_and_reacquires(tmp_path: Path) -> None:
    store = DurableReceiptStore(tmp_path / "receipts")
    plan = _plan()
    receipt = store.start(plan=plan, execution_id="window-1", operation=ReceiptOperation.CANDIDATE)
    lock_path = _lock_file(store, plan, "window-1", ReceiptOperation.CANDIDATE)
    assert lock_path.exists()
    store.advance(receipt, ReceiptStageV2.PREFLIGHT_COMPLETED, safety_state=SafetyState.PREFLIGHT)
    # The lock anchor is permanent (never unlinked) and still reacquirable.
    assert lock_path.exists()


def test_legacy_guard_fails_closed_and_is_preserved(tmp_path: Path) -> None:
    plan = _plan()
    store = DurableReceiptStore(tmp_path / "receipts")
    identity = store._execution_digest(plan.digest, "window-1").removeprefix("sha256:")
    legacy_guard = tmp_path / "receipts" / f".{identity}.candidate.guard"
    legacy_guard.parent.mkdir(parents=True, exist_ok=True)
    legacy_guard.write_bytes(b"")

    with pytest.raises(ReceiptStoreError, match="legacy receipt guard"):
        store.start(plan=plan, execution_id="window-1", operation=ReceiptOperation.CANDIDATE)

    assert legacy_guard.exists()
    assert not list((tmp_path / "receipts").glob("*.json"))


def test_legacy_guard_directory_also_fails_closed(tmp_path: Path) -> None:
    plan = _plan()
    store = DurableReceiptStore(tmp_path / "receipts")
    identity = store._execution_digest(plan.digest, "window-1").removeprefix("sha256:")
    legacy_guard = tmp_path / "receipts" / f".{identity}.candidate.guard"
    legacy_guard.mkdir(parents=True)

    with pytest.raises(ReceiptStoreError, match="legacy receipt guard"):
        store.start(plan=plan, execution_id="window-1", operation=ReceiptOperation.CANDIDATE)

    assert legacy_guard.is_dir()


@pytest.mark.parametrize("fstype", [None, "nfs", "unclassified-local-fs"])
def test_linux_unknown_or_unsupported_filesystem_fails_closed(
    fstype: str | None,
) -> None:
    with pytest.raises(ReceiptStoreUnavailable):
        DurableReceiptStore._assert_supported_linux_filesystem(fstype)


def test_lock_unavailable_is_a_receipt_store_error() -> None:
    assert issubclass(ReceiptStoreUnavailable, ReceiptStoreError)


@pytest.mark.skipif(os.name != "nt", reason="Windows drive classification")
def test_windows_mapped_drive_fails_before_creating_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DurableReceiptStore(tmp_path / "receipts")
    monkeypatch.setattr(store, "_windows_drive_type", lambda _root: 4)

    with (
        pytest.raises(ReceiptStoreUnavailable, match="remote"),
        store._mutex(_plan().digest, "window-1", ReceiptOperation.CANDIDATE),
    ):
        pass

    assert not store.root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-path semantics")
def test_windows_long_path_lock(tmp_path: Path) -> None:
    plan = _plan()
    # Keep the store root below MAX_PATH (so mkdir/glob work) but push each
    # content/lock file path beyond it, forcing the Windows extended-path
    # (``\\?\``) branch inside ``_native_path``.
    target_root_length = 230
    padding = target_root_length - len(str(tmp_path.absolute())) - 1
    assert 1 <= padding < 255
    long_segment = "x" * padding
    store_root = tmp_path / long_segment
    store_root.mkdir(parents=True, exist_ok=True)
    assert len(str(store_root.absolute())) == target_root_length
    store = DurableReceiptStore(store_root)
    lock_path = _lock_file(store, plan, "window-long", ReceiptOperation.CANDIDATE)
    assert len(str(lock_path)) > 260
    receipt = store.start(
        plan=plan, execution_id="window-long", operation=ReceiptOperation.CANDIDATE
    )
    assert receipt.stage is ReceiptStageV2.PLANNED
    store.advance(receipt, ReceiptStageV2.PREFLIGHT_COMPLETED, safety_state=SafetyState.PREFLIGHT)


def test_lock_capability_probe_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DurableReceiptStore(tmp_path / "receipts")
    plan = _plan()

    def fail_probe(self: DurableReceiptStore, path: Path) -> None:
        raise ReceiptStoreUnavailable("simulated probe failure")

    monkeypatch.setattr(DurableReceiptStore, "_verify_lock_exclusion", fail_probe)

    with pytest.raises(ReceiptStoreUnavailable, match="probe"):
        store.start(plan=plan, execution_id="window-1", operation=ReceiptOperation.CANDIDATE)

    assert not list((tmp_path / "receipts").glob("*.json"))
    assert not list((tmp_path / "receipts").glob("*.current.json"))
