from __future__ import annotations

RETIRED_BENCHMARK_IDS = frozenset({
    "benchbase.smallbank.postgres",
    "looper.fixture.config-driven",
    "looper.demo.compression",
})


def is_retired_benchmark(benchmark_id: str) -> bool:
    return benchmark_id in RETIRED_BENCHMARK_IDS
