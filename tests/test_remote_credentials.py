from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from looper_api import remote_recovery
from looper_api.config import Settings
from looper_api.external_targets import ConnectExternalTargetRequest
from looper_api.remote_credentials import (
    EncryptedSshCredentialStore,
    RemoteCredentialError,
)


def _request(**overrides) -> ConnectExternalTargetRequest:
    payload = {
        "endpoint": "10.0.0.8",
        "port": 22,
        "username": "ubuntu",
        "auth_method": "password",
        "password": "one-time-secret",
        "timeout_seconds": 7,
    }
    payload.update(overrides)
    return ConnectExternalTargetRequest.model_validate(payload)


def test_ssh_credentials_round_trip_without_plaintext_at_rest(tmp_path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    store = EncryptedSshCredentialStore(settings)
    target_id = "external:10.0.0.8"
    host_key = "SHA256:" + "A" * 43

    assert store.save(target_id, _request(), host_key) is True

    store_bytes = settings.remote_credential_store_path.read_bytes()
    key_bytes = settings.remote_credential_key_path.read_bytes()
    combined = store_bytes + key_bytes
    assert b"one-time-secret" not in combined
    assert b"ubuntu" not in combined
    assert store.target_ids() == [target_id]

    recovered = store.load(target_id)
    assert recovered.endpoint == "10.0.0.8"
    assert recovered.username == "ubuntu"
    assert recovered.password is not None
    assert recovered.password.get_secret_value() == "one-time-secret"
    assert recovered.expected_host_key_sha256 == host_key
    assert recovered.deploy_worker is True


def test_tampered_ciphertext_fails_closed(tmp_path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    store = EncryptedSshCredentialStore(settings)
    target_id = "external:10.0.0.8"
    store.save(target_id, _request(), "SHA256:" + "A" * 43)
    document = json.loads(settings.remote_credential_store_path.read_text(encoding="utf-8"))
    document["credentials"][target_id] = Fernet.generate_key().decode("ascii")
    settings.remote_credential_store_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RemoteCredentialError, match="could not be decrypted"):
        store.load(target_id)


def test_disabled_store_does_not_create_key_or_ciphertext(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        remember_ssh_credentials=False,
    )
    store = EncryptedSshCredentialStore(settings)

    assert store.save("external:disabled", _request(), "SHA256:" + "A" * 43) is False
    assert store.target_ids() == []
    assert not settings.remote_credential_key_path.exists()
    assert not settings.remote_credential_store_path.exists()


def test_recovery_pins_host_key_and_redeploys_remembered_target(tmp_path, monkeypatch) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    target_id = "external:10.0.0.8"
    host_key = "SHA256:" + "A" * 43
    EncryptedSshCredentialStore(settings).save(target_id, _request(), host_key)
    target = SimpleNamespace(
        id=target_id,
        provider="external",
        lifecycle_status="active",
        fingerprint_json={"host_key_sha256": host_key},
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def get(self, _model, requested_id):
            return target if requested_id == target_id else None

    deployments = []
    monkeypatch.setattr(remote_recovery, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        remote_recovery,
        "deploy_remote_worker",
        lambda request, record, app_settings: deployments.append(
            (request, record, app_settings)
        ),
    )

    assert remote_recovery.recover_remembered_target(target_id, settings) is True
    assert len(deployments) == 1
    request, record, recovered_settings = deployments[0]
    assert request.expected_host_key_sha256 == host_key
    assert request.password.get_secret_value() == "one-time-secret"
    assert record is target
    assert recovered_settings is settings


def test_recovery_refuses_changed_host_key(tmp_path, monkeypatch) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    target_id = "external:10.0.0.8"
    EncryptedSshCredentialStore(settings).save(
        target_id,
        _request(),
        "SHA256:" + "A" * 43,
    )
    target = SimpleNamespace(
        id=target_id,
        provider="external",
        lifecycle_status="active",
        fingerprint_json={"host_key_sha256": "SHA256:" + "B" * 43},
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def get(self, _model, _target_id):
            return target

    def deploy(*_args) -> None:
        pytest.fail("changed host key must not be used")

    monkeypatch.setattr(remote_recovery, "SessionLocal", FakeSession)
    monkeypatch.setattr(remote_recovery, "deploy_remote_worker", deploy)

    assert remote_recovery.recover_remembered_target(target_id, settings) is True
