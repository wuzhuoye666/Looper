from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_measurement_module() -> object:
    path = Path(__file__).parents[1] / "examples" / "system-optimizer" / "fio_randread_measure.py"
    spec = importlib.util.spec_from_file_location("fio_randread_measure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fio_measurement_sums_iops_and_uses_conservative_job_p99() -> None:
    module = _load_measurement_module()
    payload = {
        "jobs": [
            {
                "error": 0,
                "read": {
                    "iops": 100.5,
                    "io_bytes": 4096,
                    "clat_ns": {"percentile": {"99.000000": 2000}},
                },
            },
            {
                "error": 0,
                "read": {
                    "iops": 99.5,
                    "io_bytes": 8192,
                    "clat_ns": {"percentile": {"99.000000": 3000}},
                },
            },
        ]
    }

    assert module.extract_metrics(payload) == (200.0, 3.0, True)
