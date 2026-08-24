from __future__ import annotations

import os
from unittest.mock import Mock

import httpx
import pytest
from looper_api.config import Settings
from looper_api.remote_worker import _worker_api_endpoint
from looper_worker import main as worker_main


def test_remote_worker_uses_restart_safe_direct_endpoint(tmp_path) -> None:
    transport = Mock()
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        remote_worker_api_url="http://10.0.0.2:8000",
    )

    endpoint, remote_port, mode = _worker_api_endpoint(settings, transport)

    assert endpoint == "http://10.0.0.2:8000"
    assert remote_port is None
    assert mode == "direct"
    transport.request_port_forward.assert_not_called()
    assert "10.0.0.2" in settings.trusted_host_list


def test_remote_worker_keeps_reverse_tunnel_as_compatible_fallback(tmp_path) -> None:
    transport = Mock()
    transport.request_port_forward.return_value = 32123
    settings = Settings(_env_file=None, data_dir=tmp_path)

    endpoint, remote_port, mode = _worker_api_endpoint(settings, transport)

    assert endpoint == "http://127.0.0.1:32123"
    assert remote_port == 32123
    assert mode == "reverse-tunnel"


def test_empty_remote_worker_api_url_is_treated_as_unset(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        remote_worker_api_url="   ",
    )

    assert settings.remote_worker_api_url is None


def test_worker_loads_local_token_from_dotenv(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "LOOPER_LOCAL_WORKER_TOKEN=shared-development-token\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOOPER_ENV_FILE", raising=False)
    monkeypatch.delenv("LOOPER_LOCAL_WORKER_TOKEN", raising=False)

    worker_main._load_worker_environment()

    assert os.environ["LOOPER_LOCAL_WORKER_TOKEN"] == "shared-development-token"


def test_worker_reregisters_after_control_plane_connection_loss(tmp_path, monkeypatch) -> None:
    class FakeClient:
        registrations = 0

        def __init__(self, *_args) -> None:
            pass

        def register(self, **_kwargs) -> None:
            self.registrations += 1
            if self.registrations == 2:
                raise KeyboardInterrupt

        def claim(self) -> None:
            raise httpx.ConnectError("control plane restarted")

        def close(self) -> None:
            pass

    client = FakeClient()
    monkeypatch.setattr(worker_main, "ControlPlaneClient", lambda *_args: client)
    monkeypatch.setattr(worker_main, "LocalAttemptRunner", Mock)
    monkeypatch.setattr(worker_main, "cleanup_orphan_processes", lambda _path: None)
    monkeypatch.setattr(worker_main, "worker_capabilities", lambda: ["process"])
    monkeypatch.setattr(worker_main, "worker_fingerprint", lambda: {})
    monkeypatch.setattr(worker_main.time, "sleep", lambda _seconds: None)

    with pytest.raises(KeyboardInterrupt):
        worker_main.run_worker(
            "http://10.0.0.2:8000",
            "token",
            "remote-worker",
            tmp_path,
            target_ids=["external:machine"],
        )

    assert client.registrations == 2


def test_remote_login_identity_elevates_with_passwordless_sudo(monkeypatch) -> None:
    from looper_api import remote_worker

    def fake_run(client, command, *, timeout=300):
        if "id -u" in command:
            return "1000" + chr(10)
        if "id -g" in command:
            return "1000" + chr(10)
        return "1" + chr(10)

    monkeypatch.setattr(remote_worker, "_run", fake_run)
    elevate, uid, gid = remote_worker._remote_login_identity(Mock())
    assert elevate is True
    assert uid == "1000"
    assert gid == "1000"


def test_remote_login_identity_root_account_stays_root(monkeypatch) -> None:
    from looper_api import remote_worker

    def fake_run(client, command, *, timeout=300):
        if "id -u" in command:
            return "0" + chr(10)
        if "id -g" in command:
            return "0" + chr(10)
        raise AssertionError(f"sudo probe must not run for root: {command}")

    monkeypatch.setattr(remote_worker, "_run", fake_run)
    elevate, uid, gid = remote_worker._remote_login_identity(Mock())
    assert elevate is False
    assert uid == "0"


def test_remote_login_identity_without_sudo_stays_unprivileged(monkeypatch) -> None:
    from looper_api import remote_worker

    def fake_run(client, command, *, timeout=300):
        if "id -u" in command:
            return "1000" + chr(10)
        if "id -g" in command:
            return "1000" + chr(10)
        return "0" + chr(10)

    monkeypatch.setattr(remote_worker, "_run", fake_run)
    elevate, uid, gid = remote_worker._remote_login_identity(Mock())
    assert elevate is False
    assert uid == "1000"


def test_ensure_remote_python_installs_when_no_candidate(monkeypatch) -> None:
    from looper_api import remote_worker

    def fake_run(client, command, *, timeout=300):
        if command.startswith("for p in python3.13"):
            raise remote_worker.ExternalTargetError("no candidate on PATH")
        return "python3.12" + chr(10)

    monkeypatch.setattr(remote_worker, "_run", fake_run)
    assert remote_worker._ensure_remote_python(Mock(), "") == "python3.12"


def test_install_remote_python_uses_deadsnakes_fallback(monkeypatch) -> None:
    from looper_api import remote_worker

    commands: list[str] = []

    def fake_run(client, command, *, timeout=300):
        commands.append(command)
        return "python3.12" + chr(10)

    monkeypatch.setattr(remote_worker, "_run", fake_run)
    assert remote_worker._install_remote_python(Mock(), "sudo -n ") == "python3.12"
    install = next(
        command for command in commands if "apt-get install -y -qq python3.12" in command
    )
    assert "add-apt-repository -y ppa:deadsnakes/ppa >/dev/null" in install
    assert "software-properties-common >/dev/null" in install
    assert "python3.12 python3.12-venv >/dev/null" in install


def test_ensure_remote_venv_recreates_below_floor_venv(monkeypatch) -> None:
    from pathlib import PurePosixPath

    from looper_api import remote_worker

    calls: list[str] = []
    state = {"checks": 0}

    def fake_run(client, command, *, timeout=300):
        calls.append(command)
        if command.startswith("/root/.looper-worker/venv/bin/python -c"):
            state["checks"] += 1
            if state["checks"] == 1:
                raise remote_worker.ExternalTargetError("bad interpreter")
        return ""

    monkeypatch.setattr(remote_worker, "_run", fake_run)
    remote_worker._ensure_remote_venv(
        Mock(), PurePosixPath("/root/.looper-worker"), "python3.12", ""
    )
    assert any("rm -rf /root/.looper-worker/venv" in call for call in calls)
    assert any("python3.12 -m venv /root/.looper-worker/venv" in call for call in calls)
    assert state["checks"] == 2


def test_ensure_remote_venv_keeps_compliant_venv(monkeypatch) -> None:
    from pathlib import PurePosixPath

    from looper_api import remote_worker

    calls: list[str] = []

    def fake_run(client, command, *, timeout=300):
        calls.append(command)
        return ""

    monkeypatch.setattr(remote_worker, "_run", fake_run)
    remote_worker._ensure_remote_venv(
        Mock(), PurePosixPath("/root/.looper-worker"), "python3.12", ""
    )
    assert len(calls) == 1
    assert not any("rm -rf" in call for call in calls)


def test_ensure_remote_venv_installs_venv_package_when_missing(monkeypatch) -> None:
    from pathlib import PurePosixPath

    from looper_api import remote_worker

    calls: list[str] = []
    state = {"creates": 0, "ready": False}

    def fake_run(client, command, *, timeout=300):
        calls.append(command)
        if command.startswith("/root/.looper-worker/venv/bin/python -c"):
            if not state["ready"]:
                raise remote_worker.ExternalTargetError("venv not ready")
            return ""
        if command.endswith("python3.12 -m venv /root/.looper-worker/venv"):
            state["creates"] += 1
            if state["creates"] == 1:
                raise remote_worker.ExternalTargetError("venv package missing")
            state["ready"] = True
        return ""

    monkeypatch.setattr(remote_worker, "_run", fake_run)
    remote_worker._ensure_remote_venv(
        Mock(), PurePosixPath("/root/.looper-worker"), "python3.12", ""
    )
    assert any("apt-get install -y -qq python3.12-venv" in call for call in calls)
    assert state["creates"] == 2