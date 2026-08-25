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

_AVAILABLE_PATTERN = re.compile(r"available:\s+(?P<count>\d+)\s+nodes?\s+\((?P<nodes>[^)]*)\)")
_THROUGHPUT_PATTERN = re.compile(
    r"[0-9.]+ MiB transferred \((?P<rate>[0-9.]+) MiB/sec\)"
)


def _target_identifier() -> str:
    try:
        raw = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        raw = "unavailable"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_numa_nodes(output: str) -> list[int]:
    match = _AVAILABLE_PATTERN.search(output)
    if match is None:
        raise ValueError("numactl output contains no available-node declaration")
    count = int(match.group("count"))
    nodes: list[int] = []
    for token in match.group("nodes").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, end = (int(value) for value in token.split("-", 1))
            nodes.extend(range(start, end + 1))
        else:
            nodes.append(int(token))
    if len(nodes) != count or len(nodes) != len(set(nodes)):
        raise ValueError("numactl available-node count and node list disagree")
    return nodes


def _numa_hardware(numactl: str) -> tuple[str, list[int]]:
    result = subprocess.run(
        [numactl, "--hardware"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "numactl --hardware failed")
    return result.stdout, parse_numa_nodes(result.stdout)


def _write_capability(args: argparse.Namespace) -> tuple[dict[str, object], list[int]]:
    hardware, nodes = _numa_hardware(args.numactl)
    requested = {args.cpu_node, args.local_memory_node, args.remote_memory_node}
    available = len(nodes) >= 2 and requested <= set(nodes)
    reason = (
        "at least two declared NUMA nodes are available"
        if available
        else "requires at least two NUMA nodes and all explicitly selected nodes"
    )
    payload: dict[str, object] = {
        "schema_version": "looper.numa-pressure-capability/v1alpha1",
        "target": _target_identifier(),
        "available": available,
        "reason": reason,
        "nodes": nodes,
        "cpu_node": args.cpu_node,
        "local_memory_node": args.local_memory_node,
        "remote_memory_node": args.remote_memory_node,
        "hardware_output": hardware,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "capability.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return payload, nodes


def _probe(args: argparse.Namespace) -> None:
    payload, _ = _write_capability(args)
    print(json.dumps(payload, separators=(",", ":")))


def _prepare(args: argparse.Namespace) -> None:
    if os.name != "posix" or not Path("/sys/devices/system/node").is_dir():
        raise SystemExit("NUMA pressure probe requires Linux NUMA sysfs")
    for executable in (args.numactl, args.sysbench):
        if shutil.which(executable) is None:
            raise SystemExit(f"required executable is unavailable: {executable}")
    payload, _ = _write_capability(args)
    if payload["available"] is not True:
        raise SystemExit(str(payload["reason"]))


def _run_bandwidth(args: argparse.Namespace, memory_node: int, output: Path) -> float:
    command = [
        args.numactl,
        f"--cpunodebind={args.cpu_node}",
        f"--membind={memory_node}",
        args.sysbench,
        "memory",
        f"--threads={args.threads}",
        f"--memory-block-size={args.block_size}",
        f"--memory-total-size={args.total_size}",
        "--memory-scope=global",
        "--memory-oper=write",
        "--memory-access-mode=seq",
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
        raise RuntimeError(result.stderr or f"bound sysbench exited {result.returncode}")
    match = _THROUGHPUT_PATTERN.search(result.stdout)
    if match is None:
        raise ValueError("bound sysbench output contains no memory throughput")
    value = float(match.group("rate"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("bound sysbench throughput is not positive and finite")
    return value


def _warmup(args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _run_bandwidth(args, args.local_memory_node, args.output_dir / "warmup-local.txt")
    _run_bandwidth(args, args.remote_memory_node, args.output_dir / "warmup-remote.txt")


def _measure(args: argparse.Namespace) -> None:
    if args.repeats < 2:
        raise SystemExit("repeats must be at least 2")
    started = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    local: list[float] = []
    remote: list[float] = []
    for index in range(args.repeats):
        order = (
            [("local", args.local_memory_node), ("remote", args.remote_memory_node)]
            if index % 2 == 0
            else [("remote", args.remote_memory_node), ("local", args.local_memory_node)]
        )
        values: dict[str, float] = {}
        for label, node in order:
            values[label] = _run_bandwidth(
                args,
                node,
                args.output_dir / f"numa-{started}-{index + 1}-{label}.txt",
            )
        local.append(values["local"])
        remote.append(values["remote"])
    ratio = [
        remote_value / local_value
        for local_value, remote_value in zip(local, remote, strict=True)
    ]
    batch = {
        "identity": {
            "target": _target_identifier(),
            "workload": (
                f"numa-sysbench-write-cpu{args.cpu_node}-"
                f"local{args.local_memory_node}-remote{args.remote_memory_node}"
            ),
            "phase": "alternating-local-remote-after-discarded-warmup",
            "tool": "numactl+sysbench",
            "statistics": (
                f"repeats={args.repeats};duration={args.duration_seconds}s;"
                f"threads={args.threads};block={args.block_size}"
            ),
        },
        "metrics": {
            "numa.local-bandwidth-mib-per-second": {
                "metric_id": "numa.local-bandwidth-mib-per-second",
                "values": local,
            },
            "numa.remote-bandwidth-mib-per-second": {
                "metric_id": "numa.remote-bandwidth-mib-per-second",
                "values": remote,
            },
            "numa.remote-to-local-ratio": {
                "metric_id": "numa.remote-to-local-ratio",
                "values": ratio,
            },
            "numa.success": {
                "metric_id": "numa.success",
                "values": [1.0 for _ in local],
            },
        },
        "gate_values": {"numa.success": True},
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
    values = payload["metrics"]["numa.local-bandwidth-mib-per-second"]["values"]
    if len(values) != args.repeats or not all(
        math.isfinite(float(value)) and float(value) > 0 for value in values
    ):
        raise SystemExit("NUMA measurement sample count or values are invalid")
    if payload.get("gate_values", {}).get("numa.success") is not True:
        raise SystemExit("NUMA success gate is not true")


def _cleanup(args: argparse.Namespace) -> None:
    marker = args.output_dir / "active-pressure.pid"
    if marker.exists():
        raise SystemExit("NUMA pressure PID marker remains after synchronous execution")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["probe", "prepare", "warmup", "measure", "verify", "cleanup"]
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--cpu-node", type=int, required=True)
    parser.add_argument("--local-memory-node", type=int, required=True)
    parser.add_argument("--remote-memory-node", type=int, required=True)
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--block-size", required=True)
    parser.add_argument("--total-size", required=True)
    parser.add_argument("--numactl", default="numactl")
    parser.add_argument("--sysbench", default="sysbench")
    args = parser.parse_args()
    if min(
        args.cpu_node,
        args.local_memory_node,
        args.remote_memory_node,
    ) < 0:
        raise SystemExit("NUMA node ids cannot be negative")
    if args.local_memory_node == args.remote_memory_node:
        raise SystemExit("local and remote NUMA nodes must differ")
    if args.threads < 1 or args.duration_seconds < 1:
        raise SystemExit("threads and duration-seconds must be positive")
    actions = {
        "probe": _probe,
        "prepare": _prepare,
        "warmup": _warmup,
        "measure": _measure,
        "verify": _verify,
        "cleanup": _cleanup,
    }
    actions[args.action](args)


if __name__ == "__main__":
    main()
