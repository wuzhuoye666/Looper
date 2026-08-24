"""M3 两阶段干预合同纯单元测试（phase-gate R3 / D5-I1 + D5-I1-R1）。

覆盖：风险必填、manifest 下界、change_count 派生、执行前门禁边界、
receipt 状态约束、outcome 回绑、纯函数不触碰 backend，以及 D5-I1-R1 的
manifest 风险绕过关闭 / kind 语义 / schema / 排序 / 输入严格校验。
"""

from __future__ import annotations

import pytest
from looper_core.canonical import canonical_digest
from looper_core.system_opt.config_manifest import (
    ActivationMode,
    CommandTemplate,
    CompatibilitySpec,
    ConfigCategory,
    ConfigComponent,
    ConfigItem,
    ConfigManifest,
    ConfigValueType,
    ReadSpec,
    RiskLevel,
    RollbackMode,
    RollbackSpec,
    ValueDomain,
    ValueParser,
)
from looper_core.system_opt.hypothesis import ComponentHypothesis
from looper_core.system_opt.intervention import (
    INTERVENTION_RISK_SOURCE_SCHEMA,
    InterventionContractError,
    InterventionExecutionReceipt,
    InterventionOutcome,
    InterventionPlan,
    ReceiptStage,
    ResolvedPlanRisk,
    ResolvedRiskItem,
    RiskSource,
    RiskSourceItem,
    RiskSourceKind,
    evaluate_intervention_gate,
    resolve_plan_risk,
    verify_outcome_binding,
)
from looper_core.system_opt.phase_gate import (
    DynamicPhaseGateContract,
    GateStopClass,
    PhaseBudget,
    SloTarget,
)
from looper_core.system_opt.safety import SafetyState
from pydantic import ValidationError

P_LOW = "system.item-0"
P_MED = "system.item-1"
P_HIGH = "system.item-2"
EVIDENCE = "sha256:" + "e" * 64
CONTRACT = "sha256:" + "c" * 64

HYPOTHESIS = ComponentHypothesis(
    hypothesis_id="hyp-cpu",
    symptom_id="symptom-w1",
    component=ConfigComponent.CPU,
    rank=1,
)


def _item(item_id: str, risk: RiskLevel) -> ConfigItem:
    return ConfigItem(
        id=item_id,
        category=ConfigCategory.SYSCTL,
        primary_component=ConfigComponent.MEMORY,
        related_components=[ConfigComponent.NUMA],
        target=f"sys.{item_id}",
        value_type=ConfigValueType.INTEGER,
        domain=ValueDomain(minimum=0, maximum=100, step=1, choices=None, log=False),
        default=50,
        read=ReadSpec(
            command=CommandTemplate(argv=["sysctl", "-n", "{target}"], timeout_seconds=5),
            parser=ValueParser.INTEGER,
        ),
        apply=CommandTemplate(argv=["sysctl", "-w", "{target}={value}"], timeout_seconds=5),
        rollback=RollbackSpec(mode=RollbackMode.RESTORE_SNAPSHOT),
        activation=ActivationMode.IMMEDIATE,
        risk=risk,
        risk_reason="synthetic high-risk item" if risk is RiskLevel.HIGH else None,
        compatibility=CompatibilitySpec(required_commands=["sysctl"]),
        searchable=risk is not RiskLevel.HIGH,
        description=f"Synthetic {risk.value}-risk item for intervention contract tests.",
        source="test fixture",
    )


MANIFEST = ConfigManifest(
    id="test-linux-guest",
    version="1",
    description="Synthetic manifest for intervention contract tests.",
    items=[
        _item("item-0", RiskLevel.LOW),
        _item("item-1", RiskLevel.MEDIUM),
        _item("item-2", RiskLevel.HIGH),
    ],
)


def _plan(
    manifest: ConfigManifest,
    *,
    change: dict,
    risk: RiskLevel,
    items: list[RiskSourceItem] | None = None,
    kind: RiskSourceKind = RiskSourceKind.MANIFEST_DERIVED,
    manifest_digest: str | None = None,
    rationale: str | None = None,
) -> InterventionPlan:
    if items is None:
        items = [
            RiskSourceItem(
                item_id=manifest.item_for_parameter(parameter_id).id,
                risk=manifest.item_for_parameter(parameter_id).risk,
            )
            for parameter_id in change
        ]
        items.sort(key=lambda item: item.item_id)
    return InterventionPlan(
        hypothesis=HYPOTHESIS,
        change=change,
        risk=risk,
        risk_source=RiskSource(
            schema_version=INTERVENTION_RISK_SOURCE_SCHEMA,
            kind=kind,
            manifest_digest=manifest_digest or manifest.digest,
            items=items,
            rationale=rationale,
        ),
    )


def _gate(risk_quota: int = 1) -> DynamicPhaseGateContract:
    return DynamicPhaseGateContract(
        workload_contract_digest=CONTRACT,
        slo=SloTarget(
            metric_id="stress-ng.bogo-ops", comparator="at-least", bound=1.0, hold_windows=1
        ),
        convergence={"rounds": 4, "lcb_threshold": 0.0},
        budget=PhaseBudget(
            max_interventions=5, wall_clock_seconds=3600.0, risk_quota=risk_quota
        ),
        degradation={"metric_id": "stress-ng.bogo-ops", "relative_limit": 0.05},
        reactivation_holdout_windows=2,
    )


def _outcome(plan_digest: str, **overrides) -> InterventionOutcome:
    payload: dict = dict(
        plan_digest=plan_digest,
        write_attempted=True,
        apply_started=True,
        rollback_attempted=False,
        rollback_verified=False,
        safety_state=SafetyState.KEPT,
        evidence_digest="sha256:" + "f" * 64,
    )
    payload.update(overrides)
    return InterventionOutcome(**payload)


class TestPlanModel:
    def test_risk_is_required(self):
        payload = _plan(MANIFEST, change={P_LOW: 1}, risk=RiskLevel.LOW).model_dump(
            mode="json"
        )
        del payload["risk"]
        with pytest.raises(ValidationError):
            InterventionPlan.model_validate(payload)

    def test_empty_change_rejected(self):
        source = RiskSource(
            schema_version=INTERVENTION_RISK_SOURCE_SCHEMA,
            kind=RiskSourceKind.MANIFEST_DERIVED,
            manifest_digest=MANIFEST.digest,
            items=[RiskSourceItem(item_id="item-0", risk=RiskLevel.LOW)],
        )
        with pytest.raises(ValidationError):
            InterventionPlan(
                hypothesis=HYPOTHESIS, change={}, risk=RiskLevel.LOW, risk_source=source
            )

    def test_change_count_is_derived_not_forgeable(self):
        plan = _plan(MANIFEST, change={P_LOW: 1, P_MED: 2}, risk=RiskLevel.MEDIUM)
        assert plan.change_count == 2
        with pytest.raises(ValidationError):
            InterventionPlan(
                hypothesis=HYPOTHESIS,
                change={P_LOW: 1, P_MED: 2},
                risk=RiskLevel.MEDIUM,
                risk_source=plan.risk_source,
                change_count=5,
            )

    def test_digest_is_computed_not_self_reported(self):
        plan = _plan(MANIFEST, change={P_LOW: 1}, risk=RiskLevel.LOW)
        assert plan.digest == canonical_digest(
            plan.model_dump(mode="json", exclude_none=False)
        )
        with pytest.raises(ValidationError):
            InterventionPlan(
                hypothesis=HYPOTHESIS,
                change={P_LOW: 1},
                risk=RiskLevel.LOW,
                risk_source=plan.risk_source,
                digest="sha256:" + "a" * 64,
            )


class TestRiskSourceModel:
    def test_schema_version_is_required(self):
        with pytest.raises(ValidationError):
            RiskSource(
                kind=RiskSourceKind.MANIFEST_DERIVED,
                manifest_digest=MANIFEST.digest,
                items=[RiskSourceItem(item_id="item-0", risk=RiskLevel.LOW)],
            )

    def test_wrong_schema_version_rejected(self):
        with pytest.raises(ValidationError):
            RiskSource(
                schema_version="looper.wrong/v1",
                kind=RiskSourceKind.MANIFEST_DERIVED,
                manifest_digest=MANIFEST.digest,
                items=[RiskSourceItem(item_id="item-0", risk=RiskLevel.LOW)],
            )

    def test_reversed_item_order_rejected(self):
        with pytest.raises(ValidationError, match="ordered"):
            RiskSource(
                schema_version=INTERVENTION_RISK_SOURCE_SCHEMA,
                kind=RiskSourceKind.MANIFEST_DERIVED,
                manifest_digest=MANIFEST.digest,
                items=[
                    RiskSourceItem(item_id="item-1", risk=RiskLevel.MEDIUM),
                    RiskSourceItem(item_id="item-0", risk=RiskLevel.LOW),
                ],
            )

    def test_duplicate_items_rejected(self):
        with pytest.raises(ValidationError, match="unique"):
            RiskSource(
                schema_version=INTERVENTION_RISK_SOURCE_SCHEMA,
                kind=RiskSourceKind.MANIFEST_DERIVED,
                manifest_digest=MANIFEST.digest,
                items=[
                    RiskSourceItem(item_id="item-0", risk=RiskLevel.LOW),
                    RiskSourceItem(item_id="item-0", risk=RiskLevel.LOW),
                ],
            )


class TestResolvePlanRisk:
    @pytest.mark.parametrize(
        ("parameter_id", "level"),
        [
            (P_LOW, RiskLevel.LOW),
            (P_MED, RiskLevel.MEDIUM),
            (P_HIGH, RiskLevel.HIGH),
        ],
    )
    def test_resolves_manifest_risk(self, parameter_id, level):
        plan = _plan(MANIFEST, change={parameter_id: 1}, risk=level)
        resolved = resolve_plan_risk(plan, MANIFEST)
        assert resolved.manifest_risk is level
        assert resolved.task_risk is level
        assert resolved.final_risk is level
        assert resolved.manifest_digest == MANIFEST.digest

    def test_task_override_raises_above_manifest(self):
        plan = _plan(
            MANIFEST,
            change={P_MED: 1},
            risk=RiskLevel.HIGH,
            kind=RiskSourceKind.TASK_OVERRIDE,
            rationale="task contract requires higher risk than the manifest",
        )
        resolved = resolve_plan_risk(plan, MANIFEST)
        assert resolved.manifest_risk is RiskLevel.MEDIUM
        assert resolved.final_risk is RiskLevel.HIGH

    def test_task_risk_downgrade_rejected(self):
        plan = _plan(MANIFEST, change={P_MED: 1}, risk=RiskLevel.LOW)
        with pytest.raises(InterventionContractError, match="below the manifest lower bound"):
            resolve_plan_risk(plan, MANIFEST)

    def test_high_manifest_item_rejects_non_risky_claim(self):
        plan = _plan(MANIFEST, change={P_HIGH: 1}, risk=RiskLevel.LOW)
        with pytest.raises(InterventionContractError, match="below the manifest lower bound"):
            resolve_plan_risk(plan, MANIFEST)

    def test_missing_manifest_binding_rejected(self):
        plan = _plan(
            MANIFEST,
            change={P_LOW: 1, P_MED: 2},
            risk=RiskLevel.MEDIUM,
            items=[RiskSourceItem(item_id="item-0", risk=RiskLevel.LOW)],
        )
        with pytest.raises(InterventionContractError, match="no manifest risk binding"):
            resolve_plan_risk(plan, MANIFEST)

    def test_risk_binding_mismatch_rejected(self):
        plan = _plan(
            MANIFEST,
            change={P_MED: 1},
            risk=RiskLevel.MEDIUM,
            items=[RiskSourceItem(item_id="item-1", risk=RiskLevel.LOW)],
        )
        with pytest.raises(InterventionContractError, match="disagrees with the manifest"):
            resolve_plan_risk(plan, MANIFEST)

    def test_manifest_digest_mismatch_rejected(self):
        plan = _plan(
            MANIFEST,
            change={P_LOW: 1},
            risk=RiskLevel.LOW,
            manifest_digest="sha256:" + "9" * 64,
        )
        with pytest.raises(InterventionContractError, match="not bound to the provided manifest"):
            resolve_plan_risk(plan, MANIFEST)

    def test_unknown_change_item_rejected(self):
        plan = _plan(
            MANIFEST,
            change={"system.nope": 1},
            risk=RiskLevel.LOW,
            items=[RiskSourceItem(item_id="item-0", risk=RiskLevel.LOW)],
        )
        with pytest.raises(InterventionContractError, match="unknown config item"):
            resolve_plan_risk(plan, MANIFEST)

    def test_raised_risk_manifest_derived_kind_rejected(self):
        plan = _plan(MANIFEST, change={P_MED: 1}, risk=RiskLevel.HIGH)
        with pytest.raises(InterventionContractError, match="task-override"):
            resolve_plan_risk(plan, MANIFEST)

    def test_task_override_requires_rationale(self):
        plan = _plan(
            MANIFEST,
            change={P_MED: 1},
            risk=RiskLevel.HIGH,
            kind=RiskSourceKind.TASK_OVERRIDE,
        )
        with pytest.raises(InterventionContractError, match="rationale"):
            resolve_plan_risk(plan, MANIFEST)

    def test_unraised_risk_task_override_kind_rejected(self):
        plan = _plan(
            MANIFEST,
            change={P_MED: 1},
            risk=RiskLevel.MEDIUM,
            kind=RiskSourceKind.TASK_OVERRIDE,
        )
        with pytest.raises(InterventionContractError, match="manifest-derived"):
            resolve_plan_risk(plan, MANIFEST)


class TestResolvedPlanRiskBinding:
    def test_binds_plan_digest_and_recomputes(self):
        plan = _plan(MANIFEST, change={P_MED: 1}, risk=RiskLevel.MEDIUM)
        resolved = resolve_plan_risk(plan, MANIFEST)
        assert resolved.plan_digest == plan.digest
        rebuilt = ResolvedPlanRisk.model_validate(resolved.model_dump(mode="json"))
        assert resolved.digest == rebuilt.digest

    def test_final_risk_consistency_enforced(self):
        items = [
            ResolvedRiskItem(
                parameter_id=P_MED, item_id="item-1", manifest_risk=RiskLevel.MEDIUM
            )
        ]
        with pytest.raises(ValidationError, match="final risk"):
            ResolvedPlanRisk(
                plan_digest="sha256:" + "a" * 64,
                manifest_digest=MANIFEST.digest,
                items=items,
                manifest_risk=RiskLevel.HIGH,
                task_risk=RiskLevel.HIGH,
                final_risk=RiskLevel.LOW,
            )


class TestInterventionGate:
    def test_single_change_rejection(self):
        plan = _plan(MANIFEST, change={P_LOW: 1, P_MED: 2}, risk=RiskLevel.MEDIUM)
        decision = evaluate_intervention_gate(
            plan=plan,
            manifest=MANIFEST,
            contract=_gate(),
            risky_interventions=0,
            evidence_digest=EVIDENCE,
        )
        assert decision is not None and decision.stop
        assert decision.stop_class is GateStopClass.BUDGET_EXHAUSTED
        assert decision.triggered_field == "single_change_per_window"
        assert decision.contract_digest == _gate().digest

    def test_risk_quota_ge_boundary(self):
        cases = [(0, 0, True), (2, 1, False), (2, 2, True), (2, 3, True), (3, 2, False)]
        for quota, risky, rejected in cases:
            plan = _plan(MANIFEST, change={P_MED: 1}, risk=RiskLevel.MEDIUM)
            decision = evaluate_intervention_gate(
                plan=plan,
                manifest=MANIFEST,
                contract=_gate(risk_quota=quota),
                risky_interventions=risky,
                evidence_digest=EVIDENCE,
            )
            if rejected:
                assert decision is not None and decision.stop
                assert decision.triggered_field == "budget.risk_quota"
            else:
                assert decision is None

    def test_low_risk_never_hits_risk_quota(self):
        plan = _plan(MANIFEST, change={P_LOW: 1}, risk=RiskLevel.LOW)
        decision = evaluate_intervention_gate(
            plan=plan,
            manifest=MANIFEST,
            contract=_gate(risk_quota=0),
            risky_interventions=0,
            evidence_digest=EVIDENCE,
        )
        assert decision is None

    def test_preflight_rejection_does_not_count(self):
        counter = {"risky": 0}
        plan = _plan(MANIFEST, change={P_LOW: 1, P_MED: 2}, risk=RiskLevel.MEDIUM)
        decision = evaluate_intervention_gate(
            plan=plan,
            manifest=MANIFEST,
            contract=_gate(),
            risky_interventions=counter["risky"],
            evidence_digest=EVIDENCE,
        )
        assert decision is not None and decision.stop
        assert counter["risky"] == 0

    def test_high_manifest_plan_low_quota_zero_rejected(self):
        plan = _plan(MANIFEST, change={P_HIGH: 1}, risk=RiskLevel.LOW)
        with pytest.raises(InterventionContractError, match="below the manifest lower bound"):
            evaluate_intervention_gate(
                plan=plan,
                manifest=MANIFEST,
                contract=_gate(risk_quota=0),
                risky_interventions=0,
                evidence_digest=EVIDENCE,
            )

    def test_risky_interventions_negative_rejected(self):
        plan = _plan(MANIFEST, change={P_MED: 1}, risk=RiskLevel.MEDIUM)
        with pytest.raises(InterventionContractError, match="negative"):
            evaluate_intervention_gate(
                plan=plan,
                manifest=MANIFEST,
                contract=_gate(),
                risky_interventions=-1,
                evidence_digest=EVIDENCE,
            )

    def test_risky_interventions_bool_rejected(self):
        plan = _plan(MANIFEST, change={P_MED: 1}, risk=RiskLevel.MEDIUM)
        with pytest.raises(InterventionContractError, match="bool"):
            evaluate_intervention_gate(
                plan=plan,
                manifest=MANIFEST,
                contract=_gate(),
                risky_interventions=True,
                evidence_digest=EVIDENCE,
            )

    def test_malformed_evidence_digest_rejected_on_pass_path(self):
        plan = _plan(MANIFEST, change={P_LOW: 1}, risk=RiskLevel.LOW)
        with pytest.raises(InterventionContractError, match="digest"):
            evaluate_intervention_gate(
                plan=plan,
                manifest=MANIFEST,
                contract=_gate(),
                risky_interventions=0,
                evidence_digest="sha256:NOTHEX",
            )


class TestOutcomeBinding:
    def test_plan_digest_mismatch_rejected(self):
        plan = _plan(MANIFEST, change={P_LOW: 1}, risk=RiskLevel.LOW)
        outcome = _outcome("sha256:" + "f" * 64)
        with pytest.raises(InterventionContractError, match="not bound"):
            verify_outcome_binding(outcome, plan)

    def test_correct_plan_digest_accepted(self):
        plan = _plan(MANIFEST, change={P_LOW: 1}, risk=RiskLevel.LOW)
        outcome = _outcome(plan.digest)
        assert verify_outcome_binding(outcome, plan) is outcome

    def test_outcome_progression_constraints(self):
        with pytest.raises(ValidationError, match="apply_started implies write_attempted"):
            _outcome("sha256:" + "f" * 64, apply_started=True, write_attempted=False)


class TestExecutionReceipt:
    def test_illegal_state_combinations(self):
        with pytest.raises(ValidationError, match="apply_started implies write_attempted"):
            InterventionExecutionReceipt(
                plan_digest="sha256:" + "a" * 64, apply_started=True, write_attempted=False
            )
        with pytest.raises(ValidationError, match="rollback_verified implies rollback_attempted"):
            InterventionExecutionReceipt(
                plan_digest="sha256:" + "a" * 64,
                write_attempted=True,
                apply_started=True,
                rollback_verified=True,
                rollback_attempted=False,
            )

    def test_plan_digest_must_be_strict_sha256(self):
        with pytest.raises(ValidationError):
            InterventionExecutionReceipt(plan_digest="not-a-digest")

    def test_stage_cannot_move_backward(self):
        receipt = InterventionExecutionReceipt(
            plan_digest="sha256:" + "a" * 64, write_attempted=True, apply_started=True
        )
        assert receipt.stage is ReceiptStage.APPLY_STARTED
        with pytest.raises(InterventionContractError, match="cannot move backward"):
            receipt.advance(ReceiptStage.WRITE_ATTEMPTED)

    def test_advance_forward_sets_monotonic_flags(self):
        receipt = InterventionExecutionReceipt(plan_digest="sha256:" + "a" * 64)
        assert receipt.stage is ReceiptStage.PLANNED
        assert not receipt.apply_started and not receipt.write_attempted

        at_apply = receipt.advance(ReceiptStage.APPLY_STARTED)
        assert at_apply.write_attempted and at_apply.apply_started
        assert not at_apply.rollback_attempted and not at_apply.rollback_verified

        final = at_apply.advance(ReceiptStage.ROLLBACK_VERIFIED)
        assert final.rollback_attempted and final.rollback_verified

    def test_digest_binds_plan_and_is_recomputable(self):
        plan = _plan(MANIFEST, change={P_LOW: 1}, risk=RiskLevel.LOW)
        receipt = InterventionExecutionReceipt(
            plan_digest=plan.digest, write_attempted=True, apply_started=True
        )
        rebuilt = InterventionExecutionReceipt.model_validate(receipt.model_dump(mode="json"))
        assert receipt.digest == rebuilt.digest
        assert receipt.plan_digest == plan.digest


class TestPurity:
    def test_pure_functions_never_touch_filesystem_or_backend(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise AssertionError("pure function performed IO")

        monkeypatch.setattr("builtins.open", _boom)
        monkeypatch.setattr("pathlib.Path.write_text", _boom)
        monkeypatch.setattr("pathlib.Path.mkdir", _boom)

        plan = _plan(MANIFEST, change={P_MED: 1}, risk=RiskLevel.MEDIUM)
        resolved = resolve_plan_risk(plan, MANIFEST)
        assert resolved.final_risk is RiskLevel.MEDIUM
        assert (
            evaluate_intervention_gate(
                plan=plan,
                manifest=MANIFEST,
                contract=_gate(),
                risky_interventions=0,
                evidence_digest=EVIDENCE,
            )
            is None
        )
        receipt = InterventionExecutionReceipt(plan_digest=plan.digest)
        receipt.advance(ReceiptStage.APPLY_STARTED)
