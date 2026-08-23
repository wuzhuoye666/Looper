from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigCategory, ConfigItem


class BackendKind(StrEnum):
    SIMULATED = "simulated"
    LOCAL_LINUX = "local-linux"
    SSH_REMOTE = "ssh-remote"


class OperationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission-denied"
    UNKNOWN = "unknown"


class BackendCapabilities(StrictModel):
    kind: BackendKind
    target_id: str = Field(min_length=1, max_length=200)
    os: str = Field(min_length=1, max_length=80)
    enabled: bool
    privileged: bool
    categories: set[ConfigCategory] = Field(default_factory=set)
    supports_fencing: bool
    out_of_band_recovery: bool = False


class ProbeResult(StrictModel):
    item_id: str
    status: OperationStatus
    value: Any | None = None
    argv: list[str] | None = None
    message: str | None = None
    raw_output: str | None = None
    virtual_elapsed_seconds: float = Field(default=0.0, ge=0)


class PreflightCheckResult(StrictModel):
    item_id: str
    status: OperationStatus
    message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == OperationStatus.SUCCEEDED


class SnapshotEntry(StrictModel):
    item_id: str
    target: str
    status: OperationStatus
    value: Any | None = None
    message: str | None = None
    raw_output: str | None = None


class ConfigSnapshot(StrictModel):
    target_id: str
    entries: dict[str, SnapshotEntry]

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=True))

    @property
    def complete(self) -> bool:
        return bool(self.entries) and all(
            entry.status == OperationStatus.SUCCEEDED for entry in self.entries.values()
        )


class OperationResult(StrictModel):
    operation: str
    item_id: str
    status: OperationStatus
    old_value: Any | None = None
    requested_value: Any | None = None
    readback_value: Any | None = None
    argv: list[str] | None = None
    message: str | None = None
    virtual_elapsed_seconds: float = Field(default=0.0, ge=0)

    @property
    def succeeded(self) -> bool:
        return self.status == OperationStatus.SUCCEEDED


class CommandResult(StrictModel):
    status: OperationStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    elapsed_seconds: float = Field(default=0.0, ge=0)


@runtime_checkable
class CommandRunner(Protocol):
    def run(self, argv: list[str], *, timeout_seconds: float) -> CommandResult: ...


@runtime_checkable
class ExecutorBackend(Protocol):
    @property
    def capabilities(self) -> BackendCapabilities: ...

    def probe(self, item: ConfigItem, *, fencing_token: int) -> ProbeResult: ...

    def preflight_check(self, item: ConfigItem) -> PreflightCheckResult: ...

    def snapshot(self, items: list[ConfigItem], *, fencing_token: int) -> ConfigSnapshot: ...

    def apply(self, item: ConfigItem, value: Any, *, fencing_token: int) -> OperationResult: ...

    def verify(self, item: ConfigItem, expected: Any, *, fencing_token: int) -> OperationResult: ...

    def rollback(
        self, item: ConfigItem, snapshot_value: Any, *, fencing_token: int
    ) -> OperationResult: ...


__all__ = [
    "BackendCapabilities",
    "BackendKind",
    "CommandResult",
    "CommandRunner",
    "ConfigSnapshot",
    "ExecutorBackend",
    "OperationResult",
    "OperationStatus",
    "PreflightCheckResult",
    "ProbeResult",
    "SnapshotEntry",
]
