"""dynamic-reactivate CLI tests (D5: between-phase eligibility, never auto-restart).

Feeds the simulated demo run (target-met stop, trailing windows healthy) and an
operator-declared SLO-persistence scenario through the same command path.
Self-contained mirror of the demo input helpers (tests is not a package).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from looper_api.cli import _current_environment_digest, app
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.dynamic_demo import (
    build_demo_initial_state,
    build_dynamic_demo_session,
)
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


def _write_demo_inputs(root: Path) -> dict[str, Path]:
    manifest = build_demo_manifest()
    manifest_path = root / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), allow_unicode=True),
        encoding="utf-8",
    )
    source = StateSource(
        kind="user-declaration",
        locator="demo://dynamic-session",
        content_sha256=hashlib.sha256(b"demo://dynamic-session").hexdigest(),
        line=1,
        raw_value=None,
    )
    evidence = ConfigurationStateEvidence(
        schema_version=STATE_EVIDENCE_SCHEMA,
        target_id="demo-dynamic-target",
        manifest_digest=manifest.digest,
        environment_digest=_current_environment_digest(),
        collected_at=datetime(2026, 8, 24, tzinfo=UTC),
        source_scope=["demo://dynamic-session"],
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
                reason="simulated demo session: operator verified no external writer",
            )
            for item in manifest.items
        ],
        counting_basis="one UNOWNED record per demo manifest item",
    )
    evidence_path = root / "state-evidence.json"
    evidence_path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
    initial_path = root / "initial-state.json"
    initial_path.write_text(
        json.dumps(build_demo_initial_state(), indent=2), encoding="utf-8"
    )
    return {"manifest": manifest_path, "evidence": evidence_path, "initial": initial_path}


def _run_demo_dynamic_phase(session: Path, inputs: dict[str, Path], root: Path) -> Path:
    output = root / "dynamic-run.json"
    result = runner.invoke(
        app,
        [
            "system-opt",
            "dynamic-run",
            "--session",
            str(session),
            "--manifest",
            str(inputs["manifest"]),
            "--state-evidence",
            str(inputs["evidence"]),
            "--backend",
            "simulated",
            "--initial-state",
            str(inputs["initial"]),
            "--target-id",
            "demo-dynamic-target",
            "--owner-id",
            "demo-owner",
            "--lease-root",
            str(root / "leases"),
            "--lease-ttl-seconds",
            "7200",
            "--max-windows",
            "6",
            "--probe-top-k",
            "2",
            "--verification-windows",
            "2",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    return output


def test_reactivate_derives_trailing_violations_and_stays_ineligible(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session"
    build_dynamic_demo_session(session)
    inputs = _write_demo_inputs(tmp_path)
    run_path = _run_demo_dynamic_phase(session, inputs, tmp_path)

    output = tmp_path / "reactivation.json"
    result = runner.invoke(
        app,
        [
            "system-opt",
            "dynamic-reactivate",
            "--run",
            str(run_path),
            "--gate-contract",
            str(session / "gate-contract.json"),
            "--max-reactivations",
            "1",
            "--slo-violation-windows",
            "2",
            "--windows-since-stop",
            "5",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output

    decision = json.loads(output.read_text(encoding="utf-8"))
    # The demo stopped target-met: its trailing windows are healthy, so the
    # derived violation count is 0 and no trigger fires.
    assert decision["eligible"] is False
    assert decision["trigger"] is None


def test_reactivate_eligible_via_operator_declared_slo_persistence(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session"
    build_dynamic_demo_session(session)
    inputs = _write_demo_inputs(tmp_path)
    run_path = _run_demo_dynamic_phase(session, inputs, tmp_path)

    output = tmp_path / "reactivation.json"
    result = runner.invoke(
        app,
        [
            "system-opt",
            "dynamic-reactivate",
            "--run",
            str(run_path),
            "--gate-contract",
            str(session / "gate-contract.json"),
            "--max-reactivations",
            "1",
            "--slo-violation-windows",
            "2",
            "--windows-since-stop",
            "5",
            "--consecutive-slo-violations",
            "3",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output

    decision = json.loads(output.read_text(encoding="utf-8"))
    assert decision["eligible"] is True
    assert decision["trigger"] == "slo-violation-persistence"


def test_reactivate_rejects_a_gate_contract_from_another_run(tmp_path: Path) -> None:
    session = tmp_path / "session"
    build_dynamic_demo_session(session)
    inputs = _write_demo_inputs(tmp_path)
    run_path = _run_demo_dynamic_phase(session, inputs, tmp_path)
    # Tamper with the gate contract so its digest no longer matches the run.
    gate_path = tmp_path / "foreign-gate-contract.json"
    payload = json.loads((session / "gate-contract.json").read_text(encoding="utf-8"))
    payload["reactivation_holdout_windows"] = 99
    gate_path.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "system-opt",
            "dynamic-reactivate",
            "--run",
            str(run_path),
            "--gate-contract",
            str(gate_path),
            "--max-reactivations",
            "1",
            "--slo-violation-windows",
            "2",
            "--windows-since-stop",
            "5",
            "--output",
            str(tmp_path / "out.json"),
        ],
    )
    assert result.exit_code != 0
    # The rich error box wraps lines, so assert on an unbroken fragment.
    assert "not gated by this gate contract" in result.output
