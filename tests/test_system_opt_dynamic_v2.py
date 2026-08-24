from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from looper_core.system_opt.config_manifest import ConfigComponent, RiskLevel
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.dynamic_adapters import (
    HYPOTHESIS_PROPOSALS_SCHEMA,
    BusinessRetestPlanner,
    FileHypothesisProposalsV2,
    FileLoadIdentity,
    FileO0Source,
    HypothesisProposalsFileV2,
    HypothesisProposalV2,
    TwoStageSafetyBackedIntervention,
    load_business_policy,
    load_hypothesis_proposals_versioned,
    load_workload_contract,
)
from looper_core.system_opt.dynamic_demo import (
    build_demo_gate_contract,
    build_demo_initial_state,
    build_dynamic_demo_session,
)
from looper_core.system_opt.dynamic_loop import (
    DynamicPhaseRun,
    DynamicWindowRecord,
    WindowAction,
    load_dynamic_phase_run,
    run_dynamic_phase_v2,
)
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.hypothesis import ComponentHypothesis
from looper_core.system_opt.intervention import (
    ReceiptOperation,
    ReceiptStageV2,
    RiskSourceKind,
)
from looper_core.system_opt.intervention_receipt import DurableReceiptStore, ReceiptStoreError
from looper_core.system_opt.phase_gate import (
    DynamicPhaseGateContractV2,
    PhaseBudget,
    load_dynamic_phase_gate,
)
from looper_core.system_opt.result_vector import PromotionContract
from looper_core.system_opt.safety import SafetyController, SafetyPolicy
from looper_core.system_opt.scoring import MeasurementBatch


def _proposal(
    hypothesis_id: str,
    component: ConfigComponent,
    rank: int,
    change: dict[str, object],
    risk: RiskLevel,
) -> HypothesisProposalV2:
    override = risk is not RiskLevel.LOW
    return HypothesisProposalV2(
        hypothesis_id=hypothesis_id,
        component=component,
        rank=rank,
        rationale=f"explicit synthetic {component.value} hypothesis",
        change=change,
        risk=risk,
        risk_kind=(RiskSourceKind.TASK_OVERRIDE if override else RiskSourceKind.MANIFEST_DERIVED),
        risk_rationale="task raises risk for quota test" if override else None,
    )


def _proposals(risk: RiskLevel = RiskLevel.LOW) -> HypothesisProposalsFileV2:
    return HypothesisProposalsFileV2(
        proposals=[
            _proposal(
                "hyp-governor-performance",
                ConfigComponent.CPU,
                1,
                {"system.cpu-governor": "performance"},
                risk,
            ),
            _proposal(
                "hyp-swappiness-10",
                ConfigComponent.MEMORY,
                2,
                {"system.vm-swappiness": 10},
                risk,
            ),
            _proposal(
                "hyp-somaxconn-1152",
                ConfigComponent.NETWORK,
                3,
                {"system.net-somaxconn": 1152},
                risk,
            ),
        ]
    )


def _gate(contract_digest: str, quota: int = 0) -> DynamicPhaseGateContractV2:
    legacy = build_demo_gate_contract(contract_digest)
    return DynamicPhaseGateContractV2(
        workload_contract_digest=contract_digest,
        slo=legacy.slo,
        convergence=legacy.convergence,
        budget=PhaseBudget(
            max_interventions=legacy.budget.max_interventions,
            wall_clock_seconds=legacy.budget.wall_clock_seconds,
            risk_quota=quota,
        ),
        degradation=legacy.degradation,
        reactivation_holdout_windows=legacy.reactivation_holdout_windows,
    )


def _clock():
    state = {"now": datetime(2026, 8, 24, 8, 0, tzinfo=UTC)}

    def tick() -> datetime:
        state["now"] += timedelta(seconds=1)
        return state["now"]

    return tick


def _adapter(root: Path, proposals: HypothesisProposalsFileV2, store_root: str = "r"):
    layout = build_dynamic_demo_session(root)
    contract = load_workload_contract(layout)
    policy = load_business_policy(layout.business_policy)
    baseline = MeasurementBatch.model_validate_json(layout.baseline_batch.read_text("utf-8"))
    manifest = build_demo_manifest()
    backend = SimulatedBackend(build_demo_initial_state(), target_id="dynamic-v2-target")
    store = DurableReceiptStore(layout.control / store_root)
    attention: list[tuple[str, str]] = []
    adapter = TwoStageSafetyBackedIntervention(
        controller=SafetyController(SafetyPolicy(allow_keep=True)),
        manifest=manifest,
        backend=backend,
        fencing_token=1,
        proposals=proposals.by_id(),
        planner=BusinessRetestPlanner(
            contract=contract, policy=policy, baseline_batch=baseline, layout=layout
        ),
        layout=layout,
        receipt_store=store,
        attention_sink=lambda reason, digest: attention.append((reason, digest)),
    )
    return layout, contract, manifest, backend, store, attention, adapter


def _run(layout, contract, manifest, proposals, adapter, *, quota: int, windows: int):
    policy = load_business_policy(layout.business_policy)
    return run_dynamic_phase_v2(
        contract=contract,
        gate_contract=_gate(contract.digest, quota),
        manifest=manifest,
        promotion_contract=PromotionContract(
            min_observations=2, min_distinct_time_blocks=2, min_environments=1
        ),
        environment_digest="sha256:" + "e" * 64,
        max_windows=windows,
        probe_top_k=3,
        load_identity=FileLoadIdentity(layout, policy),
        o0_source=FileO0Source(layout, policy),
        hypothesis_source=FileHypothesisProposalsV2(proposals),
        prepare_intervention=adapter.prepare_intervention,
        execute_intervention=adapter.execute_intervention,
        clock=_clock(),
        component_probe=lambda _hypothesis, window: window.digest,
    )


def _reject_governor(layout) -> None:
    payload = (
        "metrics:\n- stressor: cpu\n  bogo-ops: 9600\n  bogo-ops-per-second-usr-sys-time: 80.0\n"
    )
    for index in range(1, 6):
        (layout.window(f"retest-hyp-governor-performance-run{index}") / "o0.txt").write_text(
            payload, encoding="utf-8"
        )


def test_version_dispatch_preserves_legacy_models_and_digests(tmp_path: Path) -> None:
    path = tmp_path / "proposals.yaml"
    path.write_text(
        "schema_version: looper.hypothesis-proposals/v1alpha1\n"
        "proposals:\n- hypothesis_id: legacy\n  component: cpu\n  rank: 1\n"
        "  rationale: legacy fixture\n  change: {system.cpu-governor: performance}\n",
        encoding="utf-8",
    )
    assert load_hypothesis_proposals_versioned(path).schema_version == HYPOTHESIS_PROPOSALS_SCHEMA
    digest = "sha256:" + "a" * 64
    legacy_gate = build_demo_gate_contract(digest)
    assert load_dynamic_phase_gate(legacy_gate.model_dump()).digest == legacy_gate.digest
    legacy_run = DynamicPhaseRun(
        workload_contract_digest=digest,
        gate_contract_digest=legacy_gate.digest,
        windows=[DynamicWindowRecord(window_id="window-1", action=WindowAction.OBSERVE)],
    )
    assert load_dynamic_phase_run(legacy_run.model_dump()).digest == legacy_run.digest
    with pytest.raises(ValueError, match="unsupported dynamic phase run"):
        load_dynamic_phase_run({"schema_version": "v9"})


def test_candidate_and_recovery_receipt_chains(tmp_path: Path) -> None:
    proposals = _proposals()
    layout, _, _, backend, store, attention, adapter = _adapter(tmp_path / "s", proposals)
    hypothesis = ComponentHypothesis(
        hypothesis_id="hyp-governor-performance",
        symptom_id="symptom-window-1",
        component=ConfigComponent.CPU,
        rank=1,
    )
    plan = adapter.prepare_intervention(hypothesis)
    accepted = adapter.execute_intervention(plan, "accepted")
    assert accepted.outcome.experiment is not None and accepted.outcome.experiment.accepted
    assert backend.state()["cpu-governor"] == "performance"

    backend.inject_drift("cpu-governor", "powersave")
    _reject_governor(layout)
    rejected = adapter.execute_intervention(plan, "rejected")
    assert rejected.outcome.experiment is not None and not rejected.outcome.experiment.accepted
    assert rejected.recovery_receipt_digest is not None
    assert backend.state()["cpu-governor"] == "powersave"
    recovery = store.verify_chain(plan.digest, "rejected", ReceiptOperation.RECOVERY)
    assert recovery.digest == rejected.recovery_receipt_digest
    assert recovery.parent_receipt_digest is not None
    assert [head.digest for head in store.heads()] == [
        accepted.candidate_receipt_digest,
        rejected.candidate_receipt_digest,
        recovery.digest,
    ]
    assert attention == []


def test_quota_is_checked_before_write_and_k_plus_one_is_rejected(tmp_path: Path) -> None:
    proposals = _proposals(RiskLevel.MEDIUM)
    layout, contract, manifest, backend, store, attention, adapter = _adapter(
        tmp_path / "s", proposals
    )
    blocked = _run(layout, contract, manifest, proposals, adapter, quota=0, windows=3)
    assert blocked.windows[-1].action is WindowAction.GATE_REJECTED
    assert blocked.risky_interventions == 0
    assert backend.state()["cpu-governor"] == "powersave"
    with pytest.raises(ReceiptStoreError, match="does not exist"):
        store.verify_chain(
            blocked.windows[-1].plan_digest or "", "window-3", ReceiptOperation.CANDIDATE
        )

    layout, contract, manifest, backend, _, attention, adapter = _adapter(
        tmp_path / "s2", proposals
    )
    _reject_governor(layout)
    run = _run(layout, contract, manifest, proposals, adapter, quota=1, windows=5)
    assert run.risky_interventions == 1
    assert run.windows[-1].action is WindowAction.GATE_REJECTED
    assert run.stop_gate_decision is not None
    assert run.stop_gate_decision.triggered_field == "budget.risk_quota"
    assert backend.state()["cpu-governor"] == "powersave"
    assert attention == []


class _FailingTerminalStore(DurableReceiptStore):
    failed = False

    def advance(self, current, stage, **fields):
        if (
            not self.failed
            and current.operation is ReceiptOperation.CANDIDATE
            and stage is ReceiptStageV2.SAFETY_TERMINAL
        ):
            self.failed = True
            raise OSError("injected receipt failure")
        return super().advance(current, stage, **fields)


def test_post_apply_receipt_failure_counts_and_stops_with_attention(tmp_path: Path) -> None:
    proposals = _proposals(RiskLevel.MEDIUM)
    layout, contract, manifest, backend, _, attention, adapter = _adapter(tmp_path / "s", proposals)
    adapter._receipt_store = _FailingTerminalStore(layout.control / "f")
    run = _run(layout, contract, manifest, proposals, adapter, quota=1, windows=3)
    failed = run.windows[-1]
    assert failed.action is WindowAction.INTERVENTION_FAILED
    assert run.risky_interventions == 1
    assert run.stop_gate_decision is not None
    assert run.stop_gate_decision.triggered_field == "intervention.receipt"
    assert backend.state()["cpu-governor"] == "performance"
    assert len(attention) == 1


def test_receipt_store_supports_windows_paths_beyond_legacy_max_path(tmp_path: Path) -> None:
    proposals = _proposals()
    padding = "receipt-root-segment-" * 4
    _, _, _, _, store, _, adapter = _adapter(
        tmp_path / "s", proposals, store_root=padding
    )
    hypothesis = ComponentHypothesis(
        hypothesis_id="hyp-governor-performance",
        symptom_id="symptom-window-1",
        component=ConfigComponent.CPU,
        rank=1,
    )
    plan = adapter.prepare_intervention(hypothesis)
    execution = adapter.execute_intervention(plan, "window-3")
    pointer = store._pointer_path(plan.digest, "window-3", ReceiptOperation.CANDIDATE)
    assert len(str(pointer.absolute())) > 260
    assert store.verify_chain(plan.digest, "window-3", ReceiptOperation.CANDIDATE).digest == (
        execution.candidate_receipt_digest
    )
