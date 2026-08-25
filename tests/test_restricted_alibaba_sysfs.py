from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import looper_api.restricted_alibaba_sysfs as restricted_module
import pytest
from looper_api.config import Settings
from looper_api.external_targets import ConnectExternalTargetRequest
from looper_api.models import Base, TargetRecord
from looper_api.restricted_alibaba_sysfs import (
    BoundAlibabaSshTarget,
    RestrictedAlibabaSysfsBackend,
    bind_alibaba_ssh_target,
)
from looper_core.canonical import canonical_digest
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.executor import CommandResult, OperationStatus
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def _digest(seed: str) -> str:
    return canonical_digest({"seed": seed})


def _binding() -> BoundAlibabaSshTarget:
    return BoundAlibabaSshTarget(
        target_id="target-a",
        endpoint="10.0.0.8",
        port=22,
        username="root",
        host_key_sha256="SHA256:" + "A" * 43,
        credential_binding_digest=_digest("credential-binding"),
    )


class FakeRestrictedRunner:
    def __init__(
        self,
        *,
        root: bool = True,
        sudo_allowed: bool = True,
        tamper_digest: bool = False,
    ) -> None:
        self.root = root
        self.sudo_allowed = sudo_allowed
        self.tamper_digest = tamper_digest
        self.state = {"scheduler": "mq-deadline", "nomerges": "0"}
        self.commands: list[list[str]] = []

    def run(self, argv: list[str], *, timeout_seconds: float) -> CommandResult:
        del timeout_seconds
        self.commands.append(list(argv))
        if argv == ["id", "-u"]:
            return CommandResult(
                status=OperationStatus.SUCCEEDED,
                exit_code=0,
                stdout="0\n" if self.root else "1000\n",
            )
        if argv == ["sudo", "-n", "--", "true"]:
            return CommandResult(
                status=(
                    OperationStatus.SUCCEEDED
                    if self.sudo_allowed
                    else OperationStatus.PERMISSION_DENIED
                ),
                exit_code=0 if self.sudo_allowed else 1,
            )
        if argv[-2:] == ["-c", "import sys;sys.exit(0)"]:
            return CommandResult(status=OperationStatus.SUCCEEDED, exit_code=0)
        operation, path, value, token = argv[-4:]
        assert operation in {"probe", "write"}
        control = path.rsplit("/", 1)[-1]
        if operation == "write":
            self.state[control] = value
        if control == "scheduler":
            alternatives = ["none", "mq-deadline"]
            raw = " ".join(
                f"[{candidate}]" if candidate == self.state[control] else candidate
                for candidate in alternatives
            ) + "\n"
        else:
            raw = self.state[control] + "\n"
        content = raw.encode()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if self.tamper_digest:
            digest = _digest("tampered")
        response: dict[str, Any] = {
            "status": "succeeded",
            "contentB64": base64.b64encode(content).decode(),
            "contentSha256": digest,
            "fencingToken": int(token),
            "helperSha256": restricted_module._REMOTE_HELPER_DIGEST,
        }
        return CommandResult(
            status=OperationStatus.SUCCEEDED,
            exit_code=0,
            stdout=json.dumps(response),
        )


def test_restricted_backend_runs_snapshot_apply_readback_and_rollback() -> None:
    runner = FakeRestrictedRunner()
    backend = RestrictedAlibabaSysfsBackend(_binding(), runner)
    item = build_demo_manifest().item("storage-scheduler")

    assert backend.preflight_check(item).succeeded
    snapshot = backend.snapshot([item], fencing_token=7)
    assert snapshot.complete
    assert snapshot.entries[item.id].value == "mq-deadline"

    applied = backend.apply(item, "none", fencing_token=7)
    assert applied.succeeded
    assert applied.readback_value == "none"
    assert backend.verify(item, "none", fencing_token=7).succeeded

    restored = backend.rollback(item, snapshot.entries[item.id].value, fencing_token=7)
    assert restored.succeeded
    assert backend.verify(item, "mq-deadline", fencing_token=7).succeeded
    assert all(
        event.credential_binding_digest == _digest("credential-binding")
        for event in backend.audit_events
    )
    assert all(event.readback_content_digest for event in backend.audit_events)

    remote_calls = [command for command in runner.commands if "python3" in command]
    helper_calls = [
        command
        for command in remote_calls
        if len(command) >= 4 and command[-4] in {"probe", "write"}
    ]
    assert helper_calls
    assert {command[-3] for command in helper_calls} == {
        "/sys/block/sda/queue/scheduler"
    }
    assert {command[-4] for command in helper_calls} <= {"probe", "write"}
    assert all(command[-5] == restricted_module._REMOTE_HELPER for command in helper_calls)


def test_restricted_backend_rejects_unscoped_path_value_and_stale_fence() -> None:
    runner = FakeRestrictedRunner()
    backend = RestrictedAlibabaSysfsBackend(_binding(), runner)
    item = build_demo_manifest().item("storage-scheduler")
    payload = item.model_dump(mode="python")
    payload["target"] = "/tmp/scheduler"
    unsafe = type(item).model_validate(payload)

    denied = backend.preflight_check(unsafe)
    assert denied.status == OperationStatus.PERMISSION_DENIED
    assert backend.apply(item, "kyber", fencing_token=3).status == OperationStatus.PERMISSION_DENIED
    assert backend.apply(item, "none", fencing_token=3).succeeded
    command_count = len(runner.commands)
    stale = backend.probe(item, fencing_token=2)
    assert stale.status == OperationStatus.PERMISSION_DENIED
    assert len(runner.commands) == command_count


def test_restricted_backend_requires_root_or_passwordless_sudo() -> None:
    runner = FakeRestrictedRunner(root=False, sudo_allowed=False)
    backend = RestrictedAlibabaSysfsBackend(_binding(), runner)

    result = backend.preflight_check(build_demo_manifest().item("storage-scheduler"))

    assert result.status == OperationStatus.PERMISSION_DENIED
    assert backend.capabilities.privileged is False


def test_remote_content_digest_mismatch_fails_closed_and_is_audited() -> None:
    runner = FakeRestrictedRunner(tamper_digest=True)
    backend = RestrictedAlibabaSysfsBackend(_binding(), runner)
    item = build_demo_manifest().item("storage-scheduler")

    result = backend.apply(item, "none", fencing_token=1)

    assert result.status == OperationStatus.FAILED
    assert backend.audit_events[-1].error_code == "invalid_remote_evidence"
    assert backend.audit_events[-1].readback_content_digest is None


def test_binding_requires_alibaba_inventory_and_exact_saved_host_key(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    request = ConnectExternalTargetRequest(
        endpoint="10.0.0.8",
        username="root",
        auth_method="password",
        password="secret",
        expected_host_key_sha256="SHA256:" + "A" * 43,
    )
    monkeypatch.setattr(
        restricted_module,
        "remembered_target_request",
        lambda _target, _settings: request,
    )
    with Session(engine) as session:
        target = TargetRecord(
            id="target-a",
            name="ecs-a",
            provider="alibaba",
            status="ready",
            capabilities_json=["ssh", "x86_64"],
            inventory_json={"source": "ssh-discovery"},
            fingerprint_json={"host_key_sha256": "SHA256:" + "A" * 43},
            snapshot_digest=_digest("target"),
            runnable=True,
            lifecycle_status="active",
            last_inventory_seen_at=now,
            inventory_missing_since=None,
            inventory_miss_count=0,
            archived_at=None,
            archive_reason=None,
            created_at=now,
            updated_at=now,
        )
        session.add(target)
        session.commit()

        binding, loaded = bind_alibaba_ssh_target(
            session, target.id, Settings(_env_file=None)
        )
        assert binding.host_key_sha256 == request.expected_host_key_sha256
        assert loaded is request

        target.provider = "tencent"
        session.flush()
        with pytest.raises(ValueError, match="only supports Alibaba"):
            bind_alibaba_ssh_target(session, target.id, Settings(_env_file=None))

        target.provider = "alibaba"
        target.fingerprint_json = {"host_key_sha256": "SHA256:" + "B" * 43}
        session.flush()
        with pytest.raises(ValueError, match="not bound"):
            bind_alibaba_ssh_target(session, target.id, Settings(_env_file=None))
