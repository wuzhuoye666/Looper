"""M3 动态相位重激活资格判定（workload-tuning.md D5，SO-D019 确认 A+B 案）。

结束 ≠ 永久关闭：负载模式显著变化可申请重新进入优化。确认的方向：

- **A 身份漂移**：workload 身份 digest 漂移（v1 精确比对，见 observation 的
  ``WorkloadIdentityDrift``）→ 立即具备资格。注意语义：漂移事件本身先经
  结束门禁停掉旧相位（证据链失效），资格是针对**在新声明（可能更新）的
  workload 合同之下**重开相位；
- **B SLO 持续违反**：曾达标后业务指标连续 ``slo_violation_windows`` 窗
  违反 SLO → 迟滞后具备资格（迟滞天然防噪）；
- C（O1 分布漂移检验）列 M6+（需校准数据，误激活风险最高）。

硬规则：资格消耗 ``reactivation_budget``（每次重激活计数，超预算不再具备
资格）；结束门禁的 ``reactivation_holdout_windows`` 保持窗内**不得**具备
资格（防振荡）；**资格 ≠ 自动重启**——是否重开相位由任务所有者决定，
本模块只判定并记录。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.phase_gate import DynamicPhaseGateContract

REACTIVATION_SCHEMA = "looper.reactivation-decision/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class ReactivationTrigger(StrEnum):
    IDENTITY_DRIFT = "identity-drift"
    SLO_VIOLATION_PERSISTENCE = "slo-violation-persistence"


class ReactivationPolicy(StrictModel):
    """Task-injected reactivation parameters (no defaults; C stays M6+)."""

    max_reactivations: int = Field(ge=0)
    slo_violation_windows: int = Field(ge=1)


class ReactivationState(StrictModel):
    """Counters maintained between eligibility evaluations (observed facts)."""

    reactivations_used: int = Field(ge=0)
    windows_since_stop: int = Field(ge=0)
    consecutive_slo_violations: int = Field(ge=0)
    identity_drift_events_since_stop: int = Field(ge=0)


class ReactivationDecision(StrictModel):
    schema_version: Literal[REACTIVATION_SCHEMA] = REACTIVATION_SCHEMA
    eligible: bool
    trigger: ReactivationTrigger | None = None
    reason: str = Field(min_length=1, max_length=600)
    gate_contract_digest: str = Field(pattern=_DIGEST)
    evidence_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def eligibility_consistency(self) -> ReactivationDecision:
        if self.eligible and self.trigger is None:
            raise ValueError("an eligible decision must cite its trigger")
        if not self.eligible and self.trigger is not None:
            raise ValueError("an ineligible decision must not cite a trigger")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def evaluate_reactivation(
    gate: DynamicPhaseGateContract,
    policy: ReactivationPolicy,
    state: ReactivationState,
    *,
    evidence_digest: str,
) -> ReactivationDecision:
    """Judge reactivation eligibility: budget -> holdout -> drift -> SLO persistence.

    Fixed order; eligibility never auto-restarts the phase (task owner decides).
    """

    def outcome(eligible: bool, trigger: ReactivationTrigger | None, reason: str):
        return ReactivationDecision(
            eligible=eligible,
            trigger=trigger,
            reason=reason,
            gate_contract_digest=gate.digest,
            evidence_digest=evidence_digest,
        )

    if state.reactivations_used >= policy.max_reactivations:
        return outcome(
            False,
            None,
            f"reactivation budget exhausted: {state.reactivations_used} of "
            f"{policy.max_reactivations} used",
        )
    if state.windows_since_stop < gate.reactivation_holdout_windows:
        return outcome(
            False,
            None,
            f"holdout active: {state.windows_since_stop} of "
            f"{gate.reactivation_holdout_windows} windows since stop",
        )
    if state.identity_drift_events_since_stop > 0:
        return outcome(
            True,
            ReactivationTrigger.IDENTITY_DRIFT,
            f"{state.identity_drift_events_since_stop} identity drift event(s) "
            "since stop; re-entry is eligible under a freshly declared workload "
            "contract (the drift already stopped the previous phase)",
        )
    if state.consecutive_slo_violations >= policy.slo_violation_windows:
        return outcome(
            True,
            ReactivationTrigger.SLO_VIOLATION_PERSISTENCE,
            f"SLO violated for {state.consecutive_slo_violations} consecutive "
            f"windows (hysteresis threshold {policy.slo_violation_windows})",
        )
    return outcome(
        False,
        None,
        "no reactivation trigger: no drift events and SLO violations below "
        "the hysteresis threshold",
    )


__all__ = [
    "REACTIVATION_SCHEMA",
    "ReactivationDecision",
    "ReactivationPolicy",
    "ReactivationState",
    "ReactivationTrigger",
    "evaluate_reactivation",
]
