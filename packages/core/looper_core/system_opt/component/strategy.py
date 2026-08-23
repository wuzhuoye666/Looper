"""L5 组件优化策略：每组件的候选来源、指标与边界的声明式定义。

架构层：L5（docs/system-optimizer/architecture/overall.md）。策略是**数据**，
不是代码：公式引用必须来自 formula-provenance 登记表；执行仍走
manifest/policy/protocol 合同与 L1 安全链。策略不绕过任何门禁。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel

STRATEGY_SCHEMA = "looper.component-strategy/v1alpha1"
KNOWN_COMPONENTS = frozenset({"cpu", "memory", "storage", "network", "numa"})


class CandidateSource(StrictModel):
    formula_id: str = Field(min_length=1, max_length=200)
    provenance: Literal["paper", "official-doc", "heuristic"]
    applies_when: str = Field(min_length=1, max_length=500)


class ComponentStrategy(StrictModel):
    schema_version: Literal[STRATEGY_SCHEMA] = STRATEGY_SCHEMA
    component: str = Field(min_length=1, max_length=40)
    primary_metric: str = Field(min_length=1, max_length=160)
    direction: Literal["maximize", "minimize", "diagnostic-only"]
    stability_metric: str = Field(min_length=1, max_length=160)
    search_item_ids: list[str] = Field(default_factory=list)
    candidate_sources: list[CandidateSource] = Field(min_length=1)
    hard_gates: list[str] = Field(default_factory=list)
    boundaries: str = Field(min_length=1, max_length=1000)
    references: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_component(self) -> ComponentStrategy:
        if self.component not in KNOWN_COMPONENTS:
            raise ValueError(f"unknown component: {self.component}")
        if self.direction == "diagnostic-only":
            # Unavailable components (e.g. single-NUMA-node guests) may record a
            # strategy without searchable items; they must not fake a search.
            if self.search_item_ids or self.hard_gates:
                raise ValueError(
                    "diagnostic-only strategies must not declare search items or gates"
                )
        elif not self.search_item_ids or not self.hard_gates:
            raise ValueError(
                "optimizing strategies require search_item_ids and hard_gates"
            )
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def parse_strategy_yaml(text: str) -> ComponentStrategy:
    return ComponentStrategy.model_validate(yaml.safe_load(text))


def load_strategies(root: Path) -> dict[str, ComponentStrategy]:
    """Load every strategy file under ``root``; duplicate components fail closed."""

    strategies: dict[str, ComponentStrategy] = {}
    for path in sorted(root.glob("*.yaml")):
        strategy = parse_strategy_yaml(path.read_text(encoding="utf-8"))
        if strategy.component in strategies:
            raise ValueError(
                f"duplicate strategy for component '{strategy.component}': {path}"
            )
        strategies[strategy.component] = strategy
    return strategies


__all__ = [
    "CandidateSource",
    "ComponentStrategy",
    "KNOWN_COMPONENTS",
    "load_strategies",
    "parse_strategy_yaml",
]
