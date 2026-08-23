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

候选/公式映射直连执行（scheduler 选中具体候选后定向执行）属于 PKG-B
公式映射落地后的扩展；当前 scheduler 的选中候选仅决定执行哪个组件。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.component import ComponentOptimizer
from looper_core.system_opt.engine.judge import CandidateVerdict, evaluate_candidate
from looper_core.system_opt.engine.scheduler import (
    SchedulerDecision,
    SkippedCandidate,
    select_next_candidate,
)
from looper_core.system_opt.engine.scorer import ComponentScore
from looper_core.system_opt.negative_cache import (
    NegativeCache,
    NegativeCacheEntry,
    NegativeVerdict,
    candidate_parameters_digest,
    formula_versions_digest,
)
from looper_core.system_opt.rollback import PhaseRestoration, verify_phase_restoration
from looper_core.system_opt.executor import ConfigSnapshot

ENGINE_LOOP_SCHEMA = "looper.engine-loop-result/v1alpha1"


class EngineStopReason(StrEnum):
    COMPLETED = "completed-all-components"
    ROUND_BUDGET = "round-budget-exhausted"
    ALL_CACHED = "all-candidates-negative-cached"


class EngineLoopConfig(StrictModel):
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    formula_versions: dict[str, str] = Field(min_length=1)
    pressure_protocol_digests: dict[str, str] = Field(min_length=1)
    max_rounds: int = Field(ge=1)


class EngineRoundRecord(StrictModel):
    round_index: int = Field(ge=1)
    component: str = Field(min_length=1)
    report_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    selected_parameters: dict[str, Any]
    skipped: list[SkippedCandidate]
    verdicts: list[CandidateVerdict]
    cache_entry_digests: list[str] = Field(default_factory=list)


class EngineLoopResult(StrictModel):
    schema_version: Literal[ENGINE_LOOP_SCHEMA] = ENGINE_LOOP_SCHEMA
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    formula_versions_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rounds: list[EngineRoundRecord] = Field(default_factory=list)
    stop_reason: EngineStopReason
    stop_detail: str = Field(min_length=1, max_length=1000)
    phase_restoration: PhaseRestoration | None = None
    phase_verification_note: str = Field(min_length=1, max_length=500)
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
    scores = list(component_scores) if component_scores is not None else _neutral_scores(components)
    by_component = {optimizer.component: optimizer for optimizer in component_optimizers}
    completed: set[str] = set()
    rounds: list[EngineRoundRecord] = []
    stop_reason = EngineStopReason.ROUND_BUDGET
    stop_detail = "explicit round budget exhausted"

    for round_index in range(1, config.max_rounds + 1):
        active = [by_component[name] for name in components if name not in completed]
        if not active:
            stop_reason = EngineStopReason.COMPLETED
            stop_detail = "every component optimizer finished one engine round"
            break
        pools = {
            optimizer.component: optimizer.candidate_pool()
            for optimizer in active
        }
        decision: SchedulerDecision = select_next_candidate(
            [score for score in scores if score.component in pools],
            pools,
            negative_cache,
            environment_digest=config.environment_digest,
            pressure_protocol_digests=config.pressure_protocol_digests,
            formula_versions=config.formula_versions,
        )
        if decision.selection is None:
            stop_reason = EngineStopReason.ALL_CACHED
            stop_detail = (
                "every remaining component candidate pool is fully blocked "
                "by the negative cache"
            )
            break
        component = decision.selection.component
        optimizer = by_component[component]
        optimizer.suggest_candidates()
        report = optimizer.run(
            baseline_parameters=baseline_parameters[component],
            measure=measures[component],
            fencing_token=fencing_token,
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
        cache_entry_digests: list[str] = []
        recorded_at = datetime.now(UTC)
        for candidate, verdict in zip(report.candidates, verdicts, strict=True):
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
        if not verdicts:
            raise ValueError(
                f"component '{component}' produced no candidate evaluations; "
                "the engine loop cannot judge an empty report"
            )
        rounds.append(
            EngineRoundRecord(
                round_index=round_index,
                component=component,
                report_digest=report.digest,
                selected_parameters=decision.selection.parameters,
                skipped=decision.skipped,
                verdicts=verdicts,
                cache_entry_digests=cache_entry_digests,
            )
        )
        completed.add(component)

    finished_at = datetime.now(UTC)
    if phase_baseline_snapshot is not None and current_snapshot is not None:
        phase_restoration = verify_phase_restoration(
            current_snapshot(), phase_baseline_snapshot
        )
        phase_note = "phase ending gate verified via L6 phase restoration"
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
