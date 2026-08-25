from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

WORKLOADS = {"matmul", "7z", "lbm", "sad"}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def require_machine_gate(source_root: Path, workload: str) -> str:
    gate_path = source_root / "data" / "metadata" / "gate.env"
    if not gate_path.is_file():
        raise RuntimeError(f"the provisioned VGO machine gate is missing: {gate_path}")
    gate = read_env(gate_path)
    if gate.get("VGO_PARTIAL_GO") != "1":
        raise RuntimeError("the original VGO machine gate did not permit baseline execution")
    if workload == "sad" and gate.get("VGO_FULL_GO") != "1":
        raise RuntimeError(
            "SAD requires the original VGO full gate because this workload changes and "
            "restores THP state; choose a target with hardware perf and writable THP"
        )
    return "full" if gate.get("VGO_FULL_GO") == "1" else "partial"


def bounded_integer(
    parameters: dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = int(parameters.get(key, default))
    if value < minimum or value > maximum:
        raise ValueError(f"{key} must be within [{minimum}, {maximum}]")
    return value


def experiment_id(attempt_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", attempt_id).strip("-._")
    return f"looper_{normalized or 'attempt'}"


def copy_if_present(source: Path, destination: Path) -> None:
    if source.is_file():
        shutil.copy2(source, destination)


def completed_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one real VGO baseline workload")
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    workload = str(envelope["workload"]["id"])
    if workload not in WORKLOADS:
        raise ValueError(f"unsupported VGO workload: {workload}")
    parameters = envelope["candidate"].get("parameters") or {}
    samples = bounded_integer(parameters, "samples_per_attempt", 10, 3, 30)
    warmups = bounded_integer(parameters, "warmups", 1, 0, 5)
    per_run_timeout = bounded_integer(parameters, "per_run_timeout_seconds", 300, 60, 900)

    source_root_value = os.environ.get("LOOPER_VGO_ROOT", "").strip()
    if not source_root_value:
        raise RuntimeError("LOOPER_VGO_ROOT was not supplied by the managed runtime")
    source_root = Path(source_root_value).resolve()
    run_case = source_root / "scripts" / "run_case.sh"
    if not run_case.is_file():
        raise RuntimeError(f"the provisioned VGO run_case.sh is missing: {run_case}")
    machine_gate = require_machine_gate(source_root, workload)

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = experiment_id(str(envelope["attemptId"]))
    command = [
        "bash",
        str(run_case),
        "--benchmark",
        workload,
        "--phase",
        "baseline",
        "--repetitions",
        str(samples),
        "--warmups",
        str(warmups),
        "--timeout",
        str(per_run_timeout),
        "--inter-run-delay",
        "0",
        "--experiment-id",
        run_id,
    ]
    raw_dir = source_root / "data" / "raw" / run_id / workload
    log_dir = source_root / "logs" / run_id / workload
    process_return_code = 1
    process_stdout = ""
    process_stderr = ""
    try:
        completed = subprocess.run(
            command,
            cwd=source_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=(samples + warmups) * per_run_timeout + 300,
        )
        process_return_code = completed.returncode
        process_stdout = completed.stdout
        process_stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        process_return_code = 124
        process_stdout = completed_output(error.stdout)
        process_stderr = completed_output(error.stderr) + "\nLooper VGO adapter timed out.\n"
    finally:
        copy_if_present(raw_dir / "baseline.csv", output / "vgo-raw.csv")
        copy_if_present(raw_dir / "baseline.metadata.json", output / "vgo-metadata.json")
        copy_if_present(raw_dir / "baseline.metadata.md", output / "vgo-metadata.md")
        original_log = log_dir / "baseline.log"
        original_log_text = (
            original_log.read_text(encoding="utf-8", errors="replace")
            if original_log.is_file()
            else ""
        )
        (output / "vgo-run.log").write_text(
            "$ "
            + " ".join(command)
            + "\n\n"
            + "--- stdout ---\n"
            + process_stdout
            + "\n--- stderr ---\n"
            + process_stderr
            + "\n--- original VGO phase log ---\n"
            + original_log_text,
            encoding="utf-8",
        )
        native = {
            "schemaVersion": "looper.vgo-native/v1",
            "attemptId": envelope["attemptId"],
            "experimentId": envelope["experimentId"],
            "workload": workload,
            "vgoExperimentId": run_id,
            "phase": "baseline",
            "samplesRequested": samples,
            "warmups": warmups,
            "perRunTimeoutSeconds": per_run_timeout,
            "command": command,
            "exitCode": process_return_code,
            "sourceDigest": os.environ.get("LOOPER_VGO_SOURCE_DIGEST"),
            "machineGate": machine_gate,
            "originalEntryPoint": "scripts/run_case.sh",
        }
        (output / "vgo-native.json").write_text(
            json.dumps(native, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if raw_dir.parent.exists():
            shutil.rmtree(raw_dir.parent)
        if log_dir.parent.exists():
            shutil.rmtree(log_dir.parent)

    if process_return_code != 0:
        raise SystemExit(process_return_code)
    for required in ("vgo-raw.csv", "vgo-metadata.json"):
        if not (output / required).is_file():
            raise RuntimeError(f"the original VGO script did not produce {required}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
