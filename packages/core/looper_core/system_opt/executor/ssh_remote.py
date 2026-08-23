from __future__ import annotations

import base64
import json
import re
from typing import Any

from looper_core.canonical import canonical_json
from looper_core.system_opt.config_manifest import ConfigCategory, ConfigItem, RollbackMode
from looper_core.system_opt.executor import (
    BackendCapabilities,
    BackendKind,
    CommandRunner,
    OperationStatus,
    PreflightCheckResult,
)
from looper_core.system_opt.executor.local_linux import LocalLinuxBackend

_SSH_DESTINATION = re.compile(r"^(?:[a-zA-Z0-9][a-zA-Z0-9._-]*@)?[a-zA-Z0-9][a-zA-Z0-9._:-]*$")


class SSHRemoteBackend(LocalLinuxBackend):
    def __init__(
        self,
        destination: str,
        *,
        target_id: str,
        enabled: bool = False,
        runner: CommandRunner | None = None,
        remote_helper: str = "looper-system-opt-agent",
        out_of_band_recovery: bool = False,
        privileged: bool = False,
        verified_preconditions: dict[str, bool] | None = None,
    ) -> None:
        if not _SSH_DESTINATION.fullmatch(destination) or destination.startswith("-"):
            raise ValueError("SSH destination contains unsupported characters")
        if not re.fullmatch(r"[a-zA-Z0-9._/-]+", remote_helper) or remote_helper.startswith("-"):
            raise ValueError("remote helper contains unsupported characters")
        self._destination = destination
        self._remote_helper = remote_helper
        self._verified_preconditions = dict(verified_preconditions or {})
        super().__init__(
            target_id=target_id,
            enabled=enabled,
            runner=runner,
            system_name="linux",
            privileged=privileged,
        )
        self._capabilities = BackendCapabilities(
            kind=BackendKind.SSH_REMOTE,
            target_id=target_id,
            os="linux-remote-unverified",
            enabled=enabled and runner is not None,
            privileged=privileged,
            categories=set(ConfigCategory),
            supports_fencing=True,
            out_of_band_recovery=out_of_band_recovery,
        )

    def preflight_check(self, item: ConfigItem) -> PreflightCheckResult:
        if self._verified_preconditions.get(item.id) is True:
            return PreflightCheckResult(item_id=item.id, status=OperationStatus.SUCCEEDED)
        return PreflightCheckResult(
            item_id=item.id,
            status=OperationStatus.UNAVAILABLE,
            message="remote compatibility and preconditions were not verified",
        )

    def _wrap(self, operation: str, argv: list[str]) -> list[str]:
        payload = canonical_json({"argv": argv, "operation": operation}).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "NumberOfPasswordPrompts=0",
            self._destination,
            self._remote_helper,
            "exec",
            encoded,
        ]

    def build_probe_command(self, item: ConfigItem) -> list[str]:
        remote = item.read.command.render(target=item.target, value="", snapshot="")
        return self._wrap("probe", remote)

    def build_apply_command(self, item: ConfigItem, value: Any) -> list[str]:
        if item.apply is None:
            raise ValueError(f"{item.id} has no apply command")
        encoded = item.encode_value(value)
        remote = item.apply.render(target=item.target, value=encoded, snapshot="")
        return self._wrap("apply", remote)

    def build_rollback_command(self, item: ConfigItem, snapshot_value: Any) -> list[str]:
        encoded = item.encode_value(snapshot_value)
        if item.rollback.mode == RollbackMode.COMMAND:
            assert item.rollback.command is not None
            remote = item.rollback.command.render(
                target=item.target, value=encoded, snapshot=encoded
            )
        else:
            if item.apply is None:
                raise ValueError(f"{item.id} cannot restore its snapshot")
            remote = item.apply.render(target=item.target, value=encoded, snapshot=encoded)
        return self._wrap("rollback", remote)

    @staticmethod
    def decode_transport_payload(command: list[str]) -> dict[str, Any]:
        encoded = command[-1]
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("SSH transport payload is not an object")
        return payload

    def _unavailable_message(self) -> str:
        if not self._requested_enabled:
            return "ssh-remote backend is disabled by default and unverified"
        return "ssh-remote backend requires an injected command runner"


__all__ = ["SSHRemoteBackend"]
