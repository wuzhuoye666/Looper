from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import psutil
import pytest
from looper_worker.runner import (
    LocalAttemptRunner,
    RunnerError,
    build_container_command,
    validate_container_image,
)


class FailingHeartbeatClient:
    def __init__(self, process_file: Path) -> None:
        self.process_file = process_file
        self.pid: int | None = None

    def heartbeat(self, _attempt_id: str, _fencing_token: int) -> dict[str, object]:
        self.pid = int(json.loads(self.process_file.read_text(encoding="utf-8"))["pid"])
        raise httpx.ConnectError("control plane is offline")


def test_heartbeat_failure_terminates_benchmark_process(tmp_path: Path) -> None:
    logs = tmp_path / "attempt" / "logs"
    logs.mkdir(parents=True)
    client = FailingHeartbeatClient(logs.parent / "process.json")
    runner = LocalAttemptRunner(client, tmp_path / "worker")  # type: ignore[arg-type]

    result = runner._run_stage(
        "attempt-1",
        1,
        "run",
        {
            "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
            "timeoutSeconds": 5,
            "allowedExitCodes": [0],
        },
        {"workingDirectory": "."},
        {"benchmarkRoot": str(tmp_path)},
        logs,
        1024 * 1024,
    )

    assert result.status == "failed"
    assert result.message and "heartbeat failed" in result.message
    assert client.pid is not None
    assert not psutil.pid_exists(client.pid)
    assert not client.process_file.exists()


def _container_paths(tmp_path: Path) -> dict[str, Path]:
    paths = {
        name: tmp_path / name for name in ("input", "output", "workspace", "benchmarkRoot")
    }
    for path in paths.values():
        path.mkdir()
    return paths


def test_container_command_is_digest_pinned_and_least_privilege(tmp_path: Path) -> None:
    digest = "1" * 64
    argv, name = build_container_command(
        {
            "type": "container",
            "image": f"registry.example/looper/runtime@sha256:{digest}",
            "workingDirectory": ".",
            "commands": {},
        },
        {
            "argv": ["looper-run", "--envelope", "{envelope}", "--output", "{output}"],
            "timeoutSeconds": 30,
        },
        placeholders={
            "input": "/looper/input",
            "output": "/looper/output",
            "workspace": "/looper/workspace",
            "envelope": "/looper/input/run-envelope.json",
            "benchmarkRoot": "/looper/benchmark",
        },
        host_paths=_container_paths(tmp_path),
        attempt_id="attempt-1",
        fencing_token=2,
        stage="run",
    )

    assert name == "looper-attempt-1-run-2"
    assert argv[:5] == ["docker", "run", "--rm", "--init", "--pull"]
    assert argv[argv.index("--network") : argv.index("--network") + 2] == ["--network", "none"]
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--privileged" not in argv
    assert "--device" not in argv
    assert "--pull" in argv and argv[argv.index("--pull") + 1] == "never"
    assert argv[-6:] == [
        f"registry.example/looper/runtime@sha256:{digest}",
        "looper-run",
        "--envelope",
        "/looper/input/run-envelope.json",
        "--output",
        "/looper/output",
    ]


@pytest.mark.parametrize(
    "image",
    [
        None,
        "registry.example/looper/runtime:latest",
        "registry.example/looper/runtime@sha256:abc",
        "Registry.example/looper/runtime@sha256:" + "1" * 64,
        "registry.example/looper/runtime@sha512:" + "1" * 64,
    ],
)
def test_container_image_validation_rejects_mutable_or_malformed_references(
    image: object,
) -> None:
    with pytest.raises(RunnerError, match="pinned by sha256"):
        validate_container_image(image)


@pytest.mark.parametrize(
    ("runtime_update", "command_update", "message"),
    [
        ({"workingDirectory": "../escape"}, {}, "workingDirectory"),
        ({"networkMode": "host"}, {}, "networkMode"),
        ({}, {"environment": {"AWS_SECRET_ACCESS_KEY": "value"}}, "sensitive"),
        ({}, {"argv": ["{python}", "run.py"]}, "host.*python"),
    ],
)
def test_container_command_rejects_unsafe_runtime_options(
    tmp_path: Path,
    runtime_update: dict[str, object],
    command_update: dict[str, object],
    message: str,
) -> None:
    runtime = {
        "type": "container",
        "image": "registry.example/runtime@sha256:" + "2" * 64,
        "workingDirectory": ".",
        **runtime_update,
    }
    command = {"argv": ["run"], "timeoutSeconds": 30, **command_update}
    with pytest.raises(RunnerError, match=message):
        build_container_command(
            runtime,
            command,
            placeholders={
                "input": "/looper/input",
                "output": "/looper/output",
                "workspace": "/looper/workspace",
                "envelope": "/looper/input/run-envelope.json",
                "benchmarkRoot": "/looper/benchmark",
            },
            host_paths=_container_paths(tmp_path),
            attempt_id="attempt-1",
            fencing_token=1,
            stage="run",
        )
