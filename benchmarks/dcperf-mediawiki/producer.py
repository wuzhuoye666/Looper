#!/usr/bin/env python3
"""Run the real DCPerf MediaWiki MLP job and preserve native evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

JOB_NAME = "oss_performance_mediawiki_mlp"
HHVM_BIN = "/usr/local/hphpi/legacy/bin/hhvm"
HHVM_LIB = "/opt/local/hhvm-3.30/lib"
SOURCE_REVISION = "9308c3e3c404e0466f0a2929f15ddcf62b2215f6"


class ProducerError(RuntimeError):
    pass


def log(message: str) -> None:
    print("[dcperf-producer] " + message, flush=True)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProducerError(f"expected JSON object: {path}")
    return value


def parameters(envelope: dict[str, Any]) -> dict[str, Any]:
    candidate = envelope.get("candidate")
    if isinstance(candidate, dict) and isinstance(candidate.get("parameters"), dict):
        return dict(candidate["parameters"])
    if isinstance(envelope.get("parameters"), dict):
        return dict(envelope["parameters"])
    return {}


def positive_int(value: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as error:
        raise ProducerError(f"{name} must be an integer") from error
    if parsed < minimum or parsed > maximum:
        raise ProducerError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ProducerError("profile must be boolean")


def read_cpu_sample() -> tuple[int, int] | None:
    try:
        line = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0]
        fields = line.split()
        if fields[0] != "cpu" or len(fields) < 5:
            return None
        total = sum(int(value) for value in fields[1:])
        idle = int(fields[4]) + (int(fields[5]) if len(fields) > 5 else 0)
        return total, idle
    except (OSError, ValueError, IndexError):
        return None


def cpu_monitor(stop: threading.Event, samples: list[float]) -> None:
    previous = read_cpu_sample()
    while not stop.wait(1.0):
        current = read_cpu_sample()
        if previous is not None and current is not None:
            total_delta = current[0] - previous[0]
            idle_delta = current[1] - previous[1]
            if total_delta > 0:
                samples.append(max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta))))
        previous = current


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return float(ordered[index])


def run_process(
    command: list[str], cwd: Path, output: Path, env: dict[str, str], timeout: int
) -> int:
    stdout_path = output / "benchpress.stdout.log"
    stderr_path = output / "benchpress.stderr.log"
    log("running pinned Benchpress job")
    with (
        stdout_path.open("w", encoding="utf-8", newline="\n") as stdout,
        stderr_path.open("w", encoding="utf-8", newline="\n") as stderr,
    ):
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"native job exceeded {timeout} seconds; terminating its process group")
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, AttributeError):
                process.terminate()
            try:
                return process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, AttributeError):
                    process.kill()
                return process.wait(timeout=20)


def copy_regular_files(source: Path, destination: Path) -> list[str]:
    copied: list[str] = []
    if not source.is_dir():
        return copied
    for item in source.rglob("*"):
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.append(relative.as_posix())
    return copied


def find_native_json(directory: Path, marker: str) -> list[Path]:
    return sorted(
        [path for path in directory.rglob("*.json") if path.is_file() and marker in path.name],
        key=lambda path: path.stat().st_mtime_ns,
    )


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    native = output / "dcperf-native"
    if native.exists():
        shutil.rmtree(native)
    native.mkdir(parents=True)
    try:
        envelope = load_json(arguments.envelope)
        params = parameters(envelope)
        duration = positive_int(params.get("duration_seconds"), "duration_seconds", 600, 45, 3600)
        timeout = positive_int(
            params.get("timeout_seconds"), "timeout_seconds", duration + 60, duration + 1, 7200
        )
        scale_out = positive_int(params.get("scale_out"), "scale_out", 1, 1, 8)
        logical_cpus = os.cpu_count() or 1
        client_threads = positive_int(
            params.get("client_threads"),
            "client_threads",
            2 * logical_cpus,
            1,
            4096,
        )
        profile = bool_value(params.get("profile"), True)
        attempt_id = str(envelope.get("attemptId") or envelope.get("id") or "dcperf-run")
        run_id = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:8]
        timestamp = int(time.time())
        dcperf_root = arguments.cache.resolve() / "runtime" / "dcperf"
        cli = dcperf_root / "benchpress_cli.py"
        wrk = dcperf_root / "benchmarks/oss_performance_mediawiki/wrk/wrk"
        marker = arguments.cache.resolve() / "dcperf-mediawiki-ready.json"
        if not marker.is_file() or not cli.is_file() or not wrk.is_file():
            raise ProducerError("managed DCPerf cache is incomplete; run prepare first")
        if not Path(HHVM_BIN).is_file():
            raise ProducerError(f"HHVM executable is missing: {HHVM_BIN}")

        override = (
            f"{JOB_NAME}: "
            f"-r{HHVM_BIN} -nnginx -L wrk -s {shlex_join([str(wrk)])} "
            f"-R{scale_out} -c{client_threads} -- --mediawiki-mlp "
            f"--client-duration={duration}s --client-timeout={timeout}s "
            "--run-as-root --i-am-not-benchmarking"
        )
        native_python = (
            Path("/usr/bin/python3") if Path("/usr/bin/python3").is_file() else Path(sys.executable)
        )
        command = [
            str(native_python),
            str(cli),
            "-u",
            run_id,
            "-t",
            str(timestamp),
            "-o",
            override,
            "-r",
            str(native / "history"),
            "run",
            JOB_NAME,
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(dcperf_root) + os.pathsep + env.get("PYTHONPATH", "")
        env["HOME"] = env.get("HOME", "/root")
        env["DCPERF_PERF_RECORD"] = "1" if profile else "0"
        env["LD_LIBRARY_PATH"] = HHVM_LIB + ":" + env.get("LD_LIBRARY_PATH", "")
        samples: list[float] = []
        stop = threading.Event()
        monitor = threading.Thread(target=cpu_monitor, args=(stop, samples), daemon=True)
        monitor.start()
        started = time.monotonic()
        return_code = run_process(command, dcperf_root, native, env, timeout + 300)
        stop.set()
        monitor.join(timeout=2)
        elapsed = time.monotonic() - started

        metric_files = find_native_json(native, "_metrics_")
        system_files = find_native_json(native, "_system_specs_")
        if metric_files:
            shutil.copy2(metric_files[-1], output / "benchpress-result.json")
        if system_files:
            shutil.copy2(system_files[-1], output / "native-system-specs.json")
        profile_data = dcperf_root / "oss-performance" / "perf.data"
        if profile_data.is_file():
            shutil.copy2(profile_data, output / "perf.data")
        profile_log = Path("/tmp/mw-perf-record.log")
        if profile_log.is_file():
            shutil.copy2(profile_log, output / "perf-record.log")
        stdout_text = (native / "benchpress.stdout.log").read_text(
            encoding="utf-8", errors="replace"
        )
        stderr_text = (native / "benchpress.stderr.log").read_text(
            encoding="utf-8", errors="replace"
        )
        (output / "benchmark.log").write_text(
            "=== command ===\n"
            + " ".join(command)
            + "\n=== stdout ===\n"
            + stdout_text
            + "\n=== stderr ===\n"
            + stderr_text,
            encoding="utf-8",
        )
        status = {
            "schemaVersion": "looper.dcperf.native-run/v1",
            "status": "succeeded" if return_code == 0 and metric_files else "failed",
            "exitCode": return_code,
            "elapsedSeconds": round(elapsed, 3),
            "sourceRevision": SOURCE_REVISION,
            "job": JOB_NAME,
            "parameters": {
                "duration_seconds": duration,
                "timeout_seconds": timeout,
                "scale_out": scale_out,
                "client_threads": client_threads,
                "profile": profile,
            },
            "cpuSamples": len(samples),
            "cpuUtilizationP95": percentile(samples, 0.95),
            "nativeMetricFiles": [path.name for path in metric_files],
            "nativeSystemFiles": [path.name for path in system_files],
            "profileProduced": (output / "perf.data").is_file(),
        }
        write_json(output / "native-run.json", status)
        if status["cpuUtilizationP95"] is None:
            log("CPU monitor unavailable; normalizer will fail the resource gate")
        if return_code != 0 or not metric_files:
            raise ProducerError(
                "native Benchpress job failed "
                f"(exit={return_code}, metricFiles={len(metric_files)})"
            )
        native_result = load_json(output / "benchpress-result.json")
        monitor_values: dict[str, Any] = {
            "timeouts": 0,
            "cpu_utilization_p95": status["cpuUtilizationP95"],
        }
        metrics = native_result.get("metrics")
        combined = metrics.get("Combined", {}) if isinstance(metrics, dict) else {}
        if isinstance(combined, dict):
            try:
                monitor_values["timeouts"] = int(combined.get("Nginx 499", 0))
            except (TypeError, ValueError):
                monitor_values["timeouts"] = 0
        native_result["looper_monitor"] = monitor_values
        native_result["looper_provenance"] = {
            "source_revision": SOURCE_REVISION,
            "package_job": JOB_NAME,
            "native_file": "benchpress-result.json",
        }
        write_json(output / "native-result-enriched.json", native_result)
        return 0
    except (OSError, ValueError, KeyError, ProducerError, subprocess.SubprocessError) as error:
        write_json(
            output / "native-run.json",
            {
                "schemaVersion": "looper.dcperf.native-run/v1",
                "status": "failed",
                "error": str(error),
            },
        )
        print(f"[dcperf-producer] ERROR: {error}", file=sys.stderr, flush=True)
        return 2


def shlex_join(parts: list[str]) -> str:
    result: list[str] = []
    for part in parts:
        if not part or any(char.isspace() or char in "'\"" for char in part):
            result.append("'" + part.replace("'", "'\"'\"'") + "'")
        else:
            result.append(part)
    return " ".join(result)


if __name__ == "__main__":
    raise SystemExit(main())
