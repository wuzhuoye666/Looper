from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
from typing import Any, Literal, Protocol

from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import (
    ActivationMode,
    ConfigCategory,
    ConfigComponent,
    ConfigItem,
)
from looper_core.system_opt.executor import (
    BackendCapabilities,
    BackendKind,
    CommandResult,
    ConfigSnapshot,
    OperationResult,
    OperationStatus,
    PreflightCheckResult,
    ProbeResult,
    SnapshotEntry,
)
from looper_core.system_opt.executor.local_linux import parse_readback
from pydantic import Field
from sqlalchemy.orm import Session

from looper_api.config import Settings
from looper_api.external_targets import ConnectExternalTargetRequest, open_ssh_client
from looper_api.models import TargetRecord
from looper_api.remote_recovery import remembered_target_request

_TARGET_PATH = re.compile(
    r"^/sys/block/[A-Za-z0-9._:-]+/queue/(?P<control>scheduler|nomerges)$"
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_REMOTE_OUTPUT = 64 * 1024

# This fixed helper is the complete remote write surface. It validates the sysfs
# path and value again on the guest, serializes every operation with flock, and
# persists a monotonic fence in /run. Callers cannot supply code or a command.
_REMOTE_HELPER = r'''
import base64,fcntl,hashlib,json,os,re,sys
path_re=re.compile(r"^/sys/block/[A-Za-z0-9._:-]+/queue/(scheduler|nomerges)$")
def emit(status,**values):
 print(json.dumps({"status":status,**values},sort_keys=True,separators=(",",":")))
def fail(code,message,exit_code=1):
 emit("failed",code=code,message=message);sys.exit(exit_code)
if len(sys.argv)!=5: fail("invalid_arguments","fixed helper requires four arguments")
operation,path,value,token_text=sys.argv[1:]
match=path_re.fullmatch(path)
if operation not in {"probe","write"} or match is None:
 fail("operation_not_authorized","operation or path is outside the restricted contract")
try: token=int(token_text)
except ValueError: fail("invalid_fencing_token","fencing token is not an integer")
if token<0: fail("invalid_fencing_token","fencing token must be non-negative")
control=match.group(1)
if operation=="write":
 if control=="scheduler" and re.fullmatch(r"[A-Za-z0-9_-]{1,32}",value) is None:
  fail("value_not_authorized","scheduler value contains unsupported characters")
 if control=="nomerges" and value not in {"0","1","2"}:
  fail("value_not_authorized","nomerges value must be 0, 1, or 2")
os.makedirs("/run/looper-system-optimization",mode=0o700,exist_ok=True)
fence_name=hashlib.sha256(path.encode()).hexdigest()+".fence"
fence_path=os.path.join("/run/looper-system-optimization",fence_name)
fence_fd=os.open(fence_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
try:
 fcntl.flock(fence_fd,fcntl.LOCK_EX)
 stored=os.read(fence_fd,64).decode().strip()
 try: latest=int(stored) if stored else -1
 except ValueError: fail("fence_corrupt","remote fencing state is invalid")
 if token<latest: fail("stale_fencing_token",f"token {token} is older than {latest}",73)
 if token>latest:
  os.lseek(fence_fd,0,os.SEEK_SET);os.ftruncate(fence_fd,0)
  os.write(fence_fd,str(token).encode());os.fsync(fence_fd)
 flags=os.O_RDONLY|os.O_NOFOLLOW
 if operation=="write":
  target_fd=os.open(path,os.O_WRONLY|os.O_NOFOLLOW)
  try: os.write(target_fd,(value+"\n").encode())
  finally: os.close(target_fd)
 target_fd=os.open(path,flags)
 try: content=os.read(target_fd,8192)
 finally: os.close(target_fd)
 digest="sha256:"+hashlib.sha256(content).hexdigest()
 emit("succeeded",contentB64=base64.b64encode(content).decode(),contentSha256=digest,
      fencingToken=token,helperSha256=HELPER_SHA256)
finally:
 os.close(fence_fd)
'''.strip()
_REMOTE_HELPER_DIGEST = "sha256:" + hashlib.sha256(_REMOTE_HELPER.encode()).hexdigest()
_REMOTE_HELPER = _REMOTE_HELPER.replace("HELPER_SHA256", repr(_REMOTE_HELPER_DIGEST))
# The embedded digest changes the final bytes, so the transport digest covers the
# exact executable payload while helperSha256 identifies the reviewed template.
RESTRICTED_HELPER_TRANSPORT_DIGEST = "sha256:" + hashlib.sha256(
    _REMOTE_HELPER.encode()
).hexdigest()


class RestrictedAlibabaBindingError(ValueError):
    """The target cannot be bound to the deliberately narrow v1 write surface."""


class BoundAlibabaSshTarget(StrictModel):
    target_id: str = Field(min_length=1, max_length=100)
    provider: Literal["alibaba"] = "alibaba"
    endpoint: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    host_key_sha256: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    credential_binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RestrictedRemoteResponse(StrictModel):
    status: Literal["succeeded", "failed"]
    contentB64: str | None = None
    contentSha256: str | None = None
    fencingToken: int | None = None
    helperSha256: str | None = None
    code: str | None = None
    message: str | None = None


class RestrictedRemoteAuditEvent(StrictModel):
    operation: Literal["probe", "apply", "verify", "rollback"]
    target_id: str
    item_id: str
    path: str
    fencing_token: int
    helper_transport_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    credential_binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requested_value_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    readback_content_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    status: OperationStatus
    error_code: str | None = None


class RestrictedRemoteRunner(Protocol):
    def run(self, argv: list[str], *, timeout_seconds: float) -> CommandResult: ...


class ParamikoRestrictedRunner:
    def __init__(self, request: ConnectExternalTargetRequest) -> None:
        if not request.expected_host_key_sha256:
            raise RestrictedAlibabaBindingError(
                "restricted SSH runner requires a pinned host key"
            )
        self._request = request

    def run(self, argv: list[str], *, timeout_seconds: float) -> CommandResult:
        command = shlex.join(argv)
        client = open_ssh_client(self._request)
        try:
            _stdin, stdout, stderr = client.exec_command(
                command, timeout=timeout_seconds, get_pty=False
            )
            exit_code = stdout.channel.recv_exit_status()
            output = stdout.read(_MAX_REMOTE_OUTPUT + 1)
            error = stderr.read(_MAX_REMOTE_OUTPUT + 1)
            if len(output) > _MAX_REMOTE_OUTPUT or len(error) > _MAX_REMOTE_OUTPUT:
                return CommandResult(
                    status=OperationStatus.FAILED,
                    exit_code=exit_code,
                    stderr="restricted remote output exceeded the safety limit",
                )
            return CommandResult(
                status=(
                    OperationStatus.SUCCEEDED
                    if exit_code == 0
                    else OperationStatus.FAILED
                ),
                exit_code=exit_code,
                stdout=output.decode("utf-8", errors="replace"),
                stderr=error.decode("utf-8", errors="replace"),
            )
        except (OSError, TimeoutError) as error:
            return CommandResult(status=OperationStatus.UNAVAILABLE, stderr=str(error)[:500])
        finally:
            client.close()


def bind_alibaba_ssh_target(
    session: Session, target_id: str, settings: Settings
) -> tuple[BoundAlibabaSshTarget, ConnectExternalTargetRequest]:
    target = session.get(TargetRecord, target_id)
    if target is None:
        raise RestrictedAlibabaBindingError("target does not exist")
    if target.provider != "alibaba":
        raise RestrictedAlibabaBindingError(
            "restricted v1 executor only supports Alibaba Cloud ECS targets"
        )
    request = remembered_target_request(target, settings)
    host_key = str(target.fingerprint_json.get("host_key_sha256") or "")
    if request.expected_host_key_sha256 != host_key:
        raise RestrictedAlibabaBindingError(
            "saved credential host key is not bound to the target inventory"
        )
    binding = {
        "schemaVersion": "looper.alibaba-ssh-credential-binding/v1alpha1",
        "targetId": target.id,
        "provider": target.provider,
        "endpoint": request.endpoint,
        "port": request.port,
        "username": request.username,
        "hostKeySha256": host_key,
    }
    return (
        BoundAlibabaSshTarget(
            target_id=target.id,
            endpoint=request.endpoint,
            port=request.port,
            username=request.username,
            host_key_sha256=host_key,
            credential_binding_digest=canonical_digest(binding),
        ),
        request,
    )


def _authorized_item(item: ConfigItem) -> bool:
    return bool(
        item.category == ConfigCategory.IO
        and item.primary_component == ConfigComponent.STORAGE
        and item.activation == ActivationMode.IMMEDIATE
        and item.searchable
        and _TARGET_PATH.fullmatch(item.target)
    )


class RestrictedAlibabaSysfsBackend:
    def __init__(
        self,
        binding: BoundAlibabaSshTarget,
        runner: RestrictedRemoteRunner,
    ) -> None:
        self.binding = binding
        self.runner = runner
        self.audit_events: list[RestrictedRemoteAuditEvent] = []
        self._latest_fencing_token = -1
        self._privilege_prefix: list[str] | None = None
        self._capabilities = BackendCapabilities(
            kind=BackendKind.SSH_REMOTE,
            target_id=binding.target_id,
            os="linux-alibaba-ecs-restricted-sysfs",
            enabled=True,
            privileged=False,
            categories={ConfigCategory.IO},
            supports_fencing=True,
            out_of_band_recovery=True,
        )

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def _privilege(self) -> list[str] | None:
        identity = self.runner.run(["id", "-u"], timeout_seconds=10)
        if identity.status != OperationStatus.SUCCEEDED:
            return None
        if identity.stdout.strip() == "0":
            self._privilege_prefix = []
        else:
            sudo = self.runner.run(["sudo", "-n", "--", "true"], timeout_seconds=10)
            if sudo.status != OperationStatus.SUCCEEDED:
                return None
            self._privilege_prefix = ["sudo", "-n", "--"]
        self._capabilities = self._capabilities.model_copy(
            update={"privileged": True}
        )
        return list(self._privilege_prefix)

    def _prefix(self) -> list[str] | None:
        return (
            list(self._privilege_prefix)
            if self._privilege_prefix is not None
            else self._privilege()
        )

    def preflight_check(self, item: ConfigItem) -> PreflightCheckResult:
        if not _authorized_item(item):
            return PreflightCheckResult(
                item_id=item.id,
                status=OperationStatus.PERMISSION_DENIED,
                message="item is outside the scheduler/nomerges sysfs contract",
            )
        prefix = self._prefix()
        if prefix is None:
            return PreflightCheckResult(
                item_id=item.id,
                status=OperationStatus.PERMISSION_DENIED,
                message="remote account is neither root nor authorized for sudo -n",
            )
        check = self.runner.run(
            [*prefix, "python3", "-c", "import sys;sys.exit(0)"],
            timeout_seconds=10,
        )
        if check.status != OperationStatus.SUCCEEDED:
            return PreflightCheckResult(
                item_id=item.id,
                status=OperationStatus.UNAVAILABLE,
                message="python3 is unavailable through the verified privilege path",
            )
        return PreflightCheckResult(item_id=item.id, status=OperationStatus.SUCCEEDED)

    def _command(
        self,
        operation: Literal["probe", "write"],
        item: ConfigItem,
        value: str,
        fencing_token: int,
    ) -> tuple[CommandResult, RestrictedRemoteResponse | None]:
        if not _authorized_item(item):
            return (
                CommandResult(
                    status=OperationStatus.PERMISSION_DENIED,
                    stderr="item is outside the restricted sysfs contract",
                ),
                None,
            )
        if fencing_token < self._latest_fencing_token or fencing_token < 0:
            return (
                CommandResult(
                    status=OperationStatus.PERMISSION_DENIED,
                    stderr="stale or negative fencing token",
                ),
                None,
            )
        prefix = self._prefix()
        if prefix is None:
            return (
                CommandResult(
                    status=OperationStatus.PERMISSION_DENIED,
                    stderr="root or sudo -n is required",
                ),
                None,
            )
        result = self.runner.run(
            [
                *prefix,
                "python3",
                "-c",
                _REMOTE_HELPER,
                operation,
                item.target,
                value,
                str(fencing_token),
            ],
            timeout_seconds=30,
        )
        self._latest_fencing_token = max(self._latest_fencing_token, fencing_token)
        try:
            response = RestrictedRemoteResponse.model_validate(json.loads(result.stdout))
        except (ValueError, json.JSONDecodeError):
            return result, None
        if response.status == "succeeded" and (
            response.fencingToken != fencing_token
            or response.helperSha256 != _REMOTE_HELPER_DIGEST
        ):
            return result, None
        return result, response

    @staticmethod
    def _content(response: RestrictedRemoteResponse) -> tuple[str, str] | None:
        if (
            response.status != "succeeded"
            or response.contentB64 is None
            or response.contentSha256 is None
            or not _DIGEST.fullmatch(response.contentSha256)
        ):
            return None
        try:
            raw = base64.b64decode(response.contentB64, validate=True)
        except ValueError:
            return None
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if actual != response.contentSha256:
            return None
        return raw.decode("utf-8", errors="strict"), actual

    def _audit(
        self,
        operation: Literal["probe", "apply", "verify", "rollback"],
        item: ConfigItem,
        fencing_token: int,
        status: OperationStatus,
        *,
        requested: Any | None = None,
        content_digest: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self.audit_events.append(
            RestrictedRemoteAuditEvent(
                operation=operation,
                target_id=self.binding.target_id,
                item_id=item.id,
                path=item.target,
                fencing_token=fencing_token,
                helper_transport_digest=RESTRICTED_HELPER_TRANSPORT_DIGEST,
                credential_binding_digest=self.binding.credential_binding_digest,
                requested_value_digest=(
                    canonical_digest({"value": requested})
                    if requested is not None
                    else None
                ),
                readback_content_digest=content_digest,
                status=status,
                error_code=error_code,
            )
        )

    def probe(self, item: ConfigItem, *, fencing_token: int) -> ProbeResult:
        result, response = self._command("probe", item, "", fencing_token)
        content = self._content(response) if response is not None else None
        if result.status != OperationStatus.SUCCEEDED or content is None:
            status = (
                result.status
                if result.status != OperationStatus.SUCCEEDED
                else OperationStatus.FAILED
            )
            error_code = (
                response.code if response is not None else None
            ) or "invalid_remote_evidence"
            self._audit(
                "probe", item, fencing_token, status, error_code=error_code
            )
            return ProbeResult(
                item_id=item.id,
                status=status,
                message=(
                    (response.message if response is not None else result.stderr)
                    or "remote evidence validation failed"
                )[:500],
            )
        raw, digest = content
        try:
            value = parse_readback(item, raw)
        except ValueError as error:
            self._audit(
                "probe",
                item,
                fencing_token,
                OperationStatus.FAILED,
                content_digest=digest,
                error_code="readback_invalid",
            )
            return ProbeResult(
                item_id=item.id,
                status=OperationStatus.FAILED,
                message=str(error),
                raw_output=raw[:1000],
            )
        self._audit(
            "probe", item, fencing_token, OperationStatus.SUCCEEDED, content_digest=digest
        )
        return ProbeResult(
            item_id=item.id,
            status=OperationStatus.SUCCEEDED,
            value=value,
            raw_output=raw[:1000],
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
        return ConfigSnapshot(target_id=self.binding.target_id, entries=entries)

    def _write(
        self,
        operation: Literal["apply", "rollback"],
        item: ConfigItem,
        value: Any,
        *,
        fencing_token: int,
    ) -> OperationResult:
        try:
            item.validate_value(value)
            encoded = item.encode_value(value)
        except ValueError as error:
            self._audit(
                operation,
                item,
                fencing_token,
                OperationStatus.PERMISSION_DENIED,
                requested=value,
                error_code="value_not_authorized",
            )
            return OperationResult(
                operation=operation,
                item_id=item.id,
                status=OperationStatus.PERMISSION_DENIED,
                requested_value=value,
                message=str(error),
            )
        result, response = self._command("write", item, encoded, fencing_token)
        content = self._content(response) if response is not None else None
        if result.status != OperationStatus.SUCCEEDED or content is None:
            status = (
                result.status
                if result.status != OperationStatus.SUCCEEDED
                else OperationStatus.FAILED
            )
            error_code = (
                response.code if response is not None else None
            ) or "invalid_remote_evidence"
            self._audit(
                operation,
                item,
                fencing_token,
                status,
                requested=value,
                error_code=error_code,
            )
            return OperationResult(
                operation=operation,
                item_id=item.id,
                status=status,
                requested_value=value,
                message=(
                    (response.message if response is not None else result.stderr)
                    or "remote evidence validation failed"
                )[:500],
            )
        raw, digest = content
        try:
            readback = parse_readback(item, raw)
        except ValueError as error:
            readback = None
            status = OperationStatus.FAILED
            message = str(error)
            error_code = "readback_invalid"
        else:
            matches = canonical_json(readback) == canonical_json(value)
            status = OperationStatus.SUCCEEDED if matches else OperationStatus.FAILED
            message = None if matches else "remote readback does not match requested value"
            error_code = None if matches else "readback_mismatch"
        self._audit(
            operation,
            item,
            fencing_token,
            status,
            requested=value,
            content_digest=digest,
            error_code=error_code,
        )
        return OperationResult(
            operation=operation,
            item_id=item.id,
            status=status,
            requested_value=value,
            readback_value=readback,
            message=message,
        )

    def apply(self, item: ConfigItem, value: Any, *, fencing_token: int) -> OperationResult:
        return self._write("apply", item, value, fencing_token=fencing_token)

    def verify(self, item: ConfigItem, expected: Any, *, fencing_token: int) -> OperationResult:
        result = self.probe(item, fencing_token=fencing_token)
        matches = result.status == OperationStatus.SUCCEEDED and canonical_json(
            result.value
        ) == canonical_json(expected)
        status = OperationStatus.SUCCEEDED if matches else OperationStatus.FAILED
        content_digest = (
            self.audit_events[-1].readback_content_digest if self.audit_events else None
        )
        self._audit(
            "verify",
            item,
            fencing_token,
            status,
            requested=expected,
            content_digest=content_digest,
            error_code=None if matches else "readback_mismatch",
        )
        return OperationResult(
            operation="verify",
            item_id=item.id,
            status=status,
            requested_value=expected,
            readback_value=result.value,
            message=None if matches else "remote readback does not match expected value",
        )

    def rollback(
        self, item: ConfigItem, snapshot_value: Any, *, fencing_token: int
    ) -> OperationResult:
        return self._write(
            "rollback", item, snapshot_value, fencing_token=fencing_token
        )


def build_restricted_alibaba_sysfs_backend(
    session: Session,
    target_id: str,
    settings: Settings,
) -> RestrictedAlibabaSysfsBackend:
    binding, request = bind_alibaba_ssh_target(session, target_id, settings)
    return RestrictedAlibabaSysfsBackend(binding, ParamikoRestrictedRunner(request))


__all__ = [
    "RESTRICTED_HELPER_TRANSPORT_DIGEST",
    "BoundAlibabaSshTarget",
    "ParamikoRestrictedRunner",
    "RestrictedAlibabaBindingError",
    "RestrictedAlibabaSysfsBackend",
    "RestrictedRemoteAuditEvent",
    "RestrictedRemoteRunner",
    "bind_alibaba_ssh_target",
    "build_restricted_alibaba_sysfs_backend",
]
