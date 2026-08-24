from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from looper_core.fingerprint import system_fingerprint


def worker_fingerprint() -> dict[str, Any]:
    return system_fingerprint()


def read_os_release() -> dict[str, str]:
    """Read /etc/os-release into a plain dictionary (empty on non-Linux)."""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip():
            values[key.strip()] = value.strip().strip('"')
    return values


def passwordless_sudo() -> bool:
    """True when ``sudo -n true`` succeeds without prompting for a password."""
    if platform.system() != "Linux" or shutil.which("sudo") is None:
        return False
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            check=False,
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def systemd_running() -> bool:
    """True when systemd is the running init and systemctl is available."""
    return Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None


def host_capabilities() -> list[str]:
    """Probe host facts that Benchmark manifests may declare as host prerequisites.

    The vocabulary mirrors what the SSH discovery probe reports from the
    control plane: OS family, architecture, distribution id/version, systemd,
    root (the worker process runs as uid 0 when the deployment elevated via
    passwordless sudo, so root covers that case), passwordless sudo, and the
    presence of perf/perl/python3 tooling.
    """
    capabilities = {
        platform.system().lower(),
        platform.machine().lower(),
    }
    if platform.system() == "Linux":
        release = read_os_release()
        distro_id = (release.get("ID") or "").casefold()
        version_id = (release.get("VERSION_ID") or "").strip()
        if distro_id:
            capabilities.add(distro_id)
            if version_id:
                capabilities.add(f"{distro_id}-{version_id}")
        if systemd_running():
            capabilities.add("systemd")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            capabilities.add("root")
        elif passwordless_sudo():
            capabilities.add("sudo")
        if shutil.which("perf"):
            capabilities.add("perf")
        if shutil.which("perl"):
            capabilities.add("perl")
    return sorted(capabilities)


def worker_capabilities() -> list[str]:
    capabilities = {
        "python",
        "local-process",
        *host_capabilities(),
    }
    from looper_worker.runner import container_runtime_available

    if container_runtime_available():
        capabilities.update(
            {
                "container",
                "placement.isolated-container",
                "network.none",
                "storage.workspace",
                "evidence.looper.system-fingerprint/v1alpha1",
            }
        )
    if shutil.which("sysbench"):
        capabilities.add("sysbench")
    return sorted(capabilities)
