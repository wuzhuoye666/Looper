from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module() -> object:
    path = (
        Path(__file__).parents[1]
        / "examples"
        / "system-optimizer"
        / "numa_pressure_measure.py"
    )
    spec = importlib.util.spec_from_file_location("numa_pressure_measure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parses_single_and_ranged_numa_node_lists() -> None:
    module = _load_module()

    assert module.parse_numa_nodes("available: 1 nodes (0)\n") == [0]
    assert module.parse_numa_nodes("available: 4 nodes (0-3)\n") == [0, 1, 2, 3]


def test_rejects_inconsistent_numa_count() -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="count and node list disagree"):
        module.parse_numa_nodes("available: 2 nodes (0)\n")
