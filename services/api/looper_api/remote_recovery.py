"""Rebuild remembered remote Worker tunnels after control-plane restart."""

from __future__ import annotations

import logging

from looper_api.config import Settings
from looper_api.database import SessionLocal
from looper_api.models import TargetRecord
from looper_api.remote_credentials import EncryptedSshCredentialStore
from looper_api.remote_worker import deploy_remote_worker

logger = logging.getLogger(__name__)


def remembered_target_ids(settings: Settings) -> list[str]:
    return EncryptedSshCredentialStore(settings).target_ids()


def recover_remembered_target(target_id: str, settings: Settings) -> bool:
    store = EncryptedSshCredentialStore(settings)
    request = store.load(target_id)
    with SessionLocal() as session:
        target = session.get(TargetRecord, target_id)
        if (
            target is None
            or target.provider != "external"
            or target.lifecycle_status != "active"
        ):
            return True
        persisted_key = target.fingerprint_json.get("host_key_sha256")
        if not persisted_key or request.expected_host_key_sha256 != persisted_key:
            logger.error("Refusing remote Worker recovery for %s: host-key pin mismatch", target_id)
            return True
        deploy_remote_worker(request, target, settings)
        return True
