from __future__ import annotations

from pathlib import Path

import pytest
from looper_core.system_opt.component.strategy import (
    ComponentStrategy,
    load_strategies,
    parse_strategy_yaml,
)

STRATEGIES = (
    Path(__file__).parents[1]
    / "packages/core/looper_core/system_opt/component/strategies"
)


def test_all_five_component_strategies_load() -> None:
    loaded = load_strategies(STRATEGIES)
    assert set(loaded) == {"cpu", "memory", "network", "storage", "numa"}
    for strategy in loaded.values():
        assert strategy.digest.startswith("sha256:")
        assert strategy.candidate_sources


def test_unknown_component_rejected() -> None:
    text = (STRATEGIES / "cpu.yaml").read_text(encoding="utf-8").replace(
        "component: cpu", "component: gpu"
    )
    with pytest.raises(ValueError, match="unknown component"):
        parse_strategy_yaml(text)


def test_duplicate_component_fails_closed(tmp_path: Path) -> None:
    source = (STRATEGIES / "cpu.yaml").read_text(encoding="utf-8")
    (tmp_path / "a.yaml").write_text(source, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate strategy"):
        load_strategies(tmp_path)


def test_strategy_round_trip_preserves_digest() -> None:
    strategy = parse_strategy_yaml((STRATEGIES / "cpu.yaml").read_text(encoding="utf-8"))
    again = ComponentStrategy.model_validate_json(strategy.model_dump_json())
    assert again.digest == strategy.digest
