from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil


def _read_text(path: str | Path, *, maximum_bytes: int = 1024 * 1024) -> str | None:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) > maximum_bytes:
        raw = raw[:maximum_bytes]
    return raw.decode("utf-8", errors="replace").strip()


def _command_output(argv: list[str], *, timeout: float = 2.0) -> str | None:
    if not argv or shutil.which(argv[0]) is None:
        return None
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _parse_cpuinfo(text: str | None) -> dict[str, Any]:
    if not text:
        return {"model_name": None, "microcode": None, "flags": []}
    first: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() and first:
            break
        key, separator, value = line.partition(":")
        if separator:
            first[key.strip().lower()] = value.strip()
    flags = first.get("flags") or first.get("features") or ""
    return {
        "model_name": first.get("model name") or first.get("hardware"),
        "microcode": first.get("microcode"),
        "flags": sorted(set(flags.split())),
    }


def _lscpu() -> dict[str, str]:
    raw = _command_output(["lscpu", "--json"])
    if not raw:
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    rows = document.get("lscpu") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return {}
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        field = str(row.get("field", "")).strip().rstrip(":")
        data = row.get("data")
        if field and data is not None:
            result[field] = str(data).strip()
    return result


def _service_state(name: str) -> str | None:
    if platform.system() != "Linux":
        return None
    output = _command_output(["systemctl", "is-active", name])
    return output.splitlines()[0] if output else None


def _runtime_version(executable: str) -> str | None:
    if executable == "docker":
        return _command_output([executable, "--version"])
    return _command_output([executable, "--version"])


def _linux_controls() -> dict[str, Any]:
    if platform.system() != "Linux":
        return {
            "cpu_governor": None,
            "energy_performance_preference": None,
            "smt_active": None,
            "transparent_hugepages": None,
            "tuned": None,
            "power_profiles_daemon": None,
            "tlp": None,
        }
    return {
        "cpu_governor": _read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "energy_performance_preference": _read_text(
            "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"
        ),
        "smt_active": _read_text("/sys/devices/system/cpu/smt/active"),
        "transparent_hugepages": _read_text("/sys/kernel/mm/transparent_hugepage/enabled"),
        "tuned": _service_state("tuned"),
        "power_profiles_daemon": _service_state("power-profiles-daemon"),
        "tlp": _service_state("tlp"),
    }


def _cgroup_snapshot() -> dict[str, Any]:
    if platform.system() != "Linux":
        return {"version": None, "membership": None, "cpu_max": None, "memory_max": None}
    version = "v2" if Path("/sys/fs/cgroup/cgroup.controllers").exists() else "v1"
    return {
        "version": version,
        "membership": _read_text("/proc/self/cgroup"),
        "cpu_max": _read_text("/sys/fs/cgroup/cpu.max") if version == "v2" else None,
        "memory_max": _read_text("/sys/fs/cgroup/memory.max") if version == "v2" else None,
    }


def _network_snapshot() -> list[dict[str, Any]]:
    stats = psutil.net_if_stats()
    return [
        {
            "name": name,
            "is_up": item.isup,
            "mtu": item.mtu,
            "speed_mbps": item.speed if item.speed >= 0 else None,
            "duplex": int(item.duplex),
        }
        for name, item in sorted(stats.items())
    ]


def _storage_snapshot() -> list[dict[str, Any]]:
    partitions: list[dict[str, Any]] = []
    for item in psutil.disk_partitions(all=False):
        partitions.append(
            {
                "device": item.device,
                "mountpoint": item.mountpoint,
                "filesystem": item.fstype,
                "options": sorted(option for option in item.opts.split(",") if option),
            }
        )
    return sorted(partitions, key=lambda item: (item["mountpoint"], item["device"]))


def system_fingerprint() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu_frequency = psutil.cpu_freq()
    cpuinfo = _parse_cpuinfo(_read_text("/proc/cpuinfo"))
    lscpu = _lscpu() if platform.system() == "Linux" else {}
    numa_nodes = (
        sorted(path.name for path in Path("/sys/devices/system/node").glob("node[0-9]*"))
        if platform.system() == "Linux"
        else []
    )
    return {
        "schema_version": "looper.system-fingerprint/v1alpha1",
        "hostname": platform.node(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "kernel_version": platform.version(),
        "boot_command_line": _read_text("/proc/cmdline") if platform.system() == "Linux" else None,
        "machine": platform.machine(),
        "virtualization": _command_output(["systemd-detect-virt"])
        if platform.system() == "Linux"
        else None,
        "processor": platform.processor() or cpuinfo["model_name"],
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "memory_bytes": int(memory.total),
        "cpu": {
            "model_name": cpuinfo["model_name"] or lscpu.get("Model name"),
            "microcode": cpuinfo["microcode"],
            "flags": cpuinfo["flags"],
            "logical_count": psutil.cpu_count(logical=True),
            "physical_count": psutil.cpu_count(logical=False),
            "online": _read_text("/sys/devices/system/cpu/online"),
            "sockets": lscpu.get("Socket(s)"),
            "cores_per_socket": lscpu.get("Core(s) per socket"),
            "threads_per_core": lscpu.get("Thread(s) per core"),
            "byte_order": lscpu.get("Byte Order"),
            "frequency_min_mhz": cpu_frequency.min if cpu_frequency else None,
            "frequency_max_mhz": cpu_frequency.max if cpu_frequency else None,
            "caches": {
                "l1d": lscpu.get("L1d cache"),
                "l1i": lscpu.get("L1i cache"),
                "l2": lscpu.get("L2 cache"),
                "l3": lscpu.get("L3 cache"),
            },
            "numa_nodes": numa_nodes,
            "numa_node_count": len(numa_nodes) if numa_nodes else None,
        },
        "controls": _linux_controls(),
        "memory": {
            "total_bytes": int(memory.total),
            "swap_total_bytes": int(swap.total),
        },
        "cgroup": _cgroup_snapshot(),
        "network_interfaces": _network_snapshot(),
        "storage": _storage_snapshot(),
        "runtime": {
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "docker": _runtime_version("docker"),
            "containerd": _runtime_version("containerd"),
        },
    }
