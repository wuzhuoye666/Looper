"""Run the pinned PTS PHPBench profile and preserve its native JSON result."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from looper_benchmark_sdk import load_envelope

PINNED_PROFILE = "pts/phpbench-1.1.6"
PINNED_PTS_VERSION = "10.8.6"
RESULT_NAME = "looper-phpbench"
RESULT_IDENTIFIER = "looper-candidate"
MAX_TIMEOUT_MINUTES = 30
INSTALL_EXPORT_GRACE_SECONDS = 300


class PhoronixError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int


def _resolve_executable(name: str, explicit: str | None) -> str | None:
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise PhoronixError(f"configured executable is missing: {candidate}")
        return str(candidate.resolve())
    return shutil.which(name)


def resolve_pts_command() -> list[str]:
    """Resolve a shell-free PTS argv prefix.

    A source checkout can be executed with LOOPER_PHP_BIN=/usr/bin/php and
    LOOPER_PTS_BIN=/path/to/phoronix-test-suite. A system installation only
    needs the launcher on PATH.
    """

    pts_bin = _resolve_executable(
        "phoronix-test-suite", os.environ.get("LOOPER_PTS_BIN")
    )
    if pts_bin is None:
        raise PhoronixError(
            "phoronix-test-suite was not found; install the pinned PTS build or set "
            "LOOPER_PTS_BIN"
        )
    php_explicit = os.environ.get("LOOPER_PHP_BIN")
    if php_explicit:
        php_bin = _resolve_executable("php", php_explicit)
        assert php_bin is not None
        pts_path = Path(pts_bin)
        if pts_path.suffix.casefold() == ".php":
            core_entrypoint = pts_path
        else:
            core_entrypoint = pts_path.parent / "pts-core" / "phoronix-test-suite.php"
        if not core_entrypoint.is_file():
            raise PhoronixError(
                "configured PTS source checkout is missing pts-core/phoronix-test-suite.php"
            )
        return [php_bin, str(core_entrypoint.resolve())]
    return [pts_bin]


def read_run_contract(envelope: dict[str, Any]) -> tuple[str, int, int, str]:
    workload = envelope.get("workload", {})
    workload_id = str(workload.get("id", ""))
    profile = str(workload.get("metadata", {}).get("profile", ""))
    if workload_id != "phpbench" or profile != PINNED_PROFILE:
        raise PhoronixError(
            f"workload is not the pinned PHPBench contract: id={workload_id!r}, "
            f"profile={profile!r}"
        )
    parameters = envelope.get("candidate", {}).get("parameters", {})
    times_to_run = int(parameters.get("times_to_run", 3))
    timeout_minutes = int(parameters.get("test_timeout_minutes", 10))
    if not 1 <= times_to_run <= 10:
        raise PhoronixError("times_to_run must be between 1 and 10")
    if not 2 <= timeout_minutes <= MAX_TIMEOUT_MINUTES:
        raise PhoronixError("test_timeout_minutes must be between 2 and 30")
    return profile, times_to_run, timeout_minutes, workload_id


def _subprocess_environment(
    pts_user_path: Path, times_to_run: int, timeout_minutes: int
) -> dict[str, str]:
    passthrough = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in passthrough
    }
    environment.update(
        {
            "PTS_USER_PATH_OVERRIDE": str(pts_user_path),
            "PTS_SILENT_MODE": "1",
            "PHP_BIN": os.environ.get("LOOPER_PHP_BIN")
            or shutil.which("php")
            or "php",
            "TEST_RESULTS_NAME": RESULT_NAME,
            "TEST_RESULTS_IDENTIFIER": RESULT_IDENTIFIER,
            "TEST_RESULTS_DESCRIPTION": "Looper PTS PHPBench adapter run",
            "FORCE_TIMES_TO_RUN": str(times_to_run),
            "TEST_TIMEOUT_AFTER": str(timeout_minutes),
        }
    )
    return environment


def _write_metadata(output: Path, metadata: dict[str, Any]) -> None:
    (output / "run-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Kill the PTS process and any grandchildren it spawned."""
    if process.poll() is not None:
        return
    if os.name == "posix" and hasattr(os, "killpg"):
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        if os.name == "posix" and hasattr(os, "killpg"):
            with suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


def run_command(
    argv: list[str],
    *,
    environment: dict[str, str],
    timeout: int,
    log_path: Path,
) -> CommandResult:
    """Run one PTS command, streaming its merged output to the adapter log and stdout.

    The previous implementation buffered everything with capture_output, so a
    y/n prompt or a stalled download was invisible until the whole command
    finished. Streaming makes the prompt visible in the experiment terminal
    while still writing the authoritative adapter.log. stdin is closed so PTS
    cannot block waiting for an interactive answer.
    """
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        log.write(f"--- command ---\n{' '.join(argv)}\n")
        log.flush()
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=environment,
            start_new_session=(os.name == "posix"),
        )
        assert process.stdout is not None

        def _pump() -> None:
            try:
                for chunk in process.stdout:
                    log.write(chunk)
                    sys.stdout.write(chunk)
                    log.flush()
                    sys.stdout.flush()
            except (OSError, ValueError):
                return

        reader = threading.Thread(target=_pump, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        returncode: int | None = None
        timed_out = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_group(process)
                break
            try:
                returncode = process.wait(timeout=min(1.0, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if returncode is None and not timed_out:
            _terminate_process_group(process)
            with suppress(subprocess.TimeoutExpired):
                returncode = process.wait(timeout=5)
        reader.join(timeout=5)
        log.write(f"exit_code={returncode}\n")
        log.flush()
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout)
    return CommandResult(returncode if returncode is not None else -1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PTS PHPBench producer")
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    envelope = load_envelope(args.envelope)
    profile, times_to_run, timeout_minutes, workload_id = read_run_contract(envelope)
    command_prefix = resolve_pts_command()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    log_path = output / "adapter.log"
    metadata: dict[str, Any] = {
        "suite": "phoronix-test-suite",
        "suiteVersion": PINNED_PTS_VERSION,
        "profile": profile,
        "workload": workload_id,
        "parameters": {
            "times_to_run": times_to_run,
            "test_timeout_minutes": timeout_minutes,
        },
        "resultName": RESULT_NAME,
        "resultIdentifier": RESULT_IDENTIFIER,
    }

    with tempfile.TemporaryDirectory(prefix="pts-user-", dir=output) as user_dir:
        pts_user_path = Path(user_dir)
        prepared_payload = os.environ.get("LOOPER_PHPBENCH_PAYLOAD")
        if prepared_payload:
            payload = Path(prepared_payload).resolve()
            if not payload.is_file():
                raise PhoronixError(f"prepared PHPBench payload is missing: {payload}")
            observed_payload = hashlib.sha256(payload.read_bytes()).hexdigest()
            expected_payload = "32503bd4ace0c8429493de864ca48bb16febed867e52b75f4369d7145f797718"
            if observed_payload != expected_payload:
                raise PhoronixError("prepared PHPBench payload digest does not match the contract")
            download_cache = pts_user_path / "download-cache"
            download_cache.mkdir(parents=True, exist_ok=True)
            shutil.copy2(payload, download_cache / payload.name)
        environment = _subprocess_environment(
            pts_user_path, times_to_run, timeout_minutes
        )
        benchmark_argv = [*command_prefix, "default-benchmark", profile]
        total_timeout = timeout_minutes * 60 + INSTALL_EXPORT_GRACE_SECONDS
        benchmark_result = run_command(
            benchmark_argv,
            environment=environment,
            timeout=total_timeout,
            log_path=log_path,
        )
        metadata["benchmarkExitCode"] = benchmark_result.returncode
        if benchmark_result.returncode != 0:
            metadata["exportExitCode"] = None
            _write_metadata(output, metadata)
            return benchmark_result.returncode or 2

        raw_result = (output / "pts-result.json").resolve()
        export_environment = dict(environment)
        export_environment["OUTPUT_FILE"] = str(raw_result)
        export_argv = [*command_prefix, "result-file-to-json", RESULT_NAME]
        export_result = run_command(
            export_argv,
            environment=export_environment,
            timeout=60,
            log_path=log_path,
        )
        metadata["exportExitCode"] = export_result.returncode
        _write_metadata(output, metadata)
        if export_result.returncode != 0:
            return export_result.returncode or 2
        if not raw_result.is_file():
            raise PhoronixError("PTS export succeeded without producing pts-result.json")
        parsed = json.loads(raw_result.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), dict):
            raise PhoronixError("PTS exported JSON does not contain a results object")
        if not math.isfinite(float(raw_result.stat().st_size)):
            raise PhoronixError("PTS exported result has an invalid size")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PhoronixError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        print(f"PTS PHPBench producer failed closed: {error}", file=sys.stderr)
        raise SystemExit(2) from None
