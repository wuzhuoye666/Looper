"""L8 引擎主循环：打分 → 调度（查负缓存）→ 组件执行 → 判断 → 缓存/回退 → 停止。

架构层：总体架构 v2 的 L8（见 docs/system-optimizer/architecture/overall.md §4）。
第一版语义（诚实边界）：

- 一轮 = 一个组件：调度器选出下一个未被负缓存完全封锁的组件，组件优化器
  内部完成自己的搜索/安全施加/测量/回退（L1/L5），引擎只对产出的候选评估
  做终裁（判断器 S0→S2→S7）；
- 未被终裁接受的候选写入负缓存（L7，证据挂组件 report digest）；
- 停止原因必须是显式枚举（S10 纪律），不内置任何阈值：轮数预算、组件全部
  完成、候选全部被缓存封锁均由任务输入与运行事实决定；
- 相位级结束门禁：循环结束后若提供了相位验证输入，用 L6
  ``verify_phase_restoration`` 验证系统已回基线；未提供时显式记录"跳过 +
  原因"，绝不静默宣称已恢复。

scheduler 选中具体候选后，L8 将同一参数身份定向交给 L5；L5 只执行该候选，
不得重新生成另一个候选。公式映射证据必须由调用方显式提供，越域建议留痕拒绝。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import ComponentMetricSnapshot
from looper_core.system_opt.component import ComponentOptimizer
from looper_core.system_opt.component.mapping import (
    RuleRejection,
    validate_suggestions_in_domain,
)
from looper_core.system_opt.engine.incumbent import IncumbentTracker, ScreenVerdict
from looper_core.system_opt.engine.judge import CandidateVerdict, evaluate_candidate
from looper_core.system_opt.engine.scheduler import (
    SchedulerDecision,
    SkippedCandidate,
    select_next_candidate,
)
from looper_core.system_opt.engine.scorer import ComponentScore
from looper_core.system_opt.executor import ConfigSnapshot
from looper_core.system_opt.negative_cache import (
    NegativeCache,
    NegativeCacheEntry,
    NegativeVerdict,
    candidate_parameters_digest,
    formula_versions_digest,
)
from looper_core.system_opt.result_vector import PromotionContract, VerificationObservation
from looper_core.system_opt.rollback import (
    PhaseRestoration,
    RestorationStatus,
    verify_phase_restoration,
)
from looper_core.system_opt.safety import SafetyState
from looper_core.system_opt.scoring import MeasurementBatch

ENGINE_LOOP_SCHEMA = "looper.engine-loop-result/v1alpha1"


class EngineStopReason(StrEnum):
    COMPLETED = "completed-all-components"
    ROUND_BUDGET = "round-budget-exhausted"
    ALL_CACHED = "all-candidates-negative-cached"
    NO_ACTIONABLE_CANDIDATES = "no-actionable-candidates"
    SAFETY_STOP = "safety-stop-needs-attention"
    PHASE_RESTORATION_FAILED = "phase-restoration-failed"


class EngineLoopConfig(StrictModel):
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    formula_versions: dict[str, str] = Field(min_length=1)
    pressure_protocol_digests: dict[str, str] = Field(min_length=1)
    max_rounds: int = Field(ge=1)
    max_pool_size: int = Field(ge=1)
    pre_screen_tolerance: float | None = Field(default=None, ge=0)
    promotion_contract: PromotionContract | None = None


class EngineRoundRecord(StrictModel):
    round_index: int = Field(ge=1)
    component: str = Field(min_length=1)
    report_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    selected_parameters: dict[str, Any]
    skipped: list[SkippedCandidate]
    verdicts: list[CandidateVerdict]
    cache_entry_digests: list[str] = Field(default_factory=list)
    cached_exclusion_count: int = Field(default=0, ge=0)
    early_screened_candidate_ids: list[str] = Field(default_factory=list)
    # SO-D018: this is the component's OWN incumbent (same primary metric);
    # values from different components are not comparable and never share a tracker.
    incumbent_utility_after: float | None = None
    promotion_observations: list[VerificationObservation] = Field(default_factory=list)
    formula_rejections: list[RuleRejection] = Field(default_factory=list)
    note: str | None = Field(default=None, min_length=1, max_length=500)


class EngineLoopResult(StrictModel):
    schema_version: Literal[ENGINE_LOOP_SCHEMA] = ENGINE_LOOP_SCHEMA
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    formula_versions_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rounds: list[EngineRoundRecord] = Field(default_factory=list)
    stop_reason: EngineStopReason
    stop_detail: str = Field(min_length=1, max_length=1000)
    phase_restoration: PhaseRestoration | None = None
    phase_verification_note: str = Field(min_length=1, max_length=500)
    formula_rejections: dict[str, list[RuleRejection]] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def _neutral_scores(components: Sequence[str]) -> list[ComponentScore]:
    scores = [
        ComponentScore(
            component=component,
            max_pressure=0.0,
            max_adverse_change=0.0,
            best_pareto_rank=None,
            metric_count=1,
            priorities_digest=canonical_digest({"declared": component}),
        )
        for component in components
    ]
    scores.sort(key=lambda score: score.component)
    return scores


def _negative_entry(
    *,
    environment_digest: str,
    candidate_parameters: Mapping[str, Any],
    pressure_protocol_digest: str,
    formulas_digest: str,
    metric_id: str,
    verdict: NegativeVerdict,
    evidence_digest: str,
    detail: str,
    recorded_at: datetime,
) -> NegativeCacheEntry:
    from looper_core.system_opt.negative_cache import NegativeCacheIdentity

    return NegativeCacheEntry(
        identity=NegativeCacheIdentity(
            environment_digest=environment_digest,
            candidate_parameters_digest=candidate_parameters_digest(candidate_parameters),
            pressure_protocol_digest=pressure_protocol_digest,
            formula_versions_digest=formulas_digest,
        ),
        metric_id=metric_id,
        verdict=verdict,
        evidence_digests=[evidence_digest],
        detail=detail[:1000],
        recorded_at=recorded_at,
    )


def promotion_observation(
    *,
    round_index: int,
    environment_digest: str,
    candidate: Any,
    verdict: CandidateVerdict,
    evidence_digest: str,
) -> VerificationObservation | None:
    """S9 observation for candidates that earned a promotion suggestion.

    Only accepted (engine-verdict) candidates enter re-verification; rejected
    candidates never generate promotion evidence. None otherwise.
    """

    if not verdict.accepted:
        return None
    return VerificationObservation(
        candidate_id=candidate.candidate_id,
        passed=verdict.comparable and verdict.feasible,
        time_block_id=f"engine-round-{round_index}",
        environment_digest=environment_digest,
        evidence_digest=evidence_digest,
    )


def run_engine_loop(
    component_optimizers: Sequence[ComponentOptimizer],
    *,
    baseline_parameters: Mapping[str, Mapping[str, Any]],
    measures: Mapping[str, Any],
    negative_cache: NegativeCache,
    config: EngineLoopConfig,
    fencing_token: int,
    component_scores: Sequence[ComponentScore] | None = None,
    phase_baseline_snapshot: ConfigSnapshot | None = None,
    current_snapshot: Callable[[], ConfigSnapshot] | None = None,
    component_snapshots: Mapping[str, ComponentMetricSnapshot | None] | None = None,
    formula_baselines: Mapping[str, MeasurementBatch | None] | None = None,
) -> EngineLoopResult:
    """Run the L8 orchestration loop over per-component optimizers."""

    if not component_optimizers:
        raise ValueError("the engine loop requires at least one component optimizer")
    components = [optimizer.component for optimizer in component_optimizers]
    if len(set(components)) != len(components):
        raise ValueError(f"duplicate components in engine loop: {components}")
    missing = sorted(
        component
        for component in components
        if component not in baseline_parameters or component not in measures
    )
    if missing:
        raise ValueError(f"missing baseline parameters or measure for: {missing}")
    for component in components:
        if component not in config.pressure_protocol_digests:
            raise ValueError(f"missing pressure protocol digest for: {component}")

    started_at = datetime.now(UTC)
    # SO-D017 预筛按组件隔离：incumbent 只在「同一组件同一主指标」内比较
    # （S0 可比性）。跨组件混比不同主指标的改善量语义不成立（审查 C3 修复）。
    incumbent_trackers: dict[str, IncumbentTracker] = {}
    scores = list(component_scores) if component_scores is not None else _neutral_scores(components)
    by_component = {optimizer.component: optimizer for optimizer in component_optimizers}
    completed: set[str] = set()
    rounds: list[EngineRoundRecord] = []
    stop_reason = EngineStopReason.ROUND_BUDGET
    stop_detail = "explicit round budget exhausted"

    promotion_observations: list[VerificationObservation] = []
    all_formula_rejections: dict[str, list[RuleRejection]] = {}
    for round_index in range(1, config.max_rounds + 1):
        active = [by_component[name] for name in components if name not in completed]
        if not active:
            stop_reason = EngineStopReason.COMPLETED
            stop_detail = "every component optimizer finished one engine round"
            break
        formula_rejections: dict[str, list[RuleRejection]] = {}
        pools: dict[str, list[dict[str, Any]]] = {}
        for optimizer in active:
            component = optimizer.component
            proposed = optimizer.suggest_candidates(
                (component_snapshots or {}).get(component),
                (formula_baselines or {}).get(component),
            )
            accepted, domain_rejections = validate_suggestions_in_domain(
                proposed, optimizer.engine.search_space({component})
            )
            mapping_rejections = list(
                getattr(optimizer.formula_mapping, "last_rejections", ())
            )
            rejected = [*mapping_rejections, *domain_rejections]
            formula_rejections[component] = rejected
            all_formula_rejections[component] = rejected
            optimizer.retain_formula_suggestions(accepted)
            baseline_candidate = {
                name: baseline_parameters[component][name]
                for name in optimizer.engine.search_space({component})
            }
            pool_with_suggestions = optimizer.candidate_pool()
            for suggestion in accepted:
                if suggestion.parameters not in pool_with_suggestions:
                    pool_with_suggestions.append(suggestion.parameters)
            if len(pool_with_suggestions) > config.max_pool_size:
                raise ValueError(
                    f"component '{component}' final candidate pool "
                    f"{len(pool_with_suggestions)} exceeds the task cap "
                    f"max_pool_size={config.max_pool_size}; raise the cap explicitly "
                    "or narrow the authorized domain/formula suggestions"
                )
            pools[component] = [
                candidate
                for candidate in pool_with_suggestions
                if any(
                    canonical_json(value)
                    != canonical_json(baseline_candidate.get(name))
                    for name, value in candidate.items()
                )
            ]
        decision: SchedulerDecision = select_next_candidate(
            [score for score in scores if score.component in pools],
            pools,
            negative_cache,
            environment_digest=config.environment_digest,
            pressure_protocol_digests=config.pressure_protocol_digests,
            formula_versions=config.formula_versions,
        )
        if decision.selection is None:
            if decision.skipped:
                stop_reason = EngineStopReason.ALL_CACHED
                stop_detail = (
                    "every remaining actionable candidate is blocked by the negative cache"
                )
            else:
                stop_reason = EngineStopReason.NO_ACTIONABLE_CANDIDATES
                stop_detail = (
                    "remaining candidate pools contain no change from the declared baselines"
                )
            break
        component = decision.selection.component
        optimizer = by_component[component]
        tracker = incumbent_trackers.get(component)
        if tracker is None and config.pre_screen_tolerance is not None:
            tracker = IncumbentTracker(tolerance=config.pre_screen_tolerance)
            incumbent_trackers[component] = tracker
        exclusions = [
            skip.parameters for skip in decision.skipped if skip.component == component
        ]
        report = optimizer.run(
            baseline_parameters=baseline_parameters[component],
            measure=measures[component],
            fencing_token=fencing_token,
            preexisting=exclusions,
            selected_parameters=decision.selection.parameters,
        )
        if len(report.candidates) != 1 or (
            report.candidates[0].parameters != decision.selection.parameters
        ):
            raise ValueError(
                f"component '{component}' violated the scheduler selection identity: "
                f"selected={decision.selection.parameters!r}, "
                f"evaluated={[candidate.parameters for candidate in report.candidates]!r}"
            )
        primary = optimizer.engine.policy.primary_metric
        verdicts = [
            evaluate_candidate(
                candidate,
                primary_metric=primary.id,
                minimum_effect=primary.minimum_effect or 0.0,
            )
            for candidate in report.candidates
        ]
        if not verdicts:
            raise ValueError(
                f"component '{component}' produced no candidate evaluations; "
                "the engine loop cannot judge an empty report"
            )
        cache_entry_digests: list[str] = []
        early_screened: list[str] = []
        recorded_at = datetime.now(UTC)
        for candidate, verdict in zip(report.candidates, verdicts, strict=True):
            improvement = candidate.improvements.get(primary.id)
            if tracker is not None and improvement is not None:
                screen_decision = tracker.screen(
                    improvement.estimate, candidate_id=candidate.candidate_id
                )
                if screen_decision.verdict is ScreenVerdict.EARLY_SCREENED_OUT:
                    early_screened.append(candidate.candidate_id)
                    continue
                tracker.observe(
                    round_index=round_index,
                    candidate_id=candidate.candidate_id,
                    utility=improvement.estimate,
                    evidence_digest=report.run_digest or report.digest,
                )
            if verdict.accepted:
                continue
            negative_verdict = (
                NegativeVerdict.NO_IMPROVEMENT_LCB
                if verdict.comparable and verdict.feasible
                else NegativeVerdict.GATE_REJECTED
            )
            entry = _negative_entry(
                environment_digest=config.environment_digest,
                candidate_parameters=candidate.parameters,
                pressure_protocol_digest=config.pressure_protocol_digests[component],
                formulas_digest=formula_versions_digest(config.formula_versions),
                metric_id=primary.id,
                verdict=negative_verdict,
                evidence_digest=report.run_digest or report.digest,
                detail=verdict.reasons[0],
                recorded_at=recorded_at,
            )
            negative_cache.add(entry)
            cache_entry_digests.append(entry.digest)
        round_observations = [
            promotion_observation(
                round_index=round_index,
                environment_digest=config.environment_digest,
                candidate=candidate,
                verdict=verdict,
                evidence_digest=report.run_digest or report.digest,
            )
            for candidate, verdict in zip(report.candidates, verdicts, strict=True)
            if config.promotion_contract is not None
        ]
        promotion_observations.extend(round_observations)
        rounds.append(
            EngineRoundRecord(
                round_index=round_index,
                component=component,
                report_digest=report.digest,
                selected_parameters=decision.selection.parameters,
                skipped=decision.skipped,
                verdicts=verdicts,
                cache_entry_digests=cache_entry_digests,
                cached_exclusion_count=len(exclusions),
                early_screened_candidate_ids=early_screened,
                incumbent_utility_after=(
                    tracker.best.utility if tracker is not None and tracker.best else None
                ),
                promotion_observations=round_observations,
                formula_rejections=formula_rejections[component],
            )
        )
        if any(
            candidate.safety_state == SafetyState.NEEDS_ATTENTION
            for candidate in report.candidates
        ):
            stop_reason = EngineStopReason.SAFETY_STOP
            stop_detail = (
                f"component '{component}' left the target needs-attention; "
                "the engine stops immediately and no further component runs"
            )
            break
        completed.add(component)

    finished_at = datetime.now(UTC)
    if phase_baseline_snapshot is not None and current_snapshot is not None:
        phase_restoration = verify_phase_restoration(
            current_snapshot(), phase_baseline_snapshot
        )
        if phase_restoration.status is RestorationStatus.RESTORED:
            phase_note = "phase ending gate verified via L6 phase restoration"
        else:
            stop_reason = EngineStopReason.PHASE_RESTORATION_FAILED
            prior_stop = f"{stop_reason.value}: {stop_detail}"
            stop_detail = (
                "phase ending gate failed closed: "
                f"{phase_restoration.reason}; prior engine stop was {prior_stop}"
            )
            phase_note = (
                "phase ending gate did not verify restoration; engine result requires attention"
            )
    else:
        phase_restoration = None
        phase_note = (
            "phase ending gate skipped: phase verification inputs were not provided"
        )
    return EngineLoopResult(
        environment_digest=config.environment_digest,
        formula_versions_digest=formula_versions_digest(config.formula_versions),
        rounds=rounds,
        stop_reason=stop_reason,
        stop_detail=stop_detail,
        phase_restoration=phase_restoration,
        phase_verification_note=phase_note,
        formula_rejections=all_formula_rejections,
        started_at=started_at,
        finished_at=finished_at,
    )


__all__ = [
    "EngineLoopConfig",
    "EngineLoopResult",
    "EngineRoundRecord",
    "EngineStopReason",
    "run_engine_loop",
]
