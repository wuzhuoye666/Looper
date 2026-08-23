from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _target_identifier() -> str:
    try:
        raw = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        raw = "unavailable"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scheduler() -> str:
    raw = Path("/sys/block/nvme0n1/queue/scheduler").read_text(encoding="utf-8").strip()
    selected = re.search(r"\[([^]]+)]", raw)
    return selected.group(1) if selected else raw


def extract_metrics(payload: dict[str, Any]) -> tuple[float, float, bool]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("fio JSON contains no jobs")
    total_iops = 0.0
    p99_values_us: list[float] = []
    succeeded = True
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("fio job must be an object")
        read = job.get("read")
        if not isinstance(read, dict):
            raise ValueError("fio job has no read metrics")
        total_iops += float(read["iops"])
        clat = read.get("clat_ns")
        if not isinstance(clat, dict) or not isinstance(clat.get("percentile"), dict):
            raise ValueError("fio job has no completion-latency percentiles")
        p99_values_us.append(float(clat["percentile"]["99.000000"]) / 1000.0)
        succeeded = succeeded and int(job.get("error", 1)) == 0
        succeeded = succeeded and int(read.get("io_bytes", 0)) > 0
    return total_iops, max(p99_values_us), succeeded


def _fio_version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"], capture_output=True, check=True, text=True, timeout=10
    )
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--runtime-seconds", type=int, required=True)
    parser.add_argument("--ramp-seconds", type=int, required=True)
    parser.add_argument("--numjobs", type=int, required=True)
    parser.add_argument("--iodepth", type=int, required=True)
    parser.add_argument("--fio", default="fio")
    args = parser.parse_args()
    if args.repeats < 2:
        raise SystemExit("repeats must be at least 2")
    if not args.file.is_file():
        raise SystemExit(f"prepared fio file does not exist: {args.file}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    scheduler = _scheduler()
    started = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    iops_values: list[float] = []
    p99_values: list[float] = []
    gates: list[bool] = []
    for index in range(args.repeats):
        output = args.output_dir / f"fio-{scheduler}-{started}-{os.getpid()}-{index + 1}.json"
        command = [
            args.fio,
            "--name=looper-randread",
            f"--filename={args.file}",
            "--rw=randread",
            "--bs=4k",
            "--direct=1",
            "--ioengine=libaio",
            f"--iodepth={args.iodepth}",
            f"--numjobs={args.numjobs}",
            "--time_based=1",
            f"--runtime={args.runtime_seconds}",
            f"--ramp_time={args.ramp_seconds}",
            "--readonly",
            "--thread=1",
            "--randrepeat=1",
            "--allrandrepeat=1",
            "--randseed=20260823",
            "--eta=never",
            "--output-format=json",
            f"--output={output}",
        ]
        result = subprocess.run(command, capture_output=True, check=False, text=True, timeout=120)
        if result.returncode != 0:
            raise SystemExit(result.stderr or f"fio failed with exit code {result.returncode}")
        payload = json.loads(output.read_text(encoding="utf-8"))
        iops, p99_us, succeeded = extract_metrics(payload)
        iops_values.append(iops)
        p99_values.append(p99_us)
        gates.append(succeeded)

    batch = {
        "identity": {
            "target": _target_identifier(),
            "workload": f"fio-randread-4k-direct-numjobs{args.numjobs}-iodepth{args.iodepth}",
            "phase": "steady-state",
            "tool": _fio_version(args.fio),
            "statistics": (
                f"repeats={args.repeats};runtime={args.runtime_seconds}s;"
                f"ramp={args.ramp_seconds}s;randseed=20260823"
            ),
        },
        "metrics": {
            "fio.read-iops": {"metric_id": "fio.read-iops", "values": iops_values},
            "fio.read-clat-p99-us": {
                "metric_id": "fio.read-clat-p99-us",
                "values": p99_values,
            },
        },
        "gate_values": {"fio.success": all(gates)},
    }
    print(json.dumps(batch, separators=(",", ":")))


if __name__ == "__main__":
    main()
