"""L8 调度器：按组件优先级与 L7 负缓存选择下一组 (组件, 候选)。

架构层：总体架构 v2 的 L8。S3 真实组件路由（workload 症状→组件假设）属于动态
相位；第一版以打分器给出的组件顺序表代替。全部候选被负缓存命中时显式返回
"无可试候选"，不算错误。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.engine.scorer import ComponentScore
from looper_core.system_opt.negative_cache import NegativeCache

SCHEDULER_SCHEMA = "looper.scheduler-decision/v1alpha1"


class SkippedCandidate(StrictModel):
    component: str = Field(min_length=1)
    parameters: dict[str, Any]
    cache_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SchedulerSelection(StrictModel):
    component: str = Field(min_length=1)
    parameters: dict[str, Any]


class SchedulerDecision(StrictModel):
    schema_version: Literal[SCHEDULER_SCHEMA] = SCHEDULER_SCHEMA
    selection: SchedulerSelection | None
    skipped: list[SkippedCandidate]
    exhausted_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def select_next_candidate(
    component_scores: Sequence[ComponentScore],
    candidate_pools: Mapping[str, Sequence[Mapping[str, Any]]],
    negative_cache: NegativeCache,
    *,
    environment_digest: str,
    pressure_protocol_digests: Mapping[str, str],
    formula_versions: Mapping[str, str],
) -> SchedulerDecision:
    """Pick the highest-priority (component, candidate) not blocked by the cache.

    Components follow scorer order; candidates follow pool order. The decision
    records every cache-blocked candidate with its exact cache key.
    """

    if not component_scores:
        raise ValueError("select_next_candidate requires component scores")
    skipped: list[SkippedCandidate] = []
    for score in component_scores:
        component = score.component
        for parameters in candidate_pools.get(component, ()):
            hits = negative_cache.lookup(
                environment_digest=environment_digest,
                candidate_parameters=parameters,
                pressure_protocol_digest=pressure_protocol_digests[component],
                formula_versions=formula_versions,
            )
            if hits:
                skipped.append(
                    SkippedCandidate(
                        component=component,
                        parameters=dict(parameters),
                        cache_key=hits[0].identity.key,
                    )
                )
                continue
            return SchedulerDecision(
                selection=SchedulerSelection(component=component, parameters=dict(parameters)),
                skipped=skipped,
            )
    return SchedulerDecision(
        selection=None,
        skipped=skipped,
        exhausted_reason=(
            "every candidate in every scored component is blocked by the negative cache"
        ),
    )


__all__ = ["SchedulerDecision", "SchedulerSelection", "SkippedCandidate", "select_next_candidate"]
