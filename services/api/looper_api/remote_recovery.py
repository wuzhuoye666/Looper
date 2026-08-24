"""Rebuild remembered remote Worker tunnels after control-plane restart."""

from __future__ import annotations

import logging

from looper_api.config import Settings
from looper_api.database import SessionLocal
from looper_api.models import TargetRecord
from looper_api.remote_credentials import EncryptedSshCredentialStore, RemoteCredentialError
from looper_api.remote_worker import deploy_remote_worker

logger = logging.getLogger(__name__)


def remembered_target_ids(settings: Settings) -> list[str]:
    return EncryptedSshCredentialStore(settings).verified_target_ids()


def remembered_target_request(
    target: TargetRecord,
    settings: Settings,
):
    """Load a saved SSH request and keep it pinned to the discovered host key."""

    if target.provider not in {"external", "tencent", "alibaba", "volcengine", "baidu"}:
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
            or target.provider not in {"external", "tencent", "alibaba", "volcengine", "baidu"}
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
