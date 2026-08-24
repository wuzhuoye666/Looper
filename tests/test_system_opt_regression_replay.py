"""G6 independent L6c evidence replay verifier tests.

The verifier proves internal consistency and association integrity only; it
does not establish evidence authenticity (see the ``regression_evidence``
module docstring).  These tests drive the CLI to publish real evidence, then
tamper with files at the JSON level and re-bind the fixed index so the
verifier's association checks (not just its digest checks) are exercised.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from looper_api.cli import app
from looper_core.system_opt.rollback.regression import RegressionRecoveryOutcome
from looper_core.system_opt.rollback.regression_evidence import (
    RegressionRecoveryEvidenceVerificationError,
    verify_regression_recovery_evidence,
)
from test_system_opt_regression_cli import _argv, _inputs, runner

INDEX_NAME = "regression-recovery-evidence-index.json"


def _run(tmp_path: Path, *, triggered: bool = True) -> tuple[Path, Path]:
    paths = _inputs(tmp_path, triggered=triggered)
    evidence = tmp_path / "evidence"
    output = tmp_path / "out.json"
    result = runner.invoke(app, _argv(tmp_path, paths, output, evidence))
    assert result.exit_code == 0, result.output
    return evidence, output


def _index_payload(evidence: Path) -> dict:
    return json.loads((evidence / INDEX_NAME).read_text(encoding="utf-8"))


def _write_index(evidence: Path, payload: dict) -> None:
    (evidence / INDEX_NAME).write_text(json.dumps(payload), encoding="utf-8")


def _load_outcome(evidence: Path) -> RegressionRecoveryOutcome:
    payload = _index_payload(evidence)
    return RegressionRecoveryOutcome.model_validate_json(
        (evidence / payload["outcome_path"]).read_text(encoding="utf-8")
    )


def _reindex_model(evidence: Path, kind: str, model) -> None:
    """Replace a published evidence file with a tampered model and re-bind the index."""
    fields = {
        "request": ("request_digest", "request_path"),
        "outcome": ("outcome_digest", "outcome_path"),
        "rollback": ("rollback_record_digest", "rollback_record_path"),
    }
    digest_field, path_field = fields[kind]
    payload = _index_payload(evidence)
    old_path = evidence / payload[path_field]
    new_digest = model.digest
    new_name = f"{kind}-{new_digest.removeprefix('sha256:')}.json"
    (evidence / new_name).write_text(model.model_dump_json(indent=2), encoding="utf-8")
    if old_path.name != new_name and old_path.exists():
        old_path.unlink()
    payload[digest_field] = new_digest
    payload[path_field] = new_name
    _write_index(evidence, payload)


def test_not_triggered_graph_replays_and_is_stable(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path, triggered=False)
    first = verify_regression_recovery_evidence(evidence)
    second = verify_regression_recovery_evidence(evidence)
    assert first.rollback_record_digest is None
    assert first.digest == second.digest


def test_triggered_graph_replays_and_ignores_unrelated_files(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path)
    (evidence / "output-convenience.json").write_text("{}", encoding="utf-8")
    result = verify_regression_recovery_evidence(evidence)
    assert result.rollback_present is True


def test_missing_index_fails_closed(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path)
    (evidence / INDEX_NAME).unlink()
    with pytest.raises(RegressionRecoveryEvidenceVerificationError, match="index"):
        verify_regression_recovery_evidence(evidence)


def test_wrong_index_schema_fails_closed(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path)
    payload = _index_payload(evidence)
    payload["schema_version"] = "looper.regression-recovery-evidence-index/v9"
    _write_index(evidence, payload)
    with pytest.raises(RegressionRecoveryEvidenceVerificationError, match="index"):
        verify_regression_recovery_evidence(evidence)


@pytest.mark.parametrize(
    "path_field",
    ["request_path", "outcome_path", "rollback_record_path"],
)
def test_missing_evidence_file_fails_closed(tmp_path: Path, path_field: str) -> None:
    evidence, _ = _run(tmp_path)
    payload = _index_payload(evidence)
    (evidence / payload[path_field]).unlink()
    with pytest.raises(RegressionRecoveryEvidenceVerificationError, match="missing"):
        verify_regression_recovery_evidence(evidence)


@pytest.mark.parametrize(
    ("path_field", "tamper_key", "tamper_value"),
    [
        ("request_path", "regression_threshold", 0.5),
        ("outcome_path", "reason", "tampered reason"),
        ("rollback_record_path", "note", "tampered note"),
    ],
)
def test_tampered_evidence_file_fails_digest_check(
    tmp_path: Path,
    path_field: str,
    tamper_key: str,
    tamper_value: object,
) -> None:
    evidence, _ = _run(tmp_path)
    payload = _index_payload(evidence)
    path = evidence / payload[path_field]
    content = json.loads(path.read_text(encoding="utf-8"))
    content[tamper_key] = tamper_value
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(RegressionRecoveryEvidenceVerificationError, match="digest"):
        verify_regression_recovery_evidence(evidence)


def test_index_digest_filename_mismatch_fails_closed(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path)
    payload = _index_payload(evidence)
    payload["request_path"] = "request-" + "d" * 64 + ".json"
    _write_index(evidence, payload)
    with pytest.raises(RegressionRecoveryEvidenceVerificationError, match="index"):
        verify_regression_recovery_evidence(evidence)


@pytest.mark.parametrize(
    "request_path",
    [
        "../outside.json",
        "E:\\tmp\\request-" + "e" * 64 + ".json",
        "sub/request-" + "e" * 64 + ".json",
    ],
)
def test_path_traversal_index_fails_closed(tmp_path: Path, request_path: str) -> None:
    evidence, _ = _run(tmp_path)
    payload = _index_payload(evidence)
    payload["request_path"] = request_path
    _write_index(evidence, payload)
    with pytest.raises(RegressionRecoveryEvidenceVerificationError):
        verify_regression_recovery_evidence(evidence)


@pytest.mark.parametrize(
    "filename",
    [
        "request-" + "a" * 64 + ".json",
        "outcome-" + "b" * 64 + ".json",
        "rollback-" + "c" * 64 + ".json",
    ],
)
def test_orphan_known_prefix_fails_closed(tmp_path: Path, filename: str) -> None:
    evidence, _ = _run(tmp_path)
    (evidence / filename).write_text("{}", encoding="utf-8")
    with pytest.raises(RegressionRecoveryEvidenceVerificationError, match="orphan"):
        verify_regression_recovery_evidence(evidence)


def test_malformed_known_prefix_fails_closed(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path)
    (evidence / "request-nothex.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RegressionRecoveryEvidenceVerificationError, match="malformed"):
        verify_regression_recovery_evidence(evidence)


def test_unregistered_rollback_fails_closed(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path)
    payload = _index_payload(evidence)
    payload["rollback_record_digest"] = None
    payload["rollback_record_path"] = None
    _write_index(evidence, payload)
    with pytest.raises(RegressionRecoveryEvidenceVerificationError, match="rollback"):
        verify_regression_recovery_evidence(evidence)


def test_not_triggered_index_forged_rollback_fails_closed(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path, triggered=False)
    payload = _index_payload(evidence)
    payload["rollback_record_digest"] = "sha256:" + "a" * 64
    payload["rollback_record_path"] = "rollback-" + "a" * 64 + ".json"
    _write_index(evidence, payload)
    with pytest.raises(RegressionRecoveryEvidenceVerificationError, match="rollback"):
        verify_regression_recovery_evidence(evidence)


def test_forged_outcome_request_binding_fails_closed(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path)
    outcome = _load_outcome(evidence)
    forged = outcome.model_copy(update={"request_digest": "sha256:" + "f" * 64})
    _reindex_model(evidence, "outcome", forged)
    with pytest.raises(
        RegressionRecoveryEvidenceVerificationError,
        match="forged outcome request binding",
    ):
        verify_regression_recovery_evidence(evidence)


def test_forged_execution_request_binding_fails_closed(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path)
    outcome = _load_outcome(evidence)
    assert outcome.execution_evidence is not None
    forged_execution = outcome.execution_evidence.model_copy(
        update={"request_digest": "sha256:" + "f" * 64}
    )
    forged = outcome.model_copy(update={"execution_evidence": forged_execution})
    _reindex_model(evidence, "outcome", forged)
    with pytest.raises(
        RegressionRecoveryEvidenceVerificationError,
        match="forged execution request binding",
    ):
        verify_regression_recovery_evidence(evidence)


def test_forged_rollback_target_binding_fails_closed(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path)
    outcome = _load_outcome(evidence)
    assert outcome.rollback_record is not None
    forged_rollback = outcome.rollback_record.model_copy(update={"target_id": "forged"})
    forged_outcome = outcome.model_copy(update={"rollback_record": forged_rollback})
    _reindex_model(evidence, "outcome", forged_outcome)
    _reindex_model(evidence, "rollback", forged_rollback)
    with pytest.raises(
        RegressionRecoveryEvidenceVerificationError,
        match="forged rollback target binding",
    ):
        verify_regression_recovery_evidence(evidence)


def test_forged_rollback_threshold_binding_fails_closed(tmp_path: Path) -> None:
    evidence, _ = _run(tmp_path)
    outcome = _load_outcome(evidence)
    assert outcome.rollback_record is not None
    forged_rollback = outcome.rollback_record.model_copy(update={"regression_threshold": 0.99})
    forged_outcome = outcome.model_copy(update={"rollback_record": forged_rollback})
    _reindex_model(evidence, "outcome", forged_outcome)
    _reindex_model(evidence, "rollback", forged_rollback)
    with pytest.raises(
        RegressionRecoveryEvidenceVerificationError,
        match="forged rollback threshold binding",
    ):
        verify_regression_recovery_evidence(evidence)
