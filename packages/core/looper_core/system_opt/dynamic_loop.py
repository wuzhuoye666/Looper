"""M3 动态相位运行时：把六个构件串成一个受结束门禁约束的有限循环。

构件链（全部已在本包内落地，本模块只编排不重算）：

    workload 合同（D0）→ 观察窗口（D1，O0 解析 + 身份精确比对）
    → SLO/症状（D2 输入）→ 假设账本（D2 三硬规则）
    → 干预实验（外部注入：L1 施加 + 同 workload 复测在调用方）
    → S9 复验观测（D3）→ 结束门禁（D4，每窗口判定）
    → 停止收工；重激活（D5）属相位间生命周期，由外部在本运行之后判定。

边界（SO-D020）：负载由测试侧外部提供——本循环通过注入的
``load_identity``/``o0_source``/``o1_source`` 读取外部事实，**永不启动负载**；
配置施加（L1）与复测测量同样以注入回调表达（``intervention``/``retest``），
真实后端接线是这些回调的适配工作。

fail-closed 方向：探测回调缺失 → 假设停留在 proposed、不干预；
竞争假设不足（D2 规则 1）→ request_intervention 被账本拒绝并记录；
身份漂移 → 立即经门禁停止（证据链失效）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from looper_core.analysis import aggregate
from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import ComponentMetricSnapshot
from looper_core.system_opt.hypothesis import (
    ComponentHypothesis,
    HypothesisLedger,
    HypothesisRoutingError,
    HypothesisStatus,
    InterventionExperiment,
    SymptomRecord,
)
from looper_core.system_opt.observation import (
    ObservationWindow,
    WorkloadIdentityDrift,
    record_window,
)
from looper_core.system_opt.phase_gate import (
    DynamicPhaseGateContract,
    GateDecision,
    PhaseGateState,
    evaluate_phase_gate,
)
from looper_core.system_opt.result_vector import (
    PromotionContract,
    PromotionEvidence,
    VerificationObservation,
    evaluate_promotion,
)
from looper_core.system_opt.verification import RetestOutcome, verification_observation
from looper_core.system_opt.workload import (
    BoundComparator,
    LoadCommandIdentity,
    WorkloadContract,
)

DYNAMIC_PHASE_RUN_SCHEMA = "looper.dynamic-phase-run/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


def _slo_met(value: float, comparator: BoundComparator, bound: float) -> bool:
    if comparator is BoundComparator.AT_LEAST:
        return value >= bound
    if comparator is BoundComparator.AT_MOST:
        return value <= bound
    return value == bound


class WindowAction(StrEnum):
    OBSERVE = "observe"
    SYMPTOM_REGISTERED = "symptom-registered"
    PROBE_BLOCKED = "probe-blocked"
    INTERVENED = "intervened"
    VERIFIED = "verified"
    IDENTITY_DRIFT = "identity-drift"


class DynamicWindowRecord(StrictModel):
    window_id: str = Field(min_length=1, max_length=160)
    observation_window_digest: str | None = Field(default=None, pattern=_DIGEST)
    slo_met: bool | None = None
    action: WindowAction
    hypothesis_id: str | None = Field(default=None, min_length=1, max_length=160)
    note: str | None = Field(default=None, min_length=1, max_length=500)


class DynamicPhaseRun(StrictModel):
    schema_version: Literal[DYNAMIC_PHASE_RUN_SCHEMA] = DYNAMIC_PHASE_RUN_SCHEMA
    workload_contract_digest: str = Field(pattern=_DIGEST)
    gate_contract_digest: str = Field(pattern=_DIGEST)
    windows: list[DynamicWindowRecord] = Field(default_factory=list)
    verification_observations: list[VerificationObservation] = Field(default_factory=list)
    promotion: PromotionEvidence | None = None
    hypothesis_ledger_digest: str | None = Field(default=None, pattern=_DIGEST)
    stop_gate_decision: GateDecision | None = None
    note: str | None = Field(default=None, min_length=1, max_length=500)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def run_dynamic_phase(
    *,
    contract: WorkloadContract,
    gate_contract: DynamicPhaseGateContract,
    promotion_contract: PromotionContract,
    environment_digest: str,
    max_windows: int,
    probe_top_k: int,
    load_identity: Callable[[str], LoadCommandIdentity],
    o0_source: Callable[[str], str],
    hypothesis_source: Callable[[SymptomRecord], list[ComponentHypothesis]],
    clock: Callable[[], datetime],
    o1_source: Callable[[str], list[ComponentMetricSnapshot]] | None = None,
    component_probe: Callable[[ComponentHypothesis, ObservationWindow], str] | None = None,
    intervention: (
        Callable[[ComponentHypothesis], InterventionExperiment | None] | None
    ) = None,
    retest: Callable[[str], RetestOutcome] | None = None,
    verification_window_count: int = 0,
) -> DynamicPhaseRun:
    """Run one bounded dynamic phase; every stop is an explicit gate decision.

    Task inputs are explicit (max_windows, probe_top_k, verification_window_count);
    the gate contract owns budgets. Symptom detection v1 = SLO violation of the
    contract's declared SLO (objective-only workloads raise no symptoms).
    """

    if max_windows < 1 or probe_top_k < 1 or verification_window_count < 0:
        raise ValueError("max_windows and probe_top_k need >=1; verification windows >=0")
    if gate_contract.workload_contract_digest != contract.digest:
        raise ValueError("gate contract is not bound to this workload contract")

    ledger = HypothesisLedger()
    windows: list[DynamicWindowRecord] = []
    observations: list[VerificationObservation] = []
    promotion: PromotionEvidence | None = None
    symptom_registered = False
    interventions = 0
    elapsed = 0.0
    gate_state = PhaseGateState(
        consecutive_slo_met_windows=0,
        consecutive_lcb_threshold_rounds=0,
        interventions=0,
        risky_interventions=0,
        elapsed_seconds=0.0,
        identity_drift_events=0,
        degradation_events=0,
        evidence_digest=contract.digest,
    )

    def evaluate(window_digest: str) -> GateDecision:
        return evaluate_phase_gate(
            gate_contract, gate_state.model_copy(update={"evidence_digest": window_digest})
        )

    for index in range(1, max_windows + 1):
        window_id = f"window-{index}"
        started = clock()
        try:
            window = record_window(
                contract,
                window_id=window_id,
                phase_id=contract.phases[0].phase_id,
                load_command=load_identity(window_id),
                o0_raw=o0_source(window_id),
                o1=o1_source(window_id) if o1_source is not None else None,
                started_at=started,
                finished_at=clock(),
            )
        except WorkloadIdentityDrift:
            gate_state = gate_state.model_copy(
                update={"identity_drift_events": gate_state.identity_drift_events + 1}
            )
            decision = evaluate(gate_state.evidence_digest)
            windows.append(
                DynamicWindowRecord(
                    window_id=window_id,
                    action=WindowAction.IDENTITY_DRIFT,
                    note="identity drift stopped the phase via the gate",
                )
            )
            return DynamicPhaseRun(
                workload_contract_digest=contract.digest,
                gate_contract_digest=gate_contract.digest,
                windows=windows,
                verification_observations=observations,
                promotion=promotion,
                hypothesis_ledger_digest=ledger.digest,
                stop_gate_decision=decision,
            )
        elapsed += (window.finished_at - window.started_at).total_seconds()

        slo = contract.slos[0] if contract.slos else None
        slo_met: bool | None = None
        if slo is not None:
            spec = next(
                (m for m in contract.o0_metrics if m.metric_id == slo.metric_id), None
            )
            if spec is None:
                raise ValueError("SLO metric is not declared by the workload contract")
            observation = next(o for o in window.o0 if o.metric_id == spec.metric_id)
            value = aggregate(observation.values, spec.aggregation)
            slo_met = _slo_met(value, slo.comparator, slo.bound)

        action = WindowAction.OBSERVE
        hypothesis_id: str | None = None
        note: str | None = None

        if slo_met is False and not symptom_registered:
            symptom = SymptomRecord(
                symptom_id=f"symptom-{window_id}",
                window_id=window_id,
                workload_contract_digest=contract.digest,
                evidence_digest=window.digest,
                description="SLO violated by the observation window",
            )
            ledger.register_symptom(symptom)
            for hypothesis in hypothesis_source(symptom):
                ledger.register_hypothesis(hypothesis)
            symptom_registered = True
            action = WindowAction.SYMPTOM_REGISTERED
            note = f"{len(ledger.for_symptom(symptom.symptom_id))} hypotheses registered"
        elif (
            symptom_registered
            and component_probe is not None
            and intervention is not None
        ):
            queue = ledger.probe_queue(top_k=probe_top_k)
            if queue:
                head = queue[0]
                hypothesis_id = head.hypothesis_id
                if head.status is HypothesisStatus.PROPOSED:
                    head = ledger.begin_probing(head.hypothesis_id, component_probe(head, window))
                    note = "advanced to probing with component evidence"
                else:
                    try:
                        ledger.request_intervention(head.hypothesis_id)
                    except HypothesisRoutingError as error:
                        action = WindowAction.PROBE_BLOCKED
                        note = str(error)
                    else:
                        experiment = intervention(head)
                        interventions += 1
                        if experiment is None:
                            action = WindowAction.PROBE_BLOCKED
                            note = "intervention callable declined to act this window"
                        elif experiment.accepted:
                            confirmed = ledger.confirm(head.hypothesis_id, experiment)
                            action = WindowAction.INTERVENED
                            note = "business retest accepted the hypothesis"
                            hypothesis_id = confirmed.hypothesis_id
                            if retest is not None:
                                for v in range(1, verification_window_count + 1):
                                    verify_id = f"verify-{window_id}-{v}"
                                    retest_outcome = retest(verify_id)
                                    observations.append(
                                        verification_observation(
                                            window_id=verify_id,
                                            promoted_candidate_id=confirmed.hypothesis_id,
                                            environment_digest=environment_digest,
                                            outcome=retest_outcome.improvement,
                                            evidence_digest=(
                                                retest_outcome.measurement_batch_digest
                                            ),
                                        )
                                    )
                                action = WindowAction.VERIFIED
                                promotion = evaluate_promotion(observations, promotion_contract)
                        else:
                            ledger.refute(head.hypothesis_id, experiment.measurement_batch_digest)
                            action = WindowAction.INTERVENED
                            note = "business retest rejected the hypothesis; refuted"

        gate_state = gate_state.model_copy(
            update={
                "consecutive_slo_met_windows": (
                    gate_state.consecutive_slo_met_windows + 1
                    if slo_met
                    else 0
                    if slo_met is False
                    else gate_state.consecutive_slo_met_windows
                ),
                "interventions": interventions,
                "elapsed_seconds": elapsed,
            }
        )
        decision = evaluate(window.digest)
        windows.append(
            DynamicWindowRecord(
                window_id=window_id,
                observation_window_digest=window.digest,
                slo_met=slo_met,
                action=action,
                hypothesis_id=hypothesis_id,
                note=note,
            )
        )
        if decision.stop:
            return DynamicPhaseRun(
                workload_contract_digest=contract.digest,
                gate_contract_digest=gate_contract.digest,
                windows=windows,
                verification_observations=observations,
                promotion=promotion,
                hypothesis_ledger_digest=ledger.digest,
                stop_gate_decision=decision,
            )

    final = evaluate(gate_state.evidence_digest)
    return DynamicPhaseRun(
        workload_contract_digest=contract.digest,
        gate_contract_digest=gate_contract.digest,
        windows=windows,
        verification_observations=observations,
        promotion=promotion,
        hypothesis_ledger_digest=ledger.digest,
        stop_gate_decision=final,
        note="window budget reached; the final gate decision is recorded as-is",
    )


__all__ = [
    "DYNAMIC_PHASE_RUN_SCHEMA",
    "DynamicPhaseRun",
    "DynamicWindowRecord",
    "WindowAction",
    "run_dynamic_phase",
]
