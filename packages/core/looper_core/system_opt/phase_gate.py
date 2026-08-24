"""M3 动态相位结束门禁合同（workload-tuning.md D4 / overall.md §3.3）。

动态优化必须有显式终点（结束门禁是第一等约束）。本合同把五类 S10 停止
全部**参数化**：每个字段都是任务输入，无默认值；合同 digest 进入证据
身份，改参数即新身份。

判定顺序（固定、可回放）：安全触发 → 负载消失/剧变 → 预算耗尽 →
目标达成 → 收敛。前两类使当前证据链失效，必须最先判；安全触发同时
要求回滚（回滚动作本身属 L6，本层只出停止决定）。

防振荡（D4）：停止后 ``reactivation_holdout_windows`` 内不得重激活
（重激活资格的判定属 D5，本合同只声明保持窗）；每窗口至多一次配置
变更（``single_change_per_window`` 为合同化常量，由动态循环强制）。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.workload import BoundComparator

DYNAMIC_PHASE_GATE_SCHEMA = "looper.dynamic-phase-gate/v1alpha1"
DYNAMIC_PHASE_GATE_V2_SCHEMA = "looper.dynamic-phase-gate/v1alpha2"


class GateStopClass(StrEnum):
    SAFETY_TRIGGERED = "safety-triggered"
    WORKLOAD_VANISHED = "workload-vanished"
    BUDGET_EXHAUSTED = "budget-exhausted"
    TARGET_MET = "target-met"
    CONVERGED = "converged"


class SloTarget(StrictModel):
    """Stop class 1: business metric meets the target for N consecutive windows."""

    metric_id: str = Field(min_length=1, max_length=160)
    comparator: BoundComparator
    bound: float
    hold_windows: int = Field(ge=1)


class ConvergencePolicy(StrictModel):
    """Stop class 2: K consecutive rounds with business LCB at or below threshold."""

    rounds: int = Field(ge=1)
    lcb_threshold: float


class PhaseBudget(StrictModel):
    """Stop class 3: any of the three task-declared budgets exhausted."""

    max_interventions: int = Field(ge=1)
    wall_clock_seconds: float = Field(gt=0)
    risk_quota: int = Field(ge=0)


class DegradationGate(StrictModel):
    """Stop class 4: task-declared significant business regression.

    ``relative_limit`` is the maximal tolerated relative worsening of the
    declared metric versus its pre-intervention window; the metric's own
    direction (workload contract o0 spec) defines "worsening".
    """

    metric_id: str = Field(min_length=1, max_length=160)
    relative_limit: float = Field(gt=0)


class DynamicPhaseGateContract(StrictModel):
    schema_version: Literal[DYNAMIC_PHASE_GATE_SCHEMA] = DYNAMIC_PHASE_GATE_SCHEMA
    workload_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    # None = objective-only workload (no SLO stop class); convergence still applies.
    slo: SloTarget | None = None
    convergence: ConvergencePolicy
    budget: PhaseBudget
    degradation: DegradationGate
    identity_drift_action: Literal["stop-phase"] = "stop-phase"
    reactivation_holdout_windows: int = Field(ge=1)
    single_change_per_window: Literal[True] = True

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class DynamicPhaseGateContractV2(StrictModel):
    """Execution-gated contract used by the two-stage intervention loop.

    Risk quota is enforced before execution by ``evaluate_intervention_gate``;
    the v1 contract remains unchanged for deterministic replay.
    """

    schema_version: Literal[DYNAMIC_PHASE_GATE_V2_SCHEMA] = DYNAMIC_PHASE_GATE_V2_SCHEMA
    workload_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    slo: SloTarget | None = None
    convergence: ConvergencePolicy
    budget: PhaseBudget
    degradation: DegradationGate
    identity_drift_action: Literal["stop-phase"] = "stop-phase"
    reactivation_holdout_windows: int = Field(ge=1)
    single_change_per_window: Literal[True] = True

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def load_dynamic_phase_gate(
    payload: Mapping[str, Any],
) -> DynamicPhaseGateContract | DynamicPhaseGateContractV2:
    """Dispatch by schema without migrating legacy evidence."""

    version = payload.get("schema_version")
    if version == DYNAMIC_PHASE_GATE_SCHEMA:
        return DynamicPhaseGateContract.model_validate(payload)
    if version == DYNAMIC_PHASE_GATE_V2_SCHEMA:
        return DynamicPhaseGateContractV2.model_validate(payload)
    raise ValueError(f"unsupported dynamic phase gate schema_version: {version!r}")


class PhaseGateState(StrictModel):
    """Counters the dynamic loop maintains between gate evaluations.

    Every counter is plain observed fact; derivation (e.g. which rounds count
    as non-improving) happens upstream with explicit formula provenance.
    """

    consecutive_slo_met_windows: int = Field(ge=0)
    consecutive_lcb_threshold_rounds: int = Field(ge=0)
    interventions: int = Field(ge=0)
    risky_interventions: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    identity_drift_events: int = Field(ge=0)
    degradation_events: int = Field(ge=0)
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class GateDecision(StrictModel):
    """One gate evaluation outcome; stopping decisions cite their trigger."""

    schema_version: Literal["looper.phase-gate-decision/v1alpha1"] = (
        "looper.phase-gate-decision/v1alpha1"
    )
    stop: bool
    stop_class: GateStopClass | None = None
    triggered_field: str | None = None
    reason: str = Field(min_length=1, max_length=600)
    contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def stop_consistency(self) -> GateDecision:
        if self.stop and (self.stop_class is None or self.triggered_field is None):
            raise ValueError("a stopping decision must cite its stop class and field")
        if not self.stop and (self.stop_class is not None or self.triggered_field is not None):
            raise ValueError("a continuing decision must not cite a stop class or field")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def evaluate_phase_gate(contract: DynamicPhaseGateContract, state: PhaseGateState) -> GateDecision:
    """Evaluate the ending gate in the fixed order safety -> identity -> budget
    -> target -> convergence; every outcome carries the evidence digest."""

    def decision(stop_class: GateStopClass, field: str, reason: str) -> GateDecision:
        return GateDecision(
            stop=True,
            stop_class=stop_class,
            triggered_field=field,
            reason=reason,
            contract_digest=contract.digest,
            evidence_digest=state.evidence_digest,
        )

    if state.degradation_events > 0:
        return decision(
            GateStopClass.SAFETY_TRIGGERED,
            "degradation",
            f"{state.degradation_events} degradation event(s) exceeded the task-"
            f"declared gate on '{contract.degradation.metric_id}'; roll back and "
            "stop this phase",
        )
    if state.identity_drift_events > 0:
        return decision(
            GateStopClass.WORKLOAD_VANISHED,
            "identity_drift_action",
            f"{state.identity_drift_events} workload identity drift event(s); the "
            "evidence chain for this contract no longer covers the running load",
        )
    if state.interventions >= contract.budget.max_interventions:
        return decision(
            GateStopClass.BUDGET_EXHAUSTED,
            "budget.max_interventions",
            f"interventions {state.interventions} reached the task budget "
            f"{contract.budget.max_interventions}",
        )
    if state.elapsed_seconds >= contract.budget.wall_clock_seconds:
        return decision(
            GateStopClass.BUDGET_EXHAUSTED,
            "budget.wall_clock_seconds",
            f"elapsed {state.elapsed_seconds:.3f}s reached the task wall-clock "
            f"budget {contract.budget.wall_clock_seconds:.3f}s",
        )
    if state.risky_interventions > contract.budget.risk_quota:
        return decision(
            GateStopClass.BUDGET_EXHAUSTED,
            "budget.risk_quota",
            f"risky interventions {state.risky_interventions} exceeded the task "
            f"risk quota {contract.budget.risk_quota}",
        )
    if contract.slo is not None and state.consecutive_slo_met_windows >= contract.slo.hold_windows:
        return decision(
            GateStopClass.TARGET_MET,
            "slo.hold_windows",
            f"SLO met for {state.consecutive_slo_met_windows} consecutive "
            f"windows (required hold {contract.slo.hold_windows})",
        )
    if state.consecutive_lcb_threshold_rounds >= contract.convergence.rounds:
        return decision(
            GateStopClass.CONVERGED,
            "convergence.rounds",
            f"business LCB stayed at or below {contract.convergence.lcb_threshold} "
            f"for {state.consecutive_lcb_threshold_rounds} consecutive rounds "
            f"(required {contract.convergence.rounds})",
        )
    return GateDecision(
        stop=False,
        reason="no stop class triggered; the dynamic phase continues",
        contract_digest=contract.digest,
        evidence_digest=state.evidence_digest,
    )


def evaluate_phase_gate_v2(
    contract: DynamicPhaseGateContractV2, state: PhaseGateState
) -> GateDecision:
    """Evaluate v2 endings; risk quota belongs only to the pre-execution gate."""

    def decision(stop_class: GateStopClass, field: str, reason: str) -> GateDecision:
        return GateDecision(
            stop=True,
            stop_class=stop_class,
            triggered_field=field,
            reason=reason,
            contract_digest=contract.digest,
            evidence_digest=state.evidence_digest,
        )

    if state.degradation_events > 0:
        return decision(
            GateStopClass.SAFETY_TRIGGERED,
            "degradation",
            f"{state.degradation_events} degradation event(s) exceeded the task-"
            f"declared gate on '{contract.degradation.metric_id}'; roll back and "
            "stop this phase",
        )
    if state.identity_drift_events > 0:
        return decision(
            GateStopClass.WORKLOAD_VANISHED,
            "identity_drift_action",
            f"{state.identity_drift_events} workload identity drift event(s); the "
            "evidence chain for this contract no longer covers the running load",
        )
    if state.interventions >= contract.budget.max_interventions:
        return decision(
            GateStopClass.BUDGET_EXHAUSTED,
            "budget.max_interventions",
            f"interventions {state.interventions} reached the task budget "
            f"{contract.budget.max_interventions}",
        )
    if state.elapsed_seconds >= contract.budget.wall_clock_seconds:
        return decision(
            GateStopClass.BUDGET_EXHAUSTED,
            "budget.wall_clock_seconds",
            f"elapsed {state.elapsed_seconds:.3f}s reached the task wall-clock "
            f"budget {contract.budget.wall_clock_seconds:.3f}s",
        )
    if contract.slo is not None and state.consecutive_slo_met_windows >= contract.slo.hold_windows:
        return decision(
            GateStopClass.TARGET_MET,
            "slo.hold_windows",
            f"SLO met for {state.consecutive_slo_met_windows} consecutive "
            f"windows (required hold {contract.slo.hold_windows})",
        )
    if state.consecutive_lcb_threshold_rounds >= contract.convergence.rounds:
        return decision(
            GateStopClass.CONVERGED,
            "convergence.rounds",
            f"business LCB stayed at or below {contract.convergence.lcb_threshold} "
            f"for {state.consecutive_lcb_threshold_rounds} consecutive rounds "
            f"(required {contract.convergence.rounds})",
        )
    return GateDecision(
        stop=False,
        reason="no stop class triggered; the dynamic phase continues",
        contract_digest=contract.digest,
        evidence_digest=state.evidence_digest,
    )


__all__ = [
    "DYNAMIC_PHASE_GATE_SCHEMA",
    "DYNAMIC_PHASE_GATE_V2_SCHEMA",
    "ConvergencePolicy",
    "DegradationGate",
    "DynamicPhaseGateContract",
    "DynamicPhaseGateContractV2",
    "GateDecision",
    "GateStopClass",
    "PhaseBudget",
    "PhaseGateState",
    "SloTarget",
    "evaluate_phase_gate",
    "evaluate_phase_gate_v2",
    "load_dynamic_phase_gate",
]
