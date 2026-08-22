from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from statistics import mean, median
from typing import Any

from looper_core.action_loop import (
    ActionMeasurement,
    ActionState,
    JsonFileAction,
    VerificationPolicy,
    execute_verified_action,
)
from looper_core.canonical import new_id

DEFAULT_COMPRESSION_STATE = {"compression_level": 6, "chunk_size": 16384}
ALLOWED_CHUNK_SIZES = {4096, 16384, 65536}


def validate_compression_state(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {"compression_level", "chunk_size"}:
        raise ValueError("compression action requires exactly compression_level and chunk_size")
    level = int(value["compression_level"])
    chunk_size = int(value["chunk_size"])
    if not 1 <= level <= 9:
        raise ValueError("compression_level must be between 1 and 9")
    if chunk_size not in ALLOWED_CHUNK_SIZES:
        raise ValueError(f"chunk_size must be one of {sorted(ALLOWED_CHUNK_SIZES)}")
    return {"compression_level": level, "chunk_size": chunk_size}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _metric_documents(path: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("benchmark metric line must be a JSON object")
            documents.append(value)
    return documents


def _python_environment(repository_root: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name
        in {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
        }
    }
    python_paths = [
        repository_root / "packages" / "core",
        repository_root / "packages" / "benchmark-sdk",
    ]
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in python_paths)
    environment["PYTHONUTF8"] = "1"
    return environment


def _compression_measurement_runner(
    repository_root: Path,
    evidence_root: Path,
    *,
    size_kib: int,
    samples: int,
    timeout_seconds: int,
):
    benchmark_root = repository_root / "benchmarks" / "demo"
    benchmark_script = benchmark_root / "compression_benchmark.py"
    if not benchmark_script.is_file():
        raise FileNotFoundError(f"compression benchmark not found: {benchmark_script}")

    def measure(state: ActionState, repeat_index: int, phase: str) -> ActionMeasurement:
        run_root = evidence_root / f"{phase}-{repeat_index:02d}"
        input_dir = run_root / "input"
        output_dir = run_root / "output"
        input_dir.mkdir(parents=True, exist_ok=False)
        output_dir.mkdir(parents=True, exist_ok=False)
        envelope = {
            "schemaVersion": "looper.run-envelope/v1alpha1",
            "candidate": {"parameters": state.value, "configDigest": state.digest},
            "workload": {
                "id": "verified-medium",
                "metadata": {"size_kib": size_kib, "samples": samples},
            },
            # Baseline and candidate use the same seed for each repeat index.
            "seed": 20260822 + repeat_index * 104729,
        }
        envelope_path = input_dir / "run-envelope.json"
        envelope_path.write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(benchmark_script),
                "--envelope",
                str(envelope_path),
                "--output",
                str(output_dir),
            ],
            cwd=benchmark_root,
            env=_python_environment(repository_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        (run_root / "process.stdout.log").write_text(completed.stdout, encoding="utf-8")
        (run_root / "process.stderr.log").write_text(completed.stderr, encoding="utf-8")
        result_path = output_dir / "result.json"
        metrics_path = output_dir / "metrics.jsonl"
        if completed.returncode != 0 or not result_path.is_file() or not metrics_path.is_file():
            raise RuntimeError(
                "compression benchmark failed "
                f"(exit={completed.returncode}, result={result_path.is_file()}, "
                f"metrics={metrics_path.is_file()})"
            )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        metrics = _metric_documents(metrics_path)
        throughput = [
            float(item["value"])
            for item in metrics
            if item.get("metric") == "throughput_mib_s" and item.get("phase") == "measurement"
        ]
        ratios = [
            float(item["value"])
            for item in metrics
            if item.get("metric") == "compression_ratio" and item.get("phase") == "measurement"
        ]
        roundtrip_values = [
            bool(item["value"]) for item in metrics if item.get("metric") == "roundtrip_ok"
        ]
        checks = result.get("checks", [])
        if len(throughput) < samples or not ratios or not roundtrip_values:
            raise RuntimeError("compression benchmark emitted incomplete measurement evidence")
        gates = {
            "process_exit": completed.returncode == 0,
            "result_status": result.get("status") == "succeeded",
            "roundtrip": all(roundtrip_values),
            "result_checks": bool(checks) and all(bool(item.get("passed")) for item in checks),
        }
        return ActionMeasurement(
            primary=median(throughput),
            secondary=mean(ratios),
            gates=gates,
            evidence={
                "phase": phase,
                "repeatIndex": repeat_index,
                "outputDirectory": str(output_dir),
                "resultDigest": _sha256(result_path),
                "metricsDigest": _sha256(metrics_path),
                "sampleCount": len(throughput),
            },
        )

    return measure


def run_verified_compression_loop(
    workspace_root: Path,
    *,
    candidate: Mapping[str, Any],
    policy: VerificationPolicy | None = None,
    size_kib: int = 512,
    samples: int = 12,
    timeout_seconds: int = 120,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Run the first durable Looper test -> action -> retest -> keep/rollback slice."""

    if size_kib < 128 or size_kib > 65536:
        raise ValueError("size_kib must be between 128 and 65536")
    if samples < 3 or samples > 10000:
        raise ValueError("samples must be between 3 and 10000")
    if timeout_seconds < 1 or timeout_seconds > 86400:
        raise ValueError("timeout_seconds must be between 1 and 86400")
    root = workspace_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    evidence_root = root / new_id("verified")
    evidence_root.mkdir(parents=False, exist_ok=False)
    state_file = root / "active-compression-config.json"
    action = JsonFileAction(
        "looper.demo.compression-config",
        state_file,
        validate_compression_state,
        DEFAULT_COMPRESSION_STATE,
    )
    selected_policy = policy or VerificationPolicy(
        repeats=3,
        minimum_improvement_ratio=0.05,
        maximum_secondary_regression_ratio=0.15,
        confidence_level=0.95,
        bootstrap_resamples=1000,
        random_seed=20260822,
    )
    resolved_repository = (
        repository_root.resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[3]
    )
    runner = _compression_measurement_runner(
        resolved_repository,
        evidence_root,
        size_kib=size_kib,
        samples=samples,
        timeout_seconds=timeout_seconds,
    )
    result = execute_verified_action(action, candidate, runner, selected_policy)
    result.update(
        {
            "workspaceRoot": str(root),
            "stateFile": str(state_file),
            "evidenceRoot": str(evidence_root),
            "policy": selected_policy.model_dump(mode="json"),
        }
    )
    decision_path = evidence_root / "verified-action.json"
    decision_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result["decisionFile"] = str(decision_path)
    return result
