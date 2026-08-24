"""G5-R2 CLI evidence graph and path guard tests."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import looper_api.cli as cli_module
import pytest
import yaml
from looper_api.cli import _current_environment_digest, app
from looper_core.canonical import canonical_json
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.dynamic_demo import build_demo_initial_state
from looper_core.system_opt.executor import ConfigSnapshot, SnapshotEntry
from looper_core.system_opt.result_vector import GeneralResultVector, PromotionEvidence
from looper_core.system_opt.rollback.regression import LastGoodCheckpoint, RegressionRecoveryRequest
from looper_core.system_opt.state_evidence import (
    STATE_EVIDENCE_SCHEMA,
    ConfigStateRecord,
    ConfigurationStateEvidence,
    OwnershipDisposition,
    PersistenceDisposition,
    StateSource,
)
from typer.testing import CliRunner

runner = CliRunner()
TARGET = "demo-dynamic-target"
ENV = _current_environment_digest()


def _inputs(root: Path, *, triggered: bool = True) -> dict[str, Path]:
    manifest = build_demo_manifest()
    initial = build_demo_initial_state() | {"storage-scheduler": "mq-deadline"}
    source = StateSource(
        kind="user-declaration", locator="g5r://state",
        content_sha256=hashlib.sha256(b"g5r://state").hexdigest(), line=1, raw_value=None,
    )
    state = ConfigurationStateEvidence(
        schema_version=STATE_EVIDENCE_SCHEMA, target_id=TARGET,
        manifest_digest=manifest.digest, environment_digest=ENV,
        collected_at=datetime(2026, 8, 24, tzinfo=UTC), source_scope=["g5r://state"],
        assignments=[], records=[
            ConfigStateRecord(
                item_id=i.id, parameter_id=i.parameter_id,
                persistence=PersistenceDisposition.UNKNOWN, persistent_value=None,
                ownership=OwnershipDisposition.UNOWNED, owner_id=None, pinned=False,
                sources=[source], reason="G5-R2 fixture",
            ) for i in manifest.items
        ], counting_basis="one unowned record per item",
    )
    snapshot = ConfigSnapshot(target_id=TARGET, entries={
        i.id: SnapshotEntry(
            item_id=i.id, target=i.target, status="succeeded",
            value=initial[i.id], raw_output=canonical_json(initial[i.id]),
        ) for i in manifest.items
    })
    vector = GeneralResultVector(
        candidate_id="g5-last-good", u_cpu=.5, u_memory=.5, u_storage=.5,
        u_network=.5, u_stability=.5, u_regression=.1 if triggered else .9,
        normalization_digest=ENV,
    )
    promotion = PromotionEvidence(
        candidate_id="g5-last-good", promoted=True, reason="fixture",
        observation_count=3, distinct_time_blocks=2, distinct_environments=1,
        failed_observations=[],
    )
    request = RegressionRecoveryRequest(
        checkpoint=LastGoodCheckpoint(
            target_id=TARGET, candidate_id="g5-last-good", snapshot=snapshot,
            promotion_evidence=promotion,
            validated_vector=vector.model_copy(update={"u_regression": .8}),
            recorded_at=datetime(2026, 8, 24, tzinfo=UTC),
        ),
        current_vector=vector, regression_threshold=.3,
        trigger_evidence_digests=["sha256:" + "b" * 64],
        evaluated_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    paths = {
        "request": root / "request.json", "manifest": root / "manifest.yaml",
        "state": root / "state.json", "initial": root / "initial.json",
    }
    paths["request"].write_text(request.model_dump_json(indent=2), encoding="utf-8")
    paths["manifest"].write_text(yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8")
    paths["state"].write_text(state.model_dump_json(indent=2), encoding="utf-8")
    paths["initial"].write_text(json.dumps(initial), encoding="utf-8")
    return paths


def _argv(root: Path, p: dict[str, Path], output: Path, evidence: Path) -> list[str]:
    return [
        "system-opt", "regression-recovery", "--request", str(p["request"]),
        "--manifest", str(p["manifest"]), "--state-evidence", str(p["state"]),
        "--backend", "simulated", "--initial-state", str(p["initial"]),
        "--target-id", TARGET, "--owner-id", "owner", "--lease-root", str(root / "leases"),
        "--lease-ttl-seconds", "60", "--evidence-dir", str(evidence), "--output", str(output),
    ]


def test_path_guard_rejects_output_request_before_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _inputs(tmp_path)
    called = False
    original = cli_module._ensure_distinct_paths
    def probe(paths):
        nonlocal called
        called = True
        return original(paths)
    monkeypatch.setattr(cli_module, "_ensure_distinct_paths", probe)
    result = runner.invoke(app, _argv(tmp_path, p, p["request"], tmp_path / "evidence"))
    assert result.exit_code != 0
    assert called is True
    assert "Invalid value" in result.output
    assert "path collision" in result.output
    assert not list((tmp_path / "leases").glob("*.lease.json"))
    assert not (tmp_path / "evidence").exists()


def test_path_guard_rejects_output_inside_evidence_before_lease(tmp_path: Path) -> None:
    p = _inputs(tmp_path)
    evidence = tmp_path / "evidence"
    result = runner.invoke(app, _argv(tmp_path, p, evidence / "out.json", evidence))
    assert result.exit_code != 0
    assert "evidence root" in result.output
    assert not list((tmp_path / "leases").glob("*.lease.json"))


def test_valid_not_triggered_publishes_graph_without_backend_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _inputs(tmp_path, triggered=False)
    evidence, output = tmp_path / "evidence", tmp_path / "out.json"
    monkeypatch.setattr(
        cli_module.SimulatedBackend,
        "apply",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("write")),
    )
    result = runner.invoke(app, _argv(tmp_path, p, output, evidence))
    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text("utf-8"))
    assert payload["status"] == "not-triggered" and payload["stop_required"] is False
    index = json.loads((evidence / "regression-recovery-evidence-index.json").read_text("utf-8"))
    assert index["rollback_record_digest"] is None
    assert not list((tmp_path / "leases").glob("*.lease.json"))


def test_triggered_publishes_content_addressed_graph_and_releases_lease(tmp_path: Path) -> None:
    p = _inputs(tmp_path, triggered=True)
    evidence, output = tmp_path / "evidence", tmp_path / "out.json"
    result = runner.invoke(app, _argv(tmp_path, p, output, evidence))
    assert result.exit_code == 0, result.output
    index = json.loads((evidence / "regression-recovery-evidence-index.json").read_text("utf-8"))
    assert index["request_digest"].startswith("sha256:")
    assert index["outcome_digest"].startswith("sha256:")
    assert index["rollback_record_digest"].startswith("sha256:")
    assert (evidence / index["request_path"]).is_file()
    assert (evidence / index["outcome_path"]).is_file()
    assert (evidence / index["rollback_record_path"]).is_file()
    assert json.loads(output.read_text("utf-8"))["status"] == "restored"
    assert not list((tmp_path / "leases").glob("*.lease.json"))
