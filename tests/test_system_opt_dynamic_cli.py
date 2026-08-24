"""dynamic-run CLI end-to-end over a simulated demo session + convergence wiring.

The CLI test exercises the full adapter chain on Windows: session files ->
FileLoadIdentity/FileO0Source -> declarative hypotheses -> SafetyBacked-
Intervention (L1 apply-and-keep, business retest judged by S6/S7) ->
FileRetestSource verification windows -> S9 promotion -> phase restoration
through the same L1 path. No real system writes happen: the backend is
SimulatedBackend and the "external load" is the pre-produced session windows.

The two convergence tests pin the stop-class-2 wiring: intervention rounds
whose business LCB stays at or below the threshold accumulate into
``consecutive_lcb_threshold_rounds``; a round clearly above it resets it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import looper_api.cli as cli_module
import pytest
import yaml
from looper_api.cli import _current_environment_digest, app
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.dynamic_demo import (
    build_demo_initial_state,
    build_dynamic_demo_session,
)
from looper_core.system_opt.dynamic_loop import run_dynamic_phase
from looper_core.system_opt.hypothesis import (
    ComponentHypothesis,
    InterventionExperiment,
)
from looper_core.system_opt.phase_gate import (
    BoundComparator,
    ConvergencePolicy,
    DegradationGate,
    DynamicPhaseGateContract,
    PhaseBudget,
    SloTarget,
)
from looper_core.system_opt.result_vector import PromotionContract
from looper_core.system_opt.safety import SafetyState
from looper_core.system_opt.state_evidence import (
    STATE_EVIDENCE_SCHEMA,
    ConfigStateRecord,
    ConfigurationStateEvidence,
    OwnershipDisposition,
    PersistenceDisposition,
    StateSource,
)
from looper_core.system_opt.workload import WorkloadContract
from typer.testing import CliRunner

runner = CliRunner()

RATE = "stress-ng.bogo-ops-per-second-usr-sys-time"
FIXTURE = (
    Path(__file__).parents[1]
    / ".artifacts"
    / "system-opt"
    / "m2-component-calibration-20260823"
    / "looper-m2-cpu-calibration-20260823-b"
    / "cpu-20260823T052438.003303Z-1.yaml"
)
ENV = "sha256:" + "b" * 64


def _state_evidence(manifest) -> ConfigurationStateEvidence:
    source = StateSource(
        kind="user-declaration",
        locator="demo://dynamic-session",
        content_sha256=hashlib.sha256(b"demo://dynamic-session").hexdigest(),
        line=1,
        raw_value=None,
    )
    return ConfigurationStateEvidence(
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


def _write_demo_inputs(root: Path) -> dict[str, Path]:
    manifest = build_demo_manifest()
    manifest_path = root / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), allow_unicode=True),
        encoding="utf-8",
    )
    evidence_path = root / "state-evidence.json"
    evidence_path.write_text(
        _state_evidence(manifest).model_dump_json(indent=2), encoding="utf-8"
    )
    initial_path = root / "initial-state.json"
    initial_path.write_text(
        json.dumps(build_demo_initial_state(), indent=2), encoding="utf-8"
    )
    return {"manifest": manifest_path, "evidence": evidence_path, "initial": initial_path}


def test_dynamic_run_cli_simulated_end_to_end(tmp_path: Path) -> None:
    session = tmp_path / "session"
    build_dynamic_demo_session(session)
    inputs = _write_demo_inputs(tmp_path)
    output = tmp_path / "dynamic-run.json"

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
            str(tmp_path / "leases"),
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

    run_payload = json.loads(output.read_text(encoding="utf-8"))
    actions = [record["action"] for record in run_payload["windows"]]
    assert actions[0] == "symptom-registered"
    assert "verified" in actions
    assert run_payload["stop_gate_decision"]["stop_class"] == "target-met"
    promotion = run_payload["promotion"]
    assert promotion is not None
    assert promotion["promoted"] is True
    assert promotion["candidate_id"] == "hyp-governor-performance"
    assert promotion["failed_observations"] == []

    control = session / "control"
    retest_request = json.loads(
        (control / "retest-request-hyp-governor-performance.json").read_text("utf-8")
    )
    assert retest_request["change"] == {"system.cpu-governor": "performance"}
    assert len(retest_request["window_ids"]) == 5
    restoration = json.loads((control / "phase-restoration.json").read_text("utf-8"))
    assert restoration["state"] == "kept"
    verify_request = json.loads(
        (control / "retest-request-verify-window-3-1.json").read_text("utf-8")
    )
    assert verify_request["window_ids"][0] == "verify-window-3-1-run1"
    evidence_index = control / "dynamic-collection-evidence-index.json"
    assert evidence_index.is_file()


class _RunFailure(RuntimeError):
    pass


class _RestorationFailureBackend:
    def __init__(self, backend, *, fail_from_snapshot_call: int) -> None:
        self._backend = backend
        self._snapshot_calls = 0
        self._fail_from_snapshot_call = fail_from_snapshot_call

    def __getattr__(self, name):
        return getattr(self._backend, name)

    def snapshot(self, items, *, fencing_token):
        self._snapshot_calls += 1
        if self._snapshot_calls > 1 and self._snapshot_calls >= self._fail_from_snapshot_call:
            raise OSError("restoration readback unavailable")
        return self._backend.snapshot(items, fencing_token=fencing_token)


class _NonKeptRestoration:
    def __init__(self, original_execute) -> None:
        self._original_execute = original_execute
        self.keep_calls = 0

    def __call__(self, controller, *args, **kwargs):
        result = self._original_execute(controller, *args, **kwargs)
        if kwargs.get("keep_authorized"):
            self.keep_calls += 1
            if self.keep_calls >= 2:
                return result.model_copy(
                    update={
                        "state": SafetyState.NEEDS_ATTENTION,
                        "reason": "restoration readback unavailable",
                    }
                )
        return result


def _invoke_demo_dynamic_run(tmp_path: Path, session: Path, inputs: dict[str, Path]):
    return runner.invoke(
        app,
        _base_argv(session, inputs, tmp_path)
        + [
            "--backend",
            "simulated",
            "--initial-state",
            str(inputs["initial"]),
        ],
    )


def test_dynamic_run_failure_after_intervention_restores_phase_start_and_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    build_dynamic_demo_session(session)
    inputs = _write_demo_inputs(tmp_path)
    original = cli_module.run_dynamic_phase
    order: list[str] = []

    def fail_after_run(**kwargs):
        original(**kwargs)
        order.append("run-failed")
        raise _RunFailure("post-intervention failure")

    original_execute = cli_module.SafetyController.execute

    def record_execute(self, *args, **kwargs):
        result = original_execute(self, *args, **kwargs)
        if kwargs.get("keep_authorized"):
            order.append("restored")
        return result

    monkeypatch.setattr(cli_module, "run_dynamic_phase", fail_after_run)
    monkeypatch.setattr(cli_module.SafetyController, "execute", record_execute)

    result = _invoke_demo_dynamic_run(tmp_path, session, inputs)

    assert result.exit_code != 0
    assert result.exception is not None
    assert "post-intervention failure" in str(result.exception)
    assert order[-2:] == ["run-failed", "restored"]
    restoration = json.loads(
        (session / "control" / "phase-restoration.json").read_text(encoding="utf-8")
    )
    assert restoration["state"] == "kept"
    assert not list((tmp_path / "leases").glob("*.lease.json"))


def test_dynamic_run_restores_before_collection_persistence_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    build_dynamic_demo_session(session)
    inputs = _write_demo_inputs(tmp_path)
    order: list[str] = []
    original_execute = cli_module.SafetyController.execute

    def record_execute(self, *args, **kwargs):
        result = original_execute(self, *args, **kwargs)
        if kwargs.get("keep_authorized"):
            order.append("restored")
        return result

    def fail_persistence(*_args, **_kwargs):
        order.append("persisted")
        raise OSError("evidence sink unavailable")

    monkeypatch.setattr(cli_module.SafetyController, "execute", record_execute)
    monkeypatch.setattr(cli_module, "persist_dynamic_collection_evidence", fail_persistence)

    result = _invoke_demo_dynamic_run(tmp_path, session, inputs)

    assert result.exit_code != 0
    assert result.exception is not None
    assert "evidence persistence failed" in str(result.exception)
    assert order[-2:] == ["restored", "persisted"]
    restoration = json.loads(
        (session / "control" / "phase-restoration.json").read_text(encoding="utf-8")
    )
    assert restoration["state"] == "kept"
    assert not list((tmp_path / "leases").glob("*.lease.json"))
    assert not (tmp_path / "leases" / "demo-dynamic-target.attention.json").exists()


def test_dynamic_run_non_kept_restoration_marks_attention_with_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    build_dynamic_demo_session(session)
    inputs = _write_demo_inputs(tmp_path)
    original_run = cli_module.run_dynamic_phase
    original_execute = cli_module.SafetyController.execute

    def fail_after_run(**kwargs):
        original_run(**kwargs)
        raise _RunFailure("original dynamic failure")

    def failing_execute(controller, *args, **kwargs):
        result = original_execute(controller, *args, **kwargs)
        if kwargs.get("keep_authorized"):
            return result.model_copy(
                update={
                    "state": SafetyState.NEEDS_ATTENTION,
                    "reason": "restoration readback unavailable",
                }
            )
        return result

    monkeypatch.setattr(cli_module, "run_dynamic_phase", fail_after_run)
    monkeypatch.setattr(cli_module.SafetyController, "execute", failing_execute)

    result = _invoke_demo_dynamic_run(tmp_path, session, inputs)

    assert result.exit_code != 0
    assert result.exception is not None
    assert "original dynamic failure" in str(result.exception)
    assert "phase restoration failed" in str(result.exception)
    attention_paths = list((tmp_path / "leases").glob("*.attention.json"))
    assert len(attention_paths) == 1
    attention = json.loads(attention_paths[0].read_text(encoding="utf-8"))
    assert "original dynamic failure" in attention["reason"]
    assert "restoration readback unavailable" in attention["reason"]
    assert not list((tmp_path / "leases").glob("*.lease.json"))


# ---------------------------------------------------------------------------
# convergence wiring (stop class 2)
# ---------------------------------------------------------------------------


def _loop_contract(slo_bound: float | None) -> WorkloadContract:
    payload = dict(
        workload_id="stress-ng-standin-convergence-test",
        load_provider="external-test",
        load_command={
            "tool": "stress-ng",
            "argv_digest": "sha256:" + "a" * 64,
            "declared_duration_seconds": 120,
            "description": "test-side owned load",
        },
        o0_metrics=[
            {
                "metric_id": RATE,
                "unit": "bogo-ops/s",
                "direction": "maximize",
                "aggregation": "mean",
                "source": "stress-ng yaml metrics",
            },
            {
                "metric_id": "stress-ng.bogo-ops",
                "unit": "ops",
                "direction": "maximize",
                "aggregation": "mean",
                "source": "stress-ng yaml metrics",
            },
        ],
        objective={"primary_metric_id": RATE, "scale": 1.0, "mde": 0.5},
        slos=(
            [
                {
                    "metric_id": RATE,
                    "comparator": "at-least",
                    "bound": slo_bound,
                    "unit": "bogo-ops/s",
                }
            ]
            if slo_bound is not None
            else []
        ),
        correctness_gates=[
            {
                "metric_id": "stress-ng.bogo-ops",
                "comparator": "at-least",
                "bound": 1,
                "unit": "ops",
            }
        ],
        phases=[
            {
                "phase_id": "steady",
                "purpose": "load",
                "o0_metric_ids": [RATE, "stress-ng.bogo-ops"],
            }
        ],
        limitations="convergence wiring test fixture",
    )
    return WorkloadContract(**payload)


def _convergence_gate(contract: WorkloadContract) -> DynamicPhaseGateContract:
    return DynamicPhaseGateContract(
        workload_contract_digest=contract.digest,
        slo=SloTarget(
            metric_id=RATE,
            comparator=BoundComparator.AT_LEAST,
            bound=1000.0,
            hold_windows=2,
        ),
        convergence=ConvergencePolicy(rounds=2, lcb_threshold=0.0),
        budget=PhaseBudget(max_interventions=5, wall_clock_seconds=3600.0, risk_quota=5),
        degradation=DegradationGate(metric_id="stress-ng.bogo-ops", relative_limit=0.05),
        reactivation_holdout_windows=2,
    )


def _hypotheses_factory(count: int):
    def factory(symptom) -> list[ComponentHypothesis]:
        components = ["cpu", "memory", "network"]
        return [
            ComponentHypothesis(
                hypothesis_id=f"hyp-{components[i]}",
                symptom_id=symptom.symptom_id,
                component=components[i],
                rank=i + 1,
            )
            for i in range(count)
        ]

    return factory


def _clock():
    state = {"now": datetime(2026, 8, 23, 12, 0, tzinfo=UTC)}

    def tick() -> datetime:
        state["now"] += timedelta(seconds=30)
        return state["now"]

    return tick


def _run_convergence(lcb_values: list[float]) -> object:
    contract = _loop_contract(slo_bound=1500.0)  # fixture rate 1182.49 violates
    experiments = {"count": 0}

    def intervention(_hypothesis):
        index = experiments["count"]
        experiments["count"] += 1
        return InterventionExperiment(
            measurement_batch_digest="sha256:" + str(index + 1).zfill(64),
            business_metric_id=RATE,
            accepted=False,
            business_lcb=lcb_values[index],
        )

    run = run_dynamic_phase(
        contract=contract,
        gate_contract=_convergence_gate(contract),
        promotion_contract=PromotionContract(
            min_observations=3, min_distinct_time_blocks=3, min_environments=1
        ),
        environment_digest=ENV,
        max_windows=8,
        probe_top_k=1,
        load_identity=lambda _window: contract.load_command,
        o0_source=lambda _window: FIXTURE.read_text(encoding="utf-8"),
        # Three competing hypotheses: D2 rule 1 blocks intervening on the last
        # non-terminal one, so refuting two still leaves the third to keep the
        # second refutation admissible.
        hypothesis_source=_hypotheses_factory(3),
        clock=_clock(),
        component_probe=lambda hypothesis, window: window.digest,
        intervention=intervention,
    )
    return run, experiments["count"]


def test_convergence_counter_stops_after_k_nonimproving_rounds() -> None:
    run, count = _run_convergence(lcb_values=[0.0, 0.0])

    assert count == 2
    assert run.stop_gate_decision.stop is True
    assert run.stop_gate_decision.triggered_field == "convergence.rounds"
    assert run.stop_gate_decision.stop_class.value == "converged"


def test_convergence_counter_resets_on_a_high_lcb_round() -> None:
    run, count = _run_convergence(lcb_values=[100.0, 100.0])

    assert count == 2
    # No convergence stop: both rounds had LCB above the threshold; the
    # window budget ends the phase instead.
    assert run.stop_gate_decision.triggered_field != "convergence.rounds"


# ---------------------------------------------------------------------------
# live O1/O2 CLI wiring (fail-closed parameter discipline)
# ---------------------------------------------------------------------------


def _o1_plans_payload(environment_digest: str) -> str:
    return json.dumps(
        [
            {
                "component": "cpu",
                "target_id": "demo-dynamic-target",
                "environment_digest": environment_digest,
                "workload_phase_id": "demo-steady",
                "workload_source": "simulated demo external load",
                "collector_id": "looper.builtin-linux-guest",
                "requested_metrics": ["cpu.utilization"],
                "interval_seconds": 1.0,
                "scope": {},
            }
        ]
    )


def _base_argv(session, inputs, tmp_path: Path) -> list[str]:
    return [
        "system-opt",
        "dynamic-run",
        "--session",
        str(session),
        "--manifest",
        str(inputs["manifest"]),
        "--state-evidence",
        str(inputs["evidence"]),
        "--target-id",
        "demo-dynamic-target",
        "--owner-id",
        "demo-owner",
        "--lease-root",
        str(tmp_path / "leases"),
        "--lease-ttl-seconds",
        "7200",
        "--max-windows",
        "6",
        "--probe-top-k",
        "2",
        "--verification-windows",
        "2",
        "--output",
        str(tmp_path / "out.json"),
    ]


def test_dynamic_run_rejects_live_o1_plans_on_the_simulated_backend(tmp_path: Path) -> None:
    session = tmp_path / "session"
    build_dynamic_demo_session(session)
    inputs = _write_demo_inputs(tmp_path)
    plans_path = tmp_path / "o1-collection-plans.json"
    plans_path.write_text(_o1_plans_payload(_current_environment_digest()), "utf-8")

    result = runner.invoke(
        app,
        _base_argv(session, inputs, tmp_path)
        + [
            "--backend",
            "simulated",
            "--initial-state",
            str(inputs["initial"]),
            "--o1-plans",
            str(plans_path),
            "--o1-window-seconds",
            "5",
        ],
    )

    assert result.exit_code != 0
    assert "local-linux" in result.output


def test_dynamic_run_rejects_o1_plans_without_window_seconds(tmp_path: Path) -> None:
    session = tmp_path / "session"
    build_dynamic_demo_session(session)
    inputs = _write_demo_inputs(tmp_path)
    plans_path = tmp_path / "o1-collection-plans.json"
    plans_path.write_text(_o1_plans_payload(_current_environment_digest()), "utf-8")

    # Simulated backend keeps the parameter check reachable on Windows (the
    # window-seconds validation fires before the local-linux backend gate).
    result = runner.invoke(
        app,
        _base_argv(session, inputs, tmp_path)
        + [
            "--backend",
            "simulated",
            "--initial-state",
            str(inputs["initial"]),
            "--o1-plans",
            str(plans_path),
        ],
    )

    assert result.exit_code != 0
    assert "--o1-window-seconds" in result.output


def test_dynamic_run_rejects_unknown_o2_source(tmp_path: Path) -> None:
    session = tmp_path / "session"
    build_dynamic_demo_session(session)
    inputs = _write_demo_inputs(tmp_path)

    result = runner.invoke(
        app,
        _base_argv(session, inputs, tmp_path)
        + [
            "--backend",
            "simulated",
            "--initial-state",
            str(inputs["initial"]),
            "--o2-source",
            "bogus",
        ],
    )

    assert result.exit_code != 0
    assert "--o2-source" in result.output


def test_o1_collection_plans_refuse_a_foreign_environment(tmp_path: Path) -> None:
    from looper_core.system_opt.dynamic_adapters import load_o1_collection_plans

    plans_path = tmp_path / "plans.json"
    plans_path.write_text(_o1_plans_payload("sha256:" + "f" * 64), "utf-8")

    with pytest.raises(ValueError, match="different environment"):
        load_o1_collection_plans(
            plans_path, environment_digest=_current_environment_digest()
        )


# ---------------------------------------------------------------------------
# degradation wiring (stop class 4: post-intervention business regression)
# ---------------------------------------------------------------------------


def _o0_yaml(rate: float) -> str:
    newline = chr(10)
    return (
        "metrics:"
        + newline
        + "- stressor: cpu"
        + newline
        + f"  bogo-ops: {int(rate * 120)}"
        + newline
        + f"  bogo-ops-per-second-usr-sys-time: {rate}"
        + newline
    )


def _run_degradation(window_rates: dict[str, float]) -> object:
    contract = _loop_contract(slo_bound=1500.0)  # every window violates -> symptom
    gate = DynamicPhaseGateContract(
        workload_contract_digest=contract.digest,
        slo=SloTarget(
            metric_id=RATE,
            comparator=BoundComparator.AT_LEAST,
            bound=1500.0,
            hold_windows=2,
        ),
        convergence=ConvergencePolicy(rounds=99, lcb_threshold=0.0),
        budget=PhaseBudget(max_interventions=5, wall_clock_seconds=3600.0, risk_quota=5),
        degradation=DegradationGate(metric_id=RATE, relative_limit=0.10),
        reactivation_holdout_windows=2,
    )

    def intervention(_hypothesis):
        return InterventionExperiment(
            measurement_batch_digest="sha256:" + "2" * 64,
            business_metric_id=RATE,
            accepted=True,
            business_lcb=100.0,
        )

    return run_dynamic_phase(
        contract=contract,
        gate_contract=gate,
        promotion_contract=PromotionContract(
            min_observations=3, min_distinct_time_blocks=3, min_environments=1
        ),
        environment_digest=ENV,
        max_windows=len(window_rates),
        probe_top_k=1,
        load_identity=lambda _window: contract.load_command,
        o0_source=lambda window_id: _o0_yaml(window_rates[window_id]),
        hypothesis_source=_hypotheses_factory(2),
        clock=_clock(),
        component_probe=lambda hypothesis, window: window.digest,
        intervention=intervention,
    )


def test_degradation_stops_the_phase_after_a_post_intervention_collapse() -> None:
    run = _run_degradation(
        {
            "window-1": 1182.0,  # symptom
            "window-2": 1181.0,  # probe
            "window-3": 1180.0,  # intervention accepted; pre-intervention reference
            "window-4": 500.0,  # post-intervention collapse (~58% worsening > 10%)
        }
    )

    assert run.stop_gate_decision.stop is True
    assert run.stop_gate_decision.triggered_field == "degradation"
    assert run.stop_gate_decision.stop_class.value == "safety-triggered"
    assert run.windows[-1].note is not None
    assert "degradation" in run.windows[-1].note


def test_no_degradation_when_business_improves_after_intervention() -> None:
    run = _run_degradation(
        {
            "window-1": 1182.0,
            "window-2": 1181.0,
            "window-3": 1180.0,
            "window-4": 1300.0,  # improvement, not worsening
        }
    )

    assert run.stop_gate_decision.stop is False
    assert run.stop_gate_decision.triggered_field != "degradation"
