from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shutil
import string
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psutil
from looper_benchmark_sdk.scenario import (
    normalize_benchbase_smallbank,
    normalize_dcperf_mediawiki,
)
from looper_core.contracts import AttemptResult, MetricObservation
from looper_core.fingerprint import system_fingerprint
from looper_core.scenario_adapters import ScenarioAdapterError

from looper_worker.client import ControlPlaneClient
from looper_worker.package_cache import PackageCacheError, materialize_package


class RunnerError(RuntimeError):
    pass


def _fingerprint_value(fingerprint: dict[str, Any], dotted_path: str) -> Any:
    value: Any = fingerprint
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def validate_execution_policy(runtime: dict[str, Any], fingerprint: dict[str, Any]) -> None:
    policy = runtime.get("executionPolicy")
    if not policy:
        return
    placement = policy["placement"]["mode"]
    if placement != "isolated-container" or runtime.get("type") != "container":
        raise RunnerError("this worker only enforces isolated-container execution policies")
    network = policy["network"]["mode"]
    if network != "none" or runtime.get("networkMode", "none") != "none":
        raise RunnerError("restricted egress requires a policy-enforcing network runner")
    storage = policy["storage"]["mode"]
    if storage != "workspace":
        raise RunnerError("bound storage inputs require a policy-enforcing host runner")
    missing = []
    for required_field in policy["environmentEvidence"]["requiredFields"]:
        value = _fingerprint_value(fingerprint, required_field)
        if value is None or value == "" or value == []:
            missing.append(required_field)
    if missing:
        raise RunnerError(f"required environment evidence is unavailable: {missing}")


@dataclass(slots=True)
class StageResult:
    stage: str
    exit_code: int | None
    status: str
    message: str | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None


@dataclass(slots=True)
class RunResult:
    status: str
    exit_code: int | None = None
    error_message: str | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)


SAFE_ENVIRONMENT = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "LANG",
    "LC_ALL",
}

_CONTAINER_IMAGE_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?@sha256:[0-9a-f]{64}$"
)
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DENIED_CONTAINER_ENVIRONMENT = {
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "TENCENTCLOUD_SECRET_ID",
    "TENCENTCLOUD_SECRET_KEY",
}


def validate_container_image(image: object) -> str:
    if not isinstance(image, str) or _CONTAINER_IMAGE_RE.fullmatch(image) is None:
        raise RunnerError("container runtime requires a lowercase image pinned by sha256 digest")
    return image


def container_runtime_available(engine: str = "docker") -> bool:
    if engine != "docker" or shutil.which(engine) is None:
        return False
    try:
        subprocess.run(
            [engine, "info", "--format", "{{.ServerVersion}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _container_name(attempt_id: str, stage: str, fencing_token: int) -> str:
    safe_attempt = re.sub(r"[^a-z0-9_.-]", "-", attempt_id.lower()).strip("-._")
    safe_stage = re.sub(r"[^a-z0-9_.-]", "-", stage.lower()).strip("-._")
    if not safe_attempt or not safe_stage:
        raise RunnerError("attempt and stage names must contain safe characters")
    name = f"looper-{safe_attempt[:60]}-{safe_stage[:24]}-{fencing_token}"
    if len(name) > 127:
        name = name[:127].rstrip("-._")
    return name


def _validate_container_environment(name: str) -> None:
    if _ENVIRONMENT_NAME_RE.fullmatch(name) is None:
        raise RunnerError(f"invalid container environment variable: {name!r}")
    upper = name.upper()
    if (
        upper in _DENIED_CONTAINER_ENVIRONMENT
        or upper.startswith("LD_")
        or "TOKEN" in upper
        or "SECRET" in upper
        or "PASSWORD" in upper
        or "CREDENTIAL" in upper
    ):
        raise RunnerError(f"sensitive container environment variable is not allowed: {name}")


def _container_working_directory(value: str) -> str:
    if value in {"", "."}:
        return "/looper/benchmark"
    if not value.startswith("/"):
        value = f"/looper/benchmark/{value}"
    normalized = posixpath.normpath(value.replace("\\", "/"))
    if normalized != "/looper/benchmark" and not normalized.startswith("/looper/benchmark/"):
        raise RunnerError("container workingDirectory must remain under /looper/benchmark")
    return normalized


def build_container_command(
    runtime: dict[str, Any],
    command: dict[str, Any],
    *,
    placeholders: dict[str, str],
    host_paths: dict[str, Path],
    attempt_id: str,
    fencing_token: int,
    stage: str,
) -> tuple[list[str], str]:
    """Build a shell-free, least-privilege Docker invocation."""
    if runtime.get("type") != "container":
        raise RunnerError("container command builder requires a container runtime")
    image = validate_container_image(runtime.get("image"))
    engine = runtime.get("engine", "docker")
    if engine != "docker":
        raise RunnerError("only the Docker engine is supported by the local worker")
    network = runtime.get("networkMode", "none")
    if network not in {"none", "bridge"}:
        raise RunnerError("container networkMode must be none or bridge")
    if "{python}" in " ".join(command.get("argv", [])):
        raise RunnerError("container commands cannot use the host {python} placeholder")
    for path_name in ("input", "output", "workspace", "benchmarkRoot"):
        path = host_paths.get(path_name)
        if path is None or not path.is_dir():
            raise RunnerError(f"container mount source is not a directory: {path_name}")
        if path.is_symlink():
            raise RunnerError(f"container mount source cannot be a symbolic link: {path}")
    argv = [_expand(value, placeholders) for value in command["argv"]]
    name = _container_name(attempt_id, stage, fencing_token)
    docker_argv = [
        engine,
        "run",
        "--rm",
        "--init",
        "--pull",
        "never",
        "--name",
        name,
        "--network",
        network,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        "256",
        "--mount",
        f"type=bind,source={host_paths['input']},destination=/looper/input,readonly",
        "--mount",
        f"type=bind,source={host_paths['output']},destination=/looper/output",
        "--mount",
        f"type=bind,source={host_paths['workspace']},destination=/looper/workspace",
        "--mount",
        f"type=bind,source={host_paths['benchmarkRoot']},destination=/looper/benchmark,readonly",
        "--workdir",
        _container_working_directory(str(runtime.get("workingDirectory", "."))),
    ]
    cache_path = host_paths.get("cache")
    if cache_path is not None:
        if not cache_path.is_dir() or cache_path.is_symlink():
            raise RunnerError("container cache mount must be a real directory")
        workdir_index = docker_argv.index("--workdir")
        docker_argv[workdir_index:workdir_index] = [
            "--mount",
            f"type=bind,source={cache_path},destination=/looper/cache",
        ]
    for environment_name, value in command.get("environment", {}).items():
        _validate_container_environment(environment_name)
        docker_argv.extend(("--env", f"{environment_name}={_expand(value, placeholders)}"))
    secret_path = host_paths.get("secrets")
    if secret_path is not None:
        if not secret_path.is_dir() or secret_path.is_symlink():
            raise RunnerError("container secret mount must be a real directory")
        docker_argv.extend(
            (
                "--mount",
                f"type=bind,source={secret_path},destination=/run/looper-secrets,readonly",
            )
        )
    docker_argv.extend(("--tmpfs", "/tmp:rw,noexec,nosuid,size=256m"))
    docker_argv.extend(("--env", "PYTHONUTF8=1", image, *argv))
    return docker_argv, name


def _within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise RunnerError(f"path escapes its allowed root: {path}") from error
    if path.is_symlink():
        raise RunnerError(f"symbolic links are not accepted: {path}")
    return resolved


def _expand(value: str, placeholders: dict[str, str]) -> str:
    for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(value):
        if field_name and field_name not in placeholders:
            raise RunnerError(f"unknown command placeholder: {field_name}")
    return value.format_map(placeholders)


def _terminate_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    processes = parent.children(recursive=True)
    processes.append(parent)
    for process in processes:
        with suppress(psutil.Error):
            process.terminate()
    _, alive = psutil.wait_procs(processes, timeout=3)
    for process in alive:
        with suppress(psutil.Error):
            process.kill()


def _cleanup_container(engine: str, name: str) -> None:
    if engine != "docker" or re.fullmatch(r"looper-[a-z0-9_.-]{1,120}", name) is None:
        return
    for command in (
        [engine, "stop", "--time", "3", name],
        [engine, "rm", "--force", name],
    ):
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )


def _read_log_chunk(path: Path, offset: int, limit: int = 7000) -> tuple[int, str]:
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(limit)
    except OSError:
        return offset, ""
    return offset + len(data), data.decode("utf-8", errors="replace")


def _display_command(argv: list[str]) -> str:
    sensitive = re.compile(r"password|secret|token|credential|private[-_]key", re.IGNORECASE)
    displayed: list[str] = []
    mask_next = False
    for value in argv:
        if mask_next:
            displayed.append("***")
            mask_next = False
            continue
        if sensitive.search(value):
            if "=" in value:
                key, _separator, _secret = value.partition("=")
                displayed.append(f"{key}=***")
            else:
                displayed.append(value)
                mask_next = True
            continue
        displayed.append(value)
    return subprocess.list2cmdline(displayed)


class LocalAttemptRunner:
    def __init__(
        self,
        client: ControlPlaneClient,
        work_root: Path,
        secret_root: Path | None = None,
    ) -> None:
        self.client = client
        self.work_root = work_root.resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.secret_root = secret_root.resolve() if secret_root is not None else None

    def _report_phase(
        self,
        attempt_id: str,
        fencing_token: int,
        phase: str,
        detail: str,
    ) -> None:
        try:
            self.client.heartbeat(
                attempt_id,
                fencing_token,
                phase=phase,
                phase_detail=detail,
            )
        except TypeError:
            # Backwards compatibility for third-party/testing clients that
            # implement the v1 heartbeat without optional phase fields.
            self.client.heartbeat(attempt_id, fencing_token)

    def _report_log(
        self,
        attempt_id: str,
        fencing_token: int,
        *,
        log_id: str,
        stage: str,
        stream: str,
        text: str,
    ) -> dict[str, Any]:
        try:
            return self.client.heartbeat(
                attempt_id,
                fencing_token,
                log_id=log_id,
                log_stage=stage,
                log_stream=stream,
                log_text=text,
            )
        except TypeError:
            # Older test clients and third-party workers can still run without
            # the optional terminal stream fields.
            return self.client.heartbeat(attempt_id, fencing_token)

    def run_claim(self, claim: dict[str, Any]) -> dict[str, Any]:
        attempt_id = str(claim["attemptId"])
        fencing_token = int(claim["fencingToken"])
        manifest = claim["manifest"]
        self._report_phase(
            attempt_id,
            fencing_token,
            "deploying-package",
            "正在校验并部署 Benchmark 脚本包",
        )
        runtime = manifest["spec"]["runtime"]
        runtime_type = runtime.get("type")
        if runtime_type == "local-process" and manifest["spec"]["trust"] != "trusted":
            raise RunnerError("the local-process runner only accepts trusted benchmarks")
        if runtime_type not in {"local-process", "container"}:
            raise RunnerError(f"unsupported worker runtime: {runtime_type!r}")
        if runtime_type == "container":
            validate_container_image(runtime.get("image"))
            if not container_runtime_available(str(runtime.get("engine", "docker"))):
                raise RunnerError("Docker daemon is unavailable for container benchmark execution")
        fingerprint = system_fingerprint()
        validate_execution_policy(runtime, fingerprint)

        workspace = self.work_root / attempt_id
        if workspace.exists():
            shutil.rmtree(workspace)
        input_dir = workspace / "input"
        output_dir = workspace / "output"
        logs_dir = workspace / "logs"
        input_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        envelope = dict(claim["envelope"])
        envelope["executionEvidence"] = {
            "profile": "looper.system-fingerprint/v1alpha1",
            "fingerprint": fingerprint,
        }
        benchmark_bundle = claim.get("benchmarkBundle")
        if isinstance(benchmark_bundle, dict):
            try:
                benchmark_root = materialize_package(
                    benchmark_bundle, self.work_root / "benchmark-packages"
                )
            except PackageCacheError as error:
                raise RunnerError(f"could not deploy Benchmark package: {error}") from error
        else:
            benchmark_root = Path(claim["benchmarkRoot"]).resolve()
        if not benchmark_root.is_dir():
            repository_root = os.environ.get("LOOPER_REPOSITORY_ROOT")
            relative_root = claim.get("benchmarkRelativeRoot")
            if repository_root and relative_root:
                benchmark_root = (Path(repository_root) / str(relative_root)).resolve()
        if not benchmark_root.is_dir():
            raise RunnerError("the deployed Worker does not contain this Benchmark package")
        cache_root = (
            self.work_root / "dependency-cache" / str(manifest["metadata"]["id"])
        ).resolve()
        provisioning = runtime.get("provisioning") or {}
        cache_identity = str(
            provisioning.get("cacheKey") or manifest["metadata"]["version"]
        ).removeprefix("sha256:")
        cache_leaf = re.sub(r"[^A-Za-z0-9._-]+", "_", cache_identity)
        dependency_cache = cache_root / cache_leaf
        legacy_cache = cache_root / str(manifest["metadata"]["version"])
        if not dependency_cache.exists() and legacy_cache.is_dir():
            cache_root.mkdir(parents=True, exist_ok=True)
            try:
                legacy_cache.replace(dependency_cache)
            except OSError:
                dependency_cache = legacy_cache
        dependency_cache.mkdir(parents=True, exist_ok=True)
        self._report_phase(
            attempt_id,
            fencing_token,
            "checking-environment",
            "正在检查目标机器基础环境",
        )
        if runtime_type == "container":
            envelope["paths"] = {
                "input": "/looper/input",
                "output": "/looper/output",
                "workspace": "/looper/workspace",
            }
            placeholders = {
                "input": "/looper/input",
                "output": "/looper/output",
                "workspace": "/looper/workspace",
                "envelope": "/looper/input/run-envelope.json",
                "benchmarkRoot": "/looper/benchmark",
                "cache": "/looper/cache",
            }
        else:
            envelope["paths"] = {
                "input": str(input_dir.resolve()),
                "output": str(output_dir.resolve()),
                "workspace": str(workspace.resolve()),
            }
            placeholders = {
                "python": sys.executable,
                "input": str(input_dir.resolve()),
                "output": str(output_dir.resolve()),
                "workspace": str(workspace.resolve()),
                "envelope": str((input_dir / "run-envelope.json").resolve()),
                "benchmarkRoot": str(benchmark_root),
                "cache": str(dependency_cache),
            }
        if self.secret_root is not None:
            benchmark_secret_dir = self.secret_root / str(manifest["metadata"]["id"])
            if benchmark_secret_dir.is_dir() and not benchmark_secret_dir.is_symlink():
                envelope["paths"]["secrets"] = (
                    "/run/looper-secrets"
                    if runtime_type == "container"
                    else str(benchmark_secret_dir.resolve())
                )
        envelope_path = input_dir / "run-envelope.json"
        envelope_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        self.client.start(attempt_id, fencing_token, envelope)
        host_paths = {
            "input": input_dir.resolve(),
            "output": output_dir.resolve(),
            "workspace": workspace.resolve(),
            "benchmarkRoot": benchmark_root,
            "cache": dependency_cache,
        }
        if self.secret_root is not None:
            benchmark_secret_dir = self.secret_root / str(manifest["metadata"]["id"])
            if benchmark_secret_dir.is_dir() and not benchmark_secret_dir.is_symlink():
                host_paths["secrets"] = benchmark_secret_dir.resolve()
        command_map = runtime["commands"]
        stage_results: list[StageResult] = []
        failure: StageResult | None = None
        cancelled = False
        lifecycle = ["prepare"]
        lifecycle.extend(["warmup"] * int(envelope.get("extensions", {}).get("warmupRuns", 0)))
        lifecycle.extend(["run", "normalize", "validate", "collect"])
        try:
            for stage in lifecycle:
                command = command_map.get(stage)
                if command is None:
                    continue
                phase_labels = {
                    "prepare": ("preparing-environment", "正在自动安装并校验测试依赖"),
                    "warmup": ("warming-up", "正在预热测试场景"),
                    "run": ("running-benchmark", "正在执行 Benchmark"),
                    "normalize": ("normalizing-results", "正在标准化测试结果"),
                    "validate": ("validating-results", "正在校验正确性与结果合同"),
                    "collect": ("collecting-evidence", "正在收集测试证据"),
                }
                phase, phase_detail = phase_labels[stage]
                self._report_phase(
                    attempt_id,
                    fencing_token,
                    phase,
                    phase_detail,
                )
                result = self._run_stage(
                    attempt_id,
                    fencing_token,
                    stage,
                    command,
                    runtime,
                    placeholders,
                    logs_dir,
                    int(claim.get("maxOutputBytes", 16 * 1024 * 1024)),
                    host_paths=host_paths,
                    lease_seconds=int(claim.get("leaseSeconds", 30)),
                )
                stage_results.append(result)
                if result.status == "cancelled":
                    cancelled = True
                    failure = result
                    break
                if result.status != "succeeded":
                    failure = result
                    break
        finally:
            cleanup = command_map.get("cleanup")
            lease_invalid = cancelled or bool(
                failure and failure.message and "control-plane heartbeat failed" in failure.message
            )
            if cleanup is not None and not lease_invalid:
                self._report_phase(
                    attempt_id,
                    fencing_token,
                    "cleaning-up",
                    "正在清理本次测试环境",
                )
                cleanup_result = self._run_stage(
                    attempt_id,
                    fencing_token,
                    "cleanup",
                    cleanup,
                    runtime,
                    placeholders,
                    logs_dir,
                    int(claim.get("maxOutputBytes", 16 * 1024 * 1024)),
                    host_paths=host_paths,
                    lease_seconds=int(claim.get("leaseSeconds", 30)),
                )
                stage_results.append(cleanup_result)
                if cleanup_result.status != "succeeded" and failure is None:
                    failure = cleanup_result

        if failure is None and not cancelled and "normalize" not in command_map:
            normalizer_failure = self._run_runtime_normalizer(manifest, output_dir)
            if normalizer_failure is not None:
                failure = normalizer_failure
        result = self._collect_result(manifest, output_dir, failure, cancelled)
        self._report_phase(
            attempt_id,
            fencing_token,
            "uploading-evidence",
            "正在回传日志、指标和原始结果",
        )
        evidence_limit = min(
            int(claim.get("maxOutputBytes", 16 * 1024 * 1024)),
            int(manifest["spec"]["outputs"]["maxBytes"]),
        )
        self._upload_evidence(
            claim,
            output_dir,
            logs_dir,
            result,
            evidence_limit,
        )
        completion = {
            "idempotencyKey": f"attempt.complete:{attempt_id}:{fencing_token}",
            "status": result.status,
            "exitCode": result.exit_code,
            "errorMessage": result.error_message,
            "observations": result.observations,
            "checks": result.checks,
        }
        return self.client.complete(attempt_id, fencing_token, completion)

    def _run_stage(
        self,
        attempt_id: str,
        fencing_token: int,
        stage: str,
        command: dict[str, Any],
        runtime: dict[str, Any],
        placeholders: dict[str, str],
        logs_dir: Path,
        max_output_bytes: int,
        *,
        host_paths: dict[str, Path] | None = None,
        lease_seconds: int = 30,
    ) -> StageResult:
        if runtime.get("type") == "container":
            if host_paths is None:
                raise RunnerError("container stages require host mount paths")
            return self._run_container_stage(
                attempt_id,
                fencing_token,
                stage,
                command,
                runtime,
                placeholders,
                host_paths,
                logs_dir,
                max_output_bytes,
                lease_seconds,
            )
        argv = [_expand(value, placeholders) for value in command["argv"]]
        benchmark_root = Path(placeholders["benchmarkRoot"]).resolve()
        working = _within(benchmark_root / runtime.get("workingDirectory", "."), benchmark_root)
        environment = {
            name: value for name, value in os.environ.items() if name in SAFE_ENVIRONMENT
        }
        environment.update(
            {
                name: _expand(value, placeholders)
                for name, value in command.get("environment", {}).items()
            }
        )
        environment["PYTHONUTF8"] = "1"
        return self._execute_process(
            attempt_id,
            fencing_token,
            stage,
            command,
            argv,
            working,
            environment,
            logs_dir,
            max_output_bytes,
            lease_seconds,
        )

    def _run_container_stage(
        self,
        attempt_id: str,
        fencing_token: int,
        stage: str,
        command: dict[str, Any],
        runtime: dict[str, Any],
        placeholders: dict[str, str],
        host_paths: dict[str, Path],
        logs_dir: Path,
        max_output_bytes: int,
        lease_seconds: int,
    ) -> StageResult:
        argv, container_name = build_container_command(
            runtime,
            command,
            placeholders=placeholders,
            host_paths=host_paths,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            stage=stage,
        )
        environment = {
            name: value for name, value in os.environ.items() if name in SAFE_ENVIRONMENT
        }
        return self._execute_process(
            attempt_id,
            fencing_token,
            stage,
            command,
            argv,
            None,
            environment,
            logs_dir,
            max_output_bytes,
            lease_seconds,
            container_name=container_name,
            engine=str(runtime.get("engine", "docker")),
        )

    def _execute_process(
        self,
        attempt_id: str,
        fencing_token: int,
        stage: str,
        command: dict[str, Any],
        argv: list[str],
        working: Path | None,
        environment: dict[str, str],
        logs_dir: Path,
        max_output_bytes: int,
        lease_seconds: int,
        *,
        container_name: str | None = None,
        engine: str | None = None,
    ) -> StageResult:
        stdout_path = logs_dir / f"{stage}.stdout.log"
        stderr_path = logs_dir / f"{stage}.stderr.log"
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=working,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=creation_flags,
                    start_new_session=os.name != "nt",
                )
            except OSError as error:
                return StageResult(
                    stage,
                    None,
                    "failed",
                    f"could not start stage: {error}",
                    stdout_path,
                    stderr_path,
                )
            process_file = logs_dir.parent / "process.json"
            runtime_file = logs_dir.parent / "runtime.json"
            try:
                metadata: dict[str, Any] = {
                    "kind": "container" if container_name else "local-process",
                    "pid": process.pid,
                    "fencing_token": fencing_token,
                }
                if container_name:
                    metadata.update({"name": container_name, "engine": engine or "docker"})
                try:
                    process_info = psutil.Process(process.pid)
                    metadata["create_time"] = process_info.create_time()
                except psutil.Error:
                    pass
                encoded_metadata = json.dumps(metadata)
                process_file.write_text(encoded_metadata, encoding="utf-8")
                runtime_file.write_text(encoded_metadata, encoding="utf-8")
                started = time.monotonic()
                last_heartbeat = 0.0
                timeout = int(command["timeoutSeconds"])
                heartbeat_interval = max(
                    1.0,
                    min(10.0, max(1.0, float(lease_seconds) / 3.0), max(1.0, timeout / 3.0)),
                )
                log_offsets = {"stdout": 0, "stderr": 0}
                log_run_id = str(time.time_ns())
                try:
                    command_heartbeat = self._report_log(
                        attempt_id,
                        fencing_token,
                        log_id=f"{log_run_id}:command",
                        stage=stage,
                        stream="command",
                        text=f"$ {_display_command(argv)}\n",
                    )
                except httpx.HTTPError as error:
                    return StageResult(
                        stage,
                        process.poll(),
                        "failed",
                        f"control-plane heartbeat failed: {error}",
                        stdout_path,
                        stderr_path,
                    )
                if command_heartbeat.get("cancelRequested"):
                    return StageResult(
                        stage,
                        process.poll(),
                        "cancelled",
                        "cancellation requested",
                        stdout_path,
                        stderr_path,
                    )
                self._report_log(
                    attempt_id,
                    fencing_token,
                    log_id=f"{log_run_id}:system:started",
                    stage=stage,
                    stream="system",
                    text=(
                        f"process started pid={process.pid} cwd={working or Path.cwd()} "
                        f"worker={getattr(self.client, 'worker_id', 'worker-unknown')} "
                        f"lease={fencing_token}\n"
                    ),
                )

                def emit_available_logs() -> tuple[bool, str | None]:
                    streams = (("stdout", stdout_path), ("stderr", stderr_path))
                    for stream_name, stream_path in streams:
                        while True:
                            start_offset = log_offsets[stream_name]
                            end_offset, text = _read_log_chunk(stream_path, start_offset)
                            if not text:
                                break
                            log_offsets[stream_name] = end_offset
                            try:
                                response = self._report_log(
                                    attempt_id,
                                    fencing_token,
                                    log_id=f"{log_run_id}:{stream_name}:{start_offset}",
                                    stage=stage,
                                    stream=stream_name,
                                    text=text,
                                )
                            except httpx.HTTPError as error:
                                return False, f"control-plane heartbeat failed: {error}"
                            if response.get("cancelRequested"):
                                return True, "cancellation requested"
                    return False, None

                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if elapsed - last_heartbeat >= heartbeat_interval:
                        try:
                            heartbeat = self.client.heartbeat(attempt_id, fencing_token)
                        except httpx.HTTPError as error:
                            return StageResult(
                                stage,
                                process.poll(),
                                "failed",
                                f"control-plane heartbeat failed: {error}",
                                stdout_path,
                                stderr_path,
                            )
                        last_heartbeat = elapsed
                        if heartbeat.get("cancelRequested"):
                            return StageResult(
                                stage,
                                process.poll(),
                                "cancelled",
                                "cancellation requested",
                                stdout_path,
                                stderr_path,
                            )
                        cancelled_by_control_plane, log_error = emit_available_logs()
                        if log_error:
                            return StageResult(
                                stage,
                                process.poll(),
                                "cancelled" if cancelled_by_control_plane else "failed",
                                log_error,
                                stdout_path,
                                stderr_path,
                            )
                    if elapsed >= timeout:
                        return StageResult(
                            stage,
                            process.poll(),
                            "timed_out",
                            f"stage exceeded {timeout} seconds",
                            stdout_path,
                            stderr_path,
                        )
                    stdout.flush()
                    stderr.flush()
                    if stdout_path.stat().st_size + stderr_path.stat().st_size > max_output_bytes:
                        return StageResult(
                            stage,
                            process.poll(),
                            "failed",
                            "stage logs exceeded the output limit",
                            stdout_path,
                            stderr_path,
                        )
                    time.sleep(0.1)
                cancelled_by_control_plane, log_error = emit_available_logs()
                if log_error:
                    return StageResult(
                        stage,
                        process.poll(),
                        "cancelled" if cancelled_by_control_plane else "failed",
                        log_error,
                        stdout_path,
                        stderr_path,
                    )
                self._report_log(
                    attempt_id,
                    fencing_token,
                    log_id=f"{log_run_id}:system:exited",
                    stage=stage,
                    stream="system",
                    text=(
                        f"process exited pid={process.pid} code={process.returncode} "
                        f"elapsed={time.monotonic() - started:.3f}s\n"
                    ),
                )
            finally:
                if process.poll() is None:
                    _terminate_tree(process.pid)
                if container_name and engine:
                    _cleanup_container(engine, container_name)
                process_file.unlink(missing_ok=True)
                runtime_file.unlink(missing_ok=True)
        allowed = set(command.get("allowedExitCodes", [0]))
        if process.returncode not in allowed:
            return StageResult(
                stage,
                process.returncode,
                "failed",
                f"stage exited with code {process.returncode}",
                stdout_path,
                stderr_path,
            )
        return StageResult(stage, process.returncode, "succeeded", None, stdout_path, stderr_path)

    def _run_runtime_normalizer(
        self, manifest: dict[str, Any], output_dir: Path
    ) -> StageResult | None:
        declaration = manifest["spec"].get("x-extensions", {}).get("runtimeNormalizer")
        if declaration is None:
            return None
        if not isinstance(declaration, dict) or declaration.get("type") != "built-in":
            return StageResult(
                "normalize", None, "failed", "runtime normalizer declaration is invalid"
            )
        normalizer_id = declaration.get("id")
        try:
            if normalizer_id == "benchbase-smallbank-v1":
                normalize_benchbase_smallbank(
                    summary_path=output_dir / "summary.json",
                    histograms_path=output_dir / "transaction-histograms.json",
                    latencies_path=output_dir / "latency.raw.csv",
                    accounting_path=output_dir / "client-load-accounting.json",
                    output=output_dir,
                )
            elif normalizer_id == "dcperf-mediawiki-v1":
                normalize_dcperf_mediawiki(
                    result_path=output_dir / "benchpress-result.json",
                    output=output_dir,
                )
            else:
                raise RunnerError(f"unknown built-in runtime normalizer: {normalizer_id!r}")
        except (OSError, ValueError, ScenarioAdapterError, RunnerError) as error:
            return StageResult(
                "normalize", None, "failed", f"runtime normalization failed: {error}"
            )
        return None

    def _collect_result(
        self,
        manifest: dict[str, Any],
        output_dir: Path,
        failure: StageResult | None,
        cancelled: bool,
    ) -> RunResult:
        metrics_path = output_dir / "metrics.jsonl"
        result_path = output_dir / "result.json"
        observations: list[dict[str, Any]] = []
        max_lines = int(manifest["spec"]["outputs"].get("maxMetricLines", 100000))
        if metrics_path.exists():
            with metrics_path.open("r", encoding="utf-8") as stream:
                for index, line in enumerate(stream):
                    if index >= max_lines:
                        raise RunnerError("metric line limit exceeded")
                    if len(line.encode("utf-8")) > 1024 * 1024:
                        raise RunnerError("metric line exceeds one MiB")
                    if line.strip():
                        observation = MetricObservation.model_validate_json(line)
                        observations.append(
                            observation.model_dump(mode="json", by_alias=True, exclude_none=True)
                        )
        checks: list[dict[str, Any]] = []
        benchmark_result: AttemptResult | None = None
        if result_path.exists():
            benchmark_result = AttemptResult.model_validate_json(
                result_path.read_text(encoding="utf-8")
            )
            checks = [
                item.model_dump(mode="json", by_alias=True, exclude_none=True)
                for item in benchmark_result.checks
            ]
        if cancelled:
            return RunResult(
                "cancelled",
                failure.exit_code if failure else None,
                failure.message if failure else "cancelled",
                observations,
                checks,
            )
        if failure:
            status = "timed_out" if failure.status == "timed_out" else "failed"
            return RunResult(status, failure.exit_code, failure.message, observations, checks)
        if benchmark_result is None:
            return RunResult("failed", None, "result.json was not produced", observations, checks)
        return RunResult(
            benchmark_result.status,
            0,
            benchmark_result.message,
            observations,
            checks,
        )

    def _upload_evidence(
        self,
        claim: dict[str, Any],
        output_dir: Path,
        logs_dir: Path,
        result: RunResult,
        max_output_bytes: int,
    ) -> None:
        attempt_id = str(claim["attemptId"])
        fencing_token = int(claim["fencingToken"])
        manifest = claim["manifest"]
        candidates: dict[str, tuple[Path, str, str, str, str]] = {}

        for path in logs_dir.glob("*.log"):
            candidates[f"log:{path.name}"] = (
                path,
                "log",
                "text/plain",
                "worker",
                path.name,
            )
        metrics_path = output_dir / "metrics.jsonl"
        if metrics_path.exists():
            candidates["result:metrics.jsonl"] = (
                metrics_path,
                "result",
                "application/x-ndjson",
                "benchmark",
                "metrics.jsonl",
            )
        result_path = output_dir / "result.json"
        if result_path.exists():
            candidates["result:result.json"] = (
                result_path,
                "result",
                "application/json",
                "benchmark",
                "result.json",
            )
        missing_required: list[str] = []
        for declaration in manifest["spec"]["outputs"]["artifacts"]:
            declared = str(declaration["path"])
            if "\\" in declared:
                raise RunnerError(f"artifact path must use POSIX separators: {declared}")
            relative = Path(declared)
            if relative.is_absolute() or ntpath.isabs(declared) or ".." in relative.parts:
                raise RunnerError(f"artifact path is unsafe: {declared}")
            path = _within(output_dir / relative, output_dir)
            if declaration.get("required", False) and (not path.exists() or not path.is_file()):
                missing_required.append(declared)
            if path.exists() and path.is_file():
                candidates[f"{declaration['role']}:{relative.as_posix()}"] = (
                    path,
                    declaration["role"],
                    declaration.get("mediaType", "application/octet-stream"),
                    "benchmark",
                    relative.as_posix(),
                )
        if missing_required:
            result.status = "failed"
            result.error_message = f"required artifacts were not produced: {missing_required}"
        total = sum(
            path.stat().st_size
            for path, _role, _media, _producer, _name in candidates.values()
        )
        if total > max_output_bytes:
            raise RunnerError("attempt evidence exceeds the output limit")
        for path, role, media_type, producer, name in candidates.values():
            self.client.upload_artifact(
                attempt_id,
                fencing_token,
                path,
                role=role,
                media_type=media_type,
                producer=producer,
                name=name,
            )


def cleanup_orphan_processes(work_root: Path) -> int:
    cleaned = 0
    if not work_root.exists():
        return cleaned
    runtime_files = list(work_root.glob("*/runtime.json"))
    runtime_parents = {path.parent for path in runtime_files}
    legacy_files = [
        path for path in work_root.glob("*/process.json") if path.parent not in runtime_parents
    ]
    for metadata_file in [*runtime_files, *legacy_files]:
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            if metadata.get("kind") == "container":
                engine = str(metadata.get("engine", "docker"))
                name = str(metadata["name"])
                if engine == "docker" and re.fullmatch(r"looper-[a-z0-9_.-]{1,120}", name):
                    _cleanup_container(engine, name)
                    cleaned += 1
            process = psutil.Process(int(metadata["pid"]))
            expected = metadata.get("create_time")
            if expected is None or abs(process.create_time() - float(expected)) < 0.01:
                _terminate_tree(process.pid)
                if metadata.get("kind") != "container":
                    cleaned += 1
        except (OSError, ValueError, KeyError, json.JSONDecodeError, psutil.Error):
            pass
        metadata_file.unlink(missing_ok=True)
        if metadata_file.name == "runtime.json":
            (metadata_file.parent / "process.json").unlink(missing_ok=True)
    return cleaned
