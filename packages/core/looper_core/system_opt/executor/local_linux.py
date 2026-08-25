"""L0 本机 Linux 执行后端：argv 白名单 + 可写根 + 读回 + 权限状态。

架构层：L0（docs/system-optimizer/architecture/overall.md）。
写入默认禁用；WSL2 只读验证过，真实 CVM 写入未验收。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any

from looper_core.canonical import canonical_json
from looper_core.system_opt.config_manifest import (
    ConfigCategory,
    ConfigItem,
    RollbackMode,
    ValueParser,
)
from looper_core.system_opt.executor import (
    BackendCapabilities,
    BackendKind,
    CommandRunner,
    ConfigSnapshot,
    OperationResult,
    OperationStatus,
    PreflightCheckResult,
    ProbeResult,
    SnapshotEntry,
)

_BRACKET_VALUE = re.compile(r"\[([^\]]+)\]")


def parse_readback(item: ConfigItem, output: str) -> Any:
    value = output.strip()
    parser = item.read.parser
    if parser == ValueParser.INTEGER:
        parsed: Any = int(value)
    elif parser == ValueParser.NUMBER:
        parsed = float(value)
    elif parser == ValueParser.BOOLEAN:
        normalized = value.lower()
        true_values = {candidate.strip().lower() for candidate in item.read.true_values}
        false_values = {candidate.strip().lower() for candidate in item.read.false_values}
        if normalized in true_values:
            parsed = True
        elif normalized in false_values:
            parsed = False
        else:
            raise ValueError(f"readback for {item.id} is not a declared boolean token")
    elif parser == ValueParser.BRACKET_SELECTED:
        match = _BRACKET_VALUE.search(value)
        if match is None:
            raise ValueError(f"readback for {item.id} has no bracket-selected value")
        parsed = match.group(1)
    else:
        parsed = value
        item.validate_readback(parsed)
    return parsed


class LocalLinuxBackend:
    def __init__(
        self,
        *,
        target_id: str = "local",
        enabled: bool = False,
        runner: CommandRunner | None = None,
        system_name: str | None = None,
        privileged: bool = False,
        target_facts: dict[str, Any] | None = None,
    ) -> None:
        self._runner = runner
        self._system_name = (system_name or platform.system()).lower()
        self._requested_enabled = enabled
        self._latest_fencing_token = -1
        self._target_facts = {
            "os": self._system_name,
            "architecture": platform.machine().lower(),
            "kernel.release": platform.release(),
            **(target_facts or {}),
        }
        self._capabilities = BackendCapabilities(
            kind=BackendKind.LOCAL_LINUX,
            target_id=target_id,
            os=self._system_name,
            enabled=enabled and self._system_name == "linux" and runner is not None,
            privileged=privileged,
            categories=set(ConfigCategory),
            supports_fencing=True,
            out_of_band_recovery=False,
        )

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    @staticmethod
    def build_probe_command(item: ConfigItem) -> list[str]:
        return item.read.command.render(target=item.target, value="", snapshot="")

    @staticmethod
    def build_apply_command(item: ConfigItem, value: Any) -> list[str]:
        if item.apply is None:
            raise ValueError(f"{item.id} has no apply command")
        encoded = item.encode_value(value)
        return item.apply.render(target=item.target, value=encoded, snapshot="")

    @staticmethod
    def build_rollback_command(item: ConfigItem, snapshot_value: Any) -> list[str]:
        encoded = item.encode_value(snapshot_value)
        if item.rollback.mode == RollbackMode.COMMAND:
            assert item.rollback.command is not None
            return item.rollback.command.render(target=item.target, value=encoded, snapshot=encoded)
        if item.apply is None:
            raise ValueError(f"{item.id} cannot restore its snapshot without an apply command")
        return item.apply.render(target=item.target, value=encoded, snapshot=encoded)

    def _unavailable_message(self) -> str:
        if not self._requested_enabled:
            return "local-linux backend is disabled by default"
        if self._system_name != "linux":
            return "local-linux backend requires Linux"
        return "local-linux backend requires an injected command runner"

    def _accept_fence(self, fencing_token: int) -> str | None:
        if fencing_token < 0:
            return "fencing token must be non-negative"
        if fencing_token < self._latest_fencing_token:
            return f"stale fencing token {fencing_token}; latest is {self._latest_fencing_token}"
        self._latest_fencing_token = max(self._latest_fencing_token, fencing_token)
        return None

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, ...]:
        numbers = re.findall(r"\d+", value)
        return tuple(int(number) for number in numbers[:4])

    @staticmethod
    def _matches(actual: Any, operator: str, expected: Any) -> bool:
        if operator == "exists":
            return actual is not None and actual is not False
        if operator == "eq":
            return canonical_json(actual) == canonical_json(expected)
        if operator in {"gt", "gte"}:
            if isinstance(actual, bool) or isinstance(expected, bool):
                return False
            try:
                return actual > expected if operator == "gt" else actual >= expected
            except TypeError:
                return False
        if operator == "in":
            return isinstance(expected, list) and any(
                canonical_json(actual) == canonical_json(value) for value in expected
            )
        return False

    def preflight_check(self, item: ConfigItem) -> PreflightCheckResult:
        compatibility = item.compatibility
        kernel = self._version_tuple(str(self._target_facts["kernel.release"]))
        if compatibility.kernel_min and kernel < self._version_tuple(compatibility.kernel_min):
            return PreflightCheckResult(
                item_id=item.id,
                status=OperationStatus.FAILED,
                message=f"kernel is below {compatibility.kernel_min}",
            )
        if compatibility.kernel_max and kernel > self._version_tuple(compatibility.kernel_max):
            return PreflightCheckResult(
                item_id=item.id,
                status=OperationStatus.FAILED,
                message=f"kernel is above {compatibility.kernel_max}",
            )
        if compatibility.architectures:
            architecture = str(self._target_facts["architecture"]).lower()
            if architecture not in {value.lower() for value in compatibility.architectures}:
                return PreflightCheckResult(
                    item_id=item.id,
                    status=OperationStatus.FAILED,
                    message=f"architecture {architecture!r} is not allowed",
                )
        for command in compatibility.required_commands:
            if shutil.which(command) is None:
                return PreflightCheckResult(
                    item_id=item.id,
                    status=OperationStatus.UNAVAILABLE,
                    message=f"required command {command!r} is unavailable",
                )
        for path in compatibility.required_paths:
            target_path = Path(path)
            if not target_path.exists():
                return PreflightCheckResult(
                    item_id=item.id,
                    status=OperationStatus.UNAVAILABLE,
                    message=f"required path {path!r} is unavailable",
                )
            if not os.access(target_path, os.R_OK):
                return PreflightCheckResult(
                    item_id=item.id,
                    status=OperationStatus.PERMISSION_DENIED,
                    message=f"required path {path!r} is not readable",
                )
            if (
                item.apply is not None
                and item.apply.argv[0] == "write-file"
                and not os.access(target_path, os.W_OK)
            ):
                return PreflightCheckResult(
                    item_id=item.id,
                    status=OperationStatus.PERMISSION_DENIED,
                    message=f"required path {path!r} is not writable",
                )
        capability_values = {
            "enabled": self.capabilities.enabled,
            "privileged": self.capabilities.privileged,
            "supports_fencing": self.capabilities.supports_fencing,
            "out_of_band_recovery": self.capabilities.out_of_band_recovery,
            "os": self.capabilities.os,
        }
        for condition in item.preconditions:
            if condition.kind == "path":
                actual: Any = Path(condition.key).exists()
            elif condition.kind == "command":
                actual = shutil.which(condition.key) is not None
            elif condition.kind == "capability":
                actual = capability_values.get(condition.key)
            else:
                actual = self._target_facts.get(condition.key)
            if not self._matches(actual, condition.operator, condition.value):
                return PreflightCheckResult(
                    item_id=item.id,
                    status=(
                        OperationStatus.UNAVAILABLE if actual is None else OperationStatus.FAILED
                    ),
                    message=f"precondition {condition.kind}:{condition.key!s} did not match",
                )
        return PreflightCheckResult(item_id=item.id, status=OperationStatus.SUCCEEDED)

    def probe(self, item: ConfigItem, *, fencing_token: int) -> ProbeResult:
        if not self.capabilities.enabled or self._runner is None:
            return ProbeResult(
                item_id=item.id,
                status=OperationStatus.UNAVAILABLE,
                message=self._unavailable_message(),
            )
        fence_error = self._accept_fence(fencing_token)
        if fence_error:
            return ProbeResult(item_id=item.id, status=OperationStatus.FAILED, message=fence_error)
        argv = self.build_probe_command(item)
        command = self._runner.run(argv, timeout_seconds=item.read.command.timeout_seconds)
        if command.status != OperationStatus.SUCCEEDED:
            return ProbeResult(
                item_id=item.id,
                status=command.status,
                argv=argv,
                message=command.stderr or "read command failed",
                raw_output=command.stdout,
                virtual_elapsed_seconds=command.elapsed_seconds,
            )
        try:
            value = parse_readback(item, command.stdout)
        except (TypeError, ValueError) as error:
            return ProbeResult(
                item_id=item.id,
                status=OperationStatus.FAILED,
                argv=argv,
                message=str(error),
                raw_output=command.stdout,
                virtual_elapsed_seconds=command.elapsed_seconds,
            )
        return ProbeResult(
            item_id=item.id,
            status=OperationStatus.SUCCEEDED,
            value=value,
            argv=argv,
            raw_output=command.stdout,
            virtual_elapsed_seconds=command.elapsed_seconds,
        )

    def snapshot(self, items: list[ConfigItem], *, fencing_token: int) -> ConfigSnapshot:
        entries: dict[str, SnapshotEntry] = {}
        for item in sorted(items, key=lambda candidate: candidate.id):
            result = self.probe(item, fencing_token=fencing_token)
            entries[item.id] = SnapshotEntry(
                item_id=item.id,
                target=item.target,
                status=result.status,
                value=result.value,
                message=result.message,
                raw_output=result.raw_output,
            )
        return ConfigSnapshot(target_id=self.capabilities.target_id, entries=entries)

    def _run_change(
        self,
        operation: str,
        item: ConfigItem,
        requested: Any,
        argv: list[str],
        *,
        fencing_token: int,
        timeout_seconds: float,
    ) -> OperationResult:
        if not self.capabilities.enabled or self._runner is None:
            return OperationResult(
                operation=operation,
                item_id=item.id,
                status=OperationStatus.UNAVAILABLE,
                requested_value=requested,
                argv=argv,
                message=self._unavailable_message(),
            )
        fence_error = self._accept_fence(fencing_token)
        if fence_error:
            return OperationResult(
                operation=operation,
                item_id=item.id,
                status=OperationStatus.FAILED,
                requested_value=requested,
                argv=argv,
                message=fence_error,
            )
        command = self._runner.run(argv, timeout_seconds=timeout_seconds)
        return OperationResult(
            operation=operation,
            item_id=item.id,
            status=command.status,
            requested_value=requested,
            argv=argv,
            message=command.stderr or None,
            virtual_elapsed_seconds=command.elapsed_seconds,
        )

    def apply(self, item: ConfigItem, value: Any, *, fencing_token: int) -> OperationResult:
        argv = self.build_apply_command(item, value)
        assert item.apply is not None
        return self._run_change(
            "apply",
            item,
            value,
            argv,
            fencing_token=fencing_token,
            timeout_seconds=item.apply.timeout_seconds,
        )

    def verify(self, item: ConfigItem, expected: Any, *, fencing_token: int) -> OperationResult:
        probe = self.probe(item, fencing_token=fencing_token)
        matches = probe.status == OperationStatus.SUCCEEDED and canonical_json(
            probe.value
        ) == canonical_json(expected)
        status = probe.status
        message = probe.message
        if probe.status == OperationStatus.SUCCEEDED and not matches:
            status = OperationStatus.FAILED
            message = "readback does not match expected value"
        return OperationResult(
            operation="verify",
            item_id=item.id,
            status=OperationStatus.SUCCEEDED if matches else status,
            requested_value=expected,
            readback_value=probe.value,
            argv=probe.argv,
            message=None if matches else message,
            virtual_elapsed_seconds=probe.virtual_elapsed_seconds,
        )

    def rollback(
        self, item: ConfigItem, snapshot_value: Any, *, fencing_token: int
    ) -> OperationResult:
        argv = self.build_rollback_command(item, snapshot_value)
        timeout = (
            item.rollback.command.timeout_seconds
            if item.rollback.command is not None
            else item.apply.timeout_seconds
            if item.apply is not None
            else item.read.command.timeout_seconds
        )
        return self._run_change(
            "rollback",
            item,
            snapshot_value,
            argv,
            fencing_token=fencing_token,
            timeout_seconds=timeout,
        )


__all__ = ["LocalLinuxBackend", "parse_readback"]
