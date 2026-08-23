"""L5 组件优化器：组件实例包装、公式映射钩子与上报结构。

架构层：总体架构 v2 的 L5（见 docs/system-optimizer/architecture/overall.md）。
一个组件优化器 = 一个组件的 manifest+policy+domains+引擎实例 + 可选公式映射。
组件**不做终裁**：``CandidateEvaluation.accepted`` 只是组件级晋级建议
（S7 组件内判定），最终接受结论由 L8 引擎的判断器
（``looper_core.system_opt.engine.evaluate_candidate``）产出。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import ComponentMetricSnapshot
from looper_core.system_opt.scoring import MeasurementBatch
from looper_core.system_opt.tuning import CandidateEvaluation, OptimizationRun, SystemOptimizationEngine

COMPONENT_REPORT_SCHEMA = "looper.component-report/v1alpha1"
NO_FINAL_VERDICT_NOTE = (
    "component-level report only: 'accepted' on candidates is a promotion "
    "suggestion (S7 in-component), the final verdict belongs to the L8 engine judge"
)


class CandidateSuggestion(StrictModel):
    """One formula-mapped candidate recommendation (a concrete value, not a direction)."""

    parameters: dict[str, Any] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=500)
    formula_id: str = Field(min_length=1, max_length=160)


class FormulaMapping(Protocol):
    """Per-component formula mapping: collected metrics -> candidate suggestions.

    First version may return no suggestions (search fallback); the interface
    itself is the contract consumed by the L8 engine loop.
    """

    def suggest(
        self,
        snapshot: ComponentMetricSnapshot | None,
        baseline: MeasurementBatch | None,
    ) -> list[CandidateSuggestion]: ...


class NullFormulaMapping:
    """Default no-op mapping: the component falls back to plain search."""

    def suggest(
        self,
        snapshot: ComponentMetricSnapshot | None,
        baseline: MeasurementBatch | None,
    ) -> list[CandidateSuggestion]:
        return []


class ComponentReport(StrictModel):
    schema_version: Literal[COMPONENT_REPORT_SCHEMA] = COMPONENT_REPORT_SCHEMA
    component: str = Field(min_length=1, max_length=40)
    policy_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    run_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    stop_reason: str | None = None
    baseline_batch_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    candidates: list[CandidateEvaluation] = Field(default_factory=list)
    promotion_suggestions: list[str] = Field(default_factory=list)
    formula_suggestions: list[CandidateSuggestion] = Field(default_factory=list)
    semantic_note: str = NO_FINAL_VERDICT_NOTE

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class ComponentOptimizer:
    """Wrap one component's engine instance behind the L5 report contract."""

    def __init__(
        self,
        engine: SystemOptimizationEngine,
        formula_mapping: FormulaMapping | None = None,
    ) -> None:
        components = list(engine.policy.authorized_components)
        if len(components) != 1:
            raise ValueError(
                "a component optimizer wraps exactly one component; "
                f"policy declares {components}"
            )
        self.engine = engine
        self.formula_mapping: FormulaMapping = formula_mapping or NullFormulaMapping()
        self._formula_suggestions: list[CandidateSuggestion] = []

    @property
    def component(self) -> str:
        return self.engine.policy.authorized_components[0]

    def suggest_candidates(
        self,
        snapshot: ComponentMetricSnapshot | None = None,
        baseline: MeasurementBatch | None = None,
    ) -> list[CandidateSuggestion]:
        """Ask the formula mapping for candidate recommendations.

        Suggestions are remembered so the next ``run`` report carries them; an
        empty result simply means the engine loop must fall back to search.
        """

        self._formula_suggestions = list(self.formula_mapping.suggest(snapshot, baseline))
        return list(self._formula_suggestions)

    def candidate_pool(self) -> list[dict[str, Any]]:
        """Enumerate this component's grid candidates for cache consultation.

        The pool is the deterministic grid over the resolved search space; it
        exists so the L8 scheduler can recognize a fully negative-cached
        component before spending measurement budget on it.
        """

        from looper_core.optimizer import grid_candidates

        space = self.engine.search_space({self.component})
        return grid_candidates(space)

    def run(
        self,
        *,
        baseline_parameters: Mapping[str, Any],
        measure: Any,
        fencing_token: int,
        diagnostic_reference: MeasurementBatch | None = None,
        preexisting: Sequence[Mapping[str, Any]] | None = None,
    ) -> ComponentReport:
        run: OptimizationRun = self.engine.run(
            baseline_parameters=baseline_parameters,
            measure=measure,
            fencing_token=fencing_token,
            diagnostic_reference=diagnostic_reference,
            preexisting=preexisting,
        )
        return self.report(run)

    def report(self, run: OptimizationRun) -> ComponentReport:
        return ComponentReport(
            component=self.component,
            policy_digest=run.policy_digest,
            manifest_digest=run.manifest_digest,
            run_digest=run.digest,
            stop_reason=run.stop_reason.value,
            baseline_batch_digest=run.baseline.digest,
            candidates=list(run.candidates),
            promotion_suggestions=[
                candidate.candidate_id
                for candidate in run.candidates
                if candidate.accepted
            ],
            formula_suggestions=list(self._formula_suggestions),
        )


__all__ = [
    "CandidateSuggestion",
    "ComponentOptimizer",
    "ComponentReport",
    "FormulaMapping",
    "NO_FINAL_VERDICT_NOTE",
    "NullFormulaMapping",
]
