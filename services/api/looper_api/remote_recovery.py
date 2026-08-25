"""Rebuild remembered remote Worker tunnels after control-plane restart."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from looper_core.canonical import utc_now
from sqlalchemy import select

from looper_api.config import Settings
from looper_api.database import SessionLocal
from looper_api.models import TargetRecord, WorkerRecord
from looper_api.remote_credentials import EncryptedSshCredentialStore, RemoteCredentialError
from looper_api.remote_worker import deploy_remote_worker, deployment_status

logger = logging.getLogger(__name__)
REMOTE_TARGET_PROVIDERS = {"external", "tencent", "alibaba", "volcengine", "baidu"}

_recovery_locks: dict[str, threading.Lock] = {}
_recovery_locks_guard = threading.Lock()


class RemoteWorkerRecoveryError(RuntimeError):
    """A target cannot safely enter the queue until its Worker reconnects."""

    status_code = 503
    code = "remote_worker_recovery_failed"


def _recovery_lock(target_id: str) -> threading.Lock:
    with _recovery_locks_guard:
        lock = _recovery_locks.get(target_id)
        if lock is None:
            lock = threading.Lock()
            _recovery_locks[target_id] = lock
        return lock


def remembered_target_ids(settings: Settings) -> list[str]:
    return EncryptedSshCredentialStore(settings).verified_target_ids()


def remembered_target_request(
    target: TargetRecord,
    settings: Settings,
):
    """Load a saved SSH request and keep it pinned to the discovered host key."""

    if target.provider not in REMOTE_TARGET_PROVIDERS:
        raise RemoteCredentialError("target provider cannot reuse saved credentials")
    if target.lifecycle_status != "active":
        raise RemoteCredentialError("target is not active")
    request = EncryptedSshCredentialStore(settings).load(target.id)
    persisted_key = str(target.fingerprint_json.get("host_key_sha256") or "")
    if not persisted_key:
        raise RemoteCredentialError("target does not have a verified SSH host key")
    if request.expected_host_key_sha256 != persisted_key:
        raise RemoteCredentialError("saved SSH credentials do not match the verified host key")
    return request


def recover_remembered_target(target_id: str, settings: Settings) -> bool:
    with SessionLocal() as session:
        target = session.get(TargetRecord, target_id)
        if (
            target is None
            or target.provider not in REMOTE_TARGET_PROVIDERS
            or target.lifecycle_status != "active"
        ):
            return True
        try:
            request = remembered_target_request(target, settings)
        except RemoteCredentialError:
            logger.error("Refusing remote Worker recovery for %s: host-key pin mismatch", target_id)
            return True
        deploy_remote_worker(request, target, settings)
        return True


def _fresh_bound_worker(
    target_id: str,
    settings: Settings,
    *,
    worker_id: str | None = None,
    not_before: datetime | None = None,
) -> WorkerRecord | None:
    """Return a fresh Worker bound to the target and optional deployment ID."""

    cutoff = utc_now() - timedelta(seconds=settings.worker_stale_seconds)
    capability = f'target.{target_id}'
    with SessionLocal() as session:
        statement = select(WorkerRecord).where(
            WorkerRecord.status == "online",
            WorkerRecord.last_heartbeat_at >= cutoff,
            WorkerRecord.capabilities_json.like(f'%"{capability}"%'),
        )
        if worker_id is not None:
            statement = statement.where(WorkerRecord.id == worker_id)
        if not_before is not None:
            statement = statement.where(WorkerRecord.last_heartbeat_at >= not_before)
        return session.scalars(
            statement.order_by(WorkerRecord.last_heartbeat_at.desc()).limit(1)
        ).first()


def target_worker_ready(target_id: str, settings: Settings) -> bool:
    """Check Worker liveness and ownership of this process's reverse tunnel."""

    deployment = deployment_status(target_id)
    expected_worker = str(deployment.get("workerId") or "") or None
    deployed_at = deployment.get("deployedAt")
    deployment_generation = (
        datetime.fromisoformat(str(deployed_at)) if deployed_at else None
    )
    worker = _fresh_bound_worker(
        target_id,
        settings,
        worker_id=expected_worker,
        not_before=(
            deployment_generation
            if deployment.get("transport") == "reverse-tunnel"
            else None
        ),
    )
    if worker is None:
        return False
    if settings.remote_worker_api_url is not None:
        return True
    return bool(
        deployment.get("active")
        and deployment.get("transport") == "reverse-tunnel"
        and deployment.get("workerId") == worker.id
    )


def ensure_target_worker(
    target_id: str,
    settings: Settings,
    *,
    registration_timeout: float = 30.0,
) -> dict[str, Any]:
    """Synchronize a target to the current endpoint and wait for registration."""

    if target_worker_ready(target_id, settings):
        return {"status": "ready", **deployment_status(target_id)}

    with _recovery_lock(target_id):
        if target_worker_ready(target_id, settings):
            return {"status": "ready", **deployment_status(target_id)}
        with SessionLocal() as session:
            target = session.get(TargetRecord, target_id)
            if target is None or target.lifecycle_status != "active":
                raise RemoteWorkerRecoveryError("所选测试机器不存在或已停用")
            try:
                request = remembered_target_request(target, settings)
            except RemoteCredentialError as error:
                raise RemoteWorkerRecoveryError(
                    "测试机器 Worker 已离线，且没有可用于自动恢复的有效 SSH 凭据；"
                    "请在候选资源页重新连接该机器"
                ) from error
            deployed_after = utc_now()
            deployment = deploy_remote_worker(request, target, settings)

        worker_id = str(deployment.get("workerId") or "") or None
        deadline = time.monotonic() + max(0.0, registration_timeout)
        while True:
            worker = _fresh_bound_worker(
                target_id,
                settings,
                worker_id=worker_id,
                not_before=deployed_after,
            )
            current = deployment_status(target_id)
            tunnel_ready = settings.remote_worker_api_url is not None or bool(
                current.get("active")
                and current.get("transport") == "reverse-tunnel"
                and current.get("workerId") == worker_id
            )
            if worker is not None and tunnel_ready:
                return {"status": "recovered", **current}
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)

    raise RemoteWorkerRecoveryError(
        "测试机器 SSH 已连接，但 Worker 未能使用最新 API 地址完成注册；"
        "请检查远端 ~/.looper-worker/worker.log"
    )
