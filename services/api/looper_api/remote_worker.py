"""Ephemeral SSH deployment and reverse-tunnel management for external workers."""

from __future__ import annotations

import base64
import hashlib
import io
import shlex
import socket
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from looper_core.canonical import canonical_digest, utc_now

from looper_api.config import Settings
from looper_api.external_targets import (
    ConnectExternalTargetRequest,
    ExternalTargetError,
    open_ssh_client,
)
from looper_api.models import TargetRecord
from looper_api.seed import repository_root


@dataclass(slots=True)
class RemoteWorkerDeployment:
    target_id: str
    client: Any
    remote_port: int
    worker_id: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    tunnel_thread: threading.Thread | None = None

    def close(self) -> None:
        self.stop_event.set()
        self.client.close()


_deployments: dict[str, RemoteWorkerDeployment] = {}
_deployments_lock = threading.Lock()


def _source_archive() -> bytes:
    root = repository_root()
    prefixes = (
        Path("services/worker/looper_worker"),
        Path("packages/core/looper_core"),
        Path("packages/benchmark-sdk/looper_benchmark_sdk"),
        Path("benchmarks"),
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for prefix in prefixes:
            source = root / prefix
            for path in source.rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    archive.write(path, path.relative_to(root).as_posix())
    return output.getvalue()


def _run(client: Any, command: str, *, timeout: int = 300) -> str:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    output = stdout.read(1024 * 1024).decode("utf-8", errors="replace")
    error = stderr.read(256 * 1024).decode("utf-8", errors="replace")
    status = stdout.channel.recv_exit_status()
    if status != 0:
        detail = " ".join(error.split())[-1000:] or f"exit status {status}"
        raise ExternalTargetError(f"remote Worker deployment failed: {detail}")
    return output


def _relay(channel: Any, local_host: str, local_port: int) -> None:
    try:
        with socket.create_connection((local_host, local_port), timeout=10) as local:
            channel.settimeout(1)
            local.settimeout(1)
            while True:
                moved = False
                try:
                    data = channel.recv(65536)
                    if not data:
                        break
                    local.sendall(data)
                    moved = True
                except TimeoutError:
                    pass
                try:
                    data = local.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                    moved = True
                except TimeoutError:
                    pass
                if not moved and channel.closed:
                    break
    except OSError:
        pass
    finally:
        channel.close()


def _serve_reverse_tunnel(deployment: RemoteWorkerDeployment, local_port: int) -> None:
    transport = deployment.client.get_transport()
    if transport is None:
        return
    while not deployment.stop_event.is_set() and transport.is_active():
        channel = transport.accept(timeout=1)
        if channel is not None:
            threading.Thread(
                target=_relay,
                args=(channel, "127.0.0.1", local_port),
                daemon=True,
            ).start()


def _remote_port(transport: Any) -> int:
    # Port zero asks SSHD to allocate an unused remote loopback port.
    allocated = transport.request_port_forward("127.0.0.1", 0)
    if not allocated:
        raise ExternalTargetError("SSH server refused reverse port forwarding")
    return int(allocated)


def deploy_remote_worker(
    request: ConnectExternalTargetRequest,
    target: TargetRecord,
    settings: Settings,
) -> dict[str, Any]:
    """Upload the Worker, establish an SSH reverse tunnel, and start it remotely."""

    client = open_ssh_client(request)
    deployment: RemoteWorkerDeployment | None = None
    try:
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise ExternalTargetError("SSH connection did not become active")
        observed = target.fingerprint_json.get("host_key_sha256")
        remote_key = transport.get_remote_server_key()
        remote_digest = base64.b64encode(hashlib.sha256(remote_key.asbytes()).digest()).decode()
        connected_fingerprint = f"SHA256:{remote_digest.rstrip('=')}"
        if observed and connected_fingerprint != observed:
            raise ExternalTargetError("SSH host key changed after inventory discovery")

        remote_port = _remote_port(transport)
        worker_id = f"remote-{canonical_digest({'target': target.id})[-16:]}"
        deployment = RemoteWorkerDeployment(target.id, client, remote_port, worker_id)
        deployment.tunnel_thread = threading.Thread(
            target=_serve_reverse_tunnel,
            args=(deployment, settings.port),
            daemon=True,
            name=f"looper-tunnel-{worker_id}",
        )
        deployment.tunnel_thread.start()

        archive = _source_archive()
        remote_home = _run(client, "printf '%s' \"$HOME\"", timeout=30).strip()
        remote_home_path = PurePosixPath(remote_home)
        if not remote_home_path.is_absolute() or ".." in remote_home_path.parts:
            raise ExternalTargetError("remote account returned an invalid home directory")
        remote_root = remote_home_path / ".looper-worker"
        _run(client, f"mkdir -p {shlex.quote(str(remote_root))}", timeout=30)
        sftp = client.open_sftp()
        try:
            with sftp.file(str(remote_root / "source.zip"), "wb") as remote_file:
                remote_file.write(archive)
            with sftp.file(str(remote_root / "worker.token"), "w") as token_file:
                token_file.write(settings.local_worker_token)
            sftp.chmod(str(remote_root / "worker.token"), 0o600)
        finally:
            sftp.close()

        bootstrap = " && ".join(
            (
                (
                    f"python3 -m venv {remote_root}/venv || "
                    "(command -v apt-get >/dev/null && "
                    "if test \"$(id -u)\" = 0; then "
                    "apt-get update -qq && env DEBIAN_FRONTEND=noninteractive "
                    "apt-get install -y -qq python3-venv; else "
                    "sudo -n apt-get update -qq && sudo -n env "
                    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv; fi && "
                    f"python3 -m venv {remote_root}/venv)"
                ),
                (
                    f"{remote_root}/venv/bin/python -m pip install "
                    "--disable-pip-version-check -q 'httpx>=0.28,<1' 'psutil>=7,<8' "
                    "'pydantic>=2.11,<3' 'PyYAML>=6,<7' 'jsonschema>=4.24,<5'"
                ),
                f"rm -rf {remote_root}/source",
                f"mkdir -p {remote_root}/source",
                (
                    f"{remote_root}/venv/bin/python -m zipfile -e "
                    f"{remote_root}/source.zip {remote_root}/source"
                ),
                (
                    f"if test -f {remote_root}/worker.pid; then "
                    f"kill \"$(cat {remote_root}/worker.pid)\" 2>/dev/null || true; fi"
                ),
            )
        )
        _run(client, bootstrap, timeout=600)

        python_path = ":".join(
            str(remote_root / "source" / path)
            for path in ("services/worker", "packages/core", "packages/benchmark-sdk")
        )
        launch = (
            f"cd {remote_root}/source && "
            f"nohup env PYTHONPATH={shlex.quote(python_path)} "
            f"LOOPER_REPOSITORY_ROOT={remote_root}/source "
            f"LOOPER_LOCAL_WORKER_TOKEN=\"$(cat {remote_root}/worker.token)\" "
            f"{remote_root}/venv/bin/python -m looper_worker.main "
            f"--api-url http://127.0.0.1:{remote_port} "
            f"--worker-id {shlex.quote(worker_id)} "
            f"--target-id {shlex.quote(target.id)} "
            f"--work-dir {remote_root}/work "
            f">{remote_root}/worker.log 2>&1 </dev/null & echo $! | tee {remote_root}/worker.pid"
        )
        pid_text = _run(client, launch, timeout=30).strip()
        try:
            remote_pid = int(pid_text.splitlines()[-1])
        except (ValueError, IndexError) as error:
            raise ExternalTargetError("remote Worker did not return a process id") from error

        with _deployments_lock:
            previous = _deployments.pop(target.id, None)
            if previous is not None:
                previous.close()
            _deployments[target.id] = deployment
        return {
            "status": "deploying",
            "workerId": worker_id,
            "remotePid": remote_pid,
            "deployedAt": utc_now().isoformat(),
        }
    except Exception:
        if deployment is not None:
            deployment.close()
        else:
            client.close()
        raise


def deployment_status(target_id: str) -> dict[str, Any]:
    with _deployments_lock:
        deployment = _deployments.get(target_id)
        active = bool(
            deployment
            and deployment.client.get_transport()
            and deployment.client.get_transport().is_active()
        )
        return {
            "active": active,
            "workerId": deployment.worker_id if deployment else None,
            "remotePort": deployment.remote_port if active and deployment else None,
        }
