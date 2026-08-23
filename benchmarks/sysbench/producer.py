"""Sysbench producer: run the native sysbench binary and capture a raw result.

Locates sysbench through ``LOOPER_SYSBENCH_BIN`` or ``PATH``. The executable is
*never* fabricated: if it is missing the run fails closed with a clear error and
no raw result is produced. A failed sysbench run is still captured as evidence
(exit code + stderr) so the failure is traceable instead of silently dropped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from looper_benchmark_sdk import load_envelope

DEFAULT_TIMEOUT_SECONDS = 360


class SysbenchError(RuntimeError):
    pass


def resolve_sysbench_bin() -> str:
    explicit = os.environ.get("LOOPER_SYSBENCH_BIN")
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise SysbenchError(
                f"LOOPER_SYSBENCH_BIN points to a missing file: {candidate}"
            )
        return str(candidate.resolve())
    found = shutil.which("sysbench")
    if found is None:
        raise SysbenchError(
            "sysbench executable not found in PATH; install sysbench or set "
            "LOOPER_SYSBENCH_BIN (e.g. WSL/Docker path to the sysbench binary)"
        )
    return found


def _first_float(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text)
    return float(match.group(1)) if match else None


def parse_sysbench_output(text: str) -> dict[str, object]:
    """Extract the stable facts from sysbench's text report.

    The report layout is stable across sysbench 1.0 tests: an events-per-second
    line, a general statistics block and a Latency (ms) block with min/avg/max/
    95th percentile/sum. Memory and fileio tests add a Throughput block.
    """

    parsed: dict[str, object] = {}
    version = re.search(r"^sysbench\s+([0-9][0-9.]*)", text, re.MULTILINE)
    if version:
        parsed["version"] = version.group(1)
    total_time = _first_float(text, r"total time:\s+([0-9.]+)s")
    if total_time is not None:
        parsed["totalTimeSeconds"] = total_time
    events_per_second = _first_float(text, r"events per second:\s+([0-9.]+)")
    total_events = re.search(r"total number of events:\s+(\d+)", text)
    if total_events:
        parsed["totalEvents"] = int(total_events.group(1))
    if events_per_second is None and total_events and total_time:
        # Some tests (memory, mutex) do not print "events per second"; derive it
        # from the total event count and measured wall time so every workload has
        # a comparable events/s figure.
        parsed["eventsPerSecond"] = float(total_events.group(1)) / total_time
    elif events_per_second is not None:
        parsed["eventsPerSecond"] = events_per_second

    latency: dict[str, float] = {}
    for key, pattern in (
        ("min", r"min:\s+([0-9.]+)"),
        ("avg", r"avg:\s+([0-9.]+)"),
        ("max", r"max:\s+([0-9.]+)"),
        ("p95", r"95th percentile:\s+([0-9.]+)"),
        ("sum", r"sum:\s+([0-9.]+)"),
    ):
        value = _first_float(text, pattern)
        if value is not None:
            latency[key] = value
    if latency:
        parsed["latencyMs"] = latency

    throughput = _first_float(text, r"([0-9.]+)\s+MiB/sec")
    if throughput is not None:
        parsed["throughput"] = {"unit": "MiB/sec", "value": throughput}

    fairness = re.search(
        r"events \(avg/stddev\):\s+([0-9.]+)/([0-9.]+)", text
    )
    if fairness:
        parsed["threadsFairness"] = {
            "eventsAvg": float(fairness.group(1)),
            "eventsStddev": float(fairness.group(2)),
        }
    return parsed


def build_argv(
    bin_path: str, test: str, threads: int, duration: int, extra: list[str]
) -> list[str]:
    # The Looper workload keeps the stable singular id ``thread`` while
    # sysbench 1.0 exposes the native built-in as ``threads``.
    native_test = "threads" if test == "thread" else test
    return [
        bin_path,
        native_test,
        f"--threads={threads}",
        f"--time={duration}",
        "--events=0",
        "--report-interval=0",
        *extra,
        "run",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sysbench producer")
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    envelope = load_envelope(args.envelope)
    parameters = envelope.get("candidate", {}).get("parameters", {})
    threads = int(parameters.get("threads", 4))
    duration = int(parameters.get("time", 10))
    test = str(envelope.get("workload", {}).get("metadata", {}).get("test", "cpu"))
    extra_args = list(envelope.get("workload", {}).get("metadata", {}).get("extraArgs", []))

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    log_path = output / "adapter.log"

    bin_path = resolve_sysbench_bin()
    argv = build_argv(bin_path, test, threads, duration, extra_args)
    timeout = min(max(duration + 60, 90), DEFAULT_TIMEOUT_SECONDS)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"producer_argv={' '.join(argv)}\n")
        log.flush()
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
        log.write(f"exit_code={completed.returncode}\n")
        log.write("--- stdout ---\n")
        log.write(completed.stdout)
        log.write("\n--- stderr ---\n")
        log.write(completed.stderr)
        log.write("\n")

    raw = {
        "suite": "sysbench",
        "test": test,
        "parameters": {"threads": threads, "time": duration},
        "argv": argv,
        "exitCode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    raw.update(parse_sysbench_output(completed.stdout))
    (output / "raw-result.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return completed.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SysbenchError, subprocess.TimeoutExpired) as error:
        print(f"sysbench producer failed closed: {error}", file=sys.stderr)
        raise SystemExit(2) from None
