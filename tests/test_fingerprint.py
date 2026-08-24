from __future__ import annotations

from types import SimpleNamespace

from looper_core.fingerprint import _parse_cpuinfo, system_fingerprint
from looper_worker.fingerprint import worker_fingerprint


def test_cpuinfo_parser_captures_fairness_fields() -> None:
    parsed = _parse_cpuinfo(
        """processor : 0
model name : Example CPU
microcode : 0x123
flags : sse avx avx2 sse

processor : 1
model name : Example CPU
"""
    )
    assert parsed == {
        "model_name": "Example CPU",
        "microcode": "0x123",
        "flags": ["avx", "avx2", "sse"],
    }


def test_system_fingerprint_has_versioned_fairness_sections() -> None:
    fingerprint = system_fingerprint()
    assert fingerprint["schema_version"] == "looper.system-fingerprint/v1alpha1"
    assert fingerprint["logical_cpu_count"] == fingerprint["cpu"]["logical_count"]
    assert "microcode" in fingerprint["cpu"]
    assert "smt_active" in fingerprint["controls"]
    assert "transparent_hugepages" in fingerprint["controls"]
    assert "version" in fingerprint["cgroup"]
    assert isinstance(fingerprint["network_interfaces"], list)
    assert isinstance(fingerprint["storage"], list)
    assert "docker" in fingerprint["runtime"]


def test_worker_uses_shared_system_fingerprint_contract() -> None:
    fingerprint = worker_fingerprint()
    assert fingerprint["schema_version"] == "looper.system-fingerprint/v1alpha1"
    assert fingerprint["runtime"]["python"] == fingerprint["python"]


def _fake_which(available: set[str]):
    def which(command: str, *args, **kwargs):
        return f"/usr/bin/{command}" if command in available else None
    return which


def test_worker_host_capabilities_ubuntu_22_04_root(monkeypatch) -> None:
    import looper_worker.fingerprint as worker_fingerprint

    monkeypatch.setattr(worker_fingerprint.platform, "system", lambda: "Linux")
    monkeypatch.setattr(worker_fingerprint.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        worker_fingerprint,
        "read_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "22.04"},
    )
    monkeypatch.setattr(worker_fingerprint, "systemd_running", lambda: True)
    monkeypatch.setattr(
        worker_fingerprint.shutil,
        "which",
        _fake_which({"systemctl", "perf", "perl"}),
    )
    monkeypatch.setattr(worker_fingerprint, "os", SimpleNamespace(geteuid=lambda: 0))

    capabilities = set(worker_fingerprint.host_capabilities())
    assert {
        "linux",
        "x86_64",
        "ubuntu",
        "ubuntu-22.04",
        "systemd",
        "root",
        "perf",
        "perl",
    } <= capabilities
    assert "sudo" not in capabilities


def test_worker_host_capabilities_sudo_account_is_not_treated_as_root(monkeypatch) -> None:
    import looper_worker.fingerprint as worker_fingerprint

    monkeypatch.setattr(worker_fingerprint.platform, "system", lambda: "Linux")
    monkeypatch.setattr(worker_fingerprint.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        worker_fingerprint,
        "read_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "22.04"},
    )
    monkeypatch.setattr(worker_fingerprint, "systemd_running", lambda: True)
    monkeypatch.setattr(worker_fingerprint.shutil, "which", _fake_which({"systemctl"}))
    monkeypatch.setattr(worker_fingerprint, "os", SimpleNamespace(geteuid=lambda: 1000))
    monkeypatch.setattr(worker_fingerprint, "passwordless_sudo", lambda: True)

    capabilities = set(worker_fingerprint.host_capabilities())
    assert "sudo" in capabilities
    assert "root" not in capabilities


def test_worker_capabilities_merge_host_facts(monkeypatch) -> None:
    import looper_worker.fingerprint as worker_fingerprint

    monkeypatch.setattr(
        worker_fingerprint,
        "host_capabilities",
        lambda: ["linux", "x86_64", "ubuntu-22.04", "systemd"],
    )
    monkeypatch.setattr(worker_fingerprint.shutil, "which", lambda command: None)

    capabilities = set(worker_fingerprint.worker_capabilities())
    assert "python" in capabilities
    assert "local-process" in capabilities
    assert {"linux", "x86_64", "ubuntu-22.04", "systemd"} <= capabilities
