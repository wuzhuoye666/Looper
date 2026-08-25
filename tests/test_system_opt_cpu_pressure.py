from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module() -> object:
    path = Path(__file__).parents[1] / "examples" / "system-optimizer" / "cpu_pressure_measure.py"
    spec = importlib.util.spec_from_file_location("cpu_pressure_measure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extracts_cpu_real_time_throughput() -> None:
    module = _load_module()
    payload = {
        "metrics": [
            {
                "stressor": "cpu",
                "bogo-ops-per-second-real-time": 1234.5,
            }
        ]
    }

    assert module._extract_stress_metric(payload) == pytest.approx(1234.5)


def test_rejects_missing_cpu_metric() -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="no cpu metric"):
        module._extract_stress_metric({"metrics": [{"stressor": "stream"}]})
