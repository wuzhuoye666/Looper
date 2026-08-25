"""DYN-END-01I R2: CLI negative tests for the window-budget single source.

N1: --max-windows 0 / -1 rejected at the typer layer.
N5: a v1alpha3 session must not accept --max-windows.
N6: a v1alpha1/v1alpha2 session must require --max-windows.
"""

from __future__ import annotations

import json
from pathlib import Path

from looper_core.system_opt.dynamic_demo import build_dynamic_demo_session
from looper_core.system_opt.phase_gate import DYNAMIC_PHASE_GATE_V3_SCHEMA
from test_system_opt_dynamic_cli import (
    _upgrade_session_to_v2,
    _write_demo_inputs,
    app,
    runner,
)


def _upgrade_session_to_v3(session: Path) -> None:
    _upgrade_session_to_v2(session)
    gate_path = session / "gate-contract.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["schema_version"] = DYNAMIC_PHASE_GATE_V3_SCHEMA
    gate["budget"]["max_windows"] = 6
    gate_path.write_text(json.dumps(gate, indent=2), encoding="utf-8")


def _argv(tmp_path: Path, session: Path, *, max_windows: str | None) -> list[str]:
    inputs = _write_demo_inputs(tmp_path)
    argv = [
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
        str(tmp_path / "leases"),
        "--lease-ttl-seconds",
        "7200",
        "--probe-top-k",
        "2",
        "--verification-windows",
        "0",
        "--output",
        str(tmp_path / "out.json"),
    ]
    if max_windows is not None:
        argv += ["--max-windows", max_windows]
    return argv


class TestMaxWindowsCLI:
    def test_zero_is_rejected_by_typer(self, tmp_path: Path) -> None:
        session = tmp_path / "session"
        build_dynamic_demo_session(session)
        result = runner.invoke(app, _argv(tmp_path, session, max_windows="0"))
        assert result.exit_code != 0
        assert "--max-windows" in result.output

    def test_negative_is_rejected_by_typer(self, tmp_path: Path) -> None:
        session = tmp_path / "session"
        build_dynamic_demo_session(session)
        result = runner.invoke(app, _argv(tmp_path, session, max_windows="-1"))
        assert result.exit_code != 0
        assert "--max-windows" in result.output

    def test_v3_session_rejects_max_windows(self, tmp_path: Path) -> None:
        session = tmp_path / "session"
        build_dynamic_demo_session(session)
        _upgrade_session_to_v3(session)
        result = runner.invoke(app, _argv(tmp_path, session, max_windows="6"))
        assert result.exit_code != 0
        assert "forbidden for v1alpha3" in result.output

    def test_v1_session_requires_max_windows(self, tmp_path: Path) -> None:
        session = tmp_path / "session"
        build_dynamic_demo_session(session)
        result = runner.invoke(app, _argv(tmp_path, session, max_windows=None))
        assert result.exit_code != 0
        assert "required for v1alpha1/v1alpha2" in result.output
