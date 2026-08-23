"""Schema-version dispatch for OptimizationRun evidence (SO-D021, D0-09 repair).

Commit 2cd521e added required fields (baseline_history, attempt_count,
round_index, attempt_index, comparison_baseline_digest) without bumping the
v1alpha1 schema string, which broke loading of pre-multi-round evidence
(first fio session). The dispatcher loads both v1alpha1 shapes plus the new
v1alpha2; legacy evidence is never back-filled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from looper_core.system_opt.demo import (
    SyntheticMeasurementAdapter,
    build_demo_manifest,
    build_demo_policy,
    resolve_demo_domains,
)
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.policy import OptimizationMode
from looper_core.system_opt.tuning import (
    OPTIMIZATION_RUN_SCHEMA_V2,
    LegacyOptimizationRun,
    OptimizationRun,
    SystemOptimizationEngine,
    load_optimization_run,
)

ARTIFACTS = Path(__file__).parents[1] / ".artifacts" / "system-opt"
LEGACY_FIO_DIGEST = "sha256:8bcc3b5e2737aa2e805eae575595f59664e4fad31e1e92813fd4d07c61e47f02"


def _load(relative: str):
    payload = json.loads(
        (ARTIFACTS / relative).read_text(encoding="utf-8")
    )
    return load_optimization_run(payload)


def test_pre_multiround_fio_evidence_loads_in_its_own_schema_without_backfill():
    run = _load("aliyun-ecs-fio-20260823/optimization-run.json")

    assert isinstance(run, LegacyOptimizationRun)
    assert run.candidates, "the legacy fio run keeps its candidate evidence"
    with pytest.raises(AttributeError):
        run.baseline_history  # noqa: B018 - absence is the honest record
    with pytest.raises(AttributeError):
        run.candidates[0].round_index  # noqa: B018
    assert run.digest == LEGACY_FIO_DIGEST


def test_post_multiround_v1alpha1_artifacts_route_to_the_current_model():
    for name in (
        "aliyun-ecs-fio-multiround-20260823",
        "m2-cpu-governor-search-20260823",
        "m2-network-cc-search-20260823",
    ):
        run = _load(f"{name}/optimization-run.json")
        assert isinstance(run, OptimizationRun), name
        assert run.attempt_count >= 1, name
        assert all(c.round_index >= 1 for c in run.candidates), name


def test_new_runs_emit_v1alpha2_and_round_trip_through_the_dispatcher():
    manifest = build_demo_manifest()
    initial = {item.id: item.default for item in manifest.items}
    backend = SimulatedBackend(initial, target_id="schema-version-test")
    policy = build_demo_policy(OptimizationMode.GENERAL)
    policy.authorized_components = ["cpu"]
    engine = SystemOptimizationEngine(
        policy, manifest, resolve_demo_domains(manifest), backend
    )
    run = engine.run(
        baseline_parameters={item.parameter_id: item.default for item in manifest.items},
        measure=SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
        fencing_token=1,
    )

    assert run.schema_version == OPTIMIZATION_RUN_SCHEMA_V2
    reloaded = load_optimization_run(run.model_dump(mode="python"))
    assert isinstance(reloaded, OptimizationRun)
    assert reloaded.digest == run.digest


def test_unknown_schema_version_is_rejected_fail_closed():
    with pytest.raises(ValueError, match="unsupported optimization-run schema_version"):
        load_optimization_run({"schema_version": "looper.system-optimization-run/v9"})
