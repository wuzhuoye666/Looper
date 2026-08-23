from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module() -> object:
    path = (
        Path(__file__).parents[1]
        / "examples"
        / "system-optimizer"
        / "memory_pressure_measure.py"
    )
    spec = importlib.util.spec_from_file_location("memory_pressure_measure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extracts_sysbench_memory_bandwidth_and_p95() -> None:
    module = _load_module()
    output = """
1000.00 MiB transferred (50000.25 MiB/sec)
Latency (ms):
         95th percentile:                        0.13
"""

    assert module.extract_metrics(output) == pytest.approx((50000.25, 0.13))


def test_rejects_partial_sysbench_memory_output() -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="missing throughput or p95"):
        module.extract_metrics("95th percentile: 0.1")
