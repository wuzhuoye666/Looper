"""L4 采集器：在压力器构建的负载下按组件采集指标快照。

架构层：总体架构 v2 的 L4（见 docs/system-optimizer/architecture/overall.md）。
为 S1 基线校准、S4 组件优先级、S6 改善量供数；不做判定、不评价收益。

Guest 盲区契约：CVM 内不可读的指标必须显式标记 ``unavailable`` 并携带
``unavailable_reason``，值保持 ``null``；绝不把不可读当 0、当缺失或当猜测值。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel

COLLECTOR_SCHEMA = "looper.component-metric-snapshot/v1alpha1"
_SUPPORTED_COMPONENTS = frozenset({"cpu", "memory", "storage", "network", "numa"})


class MetricAvailability(StrEnum):
    READABLE = "readable"
    UNAVAILABLE = "unavailable"


class CollectedMetric(StrictModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9.-]*$")
    unit: str = Field(min_length=1, max_length=40)
    value: float | None = None
    availability: MetricAvailability
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=500)
    source: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_availability_contract(self) -> CollectedMetric:
        if self.availability == MetricAvailability.READABLE:
            if self.value is None or not isfinite(self.value):
                raise ValueError("readable metrics require a finite value")
            if self.unavailable_reason is not None:
                raise ValueError("readable metrics must not carry an unavailable reason")
        else:
            if self.value is not None:
                raise ValueError("unavailable metrics must keep the value null")
            if self.unavailable_reason is None:
                raise ValueError("unavailable metrics require an explicit reason")
        return self


class ComponentMetricSnapshot(StrictModel):
    schema_version: Literal[COLLECTOR_SCHEMA] = COLLECTOR_SCHEMA
    component: str = Field(min_length=1, max_length=40)
    target_id: str = Field(min_length=1, max_length=160)
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    collected_at: datetime
    metrics: dict[str, CollectedMetric] = Field(min_length=1)
    counting_basis: str = Field(min_length=1, max_length=1000)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def _unavailable(name: str, unit: str, source: str, reason: str) -> CollectedMetric:
    return CollectedMetric(
        name=name,
        unit=unit,
        value=None,
        availability=MetricAvailability.UNAVAILABLE,
        unavailable_reason=reason,
        source=source,
    )


def _readable(name: str, unit: str, source: str, value: float) -> CollectedMetric:
    return CollectedMetric(
        name=name,
        unit=unit,
        value=value,
        availability=MetricAvailability.READABLE,
        source=source,
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _cpu_busy_ratio(first: str, second: str) -> tuple[float | None, str]:
    def cpu_total_idle(text: str) -> tuple[float, float] | None:
        for line in text.splitlines():
            fields = line.split()
            if fields and fields[0] == "cpu":
                values = [float(value) for value in fields[1:6]]
                return sum(values), values[3] + values[4]
        return None

    first_pair = cpu_total_idle(first)
    second_pair = cpu_total_idle(second)
    if first_pair is None or second_pair is None:
        return None, "no aggregate cpu line in /proc/stat samples"
    total_delta = second_pair[0] - first_pair[0]
    idle_delta = second_pair[1] - first_pair[1]
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        return None, "zero or invalid tick delta between /proc/stat samples"
    return 1.0 - idle_delta / total_delta, ""


def _collect_cpu(proc_root: Path, sys_root: Path, interval_seconds: float) -> dict[str, CollectedMetric]:
    metrics: dict[str, CollectedMetric] = {}
    first = _read_text(proc_root / "stat")
    if first is not None and interval_seconds > 0:
        time.sleep(interval_seconds)
    second = _read_text(proc_root / "stat")
    if first is None or second is None:
        metrics["cpu.busy-ratio"] = _unavailable(
            "cpu.busy-ratio",
            "ratio",
            f"{proc_root}/stat (two samples, {interval_seconds}s)",
            f"{proc_root}/stat is unreadable in this environment",
        )
    else:
        ratio, reason = _cpu_busy_ratio(first, second)
        source = f"{proc_root}/stat (two samples, {interval_seconds}s)"
        if ratio is None:
            metrics["cpu.busy-ratio"] = _unavailable("cpu.busy-ratio", "ratio", source, reason)
        else:
            metrics["cpu.busy-ratio"] = _readable("cpu.busy-ratio", "ratio", source, ratio)
        online = sum(
            1 for line in second.splitlines() if line.startswith("cpu") and line.split()[0] != "cpu"
        )
        if online:
            metrics["cpu.online-count"] = _readable(
                "cpu.online-count", "count", f"{proc_root}/stat", float(online)
            )
        else:
            metrics["cpu.online-count"] = _unavailable(
                "cpu.online-count", "count", f"{proc_root}/stat",
                "no per-cpu lines in /proc/stat",
            )
    event_sources = sorted((sys_root / "bus" / "event_source" / "devices").glob("*"))
    pmu_source = f"{sys_root}/bus/event_source/devices"
    if any(entry.is_dir() for entry in event_sources):
        metrics["cpu.pmu-event-sources"] = _readable(
            "cpu.pmu-event-sources", "count", pmu_source, float(len(event_sources))
        )
    else:
        metrics["cpu.pmu-event-sources"] = _unavailable(
            "cpu.pmu-event-sources", "count", pmu_source,
            "no /sys/bus/event_source/devices entries visible in guest (PMU not passed through)",
        )
    paranoid_text = _read_text(proc_root / "sys" / "kernel" / "perf_event_paranoid")
    paranoid_source = f"{proc_root}/sys/kernel/perf_event_paranoid"
    if paranoid_text is None or not paranoid_text.strip().lstrip("-").isdigit():
        metrics["cpu.perf-event-paranoid"] = _unavailable(
            "cpu.perf-event-paranoid", "level", paranoid_source,
            "perf_event_paranoid is unreadable in this environment",
        )
    else:
        metrics["cpu.perf-event-paranoid"] = _readable(
            "cpu.perf-event-paranoid", "level", paranoid_source,
            float(paranoid_text.strip()),
        )
    return metrics


def _collect_memory(proc_root: Path) -> dict[str, CollectedMetric]:
    source = f"{proc_root}/meminfo"
    text = _read_text(proc_root / "meminfo")
    if text is None:
        return {
            "memory.available-ratio": _unavailable(
                "memory.available-ratio", "ratio", source,
                f"{source} is unreadable in this environment",
            )
        }
    values: dict[str, float] = {}
    for line in text.splitlines():
        field = line.split(":", 1)
        if len(field) == 2:
            try:
                values[field[0].strip()] = float(field[1].split()[0])
            except (IndexError, ValueError):
                continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return {
            "memory.available-ratio": _unavailable(
                "memory.available-ratio", "ratio", source,
                "MemTotal or MemAvailable missing from /proc/meminfo",
            )
        }
    return {
        "memory.available-ratio": _readable(
            "memory.available-ratio", "ratio", source, available / total
        )
    }


def _collect_network(proc_root: Path) -> dict[str, CollectedMetric]:
    source = f"{proc_root}/net/dev"
    text = _read_text(proc_root / "net" / "dev")
    if text is None:
        reason = f"{source} is unreadable in this environment"
        return {
            "network.rx-bytes-total": _unavailable("network.rx-bytes-total", "bytes", source, reason),
            "network.tx-bytes-total": _unavailable("network.tx-bytes-total", "bytes", source, reason),
        }
    rx = 0.0
    tx = 0.0
    lines = text.splitlines()[2:]
    for line in lines:
        if ":" not in line:
            continue
        interface, rest = line.split(":", 1)
        if interface.strip() == "lo":
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        rx += float(fields[0])
        tx += float(fields[8])
    return {
        "network.rx-bytes-total": _readable("network.rx-bytes-total", "bytes", source, rx),
        "network.tx-bytes-total": _readable("network.tx-bytes-total", "bytes", source, tx),
    }


def _collect_storage(proc_root: Path) -> dict[str, CollectedMetric]:
    source = f"{proc_root}/diskstats"
    text = _read_text(proc_root / "diskstats")
    if text is None:
        reason = f"{source} is unreadable in this environment"
        return {
            metric: _unavailable(metric, unit, source, reason)
            for metric, unit in (
                ("storage.reads-completed-total", "count"),
                ("storage.writes-completed-total", "count"),
                ("storage.io-ms-total", "ms"),
            )
        }
    reads = 0.0
    writes = 0.0
    io_ms = 0.0
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 14:
            continue
        name = fields[2]
        if name.startswith(("loop", "ram", "dm-")):
            continue
        reads += float(fields[3])
        writes += float(fields[7])
        io_ms += float(fields[12])
    return {
        "storage.reads-completed-total": _readable(
            "storage.reads-completed-total", "count", source, reads
        ),
        "storage.writes-completed-total": _readable(
            "storage.writes-completed-total", "count", source, writes
        ),
        "storage.io-ms-total": _readable("storage.io-ms-total", "ms", source, io_ms),
    }


def _collect_numa(sys_root: Path) -> dict[str, CollectedMetric]:
    node_root = sys_root / "devices" / "system" / "node"
    nodes = sorted(path for path in node_root.glob("node[0-9]*") if path.is_dir())
    source = f"{node_root}"
    metrics: dict[str, CollectedMetric] = {}
    if nodes:
        metrics["numa.node-count"] = _readable(
            "numa.node-count", "count", source, float(len(nodes))
        )
        metrics["numa.binding"] = _unavailable(
            "numa.binding", "boolean", source,
            "binding candidates require at least 2 NUMA nodes; single-node target",
        ) if len(nodes) < 2 else _readable("numa.binding", "boolean", source, 1.0)
    else:
        metrics["numa.node-count"] = _unavailable(
            "numa.node-count", "count", source,
            "no node directories visible in guest at /sys/devices/system/node",
        )
        metrics["numa.binding"] = _unavailable(
            "numa.binding", "boolean", source,
            "no NUMA topology visible in guest; binding probes unavailable",
        )
    return metrics


_COLLECTORS = {
    "cpu": lambda proc, sys, interval: _collect_cpu(proc, sys, interval),
    "memory": lambda proc, sys, interval: _collect_memory(proc),
    "network": lambda proc, sys, interval: _collect_network(proc),
    "storage": lambda proc, sys, interval: _collect_storage(proc),
    "numa": lambda proc, sys, interval: _collect_numa(sys),
}

_COUNTING_BASIS = {
    "cpu": "busy ratio from two /proc/stat aggregate samples at the given interval; "
    "PMU availability from /sys/bus/event_source/devices and perf_event_paranoid",
    "memory": "MemAvailable / MemTotal from /proc/meminfo",
    "network": "sum of rx/tx byte counters over all /proc/net/dev interfaces except lo",
    "storage": "sum over /proc/diskstats block devices excluding loop*, ram*, dm-*",
    "numa": "count of /sys/devices/system/node/node[0-9]* directories",
}


def collect_component_snapshot(
    component: str,
    *,
    target_id: str,
    environment_digest: str,
    proc_root: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
    interval_seconds: float = 0.1,
    collected_at: datetime | None = None,
) -> ComponentMetricSnapshot:
    """Collect one component metric snapshot under the current load.

    Unreadable guest sources become explicit ``unavailable`` metrics with a
    reason; they never become zero, missing, or inferred values.
    """

    if component not in _SUPPORTED_COMPONENTS:
        raise ValueError(f"unsupported component: {component}")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    metrics = _COLLECTORS[component](proc_root, sys_root, interval_seconds)
    return ComponentMetricSnapshot(
        component=component,
        target_id=target_id,
        environment_digest=environment_digest,
        collected_at=collected_at or datetime.now(UTC),
        metrics=metrics,
        counting_basis=_COUNTING_BASIS[component],
    )


__all__ = [
    "CollectedMetric",
    "ComponentMetricSnapshot",
    "MetricAvailability",
    "collect_component_snapshot",
]
