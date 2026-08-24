"""M3 动态相位观察窗口与 O0 负载产物解析（workload-tuning.md D0/D1/D6 第二项）。

O0 边界（SO-D020）：本模块只**解析**外部负载自身产物（stress-ng YAML /
fio JSON / iperf3 JSON），绝不启动负载。解析结果保留逐 stressor / 逐 job
的原始事实（values 列表），聚合统计归 workload 合同的 aggregation 字段管，
本层不做派生。

观察窗口（ObservationWindow）：动态相位的基本观测单位。组装时用
``same_load`` 校验本窗口负载身份与合同一致，漂移即 fail-closed 抛
:class:`WorkloadIdentityDrift`（由 D4 ``identity_drift_policy`` 消费）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import ComponentMetricSnapshot
from looper_core.system_opt.workload import (
    LoadCommandIdentity,
    WorkloadContract,
    same_load,
)

OBSERVATION_WINDOW_SCHEMA = "looper.observation-window/v1alpha1"

# O0 指标登记：metric_id -> (tool, 提取路径)。新增指标必须在此登记，
# 未登记的 metric_id 一律 fail-closed（与公式登记表同纪律）。
_STRESS_NG_FIELDS = {
    "stress-ng.bogo-ops": "bogo-ops",
    "stress-ng.bogo-ops-per-second-usr-sys-time": "bogo-ops-per-second-usr-sys-time",
    "stress-ng.bogo-ops-per-second-real-time": "bogo-ops-per-second-real-time",
    "stress-ng.cpu-usage-per-instance": "cpu-usage-per-instance",
}
_FIO_READ_PERCENTILE = "99.000000"
_FIO_FIELDS = {
    "fio.read-iops": ("read", "iops"),
    "fio.read-bw-bytes": ("read", "bw_bytes"),
}
_IPERF3_FIELDS = {
    "iperf3.sum-received-bps": ("sum_received", "bits_per_second"),
    "iperf3.seconds": ("sum_received", "seconds"),
    "iperf3.retransmits": ("sum_sent", "retransmits"),
}
# sysbench 输出是纯文本（非 JSON/YAML），故登记 metric_id -> 正则捕获组。
# 数值语义见 research/source-metric-inventory：events_per_sec / latency_* / throughput_mib_s。
_SYSBENCH_FIELDS = {
    "sysbench.events-per-second": r"events per second:\s+([0-9.]+)",
    "sysbench.total-events": r"total number of events:\s+(\d+)",
    "sysbench.total-time-seconds": r"total time:\s+([0-9.]+)s",
    "sysbench.latency-avg-ms": r"avg:\s+([0-9.]+)",
    "sysbench.latency-p95-ms": r"95th percentile:\s+([0-9.]+)",
    "sysbench.latency-max-ms": r"max:\s+([0-9.]+)",
    "sysbench.throughput-mib-per-sec": r"([0-9.]+)\s+MiB/sec",
}


class O0ParseError(ValueError):
    """Raised when a load-tool output cannot yield a declared O0 metric."""


class WorkloadIdentityDrift(Exception):
    """Window load identity does not match the contract (SO-D020 exact match)."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            f"workload identity drift: contract load digest {expected} but "
            f"window load digest {actual}; the evidence chain for this "
            "contract does not cover this window (D4 identity_drift_policy)"
        )
        self.expected = expected
        self.actual = actual


def _require_float(value: Any, metric_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise O0ParseError(f"{metric_id}: expected a numeric field, got {type(value).__name__}")
    return float(value)


def _parse_stress_ng(metric_ids: list[str], raw: str) -> dict[str, list[float]]:
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise O0ParseError("stress-ng YAML output is invalid") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("metrics"), list):
        raise O0ParseError("stress-ng YAML output has no metrics list")
    entries = payload["metrics"]
    if not entries:
        raise O0ParseError("stress-ng YAML metrics list is empty")
    result: dict[str, list[float]] = {}
    for metric_id in metric_ids:
        field_name = _STRESS_NG_FIELDS.get(metric_id)
        if field_name is None:
            raise O0ParseError(
                f"unregistered O0 metric '{metric_id}' for tool stress-ng"
            )
        values: list[float] = []
        for entry in entries:
            if field_name not in entry:
                raise O0ParseError(
                    f"{metric_id}: stress-ng metrics entry lacks '{field_name}'"
                )
            values.append(_require_float(entry[field_name], metric_id))
        result[metric_id] = values
    return result


def _parse_fio(metric_ids: list[str], raw: str) -> dict[str, list[float]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise O0ParseError("fio JSON output is invalid") from error
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or not jobs:
        raise O0ParseError("fio JSON output has no jobs")
    result: dict[str, list[float]] = {}
    for metric_id in metric_ids:
        if metric_id == "fio.read-clat-p99-ns":
            values: list[float] = []
            for job in jobs:
                read = job.get("read") or {}
                percentiles = (read.get("clat_ns") or {}).get("percentile") or {}
                if _FIO_READ_PERCENTILE not in percentiles:
                    raise O0ParseError(
                        f"{metric_id}: read.clat_ns.percentile lacks "
                        f"'{_FIO_READ_PERCENTILE}'"
                    )
                values.append(
                    _require_float(percentiles[_FIO_READ_PERCENTILE], metric_id)
                )
            result[metric_id] = values
            continue
        path = _FIO_FIELDS.get(metric_id)
        if path is None:
            raise O0ParseError(f"unregistered O0 metric '{metric_id}' for tool fio")
        section, field_name = path
        values = []
        for job in jobs:
            section_data = job.get(section) or {}
            if field_name not in section_data:
                raise O0ParseError(f"{metric_id}: job {section} lacks '{field_name}'")
            values.append(_require_float(section_data[field_name], metric_id))
        result[metric_id] = values
    return result


def _parse_iperf3(metric_ids: list[str], raw: str) -> dict[str, list[float]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise O0ParseError("iperf3 JSON output is invalid") from error
    end = payload.get("end") if isinstance(payload, dict) else None
    if not isinstance(end, dict):
        raise O0ParseError("iperf3 JSON output has no end section")
    result: dict[str, list[float]] = {}
    for metric_id in metric_ids:
        path = _IPERF3_FIELDS.get(metric_id)
        if path is None:
            raise O0ParseError(f"unregistered O0 metric '{metric_id}' for tool iperf3")
        section, field_name = path
        section_data = end.get(section) or {}
        if field_name not in section_data:
            raise O0ParseError(f"{metric_id}: end.{section} lacks '{field_name}'")
        result[metric_id] = [_require_float(section_data[field_name], metric_id)]
    return result


def _parse_sysbench(metric_ids: list[str], raw: str) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    for metric_id in metric_ids:
        pattern = _SYSBENCH_FIELDS.get(metric_id)
        if pattern is None:
            raise O0ParseError(
                f"unregistered O0 metric '{metric_id}' for tool sysbench"
            )
        match = re.search(pattern, raw)
        if match is None:
            raise O0ParseError(f"{metric_id}: sysbench output has no matching field")
        result[metric_id] = [_require_float(float(match.group(1)), metric_id)]
    return result


_O0_PARSERS = {
    "stress-ng": _parse_stress_ng,
    "fio": _parse_fio,
    "iperf3": _parse_iperf3,
    "sysbench": _parse_sysbench,
}


def parse_o0_metrics(tool: str, metric_ids: list[str], raw: str) -> dict[str, list[float]]:
    """Parse one external load output into the declared O0 metrics.

    Values stay raw per-stressor / per-job facts; aggregation belongs to the
    workload contract, never to this layer.
    """

    parser = _O0_PARSERS.get(tool)
    if parser is None:
        raise O0ParseError(f"no O0 parser registered for tool '{tool}'")
    if not metric_ids:
        raise O0ParseError("O0 metric id list must not be empty")
    return parser(metric_ids, raw)


def _raw_digest(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class O0Observation(StrictModel):
    metric_id: str = Field(min_length=1, max_length=160)
    values: list[float] = Field(min_length=1)
    raw_output_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ObservationWindow(StrictModel):
    """One dynamic-phase observation unit (workload-tuning.md D1)."""

    schema_version: Literal[OBSERVATION_WINDOW_SCHEMA] = OBSERVATION_WINDOW_SCHEMA
    window_id: str = Field(min_length=1, max_length=160)
    phase_id: str = Field(min_length=1, max_length=120)
    workload_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    load_command: LoadCommandIdentity
    o0: list[O0Observation] = Field(min_length=1)
    o1: list[ComponentMetricSnapshot] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime

    @field_validator("o0")
    @classmethod
    def unique_metric_ids(cls, observations: list[O0Observation]) -> list[O0Observation]:
        ids = [item.metric_id for item in observations]
        if len(ids) != len(set(ids)):
            raise ValueError("observation window o0 metric ids must be unique")
        return observations

    @model_validator(mode="after")
    def require_aware_ordered_times(self) -> ObservationWindow:
        for name, value in (("started_at", self.started_at), ("finished_at", self.finished_at)):
            if value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def record_window(
    contract: WorkloadContract,
    *,
    window_id: str,
    phase_id: str,
    load_command: LoadCommandIdentity,
    o0_raw: str,
    o1: list[ComponentMetricSnapshot] | None = None,
    started_at: datetime,
    finished_at: datetime,
) -> ObservationWindow:
    """Assemble one observation window from externally captured evidence.

    The load output ``o0_raw`` is produced by the test side (SO-D020); this
    function only parses it and binds identities. Identity drift against the
    contract fails closed.
    """

    phase = next((item for item in contract.phases if item.phase_id == phase_id), None)
    if phase is None:
        raise ValueError(
            f"phase '{phase_id}' is not declared by workload contract "
            f"'{contract.workload_id}'"
        )
    if not same_load(contract.load_command, load_command):
        raise WorkloadIdentityDrift(
            expected=contract.load_command.identity_digest,
            actual=load_command.identity_digest,
        )
    parsed = parse_o0_metrics(
        contract.load_command.tool, list(phase.o0_metric_ids), o0_raw
    )
    raw_digest = _raw_digest(o0_raw)
    return ObservationWindow(
        window_id=window_id,
        phase_id=phase_id,
        workload_contract_digest=contract.digest,
        load_command=load_command,
        o0=[
            O0Observation(
                metric_id=metric_id,
                values=values,
                raw_output_digest=raw_digest,
            )
            for metric_id, values in parsed.items()
        ],
        o1=list(o1 or []),
        started_at=started_at,
        finished_at=finished_at,
    )


__all__ = [
    "O0Observation",
    "O0ParseError",
    "OBSERVATION_WINDOW_SCHEMA",
    "ObservationWindow",
    "WorkloadIdentityDrift",
    "parse_o0_metrics",
    "record_window",
]
