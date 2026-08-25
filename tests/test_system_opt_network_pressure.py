from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module() -> object:
    path = (
        Path(__file__).parents[1]
        / "examples"
        / "system-optimizer"
        / "network_pressure_measure.py"
    )
    spec = importlib.util.spec_from_file_location("network_pressure_measure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extracts_iperf_receive_throughput_and_retransmits() -> None:
    module = _load_module()
    payload = {
        "end": {
            "sum_sent": {"bits_per_second": 9_000_000_000, "retransmits": 3},
            "sum_received": {"bits_per_second": 8_000_000_000},
        }
    }

    assert module.extract_metrics(payload) == pytest.approx((8.0, 3.0))


def test_iperf_error_fails_closed() -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="connection refused"):
        module.extract_metrics({"error": "connection refused"})


def test_loopback_identity_is_not_confused_with_remote_network() -> None:
    module = _load_module()

    assert module._is_loopback("127.0.0.1") is True
    assert module._is_loopback("::1") is True
    assert module._is_loopback("10.0.0.2") is False
