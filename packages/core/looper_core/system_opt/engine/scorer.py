"""L8 打分器：S4 组件优先级的编排与组件排序。

架构层：总体架构 v2 的 L8（见 docs/system-optimizer/architecture/overall.md）。
只做打分与排序，不做门禁判定（判断器职责）、不做候选选择（调度器职责）。
分数计算沿用 S4（``diagnostic_priorities``），本模块不引入新的阈值常量。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.scoring import DiagnosticPriority

SCORER_SCHEMA = "looper.component-score/v1alpha1"


class ComponentScore(StrictModel):
    schema_version: Literal[SCORER_SCHEMA] = SCORER_SCHEMA
    component: str = Field(min_length=1, max_length=40)
    max_pressure: float = Field(ge=0)
    max_adverse_change: float = Field(ge=0)
    best_pareto_rank: int | None = None
    metric_count: int = Field(ge=1)
    priorities_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def score_components(priorities: Sequence[DiagnosticPriority]) -> list[ComponentScore]:
    """Group S4 priorities by component and compute each component's score.

    Ordering rule (deterministic, no weights): higher max_pressure first, then
    higher max_adverse_change, then component name.
    """

    if not priorities:
        raise ValueError("score_components requires at least one diagnostic priority")
    grouped: dict[str, list[DiagnosticPriority]] = {}
    for priority in priorities:
        grouped.setdefault(priority.component, []).append(priority)
    scores: list[ComponentScore] = []
    for component, items in grouped.items():
        ranks = [item.pareto_rank for item in items if item.pareto_rank is not None]
        scores.append(
            ComponentScore(
                component=component,
                max_pressure=max(item.pressure for item in items),
                max_adverse_change=max(item.adverse_change for item in items),
                best_pareto_rank=min(ranks) if ranks else None,
                metric_count=len(items),
                priorities_digest=canonical_digest(
                    [item.model_dump(mode="json") for item in items]
                ),
            )
        )
    scores.sort(
        key=lambda score: (-score.max_pressure, -score.max_adverse_change, score.component)
    )
    return scores


def rank_components(scores: Sequence[ComponentScore]) -> list[str]:
    return [score.component for score in scores]


__all__ = ["ComponentScore", "rank_components", "score_components"]
