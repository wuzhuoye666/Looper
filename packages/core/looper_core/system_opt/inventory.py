from __future__ import annotations

import base64
import hashlib
import os
import platform
import re
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, TypeAdapter, ValidationError

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigManifest
from looper_core.system_opt.executor import ExecutorBackend, OperationStatus


class InventoryStatus(StrEnum):
    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission-denied"
    PARSE_FAILED = "parse-failed"
    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too-large"


class EnvironmentFingerprint(StrictModel):
    os_name: str
    kernel_release: str
    architecture: str
    distribution_id: str | None = None
    distribution_version: str | None = None
    virtualization: str
    host_identifier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    host_identifier_source: str


class InventoryMetadata(StrictModel):
    collector_environment: EnvironmentFingerprint
    scope_limitations: list[str]


def _read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        values[key] = raw_value.strip().strip('"').strip("'")
    return values


def _detect_linux_virtualization(kernel_evidence: str) -> str:
    if "microsoft-standard-wsl2" in kernel_evidence:
        return "wsl2"
    if "microsoft" in kernel_evidence:
        return "wsl-unknown-version"
    detector = shutil.which("systemd-detect-virt")
    if detector is None:
        return "unknown"
    try:
        result = subprocess.run(
            [detector],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    detected = result.stdout.strip().lower()
    if result.returncode == 0 and re.fullmatch(r"[a-z0-9._-]{1,64}", detected):
        return detected
    if detected == "none":
        return "none"
    return "unknown"


def capture_environment_fingerprint() -> EnvironmentFingerprint:
    os_name = platform.system().lower() or "unknown"
    kernel_release = platform.release() or "unknown"
    architecture = platform.machine() or "unknown"
    release = _read_os_release() if os_name == "linux" else {}
    kernel_evidence = f"{kernel_release} {platform.version()}".lower()
    virtualization = (
        _detect_linux_virtualization(kernel_evidence) if os_name == "linux" else "unknown"
    )

    identifier_source = "platform.node"
    identifier = platform.node()
    if os_name == "linux":
        try:
            machine_id = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
        except OSError:
            machine_id = ""
        if machine_id:
            identifier_source = "/etc/machine-id (sha256; raw value not retained)"
            identifier = machine_id
    if not identifier:
        identifier_source = "unavailable sentinel"
        identifier = "unavailable"

    return EnvironmentFingerprint(
        os_name=os_name,
        kernel_release=kernel_release,
        architecture=architecture,
        distribution_id=release.get("ID"),
        distribution_version=release.get("VERSION_ID"),
        virtualization=virtualization,
        host_identifier_sha256=hashlib.sha256(identifier.encode("utf-8")).hexdigest(),
        host_identifier_source=identifier_source,
    )


def _scope_limitations(fingerprint: EnvironmentFingerprint, *, target_os: str) -> list[str]:
    limitations = [
        "Evidence is specific to this target and collection environment; "
        "portability requires independent validation."
    ]
    if fingerprint.virtualization == "wsl2":
        limitations.append(
            "WSL2 uses a Microsoft-customized Linux kernel and permission model; "
            "these observations must not be extrapolated to a Tencent Cloud CVM guest."
        )
    elif fingerprint.virtualization == "wsl-unknown-version":
        limitations.append(
            "A Microsoft WSL kernel was detected, but its WSL generation was not "
            "established; do not extrapolate these observations to a CVM guest."
        )
    if fingerprint.os_name != target_os.lower():
        limitations.append(
            "Collector OS differs from target_os; this report is simulated or proxied "
            "rather than direct target evidence."
        )
    return limitations


class ConfigStateField(StrictModel):
    status: InventoryStatus
    value: Any | None = None
    source: str


class ConfigInventoryItem(StrictModel):
    item_id: str
    parameter_id: str
    target: str
    primary_component: str
    related_components: list[str]
    preflight: ConfigStateField
    current: ConfigStateField
    desired: ConfigStateField
    effective: ConfigStateField
    persistent: ConfigStateField
    ownership: ConfigStateField
    raw_readback: str | None = None
    message: str | None = None


class ConfigInventoryReport(StrictModel):
    schema_version: str
    metadata: InventoryMetadata
    target_id: str
    target_os: str
    manifest_id: str
    manifest_digest: str
    counting_basis: str
    items: list[ConfigInventoryItem]

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def _inventory_status(status: OperationStatus) -> InventoryStatus:
    return {
        OperationStatus.SUCCEEDED: InventoryStatus.SUCCEEDED,
        OperationStatus.UNAVAILABLE: InventoryStatus.UNAVAILABLE,
        OperationStatus.PERMISSION_DENIED: InventoryStatus.PERMISSION_DENIED,
        OperationStatus.FAILED: InventoryStatus.PARSE_FAILED,
        OperationStatus.TIMEOUT: InventoryStatus.UNAVAILABLE,
        OperationStatus.UNKNOWN: InventoryStatus.UNAVAILABLE,
    }[status]


class ManifestInventoryCollector:
    def collect(
        self,
        manifest: ConfigManifest,
        backend: ExecutorBackend,
        *,
        fencing_token: int,
        desired_values: dict[str, Any] | None = None,
        persistent_values: dict[str, Any] | None = None,
        ownership: dict[str, str] | None = None,
        environment: EnvironmentFingerprint | None = None,
    ) -> ConfigInventoryReport:
        desired = desired_values or {}
        persistent = persistent_values or {}
        owners = ownership or {}
        items: list[ConfigInventoryItem] = []
        for item in sorted(manifest.items, key=lambda candidate: candidate.id):
            preflight = backend.preflight_check(item)
            probe = backend.probe(item, fencing_token=fencing_token)
            current_status = _inventory_status(probe.status)
            parameter_id = item.parameter_id
            items.append(
                ConfigInventoryItem(
                    item_id=item.id,
                    parameter_id=parameter_id,
                    target=item.target,
                    primary_component=item.primary_component.value,
                    related_components=[value.value for value in item.related_components],
                    preflight=ConfigStateField(
                        status=_inventory_status(preflight.status),
                        value=None,
                        source="backend compatibility and capability preflight",
                    ),
                    current=ConfigStateField(
                        status=current_status,
                        value=probe.value,
                        source="backend readback",
                    ),
                    desired=ConfigStateField(
                        status=(
                            InventoryStatus.SUCCEEDED
                            if parameter_id in desired
                            else InventoryStatus.UNAVAILABLE
                        ),
                        value=desired.get(parameter_id),
                        source="explicit desired values",
                    ),
                    effective=ConfigStateField(
                        status=current_status,
                        value=probe.value,
                        source="same readback as current; no separate effective source declared",
                    ),
                    persistent=ConfigStateField(
                        status=(
                            InventoryStatus.SUCCEEDED
                            if parameter_id in persistent
                            else InventoryStatus.UNAVAILABLE
                        ),
                        value=persistent.get(parameter_id),
                        source="explicit persistent values",
                    ),
                    ownership=ConfigStateField(
                        status=(
                            InventoryStatus.SUCCEEDED
                            if item.id in owners
                            else InventoryStatus.UNAVAILABLE
                        ),
                        value=owners.get(item.id),
                        source="explicit ownership map",
                    ),
                    raw_readback=probe.raw_output,
                    message=probe.message,
                )
            )
        fingerprint = environment or capture_environment_fingerprint()
        return ConfigInventoryReport(
            schema_version="looper.system-config-inventory/v1alpha2",
            metadata=InventoryMetadata(
                collector_environment=fingerprint,
                scope_limitations=_scope_limitations(
                    fingerprint, target_os=backend.capabilities.os
                ),
            ),
            target_id=backend.capabilities.target_id,
            target_os=backend.capabilities.os,
            manifest_id=manifest.id,
            manifest_digest=manifest.digest,
            counting_basis=(
                "one record per Config Manifest item; no deduplication; "
                f"declared={len(manifest.items)}, emitted={len(items)}"
            ),
            items=items,
        )


class LinuxDiscoveryPolicy(StrictModel):
    roots: list[Path] = Field(min_length=1)
    max_files: int = Field(ge=1, le=10000000)
    max_bytes_per_file: int = Field(ge=1, le=104857600)


class RawConfigRecord(StrictModel):
    root: str
    path: str
    status: InventoryStatus
    byte_length: int | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raw_base64: str | None = None
    raw_text: str | None = None
    message: str | None = None


class LinuxRawInventory(StrictModel):
    schema_version: str
    metadata: InventoryMetadata
    target_os: str
    counting_basis: str
    enumeration_complete: bool
    all_values_readable: bool
    complete: bool
    roots: list[str]
    records: list[RawConfigRecord]

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class LinuxRawCollector:
    def __init__(
        self,
        *,
        system_name: str | None = None,
        environment: EnvironmentFingerprint | None = None,
    ) -> None:
        self._system_name = (system_name or platform.system()).lower()
        self._environment = environment

    def collect(self, policy: LinuxDiscoveryPolicy) -> LinuxRawInventory:
        if self._system_name != "linux":
            raise RuntimeError("Linux raw configuration collection requires Linux")
        records: list[RawConfigRecord] = []
        enumeration_complete = True
        resolved_roots = [root.resolve() for root in policy.roots]
        for root in resolved_roots:
            if not root.exists() or not root.is_dir():
                records.append(
                    RawConfigRecord(
                        root=str(root),
                        path=str(root),
                        status=InventoryStatus.UNAVAILABLE,
                        message="configured discovery root is unavailable",
                    )
                )
                enumeration_complete = False
                continue
            for directory, names, filenames in os.walk(root, followlinks=False):
                names.sort()
                filenames.sort()
                for filename in filenames:
                    path = Path(directory, filename)
                    if path.is_symlink():
                        continue
                    if len(records) >= policy.max_files:
                        enumeration_complete = False
                        break
                    records.append(self._read(root, path, policy.max_bytes_per_file))
                if len(records) >= policy.max_files:
                    break
            if len(records) >= policy.max_files:
                break
        fingerprint = self._environment or capture_environment_fingerprint()
        all_values_readable = all(record.status == InventoryStatus.SUCCEEDED for record in records)
        return LinuxRawInventory(
            schema_version="looper.linux-raw-config-inventory/v1alpha2",
            metadata=InventoryMetadata(
                collector_environment=fingerprint,
                scope_limitations=_scope_limitations(fingerprint, target_os="linux"),
            ),
            target_os="linux",
            counting_basis=(
                "one record per non-symlink directory entry visited under explicit roots; "
                "no content or metadata deduplication"
            ),
            enumeration_complete=enumeration_complete,
            all_values_readable=all_values_readable,
            complete=enumeration_complete and all_values_readable,
            roots=[str(root) for root in resolved_roots],
            records=records,
        )

    @staticmethod
    def _read(root: Path, path: Path, maximum: int) -> RawConfigRecord:
        try:
            with path.open("rb") as handle:
                payload = handle.read(maximum + 1)
        except PermissionError as error:
            return RawConfigRecord(
                root=str(root),
                path=str(path),
                status=InventoryStatus.PERMISSION_DENIED,
                message=str(error),
            )
        except OSError as error:
            return RawConfigRecord(
                root=str(root),
                path=str(path),
                status=InventoryStatus.UNAVAILABLE,
                message=str(error),
            )
        if len(payload) > maximum:
            return RawConfigRecord(
                root=str(root),
                path=str(path),
                status=InventoryStatus.TOO_LARGE,
                byte_length=len(payload),
                message="file exceeded max_bytes_per_file; content was not retained",
            )
        return RawConfigRecord(
            root=str(root),
            path=str(path),
            status=InventoryStatus.SUCCEEDED,
            byte_length=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            raw_base64=base64.b64encode(payload).decode("ascii"),
            raw_text=payload.decode("utf-8", errors="replace"),
        )


class ToolCriticality(StrEnum):
    CRITICAL = "critical"
    OPTIONAL = "optional"


class ToolRequirement(StrictModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9.-]*$")
    executable: str = Field(min_length=1, max_length=200)
    criticality: ToolCriticality
    purpose: str = Field(min_length=1, max_length=1000)
    alternatives: list[str] = Field(default_factory=list)


class ToolInventoryItem(StrictModel):
    requirement_id: str
    executable: str
    criticality: ToolCriticality
    purpose: str
    alternatives: list[str]
    status: InventoryStatus
    selected_executable: str | None = None
    resolved_path: str | None = None
    message: str | None = None


class ToolInventoryReport(StrictModel):
    schema_version: str
    metadata: InventoryMetadata
    target_os: str
    counting_basis: str
    items: list[ToolInventoryItem]
    critical_missing: list[str]
    critical_executables_resolved: bool
    verification_scope: str

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def parse_tool_requirements_yaml(content: str) -> list[ToolRequirement]:
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ValueError("tool requirements YAML is invalid") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("requirements"), list):
        raise ValueError("tool requirements YAML must contain a requirements list")
    try:
        requirements = TypeAdapter(list[ToolRequirement]).validate_python(payload["requirements"])
    except ValidationError as error:
        raise ValueError(str(error)) from error
    ids = [requirement.id for requirement in requirements]
    if len(ids) != len(set(ids)):
        raise ValueError("tool requirement ids must be unique")
    return requirements


class LocalToolInventoryCollector:
    def __init__(
        self,
        *,
        system_name: str | None = None,
        environment: EnvironmentFingerprint | None = None,
    ) -> None:
        self._system_name = (system_name or platform.system()).lower()
        self._environment = environment

    def collect(self, requirements: list[ToolRequirement]) -> ToolInventoryReport:
        if self._system_name != "linux":
            raise RuntimeError("local tool inventory requires Linux")
        items: list[ToolInventoryItem] = []
        for requirement in sorted(requirements, key=lambda value: value.id):
            selected: str | None = None
            resolved: str | None = None
            for executable in [requirement.executable, *requirement.alternatives]:
                candidate = shutil.which(executable)
                if candidate is not None:
                    selected = executable
                    resolved = candidate
                    break
            items.append(
                ToolInventoryItem(
                    requirement_id=requirement.id,
                    executable=requirement.executable,
                    criticality=requirement.criticality,
                    purpose=requirement.purpose,
                    alternatives=requirement.alternatives,
                    status=(
                        InventoryStatus.SUCCEEDED
                        if selected is not None
                        else InventoryStatus.UNAVAILABLE
                    ),
                    selected_executable=selected,
                    resolved_path=resolved,
                    message=(
                        None
                        if selected == requirement.executable
                        else (
                            f"explicit alternative {selected!r} selected"
                            if selected is not None
                            else "no declared executable or explicit alternative is installed"
                        )
                    ),
                )
            )
        critical_missing = [
            item.requirement_id
            for item in items
            if item.criticality == ToolCriticality.CRITICAL
            and item.status != InventoryStatus.SUCCEEDED
        ]
        fingerprint = self._environment or capture_environment_fingerprint()
        return ToolInventoryReport(
            schema_version="looper.local-tool-inventory/v1alpha1",
            metadata=InventoryMetadata(
                collector_environment=fingerprint,
                scope_limitations=_scope_limitations(fingerprint, target_os="linux"),
            ),
            target_os="linux",
            counting_basis=(
                "one record per explicit tool requirement; no inferred alternatives or "
                "package names; no automatic installation"
            ),
            items=items,
            critical_missing=critical_missing,
            critical_executables_resolved=not critical_missing,
            verification_scope=(
                "PATH resolution only; executable presence does not prove workload, "
                "kernel feature, PMU event, permission, or measurement usability"
            ),
        )


__all__ = [
    "ConfigInventoryReport",
    "ConfigInventoryItem",
    "EnvironmentFingerprint",
    "InventoryStatus",
    "InventoryMetadata",
    "LinuxDiscoveryPolicy",
    "LinuxRawCollector",
    "LinuxRawInventory",
    "LocalToolInventoryCollector",
    "ManifestInventoryCollector",
    "RawConfigRecord",
    "ToolCriticality",
    "ToolInventoryReport",
    "ToolRequirement",
    "capture_environment_fingerprint",
    "parse_tool_requirements_yaml",
]
