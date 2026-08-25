from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from looper_api.cli import app
from looper_core.system_opt.negative_cache import (
    NegativeCache,
    NegativeCacheEntry,
    NegativeCacheIdentity,
    NegativeVerdict,
    candidate_parameters_digest,
    formula_versions_digest,
)
from typer.testing import CliRunner

runner = CliRunner()


def test_engine_demo_runs_multi_component_loop(tmp_path: Path) -> None:
    output = tmp_path / "engine-demo.json"
    result = runner.invoke(
        app, ["system-opt", "engine-demo", "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["evidence_kind"] == "synthetic"
    assert summary["stop_reason"] == "completed-all-components"
    assert {round["component"] for round in summary["rounds"]} == {"cpu", "memory"}
    assert all(round["verdicts"] > 0 for round in summary["rounds"])
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["environment_digest"].startswith("sha256:")
    assert len(payload["rounds"]) == 2


def test_engine_demo_respects_round_budget(tmp_path: Path) -> None:
    output = tmp_path / "engine-demo-budget.json"
    result = runner.invoke(
        app,
        ["system-opt", "engine-demo", "--output", str(output), "--max-rounds", "1"],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["stop_reason"] == "round-budget-exhausted"
    assert len(summary["rounds"]) == 1


def test_cache_inspect_summarizes_verdicts(tmp_path: Path) -> None:
    cache_path = tmp_path / "negcache.jsonl"
    env = "sha256:" + "1" * 64
    protocol = "sha256:" + "2" * 64
    formulas = {"F-DEMO": "v1"}
    fixed_at = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    entry = NegativeCacheEntry(
        identity=NegativeCacheIdentity(
            environment_digest=env,
            candidate_parameters_digest=candidate_parameters_digest({"k": "v"}),
            pressure_protocol_digest=protocol,
            formula_versions_digest=formula_versions_digest(formulas),
        ),
        metric_id="cpu.bogo-ops-per-second",
        verdict=NegativeVerdict.NO_IMPROVEMENT_LCB,
        evidence_digests=["sha256:" + "3" * 64],
        detail="LCB95 <= MDE",
        recorded_at=fixed_at,
    )
    NegativeCache().append_to(cache_path, entry)
    result = runner.invoke(
        app, ["system-opt", "cache-inspect", "--path", str(cache_path)]
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["entries"] == 1
    assert summary["verdict_counts"] == {"no-improvement-lcb": 1}
    assert summary["distinct_environments"] == 1
    assert summary["distinct_metrics"] == ["cpu.bogo-ops-per-second"]


def test_cache_inspect_fails_closed_on_corrupt_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "broken.jsonl"
    cache_path.write_text('{"not": "an entry"}\n', encoding="utf-8")
    result = runner.invoke(
        app, ["system-opt", "cache-inspect", "--path", str(cache_path)]
    )
    assert result.exit_code != 0
