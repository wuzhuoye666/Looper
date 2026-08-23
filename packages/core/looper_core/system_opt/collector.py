"""L4 component collection under a controlled workload.

L4 records raw facts and explicit unavailability.  It does not build load,
score components, infer tuning benefit, or invent values for guest blind spots.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import time
import zipfile
from threading import Event, Lock, Thread
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

import yaml
from pydantic import Field, field_validator, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.scoring import MeasurementBatch, MetricEvidence

COLLECTOR_SCHEMA = "looper.component-metric-snapshot/v1alpha1"
COLLECTION_PLAN_SCHEMA = "looper.component-collection-plan/v1alpha1"
COLLECTION_REQUEST_SCHEMA = "looper.component-collection-request/v1alpha1"
COLLECTION_RUN_SCHEMA = "looper.component-collection-run/v1alpha1"
COLLECTION_ENVELOPE_SCHEMA = "looper.collection-measurement-envelope/v1alpha1"
COLLECTION_OVERHEAD_SCHEMA = "looper.collection-overhead-ab-evidence/v1alpha1"
COLLECTION_BUNDLE_MANIFEST_SCHEMA = "looper.collection-artifact-bundle-manifest/v1alpha1"
COLLECTION_BUNDLE_MANIFEST_NAME = "manifest.json"
COLLECTION_BUNDLE_MEDIA_TYPE = "application/vnd.looper.collection-artifact-bundle+zip"

ComponentName = Literal["cpu", "memory", "storage", "network", "numa"]
_SUPPORTED_COMPONENTS = frozenset({"cpu", "memory", "storage", "network", "numa"})


class MetricAvailability(StrEnum):
    READABLE = "readable"
    UNAVAILABLE = "unavailable"


class CollectedMetric(StrictModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9.-]*$")
    unit: str = Field(min_length=1, max_length=40)
    value: float | list[float] | None = None
    availability: MetricAvailability
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=500)
    source: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_availability_contract(self) -> CollectedMetric:
        if self.availability == MetricAvailability.READABLE:
            if self.value is None:
                raise ValueError("readable metrics require a finite scalar or non-empty series")
            values = self.value if isinstance(self.value, list) else [self.value]
            if not values or any(not isfinite(value) for value in values):
                raise ValueError("readable metrics require a finite scalar or non-empty series")
            if self.unavailable_reason is not None:
                raise ValueError("readable metrics must not carry an unavailable reason")
        else:
            if self.value is not None:
                raise ValueError("unavailable metrics must keep the value null")
            if self.unavailable_reason is None:
                raise ValueError("unavailable metrics require an explicit reason")
        return self


class ComponentMetricSnapshot(StrictModel):
    """Stable v1alpha1 snapshot shape; validators strengthen its contract only."""

    schema_version: Literal[COLLECTOR_SCHEMA] = COLLECTOR_SCHEMA
    component: ComponentName
    target_id: str = Field(min_length=1, max_length=160)
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    collected_at: datetime
    metrics: dict[str, CollectedMetric] = Field(min_length=1)
    counting_basis: str = Field(min_length=1, max_length=1000)

    @field_validator("collected_at")
    @classmethod
    def require_aware_collected_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collected_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_metric_identity(self) -> ComponentMetricSnapshot:
        prefix = f"{self.component}."
        for key, metric in self.metrics.items():
            if key != metric.name:
                raise ValueError(f"metric map key {key!r} must equal metric.name {metric.name!r}")
            if not metric.name.startswith(prefix):
                raise ValueError(
                    f"metric {metric.name!r} does not belong to component {self.component!r}"
                )
        return self

    @property
    def digest(self) -> str:
        # Do not change serialization flags: existing v1alpha1 artifact digests stay stable.
        return canonical_digest(self.model_dump(mode="json"))


class ComponentCollectionScope(StrictModel):
    """Exact guest resources included in a collection window."""

    network_interfaces: list[str] | None = None
    storage_devices: list[str] | None = None

    @field_validator("network_interfaces", "storage_devices")
    @classmethod
    def validate_explicit_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("an explicit scope list must not be empty")
        if any(not item or item.strip() != item for item in value):
            raise ValueError("scope names must be non-empty and have no surrounding whitespace")
        if len(set(value)) != len(value):
            raise ValueError("scope names must be unique")
        return value


class ComponentCollectionPlan(StrictModel):
    """Artifact-independent L4 window identity known before the workload starts."""

    schema_version: Literal[COLLECTION_PLAN_SCHEMA] = COLLECTION_PLAN_SCHEMA
    component: ComponentName
    target_id: str = Field(min_length=1, max_length=160)
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workload_phase_id: str = Field(min_length=1, max_length=160)
    workload_source: str = Field(min_length=1, max_length=300)
    collector_id: str = Field(min_length=1, max_length=160)
    requested_metrics: list[str] = Field(min_length=1)
    interval_seconds: float = Field(gt=0)
    scope: ComponentCollectionScope

    @field_validator("requested_metrics")
    @classmethod
    def validate_requested_metrics(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("requested_metrics must be unique")
        pattern = re.compile(r"^[a-z][a-z0-9.-]*$")
        if any(pattern.fullmatch(metric) is None for metric in value):
            raise ValueError("requested_metrics contain an invalid metric name")
        return value

    @field_validator("interval_seconds")
    @classmethod
    def require_finite_interval(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("interval_seconds must be finite")
        return value

    @model_validator(mode="after")
    def validate_component_scope(self) -> ComponentCollectionPlan:
        prefix = f"{self.component}."
        if any(not metric.startswith(prefix) for metric in self.requested_metrics):
            raise ValueError("requested_metrics must belong to the requested component")
        network = self.scope.network_interfaces
        storage = self.scope.storage_devices
        if self.component == "network":
            if network is None:
                raise ValueError("network collection requires explicit network_interfaces")
            if storage is not None:
                raise ValueError("network collection cannot carry storage_devices")
        elif self.component == "storage":
            if storage is None:
                raise ValueError("storage collection requires explicit storage_devices")
            if network is not None:
                raise ValueError("storage collection cannot carry network_interfaces")
        elif network is not None or storage is not None:
            raise ValueError(f"{self.component} collection does not accept network/storage scope")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class CollectionArtifactBundleMember(StrictModel):
    """One raw member whose bytes, type, and size are bound by the bundle manifest."""

    path: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(ge=0)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def require_safe_relative_posix_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            value == COLLECTION_BUNDLE_MANIFEST_NAME
            or "\\" in value
            or value.startswith("/")
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != value
        ):
            raise ValueError("bundle member path must be a safe relative POSIX path")
        return value


class CollectionArtifactBundleManifest(StrictModel):
    """Canonical identity of one measure execution; ZIP container bytes are not identity."""

    schema_version: Literal[COLLECTION_BUNDLE_MANIFEST_SCHEMA] = (
        COLLECTION_BUNDLE_MANIFEST_SCHEMA
    )
    members: list[CollectionArtifactBundleMember] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_member_paths(self) -> CollectionArtifactBundleManifest:
        paths = [member.path for member in self.members]
        if len(paths) != len(set(paths)):
            raise ValueError("bundle manifest member paths must be unique")
        return self

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        payload["members"] = sorted(payload["members"], key=lambda item: item["path"])
        return canonical_digest(payload)


@dataclass(frozen=True)
class VerifiedCollectionArtifactBundle:
    """Verified raw bytes indexed by the manifest's exact member paths."""

    manifest: CollectionArtifactBundleManifest
    members: Mapping[str, bytes]
    bundle_bytes: bytes


def verify_collection_artifact_bundle(
    content: bytes, *, expected_digest: str
) -> VerifiedCollectionArtifactBundle:
    """Fail closed unless ZIP members exactly match a canonical, content-hashed manifest."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(content), "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("collection artifact bundle is not a readable ZIP archive") from error
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("collection artifact bundle contains a duplicate ZIP member path")
        for info in infos:
            path = PurePosixPath(info.filename)
            if (
                "\\" in info.filename
                or info.filename.startswith("/")
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or path.as_posix() != info.filename
            ):
                raise ValueError("collection artifact bundle contains an unsafe ZIP member path")
            if info.is_dir():
                raise ValueError("collection artifact bundle must not contain directory entries")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ValueError("collection artifact bundle must not contain symbolic links")
            if info.flag_bits & 0x1:
                raise ValueError("collection artifact bundle must not contain encrypted members")
        if names.count(COLLECTION_BUNDLE_MANIFEST_NAME) != 1:
            raise ValueError("collection artifact bundle requires exactly one manifest.json")
        try:
            manifest = CollectionArtifactBundleManifest.model_validate_json(
                archive.read(COLLECTION_BUNDLE_MANIFEST_NAME)
            )
        except (KeyError, UnicodeDecodeError, ValueError) as error:
            raise ValueError("collection artifact bundle manifest is invalid") from error
        if manifest.digest != expected_digest:
            raise ValueError("collection artifact bundle manifest digest mismatch")
        expected_names = {COLLECTION_BUNDLE_MANIFEST_NAME} | {
            member.path for member in manifest.members
        }
        if set(names) != expected_names:
            raise ValueError(
                "collection artifact bundle ZIP member set does not exactly match manifest"
            )
        verified: dict[str, bytes] = {}
        for member in manifest.members:
            raw = archive.read(member.path)
            if len(raw) != member.size_bytes:
                raise ValueError(
                    f"collection artifact bundle member size mismatch: {member.path}"
                )
            actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            if actual_digest != member.digest:
                raise ValueError(
                    f"collection artifact bundle member digest mismatch: {member.path}"
                )
            verified[member.path] = raw
    return VerifiedCollectionArtifactBundle(
        manifest=manifest,
        members=verified,
        bundle_bytes=content,
    )


_STRESS_NG_MEDIA_TYPE = "application/vnd.stress-ng.metrics+yaml"
_SYSBENCH_MEMORY_MEDIA_TYPE = "text/vnd.sysbench.memory"
_IPERF3_MEDIA_TYPE = "application/vnd.iperf3+json"
_FIO_MEDIA_TYPE = "application/vnd.fio+json"
_SYSBENCH_THROUGHPUT = re.compile(
    r"[0-9.]+ MiB transferred \((?P<rate>[0-9.]+) MiB/sec\)"
)
_SYSBENCH_P95 = re.compile(r"95th percentile:\s+(?P<p95>[0-9.]+)")


def _finite_non_negative(value: object, label: str, *, positive: bool = False) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is not numeric") from error
    if not isfinite(parsed) or parsed < 0 or (positive and parsed <= 0):
        qualifier = "positive finite" if positive else "finite and non-negative"
        raise ValueError(f"{label} must be {qualifier}")
    return parsed


def _parse_stress_ng_cpu(raw: bytes) -> float:
    try:
        payload = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise ValueError("stress-ng YAML is invalid") from error
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    if not isinstance(metrics, list):
        raise ValueError("stress-ng YAML contains no metrics list")
    cpu = next(
        (item for item in metrics if isinstance(item, dict) and item.get("stressor") == "cpu"),
        None,
    )
    if cpu is None:
        raise ValueError("stress-ng YAML contains no cpu metric")
    return _finite_non_negative(
        cpu.get("bogo-ops-per-second-real-time"),
        "stress-ng CPU throughput",
        positive=True,
    )


def _parse_sysbench_memory(raw: bytes) -> tuple[float, float]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("sysbench memory output is not UTF-8") from error
    throughput = _SYSBENCH_THROUGHPUT.search(text)
    latency = _SYSBENCH_P95.search(text)
    if throughput is None or latency is None:
        raise ValueError("sysbench memory output is missing throughput or p95 latency")
    return (
        _finite_non_negative(
            throughput.group("rate"), "sysbench memory throughput", positive=True
        ),
        _finite_non_negative(latency.group("p95"), "sysbench memory p95 latency"),
    )


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} root is not an object")
    return payload


def _parse_iperf3(raw: bytes) -> tuple[float, float]:
    payload = _json_object(raw, "iperf3 output")
    if payload.get("error"):
        raise ValueError(f"iperf3 reported an error: {payload['error']}")
    end = payload.get("end")
    if not isinstance(end, dict):
        raise ValueError("iperf3 JSON contains no end object")
    received = end.get("sum_received")
    sent = end.get("sum_sent")
    if not isinstance(received, dict) or not isinstance(sent, dict):
        raise ValueError("iperf3 JSON contains no aggregate send/receive metrics")
    bits = _finite_non_negative(
        received.get("bits_per_second"), "iperf3 receive throughput", positive=True
    )
    retransmits = _finite_non_negative(sent.get("retransmits", 0), "iperf3 retransmits")
    return bits / 1_000_000_000, retransmits


def _parse_fio(raw: bytes) -> tuple[float, float]:
    payload = _json_object(raw, "fio output")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("fio JSON contains no jobs")
    total_iops = 0.0
    p99_values: list[float] = []
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("fio job must be an object")
        read = job.get("read")
        if not isinstance(read, dict):
            raise ValueError("fio job has no read metrics")
        total_iops += _finite_non_negative(read.get("iops"), "fio read IOPS")
        clat = read.get("clat_ns")
        percentiles = clat.get("percentile") if isinstance(clat, dict) else None
        if not isinstance(percentiles, dict):
            raise ValueError("fio job has no completion-latency percentiles")
        p99_values.append(
            _finite_non_negative(
                percentiles.get("99.000000"), "fio read clat p99 nanoseconds"
            )
            / 1000.0
        )
    if total_iops <= 0:
        raise ValueError("fio total read IOPS must be positive")
    return total_iops, max(p99_values)


def parse_collection_artifact_bundle_metrics(
    bundle: VerifiedCollectionArtifactBundle,
    *,
    component: ComponentName,
    requested_metrics: list[str],
    gate_values: Mapping[str, float | bool | None],
) -> dict[str, CollectedMetric]:
    """Parse only requested L4 facts; gate values are copied, never adjudicated here."""

    requested = set(requested_metrics)
    metrics: dict[str, CollectedMetric] = {}
    sources: dict[str, list[tuple[str, bytes]]] = {}
    member_by_path = {member.path: member for member in bundle.manifest.members}
    for path, raw in bundle.members.items():
        sources.setdefault(member_by_path[path].media_type, []).append((path, raw))
    bundle_source = f"manifest:{bundle.manifest.digest}"

    def add(name: str, unit: str, values: list[float], paths: list[str]) -> None:
        if name in requested and values:
            metrics[name] = _readable(name, unit, f"{bundle_source} members={','.join(paths)}", values)

    if component == "cpu" and "cpu.bogo-ops-per-second" in requested:
        entries = sources.get(_STRESS_NG_MEDIA_TYPE, [])
        add(
            "cpu.bogo-ops-per-second",
            "bogo-ops/s",
            [_parse_stress_ng_cpu(raw) for _, raw in entries],
            [path for path, _ in entries],
        )
    elif component == "memory" and requested & {
        "memory.bandwidth-mib-per-second",
        "memory.latency-p95-ms",
    }:
        entries = sources.get(_SYSBENCH_MEMORY_MEDIA_TYPE, [])
        parsed = [_parse_sysbench_memory(raw) for _, raw in entries]
        add(
            "memory.bandwidth-mib-per-second",
            "MiB/s",
            [item[0] for item in parsed],
            [path for path, _ in entries],
        )
        add(
            "memory.latency-p95-ms",
            "ms",
            [item[1] for item in parsed],
            [path for path, _ in entries],
        )
    elif component == "network" and requested & {
        "network.receive-throughput-gbps",
        "network.retransmits",
    }:
        entries = sources.get(_IPERF3_MEDIA_TYPE, [])
        parsed = [_parse_iperf3(raw) for _, raw in entries]
        add(
            "network.receive-throughput-gbps",
            "Gbps",
            [item[0] for item in parsed],
            [path for path, _ in entries],
        )
        add(
            "network.retransmits",
            "count",
            [item[1] for item in parsed],
            [path for path, _ in entries],
        )
    elif component == "storage" and requested & {
        "storage.read-iops",
        "storage.read-clat-p99-us",
    }:
        entries = sources.get(_FIO_MEDIA_TYPE, [])
        parsed = [_parse_fio(raw) for _, raw in entries]
        add(
            "storage.read-iops",
            "IOPS",
            [item[0] for item in parsed],
            [path for path, _ in entries],
        )
        add(
            "storage.read-clat-p99-us",
            "us",
            [item[1] for item in parsed],
            [path for path, _ in entries],
        )

    for name, value in gate_values.items():
        if name not in requested:
            continue
        if isinstance(value, bool):
            numeric = 1.0 if value else 0.0
        elif value is None:
            metrics[name] = _unavailable(
                name,
                "boolean",
                "pressure execution gate evidence",
                "pressure execution gate value is null",
            )
            continue
        else:
            numeric = _finite_non_negative(value, f"gate {name}")
        metrics[name] = _readable(
            name, "boolean", "pressure execution gate evidence", [numeric]
        )

    artifact_metric_names = {
        "cpu.bogo-ops-per-second",
        "memory.bandwidth-mib-per-second",
        "memory.latency-p95-ms",
        "network.receive-throughput-gbps",
        "network.retransmits",
        "storage.read-iops",
        "storage.read-clat-p99-us",
        *gate_values.keys(),
    }
    missing = sorted((requested & artifact_metric_names) - set(metrics))
    if missing:
        raise ValueError(f"requested artifact metrics are unavailable: {missing}")
    return metrics


class CollectionInputArtifact(StrictModel):
    """Digest-bound raw input made available to an L4 collector implementation."""

    artifact_id: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=120)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ComponentCollectionRequest(StrictModel):
    schema_version: Literal[COLLECTION_REQUEST_SCHEMA] = COLLECTION_REQUEST_SCHEMA
    component: ComponentName
    target_id: str = Field(min_length=1, max_length=160)
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workload_phase_id: str = Field(min_length=1, max_length=160)
    workload_source: str = Field(min_length=1, max_length=300)
    collector_id: str = Field(min_length=1, max_length=160)
    requested_metrics: list[str] = Field(min_length=1)
    input_artifacts: list[CollectionInputArtifact]
    gate_values: dict[str, float | bool | None] = Field(default_factory=dict)
    interval_seconds: float = Field(gt=0)
    scope: ComponentCollectionScope
    measurement_identity: dict[str, str] = Field(min_length=1)

    @field_validator("requested_metrics")
    @classmethod
    def validate_requested_metrics(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("requested_metrics must be unique")
        pattern = re.compile(r"^[a-z][a-z0-9.-]*$")
        if any(pattern.fullmatch(metric) is None for metric in value):
            raise ValueError("requested_metrics contain an invalid metric name")
        return value

    @model_validator(mode="after")
    def validate_artifact_identities(self) -> ComponentCollectionRequest:
        artifact_ids = [artifact.artifact_id for artifact in self.input_artifacts]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("input artifact_id values must be unique")
        return self

    @field_validator("interval_seconds")
    @classmethod
    def require_finite_interval(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("interval_seconds must be finite")
        return value

    @model_validator(mode="after")
    def validate_component_scope(self) -> ComponentCollectionRequest:
        prefix = f"{self.component}."
        if any(not metric.startswith(prefix) for metric in self.requested_metrics):
            raise ValueError("requested_metrics must belong to the requested component")
        network = self.scope.network_interfaces
        storage = self.scope.storage_devices
        if self.component == "network":
            if network is None:
                raise ValueError("network collection requires explicit network_interfaces")
            if storage is not None:
                raise ValueError("network collection cannot carry storage_devices")
        elif self.component == "storage":
            if storage is None:
                raise ValueError("storage collection requires explicit storage_devices")
            if network is not None:
                raise ValueError("storage collection cannot carry network_interfaces")
        elif network is not None or storage is not None:
            raise ValueError(f"{self.component} collection does not accept network/storage scope")
        return self

    @property
    def plan(self) -> ComponentCollectionPlan:
        return ComponentCollectionPlan(
            component=self.component,
            target_id=self.target_id,
            environment_digest=self.environment_digest,
            workload_phase_id=self.workload_phase_id,
            workload_source=self.workload_source,
            collector_id=self.collector_id,
            requested_metrics=self.requested_metrics,
            interval_seconds=self.interval_seconds,
            scope=self.scope,
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class ComponentCollector(Protocol):
    """Replaceable synchronous L4 collector boundary."""

    collector_id: str
    collector_version: str

    def collect(self, request: ComponentCollectionRequest) -> ComponentMetricSnapshot: ...


class ComponentCollectorSession(Protocol):
    """One L4 collection window opened before, and finished after, an L3 workload."""

    def finish(self, request: ComponentCollectionRequest) -> ComponentMetricSnapshot: ...

    def cancel(self) -> None: ...


class WindowedComponentCollector(Protocol):
    """Replaceable collector capable of observing the actual workload window."""

    collector_id: str
    collector_version: str

    def begin_collection(self, plan: ComponentCollectionPlan) -> ComponentCollectorSession: ...


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class ComponentCollectionWindow:
    """Runtime handle that preserves one exact plan across a workload boundary."""

    def __init__(
        self,
        *,
        plan: ComponentCollectionPlan,
        collector: WindowedComponentCollector,
        enabled: bool,
        started_at: datetime,
        wall_clock: Callable[[], datetime],
        session: ComponentCollectorSession | None,
    ) -> None:
        self.plan = plan
        self.collector = collector
        self.enabled = enabled
        self.started_at = _require_aware(started_at, "started_at")
        self.wall_clock = wall_clock
        self.session = session
        self._closed = False

    def finish(self, request: ComponentCollectionRequest) -> ComponentCollectionRun:
        if self._closed:
            raise RuntimeError("component collection window is already closed")
        if request.plan != self.plan:
            mismatch = ValueError("collection request does not match the opened collection plan")
            try:
                self.cancel()
            except Exception as cancel_error:
                mismatch.add_note(
                    "L4 collection cancellation also failed: "
                    f"{type(cancel_error).__name__}: {cancel_error}"
                )
            raise mismatch
        snapshot: ComponentMetricSnapshot | None = None
        if self.enabled:
            assert self.session is not None
            try:
                snapshot = self.session.finish(request)
            except Exception as finish_error:
                try:
                    self.session.cancel()
                except Exception as cancel_error:
                    finish_error.add_note(
                        "L4 collection cancellation also failed: "
                        f"{type(cancel_error).__name__}: {cancel_error}"
                    )
                finally:
                    self._closed = True
                raise
        self._closed = True
        return ComponentCollectionRun(
            request=request,
            collector_id=self.collector.collector_id,
            collector_version=self.collector.collector_version,
            enabled=self.enabled,
            started_at=self.started_at,
            finished_at=self.wall_clock(),
            snapshot=snapshot,
        )

    def cancel(self) -> None:
        if self._closed:
            return
        try:
            if self.session is not None:
                self.session.cancel()
        finally:
            self._closed = True


class ComponentCollectionRun(StrictModel):
    schema_version: Literal[COLLECTION_RUN_SCHEMA] = COLLECTION_RUN_SCHEMA
    request: ComponentCollectionRequest
    collector_id: str = Field(min_length=1, max_length=160)
    collector_version: str = Field(min_length=1, max_length=80)
    enabled: bool
    started_at: datetime
    finished_at: datetime
    snapshot: ComponentMetricSnapshot | None

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_aware_times(cls, value: datetime, info) -> datetime:
        return _require_aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_run_binding(self) -> ComponentCollectionRun:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.enabled != (self.snapshot is not None):
            raise ValueError(
                "enabled collection requires a snapshot; disabled collection forbids one"
            )
        if self.collector_id != self.request.collector_id:
            raise ValueError("actual collector_id does not match collection request")
        if self.snapshot is not None:
            if set(self.snapshot.metrics) != set(self.request.requested_metrics):
                raise ValueError("snapshot metrics do not exactly match requested_metrics")
            if self.snapshot.component != self.request.component:
                raise ValueError("snapshot component does not match collection request")
            if self.snapshot.target_id != self.request.target_id:
                raise ValueError("snapshot target_id does not match collection request")
            if self.snapshot.environment_digest != self.request.environment_digest:
                raise ValueError("snapshot environment_digest does not match collection request")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class CollectionMeasurementEnvelope(StrictModel):
    """L4 evidence plus an unchanged L2 MeasurementBatch bound by digest."""

    schema_version: Literal[COLLECTION_ENVELOPE_SCHEMA] = COLLECTION_ENVELOPE_SCHEMA
    collection_run: ComponentCollectionRun
    measurement_batch: MeasurementBatch
    measurement_batch_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    collection_metric_names: list[str]
    unavailable_metrics: dict[str, CollectedMetric]

    @model_validator(mode="after")
    def validate_evidence_binding(self) -> CollectionMeasurementEnvelope:
        if not self.collection_run.enabled or self.collection_run.snapshot is None:
            raise ValueError("a measurement envelope requires an enabled collection snapshot")
        if self.measurement_batch.digest != self.measurement_batch_digest:
            raise ValueError("measurement_batch_digest does not match measurement_batch")
        expected_identity = dict(self.collection_run.request.measurement_identity)
        binding = {
            "component": self.collection_run.request.component,
            "target_id": self.collection_run.request.target_id,
            "environment_digest": self.collection_run.request.environment_digest,
            "collection_run_digest": self.collection_run.digest,
        }
        if any(
            key in expected_identity and expected_identity[key] != value
            for key, value in binding.items()
        ):
            raise ValueError("request measurement_identity conflicts with collection binding")
        expected_identity.update(binding)
        if self.measurement_batch.identity != expected_identity:
            raise ValueError("measurement identity is not exactly bound to collection run")

        snapshot = self.collection_run.snapshot
        readable = {
            name: metric
            for name, metric in snapshot.metrics.items()
            if metric.availability == MetricAvailability.READABLE
        }
        unavailable = {
            name: metric
            for name, metric in snapshot.metrics.items()
            if metric.availability == MetricAvailability.UNAVAILABLE
        }
        if len(set(self.collection_metric_names)) != len(self.collection_metric_names):
            raise ValueError("collection_metric_names must be unique")
        if set(self.collection_metric_names) != set(readable):
            raise ValueError("collection_metric_names do not exactly match readable L4 metrics")
        if not set(readable).issubset(self.measurement_batch.metrics):
            raise ValueError("MeasurementBatch is missing readable L4 metrics")
        for name, metric in readable.items():
            evidence = self.measurement_batch.metrics[name]
            expected_values = metric.value if isinstance(metric.value, list) else [metric.value]
            if evidence.metric_id != name or evidence.values != expected_values:
                raise ValueError(f"MeasurementBatch evidence for {name!r} does not match snapshot")
        if self.unavailable_metrics != unavailable:
            raise ValueError("unavailable_metrics do not exactly preserve L4 unavailable evidence")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class CollectionOverheadABEvidence(StrictModel):
    """Raw paired observations only; acceptance thresholds belong to policy, not L4."""

    schema_version: Literal[COLLECTION_OVERHEAD_SCHEMA] = COLLECTION_OVERHEAD_SCHEMA
    target_id: str = Field(min_length=1, max_length=160)
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workload_identity: dict[str, str] = Field(min_length=1)
    collector_id: str = Field(min_length=1, max_length=160)
    collection_disabled_seconds: list[float] = Field(min_length=1)
    collection_enabled_seconds: list[float] = Field(min_length=1)
    collected_at: datetime

    @field_validator("collected_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        return _require_aware(value, "collected_at")

    @model_validator(mode="after")
    def validate_pairs(self) -> CollectionOverheadABEvidence:
        if len(self.collection_disabled_seconds) != len(self.collection_enabled_seconds):
            raise ValueError("enabled and disabled observations must be paired")
        observations = self.collection_disabled_seconds + self.collection_enabled_seconds
        if any(not isfinite(value) or value < 0 for value in observations):
            raise ValueError("overhead observations must be finite non-negative seconds")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


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
    except (OSError, UnicodeError):
        return None


def _parse_cpu_total_idle(text: str) -> tuple[float, float] | None:
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "cpu":
            continue
        # Linux reports user nice system idle iowait irq softirq steal guest guest_nice.
        # guest fields are already included in user/nice, so only fields through steal count.
        raw_values = fields[1:9]
        if len(raw_values) < 4:
            return None
        try:
            values = [float(value) for value in raw_values]
        except ValueError:
            return None
        if any(not isfinite(value) or value < 0 for value in values):
            return None
        values.extend([0.0] * (8 - len(values)))
        return sum(values), values[3] + values[4]
    return None


def _cpu_busy_ratio(first: str, second: str) -> tuple[float | None, str]:
    first_pair = _parse_cpu_total_idle(first)
    second_pair = _parse_cpu_total_idle(second)
    if first_pair is None or second_pair is None:
        return None, "invalid or missing aggregate cpu line in /proc/stat samples"
    total_delta = second_pair[0] - first_pair[0]
    idle_delta = second_pair[1] - first_pair[1]
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        return None, "zero or invalid tick delta between /proc/stat samples"
    return 1.0 - idle_delta / total_delta, ""


def _parse_online_cpu_count(text: str) -> int:
    return sum(
        1 for line in text.splitlines() if line.split() and re.fullmatch(r"cpu\d+", line.split()[0])
    )


def _collect_cpu(
    proc_root: Path,
    sys_root: Path,
    interval_seconds: float,
    sleep_fn: Callable[[float], None],
) -> dict[str, CollectedMetric]:
    metrics: dict[str, CollectedMetric] = {}
    stat_source = f"{proc_root}/stat (two samples, {interval_seconds}s)"
    first = _read_text(proc_root / "stat")
    if first is not None:
        sleep_fn(interval_seconds)
    second = _read_text(proc_root / "stat")
    if first is None or second is None:
        metrics["cpu.busy-ratio"] = _unavailable(
            "cpu.busy-ratio",
            "ratio",
            stat_source,
            f"{proc_root}/stat is unreadable in this environment",
        )
    else:
        ratio, reason = _cpu_busy_ratio(first, second)
        metrics["cpu.busy-ratio"] = (
            _readable("cpu.busy-ratio", "ratio", stat_source, ratio)
            if ratio is not None
            else _unavailable("cpu.busy-ratio", "ratio", stat_source, reason)
        )

    online_source = f"{proc_root}/stat"
    if second is None:
        metrics["cpu.online-count"] = _unavailable(
            "cpu.online-count", "count", online_source, f"{online_source} is unreadable"
        )
    else:
        online = _parse_online_cpu_count(second)
        metrics["cpu.online-count"] = (
            _readable("cpu.online-count", "count", online_source, float(online))
            if online
            else _unavailable(
                "cpu.online-count",
                "count",
                online_source,
                "no valid per-cpu lines in /proc/stat",
            )
        )

    cpu_pmu = sys_root / "bus" / "event_source" / "devices" / "cpu"
    pmu_source = str(cpu_pmu)
    metrics["cpu.pmu-event-sources"] = (
        _readable("cpu.pmu-event-sources", "count", pmu_source, 1.0)
        if cpu_pmu.is_dir()
        else _unavailable(
            "cpu.pmu-event-sources",
            "count",
            pmu_source,
            "canonical CPU PMU event source is not visible in this guest",
        )
    )

    paranoid_path = proc_root / "sys" / "kernel" / "perf_event_paranoid"
    paranoid_source = str(paranoid_path)
    paranoid_text = _read_text(paranoid_path)
    try:
        paranoid = float(int(paranoid_text.strip())) if paranoid_text is not None else None
    except ValueError:
        paranoid = None
    metrics["cpu.perf-event-paranoid"] = (
        _readable("cpu.perf-event-paranoid", "level", paranoid_source, paranoid)
        if paranoid is not None
        else _unavailable(
            "cpu.perf-event-paranoid",
            "level",
            paranoid_source,
            "perf_event_paranoid is unreadable or invalid in this environment",
        )
    )
    return metrics


def _collect_memory(proc_root: Path) -> dict[str, CollectedMetric]:
    source = f"{proc_root}/meminfo"
    text = _read_text(proc_root / "meminfo")
    if text is None:
        return {
            "memory.available-ratio": _unavailable(
                "memory.available-ratio",
                "ratio",
                source,
                f"{source} is unreadable in this environment",
            )
        }
    values: dict[str, float] = {}
    for line in text.splitlines():
        field = line.split(":", 1)
        if len(field) != 2:
            continue
        try:
            value = float(field[1].split()[0])
        except (IndexError, ValueError):
            continue
        if isfinite(value):
            values[field[0].strip()] = value
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        reason = "MemTotal or MemAvailable missing from /proc/meminfo"
    elif total <= 0 or available < 0 or available > total:
        reason = "MemTotal/MemAvailable values violate 0 <= available <= total and total > 0"
    else:
        return {
            "memory.available-ratio": _readable(
                "memory.available-ratio", "ratio", source, available / total
            )
        }
    return {
        "memory.available-ratio": _unavailable("memory.available-ratio", "ratio", source, reason)
    }


def _parse_network_counters(
    text: str | None, interfaces: list[str]
) -> tuple[dict[str, float] | None, str]:
    if text is None:
        return None, "/proc/net/dev is unreadable"
    found: dict[str, tuple[float, float]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        interface, rest = line.split(":", 1)
        name = interface.strip()
        if name not in interfaces:
            continue
        fields = rest.split()
        if len(fields) < 16:
            return None, f"interface {name!r} has a malformed /proc/net/dev row"
        try:
            rx, tx = float(fields[0]), float(fields[8])
        except ValueError:
            return None, f"interface {name!r} has non-numeric counters"
        if not isfinite(rx) or not isfinite(tx) or rx < 0 or tx < 0:
            return None, f"interface {name!r} has invalid counters"
        found[name] = (rx, tx)
    missing = [name for name in interfaces if name not in found]
    if missing:
        return None, f"requested interfaces not present: {', '.join(missing)}"
    return {
        "rx": sum(found[name][0] for name in interfaces),
        "tx": sum(found[name][1] for name in interfaces),
    }, ""


def _parse_storage_counters(
    text: str | None, devices: list[str]
) -> tuple[dict[str, float] | None, str]:
    if text is None:
        return None, "/proc/diskstats is unreadable"
    found: dict[str, tuple[float, float, float]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 14 or fields[2] not in devices:
            continue
        name = fields[2]
        try:
            reads, writes, io_ms = float(fields[3]), float(fields[7]), float(fields[12])
        except ValueError:
            return None, f"device {name!r} has non-numeric counters"
        values = (reads, writes, io_ms)
        if any(not isfinite(value) or value < 0 for value in values):
            return None, f"device {name!r} has invalid counters"
        found[name] = values
    missing = [name for name in devices if name not in found]
    if missing:
        return None, f"requested storage devices not present: {', '.join(missing)}"
    return {
        "reads": sum(found[name][0] for name in devices),
        "writes": sum(found[name][1] for name in devices),
        "io_ms": sum(found[name][2] for name in devices),
    }, ""


def _window_metrics(
    *,
    component: Literal["network", "storage"],
    first: dict[str, float] | None,
    second: dict[str, float] | None,
    first_reason: str,
    second_reason: str,
    interval_seconds: float,
    source: str,
    definitions: tuple[tuple[str, str, str], ...],
) -> dict[str, CollectedMetric]:
    metrics: dict[str, CollectedMetric] = {}
    for raw_key, metric_stem, raw_unit in definitions:
        total_name = f"{component}.{metric_stem}-total"
        delta_name = f"{component}.{metric_stem}-delta"
        rate_name = f"{component}.{metric_stem}-per-second"

        # A cumulative total is the raw after-window fact and does not depend on
        # the before sample.  Deltas/rates require both samples.
        if second is None:
            metrics[total_name] = _unavailable(
                total_name, raw_unit, source, second_reason or "after-window sample is unavailable"
            )
        else:
            metrics[total_name] = _readable(total_name, raw_unit, source, second[raw_key])

        if first is None or second is None:
            reason = first_reason or second_reason or "collection window sample is unavailable"
            metrics[delta_name] = _unavailable(delta_name, raw_unit, source, reason)
            metrics[rate_name] = _unavailable(rate_name, f"{raw_unit}/s", source, reason)
            continue

        delta = second[raw_key] - first[raw_key]
        if delta < 0:
            reason = (
                f"counter {raw_key!r} decreased during the collection window; "
                "reset/wrap policy is not inferred"
            )
            metrics[delta_name] = _unavailable(delta_name, raw_unit, source, reason)
            metrics[rate_name] = _unavailable(rate_name, f"{raw_unit}/s", source, reason)
        else:
            metrics[delta_name] = _readable(delta_name, raw_unit, source, delta)
            metrics[rate_name] = _readable(
                rate_name, f"{raw_unit}/s", source, delta / interval_seconds
            )
    return metrics


def _collect_network(
    proc_root: Path,
    interfaces: list[str],
    interval_seconds: float,
    sleep_fn: Callable[[float], None],
) -> dict[str, CollectedMetric]:
    path = proc_root / "net" / "dev"
    source = f"{path} interfaces={','.join(interfaces)} window={interval_seconds}s"
    first, first_reason = _parse_network_counters(_read_text(path), interfaces)
    sleep_fn(interval_seconds)
    second, second_reason = _parse_network_counters(_read_text(path), interfaces)
    return _window_metrics(
        component="network",
        first=first,
        second=second,
        first_reason=first_reason,
        second_reason=second_reason,
        interval_seconds=interval_seconds,
        source=source,
        definitions=(("rx", "rx-bytes", "bytes"), ("tx", "tx-bytes", "bytes")),
    )


def _collect_storage(
    proc_root: Path,
    devices: list[str],
    interval_seconds: float,
    sleep_fn: Callable[[float], None],
) -> dict[str, CollectedMetric]:
    path = proc_root / "diskstats"
    source = f"{path} devices={','.join(devices)} window={interval_seconds}s"
    first, first_reason = _parse_storage_counters(_read_text(path), devices)
    sleep_fn(interval_seconds)
    second, second_reason = _parse_storage_counters(_read_text(path), devices)
    return _window_metrics(
        component="storage",
        first=first,
        second=second,
        first_reason=first_reason,
        second_reason=second_reason,
        interval_seconds=interval_seconds,
        source=source,
        definitions=(
            ("reads", "reads-completed", "count"),
            ("writes", "writes-completed", "count"),
            ("io_ms", "io-ms", "ms"),
        ),
    )


def _collect_numa(sys_root: Path) -> dict[str, CollectedMetric]:
    node_root = sys_root / "devices" / "system" / "node"
    nodes = sorted(path for path in node_root.glob("node[0-9]*") if path.is_dir())
    source = str(node_root)
    if nodes:
        count = _readable("numa.node-count", "count", source, float(len(nodes)))
        binding_reason = (
            "NUMA topology visibility does not prove this workload's CPU/memory binding; "
            "no workload binding measurement was supplied"
        )
    else:
        count = _unavailable(
            "numa.node-count",
            "count",
            source,
            "no node directories visible in guest at /sys/devices/system/node",
        )
        binding_reason = "no NUMA topology or workload binding measurement is visible in guest"
    return {
        "numa.node-count": count,
        "numa.binding": _unavailable("numa.binding", "boolean", source, binding_reason),
    }


_BUILTIN_METRIC_NAMES: dict[str, list[str]] = {
    "cpu": [
        "cpu.busy-ratio",
        "cpu.online-count",
        "cpu.pmu-event-sources",
        "cpu.perf-event-paranoid",
    ],
    "memory": ["memory.available-ratio"],
    "network": [
        f"network.{stem}-{suffix}"
        for stem in ("rx-bytes", "tx-bytes")
        for suffix in ("total", "delta", "per-second")
    ],
    "storage": [
        f"storage.{stem}-{suffix}"
        for stem in ("reads-completed", "writes-completed", "io-ms")
        for suffix in ("total", "delta", "per-second")
    ],
    "numa": ["numa.node-count", "numa.binding"],
}

_COUNTING_BASIS = {
    "cpu": "busy ratio from two /proc/stat samples (user through steal; guest fields excluded "
    "from double counting); canonical CPU PMU source and perf_event_paranoid reported separately",
    "memory": "MemAvailable / MemTotal with explicit bounds validation",
    "network": (
        "exact caller-supplied /proc/net/dev interfaces; after-window cumulative totals "
        "plus window deltas and rates"
    ),
    "storage": "exact caller-supplied /proc/diskstats devices; after-window cumulative totals plus "
    "window deltas and rates; partitions are never added implicitly",
    "numa": (
        "visible node directory count only; workload binding remains unavailable unless measured"
    ),
}


class _BuiltinLinuxGuestCollectionSession:
    def __init__(self, collector: BuiltinLinuxGuestCollector, plan: ComponentCollectionPlan) -> None:
        self.collector = collector
        self.plan = plan
        self._stop = Event()
        self._lock = Lock()
        self._closed = False
        self._samples: list[object] = []
        self._started = collector.monotonic()
        self._append_sample()
        self._thread = Thread(
            target=self._sample_periodically,
            name=f"looper-l4-{plan.component}-collector",
            daemon=True,
        )
        self._thread.start()

    def _sample(self) -> object:
        component = self.plan.component
        if component == "cpu":
            return _read_text(self.collector.proc_root / "stat")
        if component == "memory":
            return _collect_memory(self.collector.proc_root)
        if component == "network":
            assert self.plan.scope.network_interfaces is not None
            return _parse_network_counters(
                _read_text(self.collector.proc_root / "net" / "dev"),
                self.plan.scope.network_interfaces,
            )
        if component == "storage":
            assert self.plan.scope.storage_devices is not None
            return _parse_storage_counters(
                _read_text(self.collector.proc_root / "diskstats"),
                self.plan.scope.storage_devices,
            )
        return _collect_numa(self.collector.sys_root)

    def _append_sample(self) -> None:
        sample = self._sample()
        with self._lock:
            self._samples.append(sample)

    def _sample_periodically(self) -> None:
        while not self._stop.wait(self.plan.interval_seconds):
            self._append_sample()

    def _stop_thread(self) -> None:
        self._stop.set()
        self._thread.join()

    def _guest_metrics(self, elapsed: float) -> dict[str, CollectedMetric]:
        with self._lock:
            samples = list(self._samples)
        component = self.plan.component
        if component == "cpu":
            first = samples[0]
            second = samples[-1]
            source = f"{self.collector.proc_root}/stat actual monotonic window={elapsed}s"
            if not isinstance(first, str) or not isinstance(second, str):
                busy = _unavailable(
                    "cpu.busy-ratio",
                    "ratio",
                    source,
                    f"{self.collector.proc_root}/stat is unreadable in this environment",
                )
            else:
                ratio, reason = _cpu_busy_ratio(first, second)
                busy = (
                    _readable("cpu.busy-ratio", "ratio", source, ratio)
                    if ratio is not None
                    else _unavailable("cpu.busy-ratio", "ratio", source, reason)
                )
            # Preserve existing static probes without opening another timed sub-window.
            static = _collect_cpu(
                self.collector.proc_root,
                self.collector.sys_root,
                self.plan.interval_seconds,
                lambda _: None,
            )
            static["cpu.busy-ratio"] = busy
            return static
        if component == "memory":
            readable_values: list[float] = []
            last_metric: CollectedMetric | None = None
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                candidate = sample.get("memory.available-ratio")
                if isinstance(candidate, CollectedMetric):
                    last_metric = candidate
                    if candidate.availability == MetricAvailability.READABLE:
                        values = candidate.value if isinstance(candidate.value, list) else [candidate.value]
                        readable_values.extend(float(value) for value in values if value is not None)
            if readable_values:
                return {
                    "memory.available-ratio": _readable(
                        "memory.available-ratio",
                        "ratio",
                        f"{self.collector.proc_root}/meminfo periodic interval={self.plan.interval_seconds}s",
                        readable_values,
                    )
                }
            if last_metric is not None:
                return {"memory.available-ratio": last_metric}
            return _collect_memory(self.collector.proc_root)
        if component in {"network", "storage"}:
            first, first_reason = samples[0]  # type: ignore[misc]
            second, second_reason = samples[-1]  # type: ignore[misc]
            if component == "network":
                assert self.plan.scope.network_interfaces is not None
                source = (
                    f"{self.collector.proc_root}/net/dev "
                    f"interfaces={','.join(self.plan.scope.network_interfaces)} "
                    f"actual monotonic window={elapsed}s"
                )
                definitions = (("rx", "rx-bytes", "bytes"), ("tx", "tx-bytes", "bytes"))
            else:
                assert self.plan.scope.storage_devices is not None
                source = (
                    f"{self.collector.proc_root}/diskstats "
                    f"devices={','.join(self.plan.scope.storage_devices)} "
                    f"actual monotonic window={elapsed}s"
                )
                definitions = (
                    ("reads", "reads-completed", "count"),
                    ("writes", "writes-completed", "count"),
                    ("io_ms", "io-ms", "ms"),
                )
            return _window_metrics(
                component=component,
                first=first,
                second=second,
                first_reason=first_reason,
                second_reason=second_reason,
                interval_seconds=elapsed,
                source=source,
                definitions=definitions,
            )
        sample = samples[-1]
        return sample if isinstance(sample, dict) else _collect_numa(self.collector.sys_root)

    def finish(self, request: ComponentCollectionRequest) -> ComponentMetricSnapshot:
        if self._closed:
            raise RuntimeError("builtin collection session is already closed")
        if request.plan != self.plan:
            raise ValueError("collection request does not match builtin collection plan")
        try:
            self._stop_thread()
            self._append_sample()
            elapsed = self.collector.monotonic() - self._started
            if not isfinite(elapsed) or elapsed <= 0:
                raise ValueError("actual monotonic collection window must be finite and positive")
            metrics = self._guest_metrics(elapsed)
            if request.input_artifacts:
                if len(request.input_artifacts) != 1:
                    raise ValueError("one measure execution requires exactly one artifact bundle")
                artifact = request.input_artifacts[0]
                if artifact.media_type != COLLECTION_BUNDLE_MEDIA_TYPE:
                    raise ValueError("collection input artifact is not a pressure bundle")
                raw = self.collector.artifact_reader(artifact)
                bundle = verify_collection_artifact_bundle(raw, expected_digest=artifact.digest)
                metrics.update(
                    parse_collection_artifact_bundle_metrics(
                        bundle,
                        component=request.component,
                        requested_metrics=request.requested_metrics,
                        gate_values=request.gate_values,
                    )
                )
            selected = {name: metrics[name] for name in request.requested_metrics if name in metrics}
            missing = sorted(set(request.requested_metrics) - set(selected))
            if missing:
                raise ValueError(f"builtin collector cannot provide requested metrics: {missing}")
            return ComponentMetricSnapshot(
                component=request.component,
                target_id=request.target_id,
                environment_digest=request.environment_digest,
                collected_at=self.collector.wall_clock(),
                metrics=selected,
                counting_basis=(
                    f"{_COUNTING_BASIS[request.component]}; periodic sampling "
                    f"interval={request.interval_seconds}s; actual monotonic window={elapsed}s; "
                    "pressure artifact identity is canonical manifest digest"
                ),
            )
        finally:
            self._closed = True

    def cancel(self) -> None:
        if self._closed:
            return
        try:
            self._stop_thread()
        finally:
            self._closed = True


class BuiltinLinuxGuestCollector:
    """Standard-library Linux guest collector with injectable I/O timing for tests."""

    collector_id = "looper.builtin-linux-guest"
    collector_version = "1.0.0"

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        sys_root: Path = Path("/sys"),
        sleep_fn: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
        artifact_reader: Callable[[CollectionInputArtifact], bytes] | None = None,
    ) -> None:
        self.proc_root = proc_root
        self.sys_root = sys_root
        self.sleep_fn = sleep_fn
        self.wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic
        self.artifact_reader = artifact_reader or (lambda artifact: Path(artifact.source).read_bytes())

    def begin_collection(
        self, plan: ComponentCollectionPlan
    ) -> _BuiltinLinuxGuestCollectionSession:
        if plan.collector_id != self.collector_id:
            raise ValueError("collection plan collector_id does not select this collector")
        return _BuiltinLinuxGuestCollectionSession(self, plan)

    def collect(self, request: ComponentCollectionRequest) -> ComponentMetricSnapshot:
        if request.collector_id != self.collector_id:
            raise ValueError("request collector_id does not select this collector")
        if request.input_artifacts:
            raise ValueError("builtin Linux guest collector does not parse input artifacts")
        if set(request.requested_metrics) != set(_BUILTIN_METRIC_NAMES[request.component]):
            raise ValueError("requested_metrics do not match builtin component metric set")
        if request.component == "cpu":
            metrics = _collect_cpu(
                self.proc_root, self.sys_root, request.interval_seconds, self.sleep_fn
            )
        elif request.component == "memory":
            metrics = _collect_memory(self.proc_root)
        elif request.component == "network":
            assert request.scope.network_interfaces is not None
            metrics = _collect_network(
                self.proc_root,
                request.scope.network_interfaces,
                request.interval_seconds,
                self.sleep_fn,
            )
        elif request.component == "storage":
            assert request.scope.storage_devices is not None
            metrics = _collect_storage(
                self.proc_root,
                request.scope.storage_devices,
                request.interval_seconds,
                self.sleep_fn,
            )
        else:
            metrics = _collect_numa(self.sys_root)
        return ComponentMetricSnapshot(
            component=request.component,
            target_id=request.target_id,
            environment_digest=request.environment_digest,
            collected_at=self.wall_clock(),
            metrics=metrics,
            counting_basis=_COUNTING_BASIS[request.component],
        )


def begin_component_collection(
    plan: ComponentCollectionPlan,
    *,
    collector: WindowedComponentCollector,
    enabled: bool,
    wall_clock: Callable[[], datetime] | None = None,
) -> ComponentCollectionWindow:
    """Open L4 before L3 starts its measure command; disabled mode calls no collector code."""

    selected = collector
    if selected.collector_id != plan.collector_id:
        raise ValueError("injected collector_id does not match collection plan")
    clock = wall_clock or (lambda: datetime.now(UTC))
    started_at = clock()
    session = selected.begin_collection(plan) if enabled else None
    return ComponentCollectionWindow(
        plan=plan,
        collector=selected,
        enabled=enabled,
        started_at=started_at,
        wall_clock=clock,
        session=session,
    )


def run_component_collection(
    request: ComponentCollectionRequest,
    *,
    collector: ComponentCollector | None = None,
    enabled: bool,
    wall_clock: Callable[[], datetime] | None = None,
) -> ComponentCollectionRun:
    """Run (or explicitly skip) an injected L4 collector.

    The disabled path never calls ``collector.collect``.  This is the switch used
    for paired collection-overhead experiments.
    """

    selected = collector or BuiltinLinuxGuestCollector()
    if selected.collector_id != request.collector_id:
        raise ValueError("injected collector_id does not match collection request")
    clock = wall_clock or (lambda: datetime.now(UTC))
    started_at = clock()
    snapshot = selected.collect(request) if enabled else None
    finished_at = clock()
    return ComponentCollectionRun(
        request=request,
        collector_id=selected.collector_id,
        collector_version=selected.collector_version,
        enabled=enabled,
        started_at=started_at,
        finished_at=finished_at,
        snapshot=snapshot,
    )


def bind_collection_to_measurement_batch(
    collection_run: ComponentCollectionRun,
    *,
    measurement_batch: MeasurementBatch | None = None,
    gate_values: dict[str, float | bool | None] | None = None,
) -> CollectionMeasurementEnvelope:
    """Bind readable L4 facts to an unchanged L2 MeasurementBatch schema.

    When a pre-existing L2 batch is supplied, its main metrics, gate values,
    pressure digest, phase evidence, and stability evidence are preserved.
    Unavailable L4 facts stay verbatim beside the L2 batch in the envelope.
    """

    if not collection_run.enabled or collection_run.snapshot is None:
        raise ValueError("only an enabled collection run can be bound to MeasurementBatch")
    snapshot = collection_run.snapshot
    collected_metrics = {
        name: MetricEvidence(
            metric_id=name,
            values=metric.value if isinstance(metric.value, list) else [metric.value],
        )
        for name, metric in snapshot.metrics.items()
        if metric.availability == MetricAvailability.READABLE and metric.value is not None
    }
    unavailable = {
        name: metric
        for name, metric in snapshot.metrics.items()
        if metric.availability == MetricAvailability.UNAVAILABLE
    }
    requested_identity = collection_run.request.measurement_identity
    if measurement_batch is not None and measurement_batch.identity != requested_identity:
        raise ValueError(
            "existing MeasurementBatch identity does not match request measurement_identity"
        )
    identity = dict(requested_identity)
    binding = {
        "component": collection_run.request.component,
        "target_id": collection_run.request.target_id,
        "environment_digest": collection_run.request.environment_digest,
        "collection_run_digest": collection_run.digest,
    }
    conflicts = {
        key: (identity[key], value)
        for key, value in binding.items()
        if key in identity and identity[key] != value
    }
    if conflicts:
        raise ValueError(f"measurement_identity conflicts with L4 binding: {conflicts}")
    identity.update(binding)

    existing_metrics = {} if measurement_batch is None else dict(measurement_batch.metrics)
    for name, evidence in collected_metrics.items():
        if name in existing_metrics and existing_metrics[name] != evidence:
            raise ValueError(
                f"existing MeasurementBatch metric {name!r} conflicts with L4 evidence"
            )
        existing_metrics[name] = evidence

    if measurement_batch is None:
        if gate_values is None:
            raise ValueError(
                "gate_values must be supplied when no existing MeasurementBatch is given"
            )
        batch = MeasurementBatch(
            identity=identity, metrics=existing_metrics, gate_values=gate_values
        )
    else:
        if gate_values is not None:
            raise ValueError("gate_values must not override an existing MeasurementBatch")
        payload = measurement_batch.model_dump(mode="python")
        payload.update({"identity": identity, "metrics": existing_metrics})
        batch = MeasurementBatch.model_validate(payload)
    return CollectionMeasurementEnvelope(
        collection_run=collection_run,
        measurement_batch=batch,
        measurement_batch_digest=batch.digest,
        collection_metric_names=sorted(collected_metrics),
        unavailable_metrics=unavailable,
    )


def _legacy_unscoped_snapshot(
    component: Literal["network", "storage"],
    *,
    target_id: str,
    environment_digest: str,
    collected_at: datetime,
    proc_root: Path,
) -> ComponentMetricSnapshot:
    source = str(proc_root / ("net/dev" if component == "network" else "diskstats"))
    if component == "network":
        definitions = (
            ("network.rx-bytes-total", "bytes"),
            ("network.tx-bytes-total", "bytes"),
            ("network.rx-bytes-delta", "bytes"),
            ("network.tx-bytes-delta", "bytes"),
            ("network.rx-bytes-per-second", "bytes/s"),
            ("network.tx-bytes-per-second", "bytes/s"),
        )
        reason = (
            "network_interfaces were not explicitly supplied; implicit aggregation is forbidden"
        )
    else:
        definitions = tuple(
            (f"storage.{stem}-{suffix}", unit if suffix != "per-second" else f"{unit}/s")
            for stem, unit in (
                ("reads-completed", "count"),
                ("writes-completed", "count"),
                ("io-ms", "ms"),
            )
            for suffix in ("total", "delta", "per-second")
        )
        reason = "storage_devices were not explicitly supplied; implicit aggregation is forbidden"
    metrics = {name: _unavailable(name, unit, source, reason) for name, unit in definitions}
    return ComponentMetricSnapshot(
        component=component,
        target_id=target_id,
        environment_digest=environment_digest,
        collected_at=collected_at,
        metrics=metrics,
        counting_basis=_COUNTING_BASIS[component],
    )


def collect_component_snapshot(
    component: str,
    *,
    target_id: str,
    environment_digest: str,
    proc_root: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
    interval_seconds: float = 0.1,
    collected_at: datetime | None = None,
    network_interfaces: list[str] | None = None,
    storage_devices: list[str] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> ComponentMetricSnapshot:
    """Compatibility entry point; new orchestration should use a collection request/run.

    Network and storage calls without exact scope fail closed as unavailable
    snapshots.  No legacy implicit whole-host aggregation is retained.
    """

    if component not in _SUPPORTED_COMPONENTS:
        raise ValueError(f"unsupported component: {component}")
    if interval_seconds <= 0 or not isfinite(interval_seconds):
        raise ValueError("interval_seconds must be a finite positive number")
    timestamp = collected_at or datetime.now(UTC)
    _require_aware(timestamp, "collected_at")
    if component in {"network", "storage"}:
        names = network_interfaces if component == "network" else storage_devices
        if names is None:
            return _legacy_unscoped_snapshot(
                component,
                target_id=target_id,
                environment_digest=environment_digest,
                collected_at=timestamp,
                proc_root=proc_root,
            )
    scope = ComponentCollectionScope(
        network_interfaces=network_interfaces,
        storage_devices=storage_devices,
    )
    request = ComponentCollectionRequest(
        component=component,
        target_id=target_id,
        environment_digest=environment_digest,
        workload_phase_id="compatibility-call",
        workload_source="collect_component_snapshot",
        collector_id=BuiltinLinuxGuestCollector.collector_id,
        requested_metrics=_BUILTIN_METRIC_NAMES[component],
        input_artifacts=[],
        interval_seconds=interval_seconds,
        scope=scope,
        measurement_identity={"entry_point": "collect_component_snapshot"},
    )
    collector = BuiltinLinuxGuestCollector(
        proc_root=proc_root,
        sys_root=sys_root,
        sleep_fn=sleep_fn,
        wall_clock=lambda: timestamp,
    )
    return collector.collect(request)


__all__ = [
    "BuiltinLinuxGuestCollector",
    "CollectedMetric",
    "CollectionInputArtifact",
    "ComponentCollectionPlan",
    "ComponentCollectionWindow",
    "CollectionMeasurementEnvelope",
    "CollectionOverheadABEvidence",
    "ComponentCollectionRequest",
    "ComponentCollectionRun",
    "ComponentCollectionScope",
    "ComponentCollector",
    "ComponentCollectorSession",
    "ComponentMetricSnapshot",
    "MetricAvailability",
    "WindowedComponentCollector",
    "begin_component_collection",
    "bind_collection_to_measurement_batch",
    "collect_component_snapshot",
    "run_component_collection",
]
