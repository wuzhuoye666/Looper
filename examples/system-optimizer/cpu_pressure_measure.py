from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


def _target_identifier() -> str:
    try:
        raw = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        raw = "unavailable"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _tool_version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() or result.stderr.strip()


def _extract_stress_metric(payload: dict[str, Any]) -> float:
    metrics = payload.get("metrics")
    if not isinstance(metrics, list):
        raise ValueError("stress-ng YAML contains no metrics list")
    cpu = next(
        (item for item in metrics if isinstance(item, dict) and item.get("stressor") == "cpu"),
        None,
    )
    if cpu is None:
        raise ValueError("stress-ng YAML contains no cpu metric")
    value = float(cpu["bogo-ops-per-second-real-time"])
    if not math.isfinite(value) or value <= 0:
        raise ValueError("stress-ng CPU throughput is not positive and finite")
    return value


def _run_once(
    *,
    stress_ng: str,
    workers: int,
    method: str,
    duration_seconds: int,
    output: Path,
) -> float:
    command = [
        stress_ng,
        "--cpu",
        str(workers),
        "--cpu-method",
        method,
        "--timeout",
        f"{duration_seconds}s",
        "--metrics-brief",
        "--verify",
        "--yaml",
        str(output),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=duration_seconds + 30,
    )
    log_path = output.with_suffix(".stderr.log")
    log_path.write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(result.stderr or f"stress-ng exited {result.returncode}")
    payload = yaml.safe_load(output.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("stress-ng YAML root is not an object")
    return _extract_stress_metric(payload)


def _prepare(args: argparse.Namespace) -> None:
    if os.name != "posix" or not Path("/proc").is_dir():
        raise SystemExit("CPU pressure probe requires Linux")
    executable = shutil.which(args.stress_ng)
    if executable is None:
        raise SystemExit(f"stress-ng is unavailable: {args.stress_ng}")
    cpu_count = os.cpu_count() or 0
    if args.workers > cpu_count:
        raise SystemExit(f"workers={args.workers} exceeds online CPU count={cpu_count}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "looper.cpu-pressure-preflight/v1alpha1",
        "target": _target_identifier(),
        "online_cpus": cpu_count,
        "workers": args.workers,
        "method": args.method,
        "duration_seconds": args.duration_seconds,
        "stress_ng": _tool_version(executable),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    (args.output_dir / "preflight.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _warmup(args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _run_once(
        stress_ng=args.stress_ng,
        workers=args.workers,
        method=args.method,
        duration_seconds=args.duration_seconds,
        output=args.output_dir / "warmup.yaml",
    )


def _measure(args: argparse.Namespace) -> None:
    if args.repeats < 2:
        raise SystemExit("repeats must be at least 2")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    values: list[float] = []
    for index in range(args.repeats):
        values.append(
            _run_once(
                stress_ng=args.stress_ng,
                workers=args.workers,
                method=args.method,
                duration_seconds=args.duration_seconds,
                output=args.output_dir / f"cpu-{started}-{index + 1}.yaml",
            )
        )
    batch = {
        "identity": {
            "target": _target_identifier(),
            "workload": f"stress-ng-cpu-{args.method}-workers{args.workers}",
            "phase": "steady-state-after-discarded-warmup",
            "tool": _tool_version(args.stress_ng),
            "statistics": (
                f"repeats={args.repeats};duration={args.duration_seconds}s;"
                f"workers={args.workers};method={args.method}"
            ),
        },
        "metrics": {
            "cpu.bogo-ops-per-second": {
                "metric_id": "cpu.bogo-ops-per-second",
                "values": values,
            },
            "cpu.success": {
                "metric_id": "cpu.success",
                "values": [1.0 for _ in values],
            },
        },
        "gate_values": {"cpu.success": True},
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
    values = payload["metrics"]["cpu.bogo-ops-per-second"]["values"]
    valid_values = all(
        math.isfinite(float(value)) and float(value) > 0 for value in values
    )
    if len(values) != args.repeats or not valid_values:
        raise SystemExit("measurement batch contains invalid CPU values")
    if payload.get("gate_values", {}).get("cpu.success") is not True:
        raise SystemExit("CPU success gate is not true")


def _cleanup(args: argparse.Namespace) -> None:
    marker = args.output_dir / "active-pressure.pid"
    if marker.exists():
        try:
            pid = int(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as error:
            raise SystemExit("active pressure PID marker is invalid") from error
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            marker.unlink(missing_ok=True)
        else:
            raise SystemExit(f"pressure process {pid} is still running")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "warmup", "measure", "verify", "cleanup"])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--stress-ng", default="stress-ng")
    args = parser.parse_args()
    if args.workers < 1 or args.duration_seconds < 1:
        raise SystemExit("workers and duration-seconds must be positive")
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
