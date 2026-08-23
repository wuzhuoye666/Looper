from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _target_identifier() -> str:
    try:
        raw = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        raw = "unavailable"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    )
    return (result.stdout or result.stderr).splitlines()[0].strip()


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.strip().lower() == "localhost"


def extract_metrics(payload: dict[str, Any]) -> tuple[float, float]:
    if payload.get("error"):
        raise ValueError(f"iperf3 reported an error: {payload['error']}")
    end = payload.get("end")
    if not isinstance(end, dict):
        raise ValueError("iperf3 JSON contains no end object")
    received = end.get("sum_received")
    sent = end.get("sum_sent")
    if not isinstance(received, dict) or not isinstance(sent, dict):
        raise ValueError("iperf3 JSON contains no aggregate send/receive metrics")
    gbps = float(received["bits_per_second"]) / 1_000_000_000
    retransmits = float(sent.get("retransmits", 0))
    if not all(math.isfinite(value) and value >= 0 for value in (gbps, retransmits)):
        raise ValueError("iperf3 metrics are not finite and non-negative")
    if gbps <= 0:
        raise ValueError("iperf3 receive throughput must be positive")
    return gbps, retransmits


def _run_once(
    args: argparse.Namespace, output: Path
) -> tuple[float, float]:
    server: subprocess.Popen[str] | None = None
    server_stdout = None
    server_stderr = None
    try:
        if args.start_local_server:
            server_stdout = output.with_suffix(".server.stdout.log").open("w", encoding="utf-8")
            server_stderr = output.with_suffix(".server.stderr.log").open("w", encoding="utf-8")
            server = subprocess.Popen(
                [args.iperf3, "-s", "-1", "-p", str(args.port)],
                stdout=server_stdout,
                stderr=server_stderr,
                text=True,
            )
            time.sleep(args.server_startup_seconds)
            if server.poll() is not None:
                raise RuntimeError("local iperf3 server exited before the client started")
        command = [
            args.iperf3,
            "-c",
            args.host,
            "-p",
            str(args.port),
            "-J",
            "-t",
            str(args.duration_seconds),
            "-O",
            str(args.omit_seconds),
            "-P",
            str(args.parallel_streams),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=args.duration_seconds + args.omit_seconds + 30,
        )
        output.write_text(result.stdout, encoding="utf-8")
        output.with_suffix(".client.stderr.log").write_text(result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(result.stderr or f"iperf3 client exited {result.returncode}")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("iperf3 JSON root is not an object")
        return extract_metrics(payload)
    finally:
        if server is not None:
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
        if server_stdout is not None:
            server_stdout.close()
        if server_stderr is not None:
            server_stderr.close()


def _prepare(args: argparse.Namespace) -> None:
    if os.name != "posix" or not Path("/proc/net").is_dir():
        raise SystemExit("network pressure probe requires Linux")
    executable = shutil.which(args.iperf3)
    if executable is None:
        raise SystemExit(f"iperf3 is unavailable: {args.iperf3}")
    loopback = _is_loopback(args.host)
    if loopback and not args.allow_loopback:
        raise SystemExit("loopback requires explicit --allow-loopback")
    if args.start_local_server and not loopback:
        raise SystemExit("local server mode is only valid with a loopback host")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "looper.network-pressure-preflight/v1alpha1",
        "target": _target_identifier(),
        "host": args.host,
        "port": args.port,
        "loopback": loopback,
        "start_local_server": args.start_local_server,
        "duration_seconds": args.duration_seconds,
        "omit_seconds": args.omit_seconds,
        "parallel_streams": args.parallel_streams,
        "server_startup_seconds": args.server_startup_seconds,
        "iperf3": _version(executable),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    (args.output_dir / "preflight.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _warmup(args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _run_once(args, args.output_dir / "warmup.json")


def _measure(args: argparse.Namespace) -> None:
    if args.repeats < 2:
        raise SystemExit("repeats must be at least 2")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    throughput: list[float] = []
    retransmits: list[float] = []
    for index in range(args.repeats):
        gbps, retry_count = _run_once(
            args,
            args.output_dir / f"network-{started}-{index + 1}.json",
        )
        throughput.append(gbps)
        retransmits.append(retry_count)
    scope = "loopback-protocol-stack" if _is_loopback(args.host) else "declared-remote-peer"
    batch = {
        "identity": {
            "target": _target_identifier(),
            "workload": (
                f"iperf3-tcp-{scope}-streams{args.parallel_streams}-port{args.port}"
            ),
            "phase": "steady-state-after-iperf-omit-and-discarded-warmup",
            "tool": _version(args.iperf3),
            "statistics": (
                f"repeats={args.repeats};duration={args.duration_seconds}s;"
                f"omit={args.omit_seconds}s;startup={args.server_startup_seconds}s"
            ),
        },
        "metrics": {
            "network.receive-throughput-gbps": {
                "metric_id": "network.receive-throughput-gbps",
                "values": throughput,
            },
            "network.retransmits": {
                "metric_id": "network.retransmits",
                "values": retransmits,
            },
            "network.success": {
                "metric_id": "network.success",
                "values": [1.0 for _ in throughput],
            },
        },
        "gate_values": {"network.success": True},
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
    values = payload["metrics"]["network.receive-throughput-gbps"]["values"]
    if len(values) != args.repeats or not all(
        math.isfinite(float(value)) and value > 0 for value in values
    ):
        raise SystemExit("measurement batch contains invalid network throughput")
    if payload.get("gate_values", {}).get("network.success") is not True:
        raise SystemExit("network success gate is not true")


def _cleanup(args: argparse.Namespace) -> None:
    marker = args.output_dir / "active-pressure.pid"
    if marker.exists():
        raise SystemExit("network pressure PID marker remains after synchronous execution")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "warmup", "measure", "verify", "cleanup"])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--omit-seconds", type=int, required=True)
    parser.add_argument("--parallel-streams", type=int, required=True)
    parser.add_argument("--server-startup-seconds", type=float, required=True)
    parser.add_argument("--start-local-server", action="store_true")
    parser.add_argument("--allow-loopback", action="store_true")
    parser.add_argument("--iperf3", default="iperf3")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be in 1..65535")
    if min(
        args.duration_seconds,
        args.parallel_streams,
        args.server_startup_seconds,
    ) <= 0 or args.omit_seconds < 0:
        raise SystemExit("duration, streams, and startup must be positive; omit cannot be negative")
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
