"""G5 CLI evidence graph, failure lifecycle, and path-guard tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import looper_api.cli as cli_module
import pytest
import yaml
from looper_api.cli import _current_environment_digest, app
from looper_core.canonical import canonical_json
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.dynamic_demo import build_demo_initial_state
from looper_core.system_opt.executor import ConfigSnapshot, SnapshotEntry
from looper_core.system_opt.executor.simulated import SimulatedFailurePlan
from looper_core.system_opt.lease import TargetAttention
from looper_core.system_opt.result_vector import GeneralResultVector, PromotionEvidence
from looper_core.system_opt.rollback import RollbackRecord
from looper_core.system_opt.rollback.regression import (
    LastGoodCheckpoint,
    RegressionRecoveryOutcome,
    RegressionRecoveryRequest,
    RegressionRecoveryStatus,
)
from looper_core.system_opt.rollback.regression_evidence import (
    RegressionRecoveryEvidenceGraph,
    RegressionRecoveryEvidenceIndex,
)
from looper_core.system_opt.state_evidence import (
    STATE_EVIDENCE_SCHEMA,
    ConfigStateRecord,
    ConfigurationStateEvidence,
    OwnershipDisposition,
    PersistenceDisposition,
    StateSource,
)
from pydantic import ValidationError
from typer.testing import CliRunner

runner = CliRunner()
TARGET = "demo-dynamic-target"
ENV = _current_environment_digest()
INDEX_NAME = "regression-recovery-evidence-index.json"


def _inputs(root: Path, *, triggered: bool = True) -> dict[str, Path]:
    manifest = build_demo_manifest()
    initial = build_demo_initial_state() | {"storage-scheduler": "mq-deadline"}
    source = StateSource(
        kind="user-declaration",
        locator="g5r://state",
        content_sha256=hashlib.sha256(b"g5r://state").hexdigest(),
        line=1,
        raw_value=None,
    )
    state = ConfigurationStateEvidence(
        schema_version=STATE_EVIDENCE_SCHEMA,
        target_id=TARGET,
        manifest_digest=manifest.digest,
        environment_digest=ENV,
        collected_at=datetime(2026, 8, 24, tzinfo=UTC),
        source_scope=["g5r://state"],
        assignments=[],
        records=[
            ConfigStateRecord(
                item_id=item.id,
                parameter_id=item.parameter_id,
                persistence=PersistenceDisposition.UNKNOWN,
                persistent_value=None,
                ownership=OwnershipDisposition.UNOWNED,
                owner_id=None,
                pinned=False,
                sources=[source],
                reason="G5 fixture",
            )
            for item in manifest.items
        ],
        counting_basis="one unowned record per item",
    )
    snapshot = ConfigSnapshot(
        target_id=TARGET,
        entries={
            item.id: SnapshotEntry(
                item_id=item.id,
                target=item.target,
                status="succeeded",
                value=initial[item.id],
                raw_output=canonical_json(initial[item.id]),
            )
            for item in manifest.items
        },
    )
    vector = GeneralResultVector(
        candidate_id="g5-last-good",
        u_cpu=0.5,
        u_memory=0.5,
        u_storage=0.5,
        u_network=0.5,
        u_stability=0.5,
        u_regression=0.1 if triggered else 0.9,
        normalization_digest=ENV,
    )
    promotion = PromotionEvidence(
        candidate_id="g5-last-good",
        promoted=True,
        reason="fixture",
        observation_count=3,
        distinct_time_blocks=2,
        distinct_environments=1,
        failed_observations=[],
    )
    request = RegressionRecoveryRequest(
        checkpoint=LastGoodCheckpoint(
            target_id=TARGET,
            candidate_id="g5-last-good",
            snapshot=snapshot,
            promotion_evidence=promotion,
            validated_vector=vector.model_copy(update={"u_regression": 0.8}),
            recorded_at=datetime(2026, 8, 24, tzinfo=UTC),
        ),
        current_vector=vector,
        regression_threshold=0.3,
        trigger_evidence_digests=["sha256:" + "b" * 64],
        evaluated_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    paths = {
        "request": root / "request.json",
        "manifest": root / "manifest.yaml",
        "state": root / "state.json",
        "initial": root / "initial.json",
    }
    paths["request"].write_text(request.model_dump_json(indent=2), encoding="utf-8")
    paths["manifest"].write_text(
        yaml.safe_dump(manifest.model_dump(mode="json")),
        encoding="utf-8",
    )
    paths["state"].write_text(state.model_dump_json(indent=2), encoding="utf-8")
    paths["initial"].write_text(json.dumps(initial), encoding="utf-8")
    return paths


def _argv(
    root: Path,
    paths: dict[str, Path],
    output: Path,
    evidence: Path,
    *,
    target_id: str = TARGET,
) -> list[str]:
    return [
        "system-opt",
        "regression-recovery",
        "--request",
        str(paths["request"]),
        "--manifest",
        str(paths["manifest"]),
        "--state-evidence",
        str(paths["state"]),
        "--backend",
        "simulated",
        "--initial-state",
        str(paths["initial"]),
        "--target-id",
        target_id,
        "--owner-id",
        "owner",
        "--lease-root",
        str(root / "leases"),
        "--lease-ttl-seconds",
        "60",
        "--evidence-dir",
        str(evidence),
        "--output",
        str(output),
    ]


def _assert_lease_released(root: Path) -> None:
    assert not list((root / "leases").glob("*.lease.json"))


def _read_attention(root: Path) -> TargetAttention | None:
    paths = list((root / "leases").glob("*.attention.json"))
    if not paths:
        return None
    assert len(paths) == 1
    return TargetAttention.model_validate_json(paths[0].read_text(encoding="utf-8"))


def _read_graph(
    evidence: Path,
) -> tuple[
    RegressionRecoveryEvidenceGraph,
    RegressionRecoveryRequest,
    RegressionRecoveryOutcome,
]:
    index = RegressionRecoveryEvidenceIndex.model_validate_json(
        (evidence / INDEX_NAME).read_text(encoding="utf-8")
    )
    request = RegressionRecoveryRequest.model_validate_json(
        (evidence / index.request_path).read_text(encoding="utf-8")
    )
    outcome = RegressionRecoveryOutcome.model_validate_json(
        (evidence / index.outcome_path).read_text(encoding="utf-8")
    )
    if index.rollback_record_path is not None:
        rollback = RollbackRecord.model_validate_json(
            (evidence / index.rollback_record_path).read_text(encoding="utf-8")
        )
        assert outcome.rollback_record == rollback
    graph = RegressionRecoveryEvidenceGraph(
        request=request,
        outcome=outcome,
        index=index,
    )
    assert index.request_digest == request.digest
    assert index.outcome_digest == outcome.digest
    return graph, request, outcome


def _inject_l1_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_l1(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected L1 exception")

    monkeypatch.setattr(cli_module.SafetyController, "execute", fail_l1)


def _inject_non_kept_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    backend_class = cli_module.SimulatedBackend

    def build_backend(
        initial_state: dict[str, Any], *, target_id: str
    ) -> cli_module.SimulatedBackend:
        drifted = dict(initial_state)
        drifted["vm-swappiness"] = 10
        return backend_class(
            drifted,
            target_id=target_id,
            failure_plan=SimulatedFailurePlan(
                verify_failures={"vm-swappiness"},
            ),
        )

    monkeypatch.setattr(cli_module, "SimulatedBackend", build_backend)


def _fail_atomic_write(
    monkeypatch: pytest.MonkeyPatch,
    predicate: Callable[[Path], bool],
    message: str,
) -> None:
    real_write = cli_module._write_json_atomic

    def injected_write(path: Path, value: object) -> None:
        if predicate(path):
            raise OSError(message)
        real_write(path, value)

    monkeypatch.setattr(cli_module, "_write_json_atomic", injected_write)


def test_path_guard_rejects_output_request_before_lease(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    evidence = tmp_path / "evidence"

    result = runner.invoke(app, _argv(tmp_path, paths, paths["request"], evidence))

    assert result.exit_code != 0
    assert "path collision" in result.output
    _assert_lease_released(tmp_path)
    assert not evidence.exists()


@pytest.mark.parametrize("path_key", ["request", "initial"])
def test_path_guard_rejects_input_inside_evidence_before_lease(
    tmp_path: Path,
    path_key: str,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    paths = _inputs(tmp_path)
    nested = evidence / paths[path_key].name
    paths[path_key].replace(nested)
    paths[path_key] = nested

    result = runner.invoke(
        app,
        _argv(tmp_path, paths, tmp_path / "out.json", evidence),
    )

    assert result.exit_code != 0
    assert "evidence root" in result.output
    _assert_lease_released(tmp_path)


def test_path_guard_rejects_output_inside_evidence_before_lease(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    evidence = tmp_path / "evidence"

    result = runner.invoke(
        app,
        _argv(tmp_path, paths, evidence / "out.json", evidence),
    )

    assert result.exit_code != 0
    assert "evidence root" in result.output
    _assert_lease_released(tmp_path)


def test_request_target_mismatch_is_rejected_before_lease(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)

    result = runner.invoke(
        app,
        _argv(
            tmp_path,
            paths,
            tmp_path / "out.json",
            tmp_path / "evidence",
            target_id="wrong-target",
        ),
    )

    assert result.exit_code != 0
    assert "request checkpoint target" in result.output
    _assert_lease_released(tmp_path)


def test_local_linux_requires_explicit_confirmation_before_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)
    arguments = _argv(
        tmp_path,
        paths,
        tmp_path / "out.json",
        tmp_path / "evidence",
    )
    arguments[arguments.index("simulated")] = "local-linux"
    monkeypatch.setattr(cli_module.platform, "system", lambda: "Linux")
    confirmation_calls: list[tuple[bool, str]] = []
    real_confirmation = cli_module._require_linux_confirmation

    def record_confirmation(enable_real: bool, confirmation: str) -> None:
        confirmation_calls.append((enable_real, confirmation))
        real_confirmation(enable_real, confirmation)

    monkeypatch.setattr(
        cli_module,
        "_require_linux_confirmation",
        record_confirmation,
    )

    result = runner.invoke(app, arguments)

    assert result.exit_code != 0
    assert confirmation_calls == [(False, "")]
    _assert_lease_released(tmp_path)


def test_not_triggered_publishes_evaluation_without_backend_write_or_attention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path, triggered=False)
    evidence = tmp_path / "evidence"
    output = tmp_path / "out.json"

    def reject_apply(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("not-triggered request attempted a backend write")

    monkeypatch.setattr(cli_module.SimulatedBackend, "apply", reject_apply)
    result = runner.invoke(app, _argv(tmp_path, paths, output, evidence))

    assert result.exit_code == 0, result.output
    graph, _, outcome = _read_graph(evidence)
    assert outcome.status is RegressionRecoveryStatus.NOT_TRIGGERED
    assert outcome.stop_required is False
    assert graph.index.rollback_record_digest is None
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "not-triggered"
    assert _read_attention(tmp_path) is None
    _assert_lease_released(tmp_path)


def test_triggered_restore_publishes_complete_graph_and_releases_lease(
    tmp_path: Path,
) -> None:
    paths = _inputs(tmp_path)
    evidence = tmp_path / "evidence"
    output = tmp_path / "out.json"

    result = runner.invoke(app, _argv(tmp_path, paths, output, evidence))

    assert result.exit_code == 0, result.output
    graph, _, outcome = _read_graph(evidence)
    assert outcome.status is RegressionRecoveryStatus.RESTORED
    assert outcome.rollback_record is not None
    assert graph.index.rollback_record_digest == outcome.rollback_record.digest
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "restored"
    assert _read_attention(tmp_path) is None
    _assert_lease_released(tmp_path)


def test_not_triggered_publication_failure_does_not_mark_target_attention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path, triggered=False)
    evidence = tmp_path / "evidence"
    _fail_atomic_write(
        monkeypatch,
        lambda path: path.name.startswith("outcome-"),
        "injected evaluation publication failure",
    )

    result = runner.invoke(
        app,
        _argv(tmp_path, paths, tmp_path / "out.json", evidence),
    )

    assert result.exit_code != 0
    assert "recovery=not-triggered" in str(result.exception)
    assert _read_attention(tmp_path) is None
    assert not (evidence / INDEX_NAME).exists()
    _assert_lease_released(tmp_path)


def test_l1_exception_publishes_needs_attention_graph_and_original_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)
    evidence = tmp_path / "evidence"
    _inject_l1_exception(monkeypatch)

    result = runner.invoke(
        app,
        _argv(tmp_path, paths, tmp_path / "out.json", evidence),
    )

    assert result.exit_code != 0
    _, _, outcome = _read_graph(evidence)
    assert outcome.status is RegressionRecoveryStatus.NEEDS_ATTENTION
    assert "injected L1 exception" in outcome.reason
    attention = _read_attention(tmp_path)
    assert attention is not None
    assert attention.evidence_digest == outcome.digest
    assert "injected L1 exception" in attention.reason
    _assert_lease_released(tmp_path)


def test_non_kept_recovery_publishes_attention_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)
    evidence = tmp_path / "evidence"
    _inject_non_kept_backend(monkeypatch)

    result = runner.invoke(
        app,
        _argv(tmp_path, paths, tmp_path / "out.json", evidence),
    )

    assert result.exit_code != 0
    _, _, outcome = _read_graph(evidence)
    assert outcome.status is RegressionRecoveryStatus.NEEDS_ATTENTION
    assert "did not keep last-good state" in outcome.reason
    attention = _read_attention(tmp_path)
    assert attention is not None
    assert attention.evidence_digest == outcome.digest
    assert outcome.reason in attention.reason
    _assert_lease_released(tmp_path)


def test_unexpected_recovery_exception_marks_request_attention_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)
    request = RegressionRecoveryRequest.model_validate_json(
        paths["request"].read_text(encoding="utf-8")
    )

    def fail_recovery(*args: Any, **kwargs: Any) -> Any:
        raise LookupError("injected outer recovery failure")

    monkeypatch.setattr(cli_module, "execute_regression_recovery", fail_recovery)
    result = runner.invoke(
        app,
        _argv(
            tmp_path,
            paths,
            tmp_path / "out.json",
            tmp_path / "evidence",
        ),
    )

    assert result.exit_code != 0
    assert "injected outer recovery failure" in str(result.exception)
    attention = _read_attention(tmp_path)
    assert attention is not None
    assert attention.evidence_digest == request.digest
    assert "injected outer recovery failure" in attention.reason
    assert not (tmp_path / "evidence" / INDEX_NAME).exists()
    _assert_lease_released(tmp_path)


@pytest.mark.parametrize("failed_name", ["outcome", "index"])
def test_restored_evidence_publication_failure_marks_request_attention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_name: str,
) -> None:
    paths = _inputs(tmp_path)
    request = RegressionRecoveryRequest.model_validate_json(
        paths["request"].read_text(encoding="utf-8")
    )
    evidence = tmp_path / "evidence"

    def should_fail(path: Path) -> bool:
        if failed_name == "index":
            return path.name == INDEX_NAME
        return path.name.startswith("outcome-")

    _fail_atomic_write(
        monkeypatch,
        should_fail,
        f"injected {failed_name} publication failure",
    )

    result = runner.invoke(
        app,
        _argv(tmp_path, paths, tmp_path / "out.json", evidence),
    )

    assert result.exit_code != 0
    assert f"injected {failed_name} publication failure" in str(result.exception)
    attention = _read_attention(tmp_path)
    assert attention is not None
    assert attention.evidence_digest == request.digest
    assert "recovery=restored" in attention.reason
    assert not (evidence / INDEX_NAME).exists()
    _assert_lease_released(tmp_path)


def test_needs_attention_publication_failure_preserves_both_contexts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)
    request = RegressionRecoveryRequest.model_validate_json(
        paths["request"].read_text(encoding="utf-8")
    )
    evidence = tmp_path / "evidence"
    _inject_l1_exception(monkeypatch)
    _fail_atomic_write(
        monkeypatch,
        lambda path: path.name.startswith("outcome-"),
        "injected needs-attention publication failure",
    )

    result = runner.invoke(
        app,
        _argv(tmp_path, paths, tmp_path / "out.json", evidence),
    )

    assert result.exit_code != 0
    attention = _read_attention(tmp_path)
    assert attention is not None
    assert attention.evidence_digest == request.digest
    assert "recovery=needs-attention" in attention.reason
    assert "injected L1 exception" in attention.reason
    assert "injected needs-attention publication failure" in attention.reason
    assert not (evidence / INDEX_NAME).exists()
    _assert_lease_released(tmp_path)


def test_attention_write_failure_keeps_graph_and_original_recovery_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)
    evidence = tmp_path / "evidence"
    _inject_l1_exception(monkeypatch)

    def fail_attention(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected attention write failure")

    monkeypatch.setattr(
        cli_module.FileTargetGuard,
        "mark_needs_attention",
        fail_attention,
    )
    result = runner.invoke(
        app,
        _argv(tmp_path, paths, tmp_path / "out.json", evidence),
    )

    assert result.exit_code != 0
    _, _, outcome = _read_graph(evidence)
    assert outcome.status is RegressionRecoveryStatus.NEEDS_ATTENTION
    error = str(result.exception)
    assert "injected L1 exception" in error
    assert "injected attention write failure" in error
    assert _read_attention(tmp_path) is None
    _assert_lease_released(tmp_path)


def test_restored_output_copy_failure_keeps_complete_graph_without_attention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)
    evidence = tmp_path / "evidence"
    output = tmp_path / "out.json"
    _fail_atomic_write(
        monkeypatch,
        lambda path: path.resolve() == output.resolve(),
        "injected output copy failure",
    )

    result = runner.invoke(app, _argv(tmp_path, paths, output, evidence))

    assert result.exit_code != 0
    _, _, outcome = _read_graph(evidence)
    assert outcome.status is RegressionRecoveryStatus.RESTORED
    assert "injected output copy failure" in str(result.exception)
    assert _read_attention(tmp_path) is None
    _assert_lease_released(tmp_path)


def test_needs_attention_output_copy_failure_records_published_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)
    evidence = tmp_path / "evidence"
    output = tmp_path / "out.json"
    _inject_l1_exception(monkeypatch)
    _fail_atomic_write(
        monkeypatch,
        lambda path: path.resolve() == output.resolve(),
        "injected output copy failure",
    )

    result = runner.invoke(app, _argv(tmp_path, paths, output, evidence))

    assert result.exit_code != 0
    _, _, outcome = _read_graph(evidence)
    attention = _read_attention(tmp_path)
    assert attention is not None
    assert attention.evidence_digest == outcome.digest
    assert "injected L1 exception" in attention.reason
    assert "injected output copy failure" in attention.reason
    _assert_lease_released(tmp_path)


def test_forged_outcome_association_is_rejected_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)
    request = RegressionRecoveryRequest.model_validate_json(
        paths["request"].read_text(encoding="utf-8")
    )
    real_execute = cli_module.execute_regression_recovery

    def forge_outcome(*args: Any, **kwargs: Any) -> RegressionRecoveryOutcome:
        outcome = real_execute(*args, **kwargs)
        return outcome.model_copy(update={"request_digest": "sha256:" + "f" * 64})

    monkeypatch.setattr(cli_module, "execute_regression_recovery", forge_outcome)
    evidence = tmp_path / "evidence"
    result = runner.invoke(
        app,
        _argv(tmp_path, paths, tmp_path / "out.json", evidence),
    )

    assert result.exit_code != 0
    assert "forged outcome request binding" in str(result.exception)
    attention = _read_attention(tmp_path)
    assert attention is not None
    assert attention.evidence_digest == request.digest
    assert not list(evidence.glob("*.json"))
    _assert_lease_released(tmp_path)


def test_forged_rollback_target_is_rejected_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)
    real_execute = cli_module.execute_regression_recovery

    def forge_rollback(*args: Any, **kwargs: Any) -> RegressionRecoveryOutcome:
        outcome = real_execute(*args, **kwargs)
        assert outcome.rollback_record is not None
        rollback = outcome.rollback_record.model_copy(update={"target_id": "forged"})
        return outcome.model_copy(update={"rollback_record": rollback})

    monkeypatch.setattr(cli_module, "execute_regression_recovery", forge_rollback)
    evidence = tmp_path / "evidence"
    result = runner.invoke(
        app,
        _argv(tmp_path, paths, tmp_path / "out.json", evidence),
    )

    assert result.exit_code != 0
    assert "forged rollback target binding" in str(result.exception)
    assert not list(evidence.glob("*.json"))
    _assert_lease_released(tmp_path)


def test_lease_release_failure_preserves_primary_error_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _inputs(tmp_path)

    def fail_recovery(*args: Any, **kwargs: Any) -> Any:
        raise LookupError("injected primary recovery failure")

    real_release = cli_module.FileTargetGuard.release

    def release_then_fail(
        guard: cli_module.FileTargetGuard,
        lease: Any,
    ) -> None:
        real_release(guard, lease)
        raise OSError("injected lease release reporting failure")

    monkeypatch.setattr(cli_module, "execute_regression_recovery", fail_recovery)
    monkeypatch.setattr(cli_module.FileTargetGuard, "release", release_then_fail)
    result = runner.invoke(
        app,
        _argv(
            tmp_path,
            paths,
            tmp_path / "out.json",
            tmp_path / "evidence",
        ),
    )

    assert result.exit_code != 0
    error = str(result.exception)
    assert "injected primary recovery failure" in error
    assert "injected lease release reporting failure" in error
    _assert_lease_released(tmp_path)


def test_evidence_index_rejects_digest_path_mismatch() -> None:
    with pytest.raises(ValidationError, match="request evidence path"):
        RegressionRecoveryEvidenceIndex(
            request_digest="sha256:" + "a" * 64,
            outcome_digest="sha256:" + "b" * 64,
            request_path="request-" + "c" * 64 + ".json",
            outcome_path="outcome-" + "b" * 64 + ".json",
        )
