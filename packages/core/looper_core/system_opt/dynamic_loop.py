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

from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from looper_core.analysis import aggregate
from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import ComponentMetricSnapshot
from looper_core.system_opt.config_manifest import ConfigManifest, RiskLevel
from looper_core.system_opt.dynamic_adapters import (
    DynamicInterventionError,
    DynamicInterventionExecution,
)
from looper_core.system_opt.hypothesis import (
    ComponentHypothesis,
    HypothesisLedger,
    HypothesisRoutingError,
    HypothesisStatus,
    InterventionExperiment,
    SymptomRecord,
)
from looper_core.system_opt.intervention import (
    InterventionPlan,
    evaluate_intervention_gate,
    resolve_plan_risk,
)
from looper_core.system_opt.observation import (
    ObservationWindow,
    WorkloadIdentityDrift,
    record_window,
)
from looper_core.system_opt.phase_gate import (
    DynamicPhaseGateContract,
    DynamicPhaseGateContractV2,
    GateDecision,
    GateStopClass,
    PhaseGateState,
    evaluate_phase_gate,
    evaluate_phase_gate_v2,
)
from looper_core.system_opt.result_vector import (
    PromotionContract,
    PromotionEvidence,
    VerificationObservation,
    evaluate_promotion,
)
from looper_core.system_opt.safety import SafetyState
from looper_core.system_opt.verification import RetestOutcome, verification_observation
from looper_core.system_opt.workload import (
    BoundComparator,
    LoadCommandIdentity,
    O0MetricDirection,
    WorkloadContract,
)

DYNAMIC_PHASE_RUN_SCHEMA = "looper.dynamic-phase-run/v1alpha1"
DYNAMIC_PHASE_RUN_V2_SCHEMA = "looper.dynamic-phase-run/v1alpha2"
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
    GATE_REJECTED = "gate-rejected"
    INTERVENTION_FAILED = "intervention-failed"


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


class DynamicWindowRecordV2(StrictModel):
    window_id: str = Field(min_length=1, max_length=160)
    observation_window_digest: str | None = Field(default=None, pattern=_DIGEST)
    slo_met: bool | None = None
    action: WindowAction
    hypothesis_id: str | None = Field(default=None, min_length=1, max_length=160)
    note: str | None = Field(default=None, min_length=1, max_length=500)
    plan_digest: str | None = Field(default=None, pattern=_DIGEST)
    outcome_digest: str | None = Field(default=None, pattern=_DIGEST)
    candidate_receipt_digest: str | None = Field(default=None, pattern=_DIGEST)
    recovery_receipt_digest: str | None = Field(default=None, pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_intervention_associations(self) -> DynamicWindowRecordV2:
        if self.action is WindowAction.GATE_REJECTED:
            if self.plan_digest is None:
                raise ValueError("gate-rejected windows require a plan digest")
            if any(
                value is not None
                for value in (
                    self.outcome_digest,
                    self.candidate_receipt_digest,
                    self.recovery_receipt_digest,
                )
            ):
                raise ValueError("gate rejection cannot carry execution evidence")
            return self
        if self.action is WindowAction.INTERVENTION_FAILED:
            if self.plan_digest is None or self.candidate_receipt_digest is None:
                raise ValueError("failed interventions require plan and candidate receipt")
            return self
        executed = self.action in {WindowAction.INTERVENED, WindowAction.VERIFIED}
        complete = all(
            value is not None
            for value in (
                self.plan_digest,
                self.outcome_digest,
                self.candidate_receipt_digest,
            )
        )
        if executed != complete:
            raise ValueError("executed windows require a complete plan/outcome/receipt set")
        if not executed and any(
            value is not None
            for value in (
                self.plan_digest,
                self.outcome_digest,
                self.candidate_receipt_digest,
                self.recovery_receipt_digest,
            )
        ):
            raise ValueError("non-intervention windows cannot carry execution evidence")
        if self.recovery_receipt_digest is not None and not executed:
            raise ValueError("recovery receipt requires a completed intervention")
        return self


class DynamicPhaseRunV2(StrictModel):
    schema_version: Literal[DYNAMIC_PHASE_RUN_V2_SCHEMA] = DYNAMIC_PHASE_RUN_V2_SCHEMA
    workload_contract_digest: str = Field(pattern=_DIGEST)
    gate_contract_digest: str = Field(pattern=_DIGEST)
    windows: list[DynamicWindowRecordV2] = Field(default_factory=list)
    verification_observations: list[VerificationObservation] = Field(default_factory=list)
    promotion: PromotionEvidence | None = None
    hypothesis_ledger_digest: str | None = Field(default=None, pattern=_DIGEST)
    stop_gate_decision: GateDecision | None = None
    risky_interventions: int = Field(ge=0)
    execution_receipts: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_execution_receipts(self) -> DynamicPhaseRunV2:
        if len(self.execution_receipts) != len(set(self.execution_receipts)):
            raise ValueError("execution receipt digests must be unique")
        expected = [
            digest
            for window in self.windows
            for digest in (
                window.candidate_receipt_digest,
                window.recovery_receipt_digest,
            )
            if digest is not None
        ]
        if self.execution_receipts != expected:
            raise ValueError("execution receipts must follow deterministic window order")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def load_dynamic_phase_run(
    payload: Mapping[str, Any],
) -> DynamicPhaseRun | DynamicPhaseRunV2:
    """Dispatch dynamic run evidence by schema without backfilling legacy fields."""

    version = payload.get("schema_version")
    if version == DYNAMIC_PHASE_RUN_SCHEMA:
        return DynamicPhaseRun.model_validate(payload)
    if version == DYNAMIC_PHASE_RUN_V2_SCHEMA:
        return DynamicPhaseRunV2.model_validate(payload)
    raise ValueError(f"unsupported dynamic phase run schema_version: {version!r}")


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
    intervention: (Callable[[ComponentHypothesis], InterventionExperiment | None] | None) = None,
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
    degradation_spec = next(
        (
            metric
            for metric in contract.o0_metrics
            if metric.metric_id == gate_contract.degradation.metric_id
        ),
        None,
    )
    if degradation_spec is None:
        raise ValueError("degradation gate metric is not declared by the workload contract")

    ledger = HypothesisLedger()
    windows: list[DynamicWindowRecord] = []
    observations: list[VerificationObservation] = []
    promotion: PromotionEvidence | None = None
    symptom_registered = False
    interventions = 0
    consecutive_lcb_rounds = 0
    degradation_events = 0
    pre_intervention_value: float | None = None
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
            spec = next((m for m in contract.o0_metrics if m.metric_id == slo.metric_id), None)
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
        elif symptom_registered and component_probe is not None and intervention is not None:
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
                        # Convergence (stop class 2) counts intervention rounds:
                        # every experiment that judged through S6 contributes its
                        # business LCB; verification windows are confirmations of
                        # an already-accepted hypothesis, not search rounds.
                        if experiment is not None and experiment.business_lcb is not None:
                            consecutive_lcb_rounds = (
                                consecutive_lcb_rounds + 1
                                if experiment.business_lcb
                                <= gate_contract.convergence.lcb_threshold
                                else 0
                            )
                        # Degradation (stop class 4): this window's O0 was
                        # produced before the change took effect, so it is the
                        # pre-intervention reference for every later window.
                        degradation_observation = next(
                            (
                                item
                                for item in window.o0
                                if item.metric_id == degradation_spec.metric_id
                            ),
                            None,
                        )
                        if degradation_observation is not None:
                            pre_intervention_value = aggregate(
                                degradation_observation.values,
                                degradation_spec.aggregation,
                            )

        # Post-intervention business regression check (stop class 4 input):
        # relative worsening of the declared metric versus the last
        # pre-intervention window; the spec's direction defines "worsening".
        # A zero reference makes the relative measure undefined — any strict
        # worsening then counts, because it cannot be bounded.
        degradation_observation = next(
            (item for item in window.o0 if item.metric_id == degradation_spec.metric_id),
            None,
        )
        if degradation_observation is not None and pre_intervention_value is not None:
            current_value = aggregate(degradation_observation.values, degradation_spec.aggregation)
            reference = pre_intervention_value
            if degradation_spec.direction is O0MetricDirection.MAXIMIZE:
                worsened = current_value < reference
                relative = (reference - current_value) / abs(reference) if reference != 0 else None
            else:
                worsened = current_value > reference
                relative = (current_value - reference) / abs(reference) if reference != 0 else None
            beyond_limit = relative is None or (relative > gate_contract.degradation.relative_limit)
            if worsened and beyond_limit:
                degradation_events += 1
                note = (
                    f"degradation: {degradation_spec.metric_id} worsened from "
                    f"{reference:.6f} to {current_value:.6f} "
                    f"(limit {gate_contract.degradation.relative_limit:.6f})"
                )

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
                "consecutive_lcb_threshold_rounds": consecutive_lcb_rounds,
                "degradation_events": degradation_events,
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


def run_dynamic_phase_v2(
    *,
    contract: WorkloadContract,
    gate_contract: DynamicPhaseGateContractV2,
    manifest: ConfigManifest,
    promotion_contract: PromotionContract,
    environment_digest: str,
    max_windows: int,
    probe_top_k: int,
    load_identity: Callable[[str], LoadCommandIdentity],
    o0_source: Callable[[str], str],
    hypothesis_source: Callable[[SymptomRecord], list[ComponentHypothesis]],
    prepare_intervention: Callable[[ComponentHypothesis], InterventionPlan],
    execute_intervention: Callable[[InterventionPlan, str], DynamicInterventionExecution],
    clock: Callable[[], datetime],
    o1_source: Callable[[str], list[ComponentMetricSnapshot]] | None = None,
    component_probe: Callable[[ComponentHypothesis, ObservationWindow], str] | None = None,
    retest: Callable[[str], RetestOutcome] | None = None,
    verification_window_count: int = 0,
) -> DynamicPhaseRunV2:
    """Run the v2 prepare -> pre-execution gate -> execute state machine."""

    if max_windows < 1 or probe_top_k < 1 or verification_window_count < 0:
        raise ValueError("max_windows and probe_top_k need >=1; verification windows >=0")
    if gate_contract.workload_contract_digest != contract.digest:
        raise ValueError("gate contract is not bound to this workload contract")
    degradation_spec = next(
        (
            metric
            for metric in contract.o0_metrics
            if metric.metric_id == gate_contract.degradation.metric_id
        ),
        None,
    )
    if degradation_spec is None:
        raise ValueError("degradation gate metric is not declared by the workload contract")

    ledger = HypothesisLedger()
    windows: list[DynamicWindowRecordV2] = []
    observations: list[VerificationObservation] = []
    promotion: PromotionEvidence | None = None
    symptom_registered = False
    interventions = 0
    risky_interventions = 0
    consecutive_lcb_rounds = 0
    degradation_events = 0
    pre_intervention_value: float | None = None
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
        return evaluate_phase_gate_v2(
            gate_contract,
            gate_state.model_copy(update={"evidence_digest": window_digest}),
        )

    def finish(decision: GateDecision, note: str | None = None) -> DynamicPhaseRunV2:
        return DynamicPhaseRunV2(
            workload_contract_digest=contract.digest,
            gate_contract_digest=gate_contract.digest,
            windows=windows,
            verification_observations=observations,
            promotion=promotion,
            hypothesis_ledger_digest=ledger.digest,
            stop_gate_decision=decision,
            risky_interventions=risky_interventions,
            execution_receipts=[
                digest
                for item in windows
                for digest in (
                    item.candidate_receipt_digest,
                    item.recovery_receipt_digest,
                )
                if digest is not None
            ],
            note=note,
        )

    def safety_stop(field: str, reason: str, evidence_digest: str) -> GateDecision:
        return GateDecision(
            stop=True,
            stop_class=GateStopClass.SAFETY_TRIGGERED,
            triggered_field=field,
            reason=reason[:600],
            contract_digest=gate_contract.digest,
            evidence_digest=evidence_digest,
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
                DynamicWindowRecordV2(
                    window_id=window_id,
                    action=WindowAction.IDENTITY_DRIFT,
                    note="identity drift stopped the phase via the gate",
                )
            )
            return finish(decision)
        elapsed += (window.finished_at - window.started_at).total_seconds()

        slo = contract.slos[0] if contract.slos else None
        slo_met: bool | None = None
        if slo is not None:
            spec = next(
                (metric for metric in contract.o0_metrics if metric.metric_id == slo.metric_id),
                None,
            )
            if spec is None:
                raise ValueError("SLO metric is not declared by the workload contract")
            observation = next(item for item in window.o0 if item.metric_id == spec.metric_id)
            slo_met = _slo_met(
                aggregate(observation.values, spec.aggregation), slo.comparator, slo.bound
            )

        action = WindowAction.OBSERVE
        hypothesis_id: str | None = None
        note: str | None = None
        plan_digest: str | None = None
        outcome_digest: str | None = None
        candidate_receipt_digest: str | None = None
        recovery_receipt_digest: str | None = None

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
        elif symptom_registered and component_probe is not None:
            queue = ledger.probe_queue(top_k=probe_top_k)
            if queue:
                head = queue[0]
                hypothesis_id = head.hypothesis_id
                if head.status is HypothesisStatus.PROPOSED:
                    ledger.begin_probing(head.hypothesis_id, component_probe(head, window))
                    note = "advanced to probing with component evidence"
                else:
                    try:
                        ledger.request_intervention(head.hypothesis_id)
                    except HypothesisRoutingError as error:
                        action = WindowAction.PROBE_BLOCKED
                        note = str(error)
                    else:
                        plan = prepare_intervention(head)
                        plan_digest = plan.digest
                        gate_rejection = evaluate_intervention_gate(
                            plan=plan,
                            manifest=manifest,
                            contract=gate_contract,
                            risky_interventions=risky_interventions,
                            evidence_digest=window.digest,
                        )
                        if gate_rejection is not None:
                            windows.append(
                                DynamicWindowRecordV2(
                                    window_id=window_id,
                                    observation_window_digest=window.digest,
                                    slo_met=slo_met,
                                    action=WindowAction.GATE_REJECTED,
                                    hypothesis_id=hypothesis_id,
                                    note=gate_rejection.reason,
                                    plan_digest=plan.digest,
                                )
                            )
                            return finish(gate_rejection)
                        resolved = resolve_plan_risk(plan, manifest)
                        try:
                            execution = execute_intervention(plan, window_id)
                        except DynamicInterventionError as error:
                            if error.outcome is not None and error.outcome.apply_started:
                                interventions += 1
                                if resolved.final_risk is not RiskLevel.LOW:
                                    risky_interventions += 1
                            outcome_digest = error.outcome.digest if error.outcome else None
                            candidate_receipt_digest = error.candidate_receipt_digest
                            recovery_receipt_digest = error.recovery_receipt_digest
                            if candidate_receipt_digest is None:
                                raise
                            windows.append(
                                DynamicWindowRecordV2(
                                    window_id=window_id,
                                    observation_window_digest=window.digest,
                                    slo_met=slo_met,
                                    action=WindowAction.INTERVENTION_FAILED,
                                    hypothesis_id=hypothesis_id,
                                    note=str(error)[:500],
                                    plan_digest=plan.digest,
                                    outcome_digest=outcome_digest,
                                    candidate_receipt_digest=candidate_receipt_digest,
                                    recovery_receipt_digest=recovery_receipt_digest,
                                )
                            )
                            return finish(
                                safety_stop(
                                    error.triggered_field,
                                    str(error),
                                    recovery_receipt_digest or candidate_receipt_digest,
                                )
                            )

                        outcome = execution.outcome
                        outcome_digest = outcome.digest
                        candidate_receipt_digest = execution.candidate_receipt_digest
                        recovery_receipt_digest = execution.recovery_receipt_digest
                        if outcome.apply_started:
                            interventions += 1
                            if resolved.final_risk is not RiskLevel.LOW:
                                risky_interventions += 1
                        experiment = outcome.experiment
                        if experiment is None:
                            action = WindowAction.INTERVENTION_FAILED
                            note = "safety execution completed without a business experiment"
                        elif experiment.accepted:
                            confirmed = ledger.confirm(head.hypothesis_id, experiment)
                            action = WindowAction.INTERVENED
                            note = "business retest accepted the hypothesis"
                            hypothesis_id = confirmed.hypothesis_id
                            if retest is not None:
                                for verification_index in range(1, verification_window_count + 1):
                                    verify_id = f"verify-{window_id}-{verification_index}"
                                    retest_outcome = retest(verify_id)
                                    observations.append(
                                        verification_observation(
                                            window_id=verify_id,
                                            promoted_candidate_id=confirmed.hypothesis_id,
                                            environment_digest=environment_digest,
                                            outcome=retest_outcome.improvement,
                                            evidence_digest=retest_outcome.measurement_batch_digest,
                                        )
                                    )
                                action = WindowAction.VERIFIED
                                promotion = evaluate_promotion(observations, promotion_contract)
                        else:
                            ledger.refute(head.hypothesis_id, experiment.measurement_batch_digest)
                            action = WindowAction.INTERVENED
                            note = "business retest rejected the hypothesis; restored and refuted"
                        if experiment is not None and experiment.business_lcb is not None:
                            consecutive_lcb_rounds = (
                                consecutive_lcb_rounds + 1
                                if experiment.business_lcb
                                <= gate_contract.convergence.lcb_threshold
                                else 0
                            )
                        before = next(
                            (
                                item
                                for item in window.o0
                                if item.metric_id == degradation_spec.metric_id
                            ),
                            None,
                        )
                        if before is not None:
                            pre_intervention_value = aggregate(
                                before.values, degradation_spec.aggregation
                            )
                        if outcome.safety_state is SafetyState.NEEDS_ATTENTION:
                            field = (
                                "intervention.recovery"
                                if recovery_receipt_digest
                                else "intervention.rollback"
                            )
                            windows.append(
                                DynamicWindowRecordV2(
                                    window_id=window_id,
                                    observation_window_digest=window.digest,
                                    slo_met=slo_met,
                                    action=WindowAction.INTERVENTION_FAILED,
                                    hypothesis_id=hypothesis_id,
                                    note=note,
                                    plan_digest=plan_digest,
                                    outcome_digest=outcome_digest,
                                    candidate_receipt_digest=candidate_receipt_digest,
                                    recovery_receipt_digest=recovery_receipt_digest,
                                )
                            )
                            return finish(
                                safety_stop(
                                    field,
                                    "intervention safety state requires operator attention",
                                    recovery_receipt_digest or candidate_receipt_digest,
                                )
                            )

        degradation_observation = next(
            (item for item in window.o0 if item.metric_id == degradation_spec.metric_id),
            None,
        )
        if degradation_observation is not None and pre_intervention_value is not None:
            current_value = aggregate(degradation_observation.values, degradation_spec.aggregation)
            reference = pre_intervention_value
            if degradation_spec.direction is O0MetricDirection.MAXIMIZE:
                worsened = current_value < reference
                relative = (reference - current_value) / abs(reference) if reference != 0 else None
            else:
                worsened = current_value > reference
                relative = (current_value - reference) / abs(reference) if reference != 0 else None
            beyond_limit = relative is None or (relative > gate_contract.degradation.relative_limit)
            if worsened and beyond_limit:
                degradation_events += 1
                note = (
                    f"degradation: {degradation_spec.metric_id} worsened from "
                    f"{reference:.6f} to {current_value:.6f} "
                    f"(limit {gate_contract.degradation.relative_limit:.6f})"
                )

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
                "risky_interventions": risky_interventions,
                "consecutive_lcb_threshold_rounds": consecutive_lcb_rounds,
                "degradation_events": degradation_events,
                "elapsed_seconds": elapsed,
            }
        )
        decision = evaluate(window.digest)
        windows.append(
            DynamicWindowRecordV2(
                window_id=window_id,
                observation_window_digest=window.digest,
                slo_met=slo_met,
                action=action,
                hypothesis_id=hypothesis_id,
                note=note,
                plan_digest=plan_digest,
                outcome_digest=outcome_digest,
                candidate_receipt_digest=candidate_receipt_digest,
                recovery_receipt_digest=recovery_receipt_digest,
            )
        )
        if decision.stop:
            return finish(decision)

    final = evaluate(gate_state.evidence_digest)
    return finish(
        final,
        note="window budget reached; the final gate decision is recorded as-is",
    )


__all__ = [
    "DYNAMIC_PHASE_RUN_SCHEMA",
    "DYNAMIC_PHASE_RUN_V2_SCHEMA",
    "DynamicPhaseRun",
    "DynamicPhaseRunV2",
    "DynamicWindowRecord",
    "DynamicWindowRecordV2",
    "WindowAction",
    "run_dynamic_phase",
    "run_dynamic_phase_v2",
    "load_dynamic_phase_run",
]
