"""M3 动态相位两阶段干预合同（phase-gate R3）：规划 → 执行前门禁 → 执行 → receipt。

R3 把「干预」拆成两个一等记录：

    prepare_intervention(hypothesis) -> InterventionPlan   # 纯规划，不写配置
    → 执行前门禁（single_change / risk_quota）             # evaluate_intervention_gate
    → execute_intervention(plan) -> InterventionOutcome    # 真正施加/复测/回退

本模块只落地**模型与纯函数**（不接 backend、不落盘），供 D5-I2 动态循环接线：

- ``InterventionPlan.digest`` / ``change_count`` 是计算属性，调用方无法伪造；
- ``resolve_plan_risk`` 以 manifest 为风险下界：任务风险只能抬高、不能降低，
  缺任务风险、缺 manifest 绑定、绑定不一致、kind/rationale 与是否抬高不一致
  一律 fail-closed；``RiskSource.items`` 必须按 item_id 严格升序（集合语义，
  反序即拒，防止重排改变 plan digest）；
- ``evaluate_intervention_gate`` 在 execute 之前做纯决策：先强制
  ``resolve_plan_risk(plan, manifest)``（自报 low 无法绕过 high manifest），
  再按**解析后的 final_risk** 做 single_change / risk_quota 检查；不修改 state、
  不执行写入，被拒时产出既有 ``GateDecision``；
- ``InterventionExecutionReceipt`` 是版本化的执行流水账，digest 可重算并绑定
  plan.digest，状态只能前进不能倒退；本阶段不伪称崩溃后已持久化。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigManifest, RiskLevel
from looper_core.system_opt.hypothesis import (
    ComponentHypothesis,
    InterventionExperiment,
)
from looper_core.system_opt.phase_gate import (
    DynamicPhaseGateContract,
    GateDecision,
    GateStopClass,
)
from looper_core.system_opt.safety import SafetyState

INTERVENTION_PLAN_SCHEMA = "looper.intervention-plan/v1alpha1"
INTERVENTION_OUTCOME_SCHEMA = "looper.intervention-outcome/v1alpha1"
INTERVENTION_RECEIPT_SCHEMA = "looper.intervention-execution-receipt/v1alpha1"
INTERVENTION_RISK_SOURCE_SCHEMA = "looper.intervention-risk-source/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"
_RISK_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


def _risk_rank(level: RiskLevel) -> int:
    return _RISK_RANK[level]


def _require_digest(value: str, field: str) -> str:
    if not re.fullmatch(_DIGEST, value):
        raise InterventionContractError(f"{field} must be a strict sha256 digest")
    return value


class InterventionContractError(ValueError):
    """Raised when a two-phase intervention contract is violated."""


class RiskSourceKind(StrEnum):
    MANIFEST_DERIVED = "manifest-derived"
    TASK_OVERRIDE = "task-override"


class RiskSourceItem(StrictModel):
    """One manifest config item the planner binds to its change, with its risk."""

    item_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9.-]*$")
    risk: RiskLevel


class RiskSource(StrictModel):
    """The planner's auditable claim of where plan risk comes from.

    ``manifest_digest`` binds the manifest the claim was derived from; ``items``
    lists the config items the change touches together with their per-item risk,
    in strict ascending ``item_id`` order (callers cannot reorder to change a
    plan digest). ``resolve_plan_risk`` re-verifies this claim against the real
    manifest.
    """

    schema_version: Literal[INTERVENTION_RISK_SOURCE_SCHEMA]
    kind: RiskSourceKind
    manifest_digest: str = Field(pattern=_DIGEST)
    items: list[RiskSourceItem] = Field(min_length=1)
    rationale: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_ordered_items(self) -> RiskSource:
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("risk source items must be unique")
        if ids != sorted(ids):
            raise ValueError("risk source items must be ordered by item_id (ascending)")
        return self


class InterventionPlan(StrictModel):
    """A prepared change; nothing is written at this stage.

    ``digest`` and ``change_count`` are computed properties so no caller can
    forge them; ``risk`` is explicitly required (no silent low default).
    """

    schema_version: Literal[INTERVENTION_PLAN_SCHEMA] = INTERVENTION_PLAN_SCHEMA
    hypothesis: ComponentHypothesis
    change: dict[str, Any] = Field(min_length=1)
    risk: RiskLevel
    risk_source: RiskSource

    @model_validator(mode="after")
    def reject_blank_change_keys(self) -> InterventionPlan:
        if any(not key.strip() for key in self.change):
            raise ValueError("change parameter ids cannot be blank")
        return self

    @property
    def change_count(self) -> int:
        return len(self.change)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class InterventionOutcome(StrictModel):
    """The result of executing a plan; ``plan_digest`` binds the plan identity.

    The booleans are plain observed facts (write/apply/rollback progress);
    ``verify_outcome_binding`` enforces that ``plan_digest`` equals the plan's
    canonical digest — the binding is explicit, not a comment.
    """

    schema_version: Literal[INTERVENTION_OUTCOME_SCHEMA] = INTERVENTION_OUTCOME_SCHEMA
    plan_digest: str = Field(pattern=_DIGEST)
    write_attempted: bool
    apply_started: bool
    rollback_attempted: bool
    rollback_verified: bool
    experiment: InterventionExperiment | None = None
    safety_state: SafetyState
    evidence_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_progression(self) -> InterventionOutcome:
        if self.apply_started and not self.write_attempted:
            raise ValueError("apply_started implies write_attempted")
        if self.rollback_attempted and not self.apply_started:
            raise ValueError("rollback_attempted implies apply_started")
        if self.rollback_verified and not self.rollback_attempted:
            raise ValueError("rollback_verified implies rollback_attempted")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class ReceiptStage(StrEnum):
    PLANNED = "planned"
    WRITE_ATTEMPTED = "write-attempted"
    APPLY_STARTED = "apply-started"
    ROLLBACK_ATTEMPTED = "rollback-attempted"
    ROLLBACK_VERIFIED = "rollback-verified"


_STAGE_RANK = {
    ReceiptStage.PLANNED: 0,
    ReceiptStage.WRITE_ATTEMPTED: 1,
    ReceiptStage.APPLY_STARTED: 2,
    ReceiptStage.ROLLBACK_ATTEMPTED: 3,
    ReceiptStage.ROLLBACK_VERIFIED: 4,
}


def _flags_for(stage: ReceiptStage) -> dict[str, bool]:
    rank = _STAGE_RANK[stage]
    return {
        "write_attempted": rank >= _STAGE_RANK[ReceiptStage.WRITE_ATTEMPTED],
        "apply_started": rank >= _STAGE_RANK[ReceiptStage.APPLY_STARTED],
        "rollback_attempted": rank >= _STAGE_RANK[ReceiptStage.ROLLBACK_ATTEMPTED],
        "rollback_verified": rank >= _STAGE_RANK[ReceiptStage.ROLLBACK_VERIFIED],
    }


class InterventionExecutionReceipt(StrictModel):
    """Versioned, independently-persistable execution journal.

    ``digest`` is recomputable and binds ``plan_digest`` (itself the plan's
    canonical digest). The progress flags form a monotonic chain, so
    ``apply_started`` implies ``write_attempted`` and ``rollback_verified``
    implies ``rollback_attempted``; ``advance`` never moves the stage backward.

    This phase models the receipt and its constraints only: it does not wire
    persistence and does not claim a receipt survives a crash.
    """

    schema_version: Literal[INTERVENTION_RECEIPT_SCHEMA] = INTERVENTION_RECEIPT_SCHEMA
    plan_digest: str = Field(pattern=_DIGEST)
    write_attempted: bool = False
    apply_started: bool = False
    rollback_attempted: bool = False
    rollback_verified: bool = False
    safety_state: SafetyState | None = None
    experiment: InterventionExperiment | None = None
    evidence_digest: str | None = Field(default=None, pattern=_DIGEST)
    error: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_progression(self) -> InterventionExecutionReceipt:
        if self.apply_started and not self.write_attempted:
            raise ValueError("apply_started implies write_attempted")
        if self.rollback_attempted and not self.apply_started:
            raise ValueError("rollback_attempted implies apply_started")
        if self.rollback_verified and not self.rollback_attempted:
            raise ValueError("rollback_verified implies rollback_attempted")
        return self

    @property
    def stage(self) -> ReceiptStage:
        if self.rollback_verified:
            return ReceiptStage.ROLLBACK_VERIFIED
        if self.rollback_attempted:
            return ReceiptStage.ROLLBACK_ATTEMPTED
        if self.apply_started:
            return ReceiptStage.APPLY_STARTED
        if self.write_attempted:
            return ReceiptStage.WRITE_ATTEMPTED
        return ReceiptStage.PLANNED

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))

    def advance(self, stage: ReceiptStage) -> InterventionExecutionReceipt:
        """Return a receipt advanced to at least ``stage``; never moves backward."""
        if _STAGE_RANK[stage] < _STAGE_RANK[self.stage]:
            raise InterventionContractError(
                f"receipt stage cannot move backward from '{self.stage.value}' "
                f"to '{stage.value}'"
            )
        return self.model_copy(update=_flags_for(stage))


class ResolvedRiskItem(StrictModel):
    """One auditable resolved binding: change key <-> manifest item <-> manifest risk."""

    parameter_id: str = Field(min_length=1, max_length=160)
    item_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9.-]*$")
    manifest_risk: RiskLevel


class ResolvedPlanRisk(StrictModel):
    """The auditable output of ``resolve_plan_risk``.

    ``plan_digest`` binds the resolved risk back to the plan it was derived
    from; ``manifest_risk`` is the highest manifest risk over the change and the
    task risk is a lower bound, so ``final_risk`` never drops below either.
    """

    plan_digest: str = Field(pattern=_DIGEST)
    manifest_digest: str = Field(pattern=_DIGEST)
    items: list[ResolvedRiskItem] = Field(min_length=1)
    manifest_risk: RiskLevel
    task_risk: RiskLevel
    final_risk: RiskLevel

    @model_validator(mode="after")
    def validate_consistency(self) -> ResolvedPlanRisk:
        if _risk_rank(self.final_risk) < _risk_rank(self.manifest_risk):
            raise ValueError("final risk cannot fall below the manifest risk")
        if _risk_rank(self.final_risk) < _risk_rank(self.task_risk):
            raise ValueError("final risk cannot fall below the task risk")
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("resolved risk items must be unique")
        if ids != sorted(ids):
            raise ValueError("resolved risk items must be ordered by item_id")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def resolve_plan_risk(
    plan: InterventionPlan, manifest: ConfigManifest
) -> ResolvedPlanRisk:
    """Validate the plan's risk claim against the manifest (manifest is a lower bound).

    Fails closed (raises :class:`InterventionContractError`) on: a manifest digest
    mismatch, a change key not bound in the risk source, a risk-source item that
    does not correspond to the change, a per-item risk that disagrees with the
    manifest, a task risk below the manifest's highest risk, or a risk-source
    kind/rationale inconsistent with whether the task risk was raised. Pure:
    never touches a backend.
    """

    if plan.risk_source.manifest_digest != manifest.digest:
        raise InterventionContractError(
            "risk source is not bound to the provided manifest"
        )

    bindings = {item.item_id: item for item in plan.risk_source.items}
    resolved: list[ResolvedRiskItem] = []
    for parameter_id in sorted(plan.change):
        try:
            item = manifest.item_for_parameter(parameter_id)
        except KeyError as error:
            raise InterventionContractError(
                f"change references an unknown config item: {error.args[0]}"
            ) from error
        binding = bindings.get(item.id)
        if binding is None:
            raise InterventionContractError(
                f"change '{parameter_id}' has no manifest risk binding"
            )
        if binding.risk is not item.risk:
            raise InterventionContractError(
                f"risk binding for '{item.id}' disagrees with the manifest "
                f"(binding={binding.risk.value}, manifest={item.risk.value})"
            )
        resolved.append(
            ResolvedRiskItem(
                parameter_id=parameter_id, item_id=item.id, manifest_risk=item.risk
            )
        )

    changed_ids = {entry.item_id for entry in resolved}
    if set(bindings) != changed_ids:
        raise InterventionContractError("risk source items do not match the planned change")

    manifest_risk = max((entry.manifest_risk for entry in resolved), key=_risk_rank)
    if _risk_rank(plan.risk) < _risk_rank(manifest_risk):
        raise InterventionContractError(
            f"task risk {plan.risk.value} is below the manifest lower bound "
            f"{manifest_risk.value}"
        )
    if _risk_rank(plan.risk) == _risk_rank(manifest_risk):
        if plan.risk_source.kind is not RiskSourceKind.MANIFEST_DERIVED:
            raise InterventionContractError(
                "task risk is not raised above the manifest; the risk source "
                "kind must be 'manifest-derived'"
            )
    else:
        if plan.risk_source.kind is not RiskSourceKind.TASK_OVERRIDE:
            raise InterventionContractError(
                "task risk is raised above the manifest; the risk source kind "
                "must be 'task-override'"
            )
        if not plan.risk_source.rationale or not plan.risk_source.rationale.strip():
            raise InterventionContractError(
                "a task-override risk requires a non-empty rationale"
            )

    final_risk = max(plan.risk, manifest_risk, key=_risk_rank)
    return ResolvedPlanRisk(
        plan_digest=plan.digest,
        manifest_digest=manifest.digest,
        items=resolved,
        manifest_risk=manifest_risk,
        task_risk=plan.risk,
        final_risk=final_risk,
    )


def evaluate_intervention_gate(
    *,
    plan: InterventionPlan,
    manifest: ConfigManifest,
    contract: DynamicPhaseGateContract,
    risky_interventions: int,
    evidence_digest: str,
) -> GateDecision | None:
    """Pure pre-execution gate: decide before execute, mutate nothing, write nothing.

    Resolves the plan's risk against the manifest (so a self-reported low risk
    can never bypass a high-risk manifest item), then applies the two pre-flight
    checks in order: single-change first, then the risk quota on the *resolved*
    final risk. Returns a stopping :class:`GateDecision` when the plan may not
    execute, or ``None`` to proceed.
    """

    if isinstance(risky_interventions, bool):
        raise InterventionContractError("risky_interventions must be an integer, not a bool")
    if not isinstance(risky_interventions, int):
        raise InterventionContractError("risky_interventions must be an integer")
    if risky_interventions < 0:
        raise InterventionContractError("risky_interventions cannot be negative")
    _require_digest(evidence_digest, "evidence_digest")

    resolved = resolve_plan_risk(plan, manifest)

    if contract.single_change_per_window and plan.change_count > 1:
        return GateDecision(
            stop=True,
            stop_class=GateStopClass.BUDGET_EXHAUSTED,
            triggered_field="single_change_per_window",
            reason=(
                f"plan declares {plan.change_count} changes but the gate contract "
                "allows one per window"
            ),
            contract_digest=contract.digest,
            evidence_digest=evidence_digest,
        )
    if (
        resolved.final_risk != RiskLevel.LOW
        and risky_interventions >= contract.budget.risk_quota
    ):
        return GateDecision(
            stop=True,
            stop_class=GateStopClass.BUDGET_EXHAUSTED,
            triggered_field="budget.risk_quota",
            reason=(
                f"risky interventions {risky_interventions} reached the task risk "
                f"quota {contract.budget.risk_quota} (final risk "
                f"{resolved.final_risk.value})"
            ),
            contract_digest=contract.digest,
            evidence_digest=evidence_digest,
        )
    return None


def verify_outcome_binding(
    outcome: InterventionOutcome, plan: InterventionPlan
) -> InterventionOutcome:
    """Explicitly enforce ``outcome.plan_digest == plan.digest`` (returns the outcome)."""

    if outcome.plan_digest != plan.digest:
        raise InterventionContractError(
            "intervention outcome is not bound to the plan's canonical digest"
        )
    return outcome


__all__ = [
    "INTERVENTION_OUTCOME_SCHEMA",
    "INTERVENTION_PLAN_SCHEMA",
    "INTERVENTION_RECEIPT_SCHEMA",
    "INTERVENTION_RISK_SOURCE_SCHEMA",
    "InterventionContractError",
    "InterventionExecutionReceipt",
    "InterventionOutcome",
    "InterventionPlan",
    "ReceiptStage",
    "ResolvedPlanRisk",
    "ResolvedRiskItem",
    "RiskSource",
    "RiskSourceItem",
    "RiskSourceKind",
    "evaluate_intervention_gate",
    "resolve_plan_risk",
    "verify_outcome_binding",
]
