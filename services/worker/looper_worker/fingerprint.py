from __future__ import annotations

import platform
import shutil
from typing import Any

from looper_core.fingerprint import system_fingerprint


def worker_fingerprint() -> dict[str, Any]:
    return system_fingerprint()


def worker_capabilities() -> list[str]:
    capabilities = {
        "python",
        "local-process",
        platform.system().lower(),
        platform.machine().lower(),
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
