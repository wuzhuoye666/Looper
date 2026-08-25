from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

WORKLOADS = {"matmul", "7z", "lbm", "sad"}
FORMAL_COUNTS = {
    "matmul": {"comparison": 300, "profile": 200, "rollback": 50},
    "7z": {"comparison": 500, "profile": 200, "rollback": 50},
    "lbm": {"comparison": 500, "profile": 200, "rollback": 50},
    "sad": {"comparison": 500, "profile": 200, "rollback": 50},
}
MITIGATIONS = {
    "matmul": {"id": "tcmalloc", "factor": "dTLB/page allocation", "mechanism": "LD_PRELOAD"},
    "7z": {
        "id": "thread_pinning",
        "factor": "context switches/CPU migration",
        "mechanism": "taskset",
    },
    "lbm": {
        "id": "thread_pinning",
        "factor": "placement/context switches",
        "mechanism": "OpenMP CPU binding",
    },
    "sad": {
        "id": "transparent_huge_pages",
        "factor": "dTLB/page faults",
        "mechanism": "THP madvise/always",
    },
}


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
        raise RuntimeError("the original VGO machine gate did not permit diagnosis execution")
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


def completed_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def scaled_plan(workload: str, scale_percent: int, blocks: int) -> dict[str, int]:
    formal = FORMAL_COUNTS[workload]
    comparison = max(blocks, math.ceil(formal["comparison"] * scale_percent / 100))
    comparison = math.ceil(comparison / blocks) * blocks
    return {
        "baseline": comparison,
        "profile": max(3, math.ceil(formal["profile"] * scale_percent / 100)),
        "mitigated": comparison,
        "rollback": max(3, math.ceil(formal["rollback"] * scale_percent / 100)),
        "blocks": blocks,
        "perConditionPerBlock": comparison // blocks,
    }


def balanced_orders(workload: str, blocks: int, seed: int) -> list[str]:
    orders = ["baseline-first"] * ((blocks + 1) // 2) + ["mitigated-first"] * (blocks // 2)
    stable_seed = int.from_bytes(hashlib.sha256(workload.encode()).digest()[:4], "big")
    random.Random(seed + stable_seed).shuffle(orders)
    return orders


def vgo_command(
    run_case: Path,
    workload: str,
    phase: str,
    repetitions: int,
    warmups: int,
    timeout: int,
    delay_ms: int,
    run_id: str,
    *,
    condition: str | None = None,
    resume: bool = False,
) -> list[str]:
    command = [
        "bash",
        str(run_case),
        "--benchmark",
        workload,
        "--phase",
        phase,
        "--repetitions",
        str(repetitions),
        "--warmups",
        str(warmups),
        "--timeout",
        str(timeout),
        "--inter-run-delay",
        f"{delay_ms / 1000:.3f}",
        "--experiment-id",
        run_id,
    ]
    if condition:
        command.extend(["--condition", condition])
    if resume:
        command.append("--resume")
    return command


def phase_file(raw_dir: Path, phase: str, condition: str | None = None) -> Path:
    name = f"{phase}_{condition}" if phase in {"pilot", "blocked"} else phase
    return raw_dir / f"{name}.csv"


def combine_csv(files: list[Path], destination: Path) -> int:
    fieldnames: list[str] | None = None
    rows: list[dict[str, str]] = []
    for path in files:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None:
                raise RuntimeError(f"VGO CSV has no header: {path}")
            if fieldnames is None:
                fieldnames = reader.fieldnames
            elif reader.fieldnames != fieldnames:
                raise RuntimeError(f"VGO CSV schemas differ: {path}")
            rows.extend(reader)
    if not fieldnames:
        raise RuntimeError("VGO produced no CSV evidence")
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a minimal complete VGO variability diagnosis")
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    workload = str(envelope["workload"]["id"])
    if workload not in WORKLOADS:
        raise ValueError(f"unsupported VGO workload: {workload}")
    parameters = envelope["candidate"].get("parameters") or {}
    scale_percent = bounded_integer(parameters, "diagnostic_scale_percent", 10, 1, 25)
    blocks = bounded_integer(parameters, "ab_blocks", 5, 2, 10)
    warmups = bounded_integer(parameters, "warmups", 1, 0, 5)
    per_run_timeout = bounded_integer(parameters, "per_run_timeout_seconds", 300, 60, 900)
    delay_ms = bounded_integer(parameters, "inter_run_delay_milliseconds", 0, 0, 5000)
    order_seed = bounded_integer(parameters, "order_seed", 2026, 0, 2147483647)
    plan = scaled_plan(workload, scale_percent, blocks)
    orders = balanced_orders(workload, blocks, order_seed)

    source_root_value = os.environ.get("LOOPER_VGO_ROOT", "").strip()
    if not source_root_value:
        raise RuntimeError("LOOPER_VGO_ROOT was not supplied by the managed runtime")
    source_root = Path(source_root_value).resolve()
    run_case = source_root / "scripts" / "run_case.sh"
    if not run_case.is_file():
        raise RuntimeError(f"the provisioned VGO run_case.sh is missing: {run_case}")
    machine_gate = require_machine_gate(source_root, workload)
    run_environment = os.environ.copy()
    if machine_gate == "partial":
        software_events = "context-switches,cpu-migrations,page-faults"
        run_environment["VGO_PERF_EVENTS"] = software_events
        run_environment["VGO_PERF_EVENTS_PROBE_OVERRIDE"] = software_events

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = experiment_id(str(envelope["attemptId"]))
    raw_dir = source_root / "data" / "raw" / run_id / workload
    log_dir = source_root / "logs" / run_id / workload
    commands: list[dict[str, Any]] = []
    process_return_code = 0
    adapter_log: list[str] = []

    def execute(label: str, command: list[str], expected_new_runs: int) -> bool:
        nonlocal process_return_code
        commands.append({"label": label, "argv": command, "expectedNewRuns": expected_new_runs})
        adapter_log.append("$ " + " ".join(command))
        try:
            completed = subprocess.run(
                command,
                cwd=source_root,
                env=run_environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=(expected_new_runs + warmups) * per_run_timeout + 300,
            )
            process_return_code = completed.returncode
            adapter_log.extend(
                ["--- stdout ---", completed.stdout, "--- stderr ---", completed.stderr]
            )
        except subprocess.TimeoutExpired as error:
            process_return_code = 124
            adapter_log.extend(
                [
                    "--- stdout ---",
                    completed_output(error.stdout),
                    "--- stderr ---",
                    completed_output(error.stderr),
                    "Looper VGO adapter timed out.",
                ]
            )
        return process_return_code == 0

    try:
        profile = vgo_command(
            run_case,
            workload,
            "profile",
            plan["profile"],
            warmups,
            per_run_timeout,
            delay_ms,
            run_id,
        )
        if execute("profile", profile, plan["profile"]):
            for block_index, order in enumerate(orders, start=1):
                target = block_index * plan["perConditionPerBlock"]
                conditions = (
                    ["baseline", "mitigated"]
                    if order == "baseline-first"
                    else ["mitigated", "baseline"]
                )
                for position, condition in enumerate(conditions, start=1):
                    command = vgo_command(
                        run_case,
                        workload,
                        "blocked",
                        target,
                        0,
                        per_run_timeout,
                        delay_ms,
                        run_id,
                        condition=condition,
                        resume=block_index > 1,
                    )
                    if not execute(
                        f"block-{block_index}-{position}-{condition}",
                        command,
                        plan["perConditionPerBlock"],
                    ):
                        break
                if process_return_code != 0:
                    break
        if process_return_code == 0:
            rollback = vgo_command(
                run_case,
                workload,
                "rollback",
                plan["rollback"],
                warmups,
                per_run_timeout,
                delay_ms,
                run_id,
            )
            execute("rollback", rollback, plan["rollback"])
    finally:
        evidence_files = [
            phase_file(raw_dir, "profile"),
            phase_file(raw_dir, "blocked", "baseline"),
            phase_file(raw_dir, "blocked", "mitigated"),
            phase_file(raw_dir, "rollback"),
        ]
        present_files = [path for path in evidence_files if path.is_file()]
        combined_rows = combine_csv(present_files, output / "vgo-raw.csv") if present_files else 0
        metadata_documents: dict[str, Any] = {}
        for csv_path in present_files:
            metadata_path = csv_path.with_suffix(".metadata.json")
            if metadata_path.is_file():
                metadata_documents[csv_path.stem] = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
        (output / "vgo-metadata.json").write_text(
            json.dumps(metadata_documents, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        original_logs: list[str] = []
        if log_dir.is_dir():
            for path in sorted(log_dir.glob("*.log")):
                original_logs.extend(
                    [
                        f"\n===== original {path.name} =====\n",
                        path.read_text(encoding="utf-8", errors="replace"),
                    ]
                )
        (output / "vgo-run.log").write_text(
            "\n".join(adapter_log + original_logs), encoding="utf-8"
        )
        native = {
            "schemaVersion": "looper.vgo-native/v2",
            "attemptId": envelope["attemptId"],
            "experimentId": envelope["experimentId"],
            "workload": workload,
            "vgoExperimentId": run_id,
            "mode": "minimal-complete-diagnosis",
            "scalePercent": scale_percent,
            "parameters": {
                "diagnosticScalePercent": scale_percent,
                "abBlocks": blocks,
                "warmups": warmups,
                "perRunTimeoutSeconds": per_run_timeout,
                "interRunDelayMilliseconds": delay_ms,
                "orderSeed": order_seed,
            },
            "formalReferenceCounts": FORMAL_COUNTS[workload],
            "requestedCounts": plan,
            "alternatingOrder": [
                {"block": index, "order": order, "runsPerCondition": plan["perConditionPerBlock"]}
                for index, order in enumerate(orders, start=1)
            ],
            "mitigation": MITIGATIONS[workload],
            "commands": commands,
            "combinedRows": combined_rows,
            "exitCode": process_return_code,
            "sourceDigest": os.environ.get("LOOPER_VGO_SOURCE_DIGEST"),
            "machineGate": machine_gate,
            "profileCapability": (
                "hardware-and-software-events" if machine_gate == "full" else "software-events-only"
            ),
            "originalEntryPoint": "scripts/run_case.sh",
        }
        (output / "vgo-native.json").write_text(
            json.dumps(native, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if raw_dir.parent.exists():
            shutil.rmtree(raw_dir.parent)
        if log_dir.parent.exists():
            shutil.rmtree(log_dir.parent)

    if process_return_code != 0:
        raise SystemExit(process_return_code)
    for required in ("vgo-raw.csv", "vgo-metadata.json"):
        if not (output / required).is_file():
            raise RuntimeError(f"the original VGO scripts did not produce {required}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
