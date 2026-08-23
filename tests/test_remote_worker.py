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
