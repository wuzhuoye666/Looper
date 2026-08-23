"""Ephemeral SSH deployment and reverse-tunnel management for external workers."""

from __future__ import annotations

import base64
import hashlib
import io
import select
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
    client: Any | None
    remote_port: int | None
    worker_id: str
    transport: str = "reverse-tunnel"
    stop_event: threading.Event = field(default_factory=threading.Event)
    tunnel_thread: threading.Thread | None = None

    def close(self) -> None:
        self.stop_event.set()
        if self.client is not None:
            self.client.close()


_deployments: dict[str, RemoteWorkerDeployment] = {}
_deployments_lock = threading.Lock()


def _source_archive() -> bytes:
    root = repository_root()
    prefixes = (
        Path("services/worker/looper_worker"),
        Path("packages/core/looper_core"),
        Path("packages/benchmark-sdk/looper_benchmark_sdk"),
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


def _launch_background(client: Any, command: str) -> int:
    """Read the detached process id without waiting for its SSH channel to linger."""

    _stdin, stdout, stderr = client.exec_command(command, timeout=15)
    try:
        pid_line = stdout.readline().strip()
        if not pid_line:
            detail = " ".join(stderr.read(8192).decode("utf-8", errors="replace").split())
            raise ExternalTargetError(
                f"remote Worker did not return a process id: {detail or 'no output'}"
            )
        return int(pid_line)
    except ValueError as error:
        raise ExternalTargetError("remote Worker returned an invalid process id") from error
    finally:
        # The Worker is detached with all stdio redirected. Closing only this
        # command channel must not close the separate SSH transport/tunnel.
        stdout.channel.close()


def _relay(channel: Any, local_host: str, local_port: int) -> None:
    try:
        with socket.create_connection((local_host, local_port), timeout=10) as local:
            # Forward whichever side is ready. Alternating blocking reads adds
            # a one-second delay per response chunk and can make a larger claim
            # response hit the Worker's 30-second HTTP timeout after the API has
            # already committed the lease.
            while not channel.closed:
                readable, _, _ = select.select([channel, local], [], [], 1)
                if channel in readable:
                    data = channel.recv(65536)
                    if not data:
                        break
                    local.sendall(data)
                if local in readable:
                    data = local.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
    except OSError:
        pass
    finally:
        channel.close()


def _serve_reverse_tunnel(deployment: RemoteWorkerDeployment, local_port: int) -> None:
    if deployment.client is None:
        return
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


def _worker_api_endpoint(settings: Settings, transport: Any) -> tuple[str, int | None, str]:
    """Choose a restart-safe direct endpoint when one was explicitly configured."""

    if settings.remote_worker_api_url is not None:
        return str(settings.remote_worker_api_url).rstrip("/"), None, "direct"
    remote_port = _remote_port(transport)
    return f"http://127.0.0.1:{remote_port}", remote_port, "reverse-tunnel"


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

        worker_api_url, remote_port, transport_mode = _worker_api_endpoint(settings, transport)
        worker_id = f"remote-{canonical_digest({'target': target.id})[-16:]}"
        deployment = RemoteWorkerDeployment(
            target.id,
            client if transport_mode == "reverse-tunnel" else None,
            remote_port,
            worker_id,
            transport=transport_mode,
        )
        if transport_mode == "reverse-tunnel":
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
                    "for pid in $(pgrep -f "
                    f"{shlex.quote('[l]ooper_worker.main.*--worker-id ' + worker_id)} "
                    "2>/dev/null || true); do kill \"$pid\" 2>/dev/null || true; done; "
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
        worker_command = (
            f"env PYTHONPATH={shlex.quote(python_path)} "
            f"LOOPER_REPOSITORY_ROOT={remote_root}/source "
            f"LOOPER_LOCAL_WORKER_TOKEN=\"$(cat {remote_root}/worker.token)\" "
            f"{remote_root}/venv/bin/python -m looper_worker.main "
            f"--api-url {shlex.quote(worker_api_url)} "
            f"--worker-id {shlex.quote(worker_id)} "
            f"--target-id {shlex.quote(target.id)} "
            f"--work-dir {remote_root}/work"
        )
        detached = (
            f"cd {remote_root}/source && nohup {worker_command} "
            f">{remote_root}/worker.log 2>&1 </dev/null & "
            f"pid=$!; printf '%s\\n' \"$pid\" >{remote_root}/worker.pid; "
            "printf '%s\\n' \"$pid\""
        )
        launch = f"sh -c {shlex.quote(detached)}"
        remote_pid = _launch_background(client, launch)

        if transport_mode == "direct":
            # The Worker now owns its reconnect loop. The request-scoped SSH
            # session is no longer needed and no credential is retained.
            client.close()

        with _deployments_lock:
            previous = _deployments.pop(target.id, None)
            if previous is not None:
                previous.close()
            _deployments[target.id] = deployment
        return {
            "status": "deploying",
            "workerId": worker_id,
            "remotePid": remote_pid,
            "transport": transport_mode,
            "restartSafe": transport_mode == "direct",
            "deployedAt": utc_now().isoformat(),
        }
    except Exception:
        if deployment is not None:
            deployment.close()
            if deployment.client is None:
                client.close()
        else:
            client.close()
        raise


def deployment_status(target_id: str) -> dict[str, Any]:
    with _deployments_lock:
        deployment = _deployments.get(target_id)
        tunnel_active = bool(
            deployment
            and deployment.client
            and deployment.client.get_transport()
            and deployment.client.get_transport().is_active()
        )
        return {
            "active": tunnel_active,
            "workerId": deployment.worker_id if deployment else None,
            "remotePort": deployment.remote_port if tunnel_active and deployment else None,
            "transport": deployment.transport if deployment else None,
            "restartSafe": bool(deployment and deployment.transport == "direct"),
        }
