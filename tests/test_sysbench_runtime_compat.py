from __future__ import annotations

import importlib.util
from pathlib import Path


def _producer_module():
    path = Path(__file__).resolve().parents[1] / "benchmarks" / "sysbench" / "producer.py"
    spec = importlib.util.spec_from_file_location("looper_sysbench_producer_compat", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_singular_thread_workload_uses_sysbench_threads_builtin() -> None:
    producer = _producer_module()
    argv = producer.build_argv("sysbench", "thread", 4, 10, [])
    assert argv[:2] == ["sysbench", "threads"]
