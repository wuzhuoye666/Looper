"""Sysbench normalizer: turn the native raw result into Looper observations.

Maps the parsed sysbench report onto standard metrics (events/s and latency
quantiles for every test, throughput for memory/fileio) and writes the
`looper-adapter/v1` result.json. Fail closed: a non-zero exit code or missing
core evidence produces a failed result with the check unmet.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from looper_benchmark_sdk import emit_metric, load_envelope, write_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sysbench normalizer")
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    envelope = load_envelope(args.envelope)
    workload = envelope.get("workload", {})
    workload_id = workload.get("id")
    output = Path(args.output)
    raw = json.loads((output / "raw-result.json").read_text(encoding="utf-8"))

    exit_code = int(raw.get("exitCode", 1))
    events_per_second = raw.get("eventsPerSecond")
    latency = raw.get("latencyMs") or {}
    has_events = isinstance(events_per_second, (int, float))
    valid = exit_code == 0 and has_events

    if valid:
        emit_metric(
            output,
            "events_per_sec",
            float(events_per_second),
            "events/s",
            workload=workload_id,
            statistic="rate",
        )
        latency_map = {
            "latency_avg_ms": latency.get("avg"),
            "latency_p95_ms": latency.get("p95"),
            "latency_max_ms": latency.get("max"),
        }
        for metric, value in latency_map.items():
            if value is None:
                continue
            emit_metric(
                output,
                metric,
                float(value),
                "ms",
                workload=workload_id,
                statistic="sample",
            )
        throughput = raw.get("throughput")
        if isinstance(throughput, dict) and throughput.get("value") is not None:
            emit_metric(
                output,
                "throughput_mib_s",
                float(throughput["value"]),
                "MiB/s",
                workload=workload_id,
                statistic="rate",
            )

    message = (
        "sysbench exit 0 with a parseable events-per-second value"
        if valid
        else f"sysbench exit={exit_code}, eventsPerSecond={events_per_second!r}"
    )
    write_result(
        output,
        {
            "schemaVersion": "v1alpha1",
            "status": "succeeded" if valid else "failed",
            "checks": [
                {
                    "id": "sysbench-run-ok",
                    "passed": valid,
                    "scope": "attempt",
                    "kind": "correctness",
                    "message": message,
                }
            ],
        },
    )
    with (output / "adapter.log").open("a", encoding="utf-8") as log:
        log.write(
            f"normalizer_workload={workload_id} valid={valid}\n"
            "normalization=completed\n"
        )
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
