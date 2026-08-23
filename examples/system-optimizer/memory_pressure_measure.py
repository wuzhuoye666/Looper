from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

_THROUGHPUT_PATTERN = re.compile(
    r"(?P<total>[0-9.]+) MiB transferred \((?P<rate>[0-9.]+) MiB/sec\)"
)
_P95_PATTERN = re.compile(r"95th percentile:\s+(?P<p95>[0-9.]+)")


def _target_identifier() -> str:
    try:
        raw = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        raw = "unavailable"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_metrics(output: str) -> tuple[float, float]:
    throughput = _THROUGHPUT_PATTERN.search(output)
    latency = _P95_PATTERN.search(output)
    if throughput is None or latency is None:
        raise ValueError("sysbench memory output is missing throughput or p95 latency")
    rate = float(throughput.group("rate"))
    p95_ms = float(latency.group("p95"))
    if not all(math.isfinite(value) and value >= 0 for value in (rate, p95_ms)):
        raise ValueError("sysbench memory metrics are not finite and non-negative")
    if rate <= 0:
        raise ValueError("sysbench memory throughput must be positive")
    return rate, p95_ms


def _version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _run_once(args: argparse.Namespace, output: Path) -> tuple[float, float]:
    command = [
        args.sysbench,
        "memory",
        f"--threads={args.threads}",
        f"--memory-block-size={args.block_size}",
        f"--memory-total-size={args.total_size}",
        f"--memory-scope={args.scope}",
        f"--memory-oper={args.operation}",
        f"--memory-access-mode={args.access_mode}",
        f"--time={args.duration_seconds}",
        "run",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=args.duration_seconds + 30,
    )
    output.write_text(result.stdout, encoding="utf-8")
    output.with_suffix(".stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(result.stderr or f"sysbench exited {result.returncode}")
    return extract_metrics(result.stdout)


def _prepare(args: argparse.Namespace) -> None:
    if os.name != "posix" or not Path("/proc/meminfo").is_file():
        raise SystemExit("memory pressure probe requires Linux")
    executable = shutil.which(args.sysbench)
    if executable is None:
        raise SystemExit(f"sysbench is unavailable: {args.sysbench}")
    if args.threads > (os.cpu_count() or 0):
        raise SystemExit("memory pressure threads exceed the online CPU count")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
    payload = {
        "schema_version": "looper.memory-pressure-preflight/v1alpha1",
        "target": _target_identifier(),
        "threads": args.threads,
        "block_size": args.block_size,
        "total_size": args.total_size,
        "scope": args.scope,
        "operation": args.operation,
        "access_mode": args.access_mode,
        "duration_seconds": args.duration_seconds,
        "sysbench": _version(executable),
        "meminfo_sha256": hashlib.sha256(meminfo.encode("utf-8")).hexdigest(),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    (args.output_dir / "preflight.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _warmup(args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _run_once(args, args.output_dir / "warmup.txt")


def _measure(args: argparse.Namespace) -> None:
    if args.repeats < 2:
        raise SystemExit("repeats must be at least 2")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    bandwidth: list[float] = []
    p95_latency: list[float] = []
    for index in range(args.repeats):
        rate, p95 = _run_once(args, args.output_dir / f"memory-{started}-{index + 1}.txt")
        bandwidth.append(rate)
        p95_latency.append(p95)
    batch = {
        "identity": {
            "target": _target_identifier(),
            "workload": (
                f"sysbench-memory-{args.operation}-{args.access_mode}-"
                f"threads{args.threads}-block{args.block_size}"
            ),
            "phase": "steady-state-after-discarded-warmup",
            "tool": _version(args.sysbench),
            "statistics": (
                f"repeats={args.repeats};duration={args.duration_seconds}s;"
                f"total={args.total_size};scope={args.scope}"
            ),
        },
        "metrics": {
            "memory.bandwidth-mib-per-second": {
                "metric_id": "memory.bandwidth-mib-per-second",
                "values": bandwidth,
            },
            "memory.latency-p95-ms": {
                "metric_id": "memory.latency-p95-ms",
                "values": p95_latency,
            },
            "memory.success": {
                "metric_id": "memory.success",
                "values": [1.0 for _ in bandwidth],
            },
        },
        "gate_values": {"memory.success": True},
    }
    (args.output_dir / "latest-batch.json").write_text(
        json.dumps(batch, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(batch, separators=(",", ":")))


def _verify(args: argparse.Namespace) -> None:
    batch_path = args.output_dir / "latest-batch.json"
    if not batch_path.is_file():
        raise SystemExit("measurement batch is missing")
    payload = json.loads(batch_path.read_text(encoding="utf-8"))
    for metric_id in ("memory.bandwidth-mib-per-second", "memory.latency-p95-ms"):
        values = payload["metrics"][metric_id]["values"]
        if len(values) != args.repeats or not all(
            math.isfinite(float(value)) for value in values
        ):
            raise SystemExit(f"measurement batch contains invalid {metric_id} values")
    if payload.get("gate_values", {}).get("memory.success") is not True:
        raise SystemExit("memory success gate is not true")


def _cleanup(args: argparse.Namespace) -> None:
    marker = args.output_dir / "active-pressure.pid"
    if marker.exists():
        raise SystemExit("memory pressure PID marker remains after synchronous execution")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "warmup", "measure", "verify", "cleanup"])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--block-size", required=True)
    parser.add_argument("--total-size", required=True)
    parser.add_argument("--scope", choices=["global", "local"], required=True)
    parser.add_argument("--operation", choices=["read", "write", "none"], required=True)
    parser.add_argument("--access-mode", choices=["seq", "rnd"], required=True)
    parser.add_argument("--sysbench", default="sysbench")
    args = parser.parse_args()
    if args.threads < 1 or args.duration_seconds < 1:
        raise SystemExit("threads and duration-seconds must be positive")
    actions = {
        "prepare": _prepare,
        "warmup": _warmup,
        "measure": _measure,
        "verify": _verify,
        "cleanup": _cleanup,
    }
    actions[args.action](args)


if __name__ == "__main__":
    main()
