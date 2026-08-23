"""组件闭环引擎（L5）：基线→候选→安全施加→测量→回退的单组件循环。

终裁语义（2026-08-23 架构 v2 起）：``CandidateEvaluation.accepted`` 是**组件级
晋级建议**（S7 组件内判定），不是最终接受结论；终裁由 L8 引擎判断器
（``looper_core.system_opt.engine.evaluate_candidate``）产出。字段名保留以兼容
存量工件，语义变化见 architecture/overall.md 与 component.py。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import Field

from looper_core.analysis import InsufficientEvidence, pareto_ranks
from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import Direction, OptimizerSpec, StrictModel
from looper_core.optimizer import SearchSpaceExhausted, suggest_candidate
from looper_core.system_opt.config_manifest import ConfigManifest
from looper_core.system_opt.domain import ResolvedDomain
from looper_core.system_opt.executor import ExecutorBackend
from looper_core.system_opt.policy import (
    MetricRole,
    OptimizationMode,
    SystemOptimizationPolicy,
)
from looper_core.system_opt.safety import (
    MeasurementResult,
    MeasurementStatus,
    SafetyController,
    SafetyPolicy,
    SafetyState,
)
from looper_core.system_opt.scoring import (
    DiagnosticPriority,
    GateEvidence,
    ImprovementEvidence,
    MeasurementBatch,
    bootstrap_improvement,
    comparable,
    diagnostic_priorities,
    evaluate_hard_gates,
)

MeasurementAdapter = Callable[[int], MeasurementBatch]


class StopReason(StrEnum):
    TARGET_ACHIEVED = "target-achieved"
    SEARCH_SPACE_EXHAUSTED = "search-space-exhausted"
    NO_IMPROVEMENT = "no-improvement-policy"
    CANDIDATE_BUDGET = "candidate-budget"
    ATTEMPT_BUDGET = "attempt-budget"
    WALL_TIME_BUDGET = "wall-time-budget"
    SAFETY_STOP = "safety-stop"
    MEASUREMENT_ERROR = "measurement-error"
    COMPLETED = "completed"


class CandidateEvaluation(StrictModel):
    round_index: int = Field(ge=1)
    attempt_index: int = Field(ge=1)
    candidate_id: str
    parameters: dict[str, Any]
    change_count: int
    safety_state: SafetyState
    safety_reason: str | None = None
    measurement_digest: str | None = None
    comparison_baseline_digest: str
    comparable: bool
    identity_mismatches: list[str]
    gates: list[GateEvidence]
    improvements: dict[str, ImprovementEvidence]
    feasible: bool
    # Semantic (architecture v2): component-level promotion suggestion only;
    # the final verdict belongs to the L8 engine judge.
    accepted: bool
    pareto_rank: int | None = None


class OptimizationRun(StrictModel):
    schema_version: str
    policy_id: str
    policy_digest: str
    manifest_digest: str
    state_evidence_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    mode: OptimizationMode
    baseline: MeasurementBatch
    baseline_history: list[MeasurementBatch]
    diagnostic_reference: MeasurementBatch | None = None
    diagnostic_priorities: list[DiagnosticPriority]
    routed_components: list[str]
    candidates: list[CandidateEvaluation]
    recommended_candidate_id: str | None = None
    stop_reason: StopReason
    stop_detail: str
    elapsed_seconds: float = Field(ge=0)
    attempt_count: int = Field(ge=1)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class SystemOptimizationEngine:
    def __init__(
        self,
        policy: SystemOptimizationPolicy,
        manifest: ConfigManifest,
        resolved_domains: Mapping[str, ResolvedDomain],
        backend: ExecutorBackend,
        state_evidence_digest: str | None = None,
    ) -> None:
        self.policy = policy
        self.manifest = manifest
        self.domains = dict(resolved_domains)
        self.backend = backend
        self.state_evidence_digest = state_evidence_digest
        self.safety = SafetyController(
            SafetyPolicy(
                max_changes=policy.safety.max_changes,
                max_changes_reason=policy.safety.max_changes_reason,
                pinned_items=set(policy.safety.pinned_items),
                ownership_unknown_items=set(policy.safety.ownership_unknown_items),
                high_risk_waivers=set(policy.safety.high_risk_waivers),
                allow_keep=False,
                require_privileged=policy.safety.require_privileged,
            )
        )

    def run(
        self,
        *,
        baseline_parameters: Mapping[str, Any],
        measure: MeasurementAdapter,
        fencing_token: int,
        diagnostic_reference: MeasurementBatch | None = None,
    ) -> OptimizationRun:
        started = time.monotonic()
        baseline = measure(self.policy.statistics.baseline_repeats)
        current_baseline = baseline
        baseline_history = [baseline]
        priorities: list[DiagnosticPriority] = []
        routed_components = list(self.policy.authorized_components)
        if self.policy.mode == OptimizationMode.WORKLOAD:
            if diagnostic_reference is None:
                raise ValueError("workload mode requires diagnostic_reference")
            matches, mismatches = comparable(
                diagnostic_reference.identity,
                baseline.identity,
                self.policy.identity.required_fields,
            )
            if not matches:
                raise InsufficientEvidence(f"diagnostic reference identity mismatch: {mismatches}")
            diagnostic_contracts = [
                metric
                for metric in self.policy.metrics
                if metric.role == MetricRole.COMPONENT_DIAGNOSTIC
            ]
            priorities = diagnostic_priorities(baseline, diagnostic_reference, diagnostic_contracts)
            component_order: list[str] = []
            for priority in priorities:
                if (
                    priority.component in self.policy.authorized_components
                    and priority.component not in component_order
                ):
                    component_order.append(priority.component)
            assert self.policy.search.routed_component_limit is not None
            routed_components = component_order[: self.policy.search.routed_component_limit]
            if not routed_components:
                return self._result(
                    started,
                    baseline,
                    baseline_history,
                    diagnostic_reference,
                    priorities,
                    [],
                    [],
                    StopReason.SEARCH_SPACE_EXHAUSTED,
                    "no diagnostic component has both evidence and task authorization",
                )

        search_space = self._search_space(set(routed_components))
        if not search_space:
            return self._result(
                started,
                baseline,
                baseline_history,
                diagnostic_reference,
                priorities,
                routed_components,
                [],
                StopReason.SEARCH_SPACE_EXHAUSTED,
                "dynamic domain intersection produced no searchable parameters",
            )
        missing_baseline = sorted(set(search_space) - set(baseline_parameters))
        if missing_baseline:
            raise ValueError(f"baseline parameters are missing: {missing_baseline}")

        optimizer = OptimizerSpec(
            type=self.policy.search.generator,
            seed=self.policy.search.random_seed,
        )
        scored_metrics = [
            metric
            for metric in self.policy.metrics
            if metric.role
            in {
                MetricRole.BUSINESS_PRIMARY,
                MetricRole.BUSINESS_SECONDARY,
                MetricRole.COST,
                MetricRole.RISK,
            }
        ]
        objective_directions = [Direction.MAXIMIZE for _ in scored_metrics]
        existing: list[dict[str, Any]] = [
            {"parameters": {name: baseline_parameters[name] for name in search_space}}
        ]
        history: list[dict[str, Any]] = []
        candidates: list[CandidateEvaluation] = []
        no_improvement = 0
        best_lower = float("-inf")
        # The initial baseline is a measurement attempt and consumes the same
        # explicit budget as candidate and periodic-baseline measurements.
        attempts = 1
        stop_reason = StopReason.CANDIDATE_BUDGET
        stop_detail = "explicit candidate budget reached"

        while len(candidates) < self.policy.search.max_candidates:
            if candidates and len(candidates) % self.policy.statistics.baseline_every_n == 0:
                if attempts >= self.policy.search.max_attempts:
                    stop_reason = StopReason.ATTEMPT_BUDGET
                    stop_detail = "explicit attempt budget reached before periodic baseline"
                    break
                if time.monotonic() - started >= self.policy.search.wall_time_seconds:
                    stop_reason = StopReason.WALL_TIME_BUDGET
                    stop_detail = "explicit wall-time budget reached before periodic baseline"
                    break
                attempts += 1
                refreshed = measure(self.policy.statistics.baseline_repeats)
                matches, mismatches = comparable(
                    baseline.identity,
                    refreshed.identity,
                    self.policy.identity.required_fields,
                )
                baseline_history.append(refreshed)
                if not matches:
                    stop_reason = StopReason.MEASUREMENT_ERROR
                    stop_detail = f"periodic baseline identity mismatch: {mismatches}"
                    break
                current_baseline = refreshed
            if attempts >= self.policy.search.max_attempts:
                stop_reason = StopReason.ATTEMPT_BUDGET
                stop_detail = "explicit attempt budget reached"
                break
            if time.monotonic() - started >= self.policy.search.wall_time_seconds:
                stop_reason = StopReason.WALL_TIME_BUDGET
                stop_detail = "explicit wall-time budget reached"
                break
            try:
                parameters = suggest_candidate(
                    search_space,
                    optimizer,
                    sequence=attempts,
                    existing=existing,
                    objective_directions=objective_directions,
                    history=history,
                )
            except SearchSpaceExhausted as error:
                stop_reason = StopReason.SEARCH_SPACE_EXHAUSTED
                stop_detail = str(error)
                break
            attempts += 1
            existing.append({"parameters": parameters})
            evaluation = self._evaluate(
                len(candidates) + 1,
                attempts,
                parameters,
                baseline_parameters,
                current_baseline,
                measure,
                fencing_token,
                scored_metrics,
            )
            candidates.append(evaluation)
            values = (
                [evaluation.improvements[metric.id].estimate for metric in scored_metrics]
                if evaluation.feasible
                and all(metric.id in evaluation.improvements for metric in scored_metrics)
                else None
            )
            history.append(
                {
                    "id": evaluation.candidate_id,
                    "parameters": parameters,
                    "values": values,
                }
            )
            if evaluation.safety_state == SafetyState.NEEDS_ATTENTION:
                stop_reason = StopReason.SAFETY_STOP
                stop_detail = evaluation.safety_reason or "target needs attention"
                break
            primary = evaluation.improvements.get(self.policy.primary_metric.id)
            if primary is not None and primary.accepted and primary.lower > best_lower:
                best_lower = primary.lower
                no_improvement = 0
            else:
                no_improvement += 1
            target = self.policy.search.target_improvement
            if primary is not None and target is not None and primary.lower >= target:
                stop_reason = StopReason.TARGET_ACHIEVED
                stop_detail = "primary confidence lower bound reached the explicit target"
                break
            if no_improvement >= self.policy.search.no_improvement_limit:
                stop_reason = StopReason.NO_IMPROVEMENT
                stop_detail = "explicit consecutive no-improvement policy triggered"
                break
        else:
            stop_reason = StopReason.CANDIDATE_BUDGET
            stop_detail = "explicit candidate budget reached"

        self._assign_pareto(candidates, scored_metrics)
        recommended = self._recommend(candidates)
        return self._result(
            started,
            baseline,
            baseline_history,
            diagnostic_reference,
            priorities,
            routed_components,
            candidates,
            stop_reason,
            stop_detail,
            recommended.candidate_id if recommended else None,
            attempts,
        )

    def search_space(self, components: set[str] | None = None) -> dict[str, Any]:
        """Public search-space view for the L8 engine loop and candidate pools."""

        return self._search_space(
            components if components is not None else set(self.policy.authorized_components)
        )

    def _search_space(self, components: set[str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for parameter_id, domain in sorted(self.domains.items()):
            item = self.manifest.item_for_parameter(parameter_id)
            if item.primary_component.value not in components:
                continue
            result[parameter_id] = domain.to_search_parameter(default=item.default)
        return result

    def _evaluate(
        self,
        round_index: int,
        attempt_index: int,
        parameters: dict[str, Any],
        baseline_parameters: Mapping[str, Any],
        baseline: MeasurementBatch,
        measure: MeasurementAdapter,
        fencing_token: int,
        scored_metrics: list[Any],
    ) -> CandidateEvaluation:
        holder: dict[str, MeasurementBatch] = {}

        def measured() -> MeasurementResult:
            batch = measure(self.policy.statistics.candidate_repeats)
            holder["batch"] = batch
            return MeasurementResult(
                status=MeasurementStatus.SUCCEEDED,
                evidence_digest=batch.digest,
            )

        safety = self.safety.execute(
            self.manifest,
            parameters,
            self.backend,
            fencing_token=fencing_token,
            measure=measured,
            keep=False,
        )
        batch = holder.get("batch")
        candidate_id = canonical_digest({"parameters": parameters})
        change_count = sum(
            canonical_json(value) != canonical_json(baseline_parameters.get(name))
            for name, value in parameters.items()
        )
        if batch is None:
            return CandidateEvaluation(
                round_index=round_index,
                attempt_index=attempt_index,
                candidate_id=candidate_id,
                parameters=parameters,
                change_count=change_count,
                safety_state=safety.state,
                safety_reason=safety.reason,
                comparison_baseline_digest=baseline.digest,
                comparable=False,
                identity_mismatches=["measurement-missing"],
                gates=[],
                improvements={},
                feasible=False,
                accepted=False,
            )
        is_comparable, mismatches = comparable(
            baseline.identity,
            batch.identity,
            self.policy.identity.required_fields,
        )
        gates = evaluate_hard_gates(self.policy.hard_gates, batch.gate_values)
        improvements: dict[str, ImprovementEvidence] = {}
        if is_comparable:
            for contract in scored_metrics:
                if contract.id not in baseline.metrics or contract.id not in batch.metrics:
                    continue
                try:
                    improvements[contract.id] = bootstrap_improvement(
                        batch.metrics[contract.id],
                        baseline.metrics[contract.id],
                        contract,
                        self.policy.statistics,
                    )
                except InsufficientEvidence:
                    continue
        feasible = (
            safety.state == SafetyState.ROLLED_BACK
            and is_comparable
            and all(gate.passed for gate in gates)
            and len(gates) == len(self.policy.hard_gates)
        )
        primary = improvements.get(self.policy.primary_metric.id)
        return CandidateEvaluation(
            round_index=round_index,
            attempt_index=attempt_index,
            candidate_id=candidate_id,
            parameters=parameters,
            change_count=change_count,
            safety_state=safety.state,
            safety_reason=safety.reason,
            measurement_digest=batch.digest,
            comparison_baseline_digest=baseline.digest,
            comparable=is_comparable,
            identity_mismatches=mismatches,
            gates=gates,
            improvements=improvements,
            feasible=feasible,
            accepted=feasible and primary is not None and primary.accepted,
        )

    @staticmethod
    def _assign_pareto(candidates: list[CandidateEvaluation], metrics: list[Any]) -> None:
        points = [
            {
                "id": candidate.candidate_id,
                "feasible": candidate.feasible
                and all(metric.id in candidate.improvements for metric in metrics),
                "objectives": {
                    metric.id: candidate.improvements[metric.id].estimate
                    for metric in metrics
                    if metric.id in candidate.improvements
                },
            }
            for candidate in candidates
        ]
        ranks = pareto_ranks(
            points,
            {metric.id: Direction.MAXIMIZE for metric in metrics},
        )
        for candidate in candidates:
            candidate.pareto_rank = ranks[candidate.candidate_id]

    def _recommend(self, candidates: list[CandidateEvaluation]) -> CandidateEvaluation | None:
        eligible = [
            candidate
            for candidate in candidates
            if candidate.accepted and candidate.pareto_rank == 1
        ]
        if not eligible:
            return None
        primary_id = self.policy.primary_metric.id

        def key(candidate: CandidateEvaluation) -> tuple[Any, ...]:
            values: list[Any] = []
            primary = candidate.improvements[primary_id]
            for rule in self.policy.search.tie_break_order:
                if rule == "primary-lower":
                    values.append(-primary.lower)
                elif rule == "primary-estimate":
                    values.append(-primary.estimate)
                elif rule == "fewer-changes":
                    values.append(candidate.change_count)
                else:
                    values.append(candidate.candidate_id)
            return tuple(values)

        return min(eligible, key=key)

    def _result(
        self,
        started: float,
        baseline: MeasurementBatch,
        baseline_history: list[MeasurementBatch],
        reference: MeasurementBatch | None,
        priorities: list[DiagnosticPriority],
        routed: list[str],
        candidates: list[CandidateEvaluation],
        stop_reason: StopReason,
        detail: str,
        recommended: str | None = None,
        attempts: int = 1,
    ) -> OptimizationRun:
        return OptimizationRun(
            schema_version="looper.system-optimization-run/v1alpha1",
            policy_id=self.policy.id,
            policy_digest=canonical_digest(self.policy.model_dump(mode="json")),
            manifest_digest=self.manifest.digest,
            state_evidence_digest=self.state_evidence_digest,
            mode=self.policy.mode,
            baseline=baseline,
            baseline_history=baseline_history,
            diagnostic_reference=reference,
            diagnostic_priorities=priorities,
            routed_components=routed,
            candidates=candidates,
            recommended_candidate_id=recommended,
            stop_reason=stop_reason,
            stop_detail=detail,
            elapsed_seconds=time.monotonic() - started,
            attempt_count=attempts,
        )


__all__ = [
    "CandidateEvaluation",
    "MeasurementAdapter",
    "OptimizationRun",
    "StopReason",
    "SystemOptimizationEngine",
]
