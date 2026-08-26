from __future__ import annotations

import asyncio
import sys

from httpx import ASGITransport, AsyncClient, Response
from looper_api.app import app
from looper_api.config import Settings, get_settings
from looper_api.deepseek_credentials import (
    EncryptedDeepSeekCredentialStore,
    effective_deepseek_key,
)


def test_deepseek_key_round_trip_is_encrypted_at_rest(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    store = EncryptedDeepSeekCredentialStore(settings)
    secret = "sk-test-secret-value-that-must-not-be-plaintext"

    store.save(secret)

    assert store.load() == secret
    assert secret.encode() not in settings.deepseek_credential_store_path.read_bytes()
    assert secret.encode() not in store.stable_key_path.read_bytes()
    assert settings.deepseek_credential_store_path.read_bytes().startswith(b"stable-v1:")
    if sys.platform != "win32":
        assert settings.deepseek_credential_store_path.stat().st_mode & 0o077 == 0
        assert store.stable_key_path.stat().st_mode & 0o077 == 0


def test_unreadable_legacy_dpapi_key_does_not_break_readiness(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    settings.deepseek_credential_key_path.write_bytes(b"dpapi-v1:not-valid-base64")
    settings.deepseek_credential_store_path.write_bytes(b"legacy-ciphertext")

    assert effective_deepseek_key(settings) == ("", None)

    store = EncryptedDeepSeekCredentialStore(settings)
    store.save("replacement-key-value-123456789")
    assert store.load() == "replacement-key-value-123456789"
    assert settings.deepseek_credential_key_path.read_bytes() == b"dpapi-v1:not-valid-base64"


def test_stored_deepseek_key_overrides_environment_and_delete_restores_it(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path, deepseek_api_key="environment-key-value-12345")
    store = EncryptedDeepSeekCredentialStore(settings)
    store.save("stored-key-value-123456789")

    assert effective_deepseek_key(settings) == ("stored-key-value-123456789", "stored")
    assert store.delete() is True
    assert effective_deepseek_key(settings) == (
        "environment-key-value-12345",
        "environment",
    )


def test_provider_config_routes_require_operator_and_never_return_plaintext(tmp_path) -> None:
    app_settings = Settings(data_dir=tmp_path, operator_token="o" * 48)
    app.dependency_overrides[get_settings] = lambda: app_settings
    secret = "sk-route-secret-value-123456789"

    async def exercise() -> tuple[Response, Response, Response, Response]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            denied = await client.get("/api/v1/source-discoveries/provider-config")
            headers = {"Authorization": f"Bearer {'o' * 48}"}
            saved = await client.put(
                "/api/v1/source-discoveries/provider-config",
                headers=headers,
                json={"apiKey": secret},
            )
            status = await client.get(
                "/api/v1/source-discoveries/provider-config", headers=headers
            )
            deleted = await client.delete(
                "/api/v1/source-discoveries/provider-config", headers=headers
            )
            return denied, saved, status, deleted

    try:
        denied, saved, status, deleted = asyncio.run(exercise())
        assert denied.status_code == 401
        assert saved.status_code == 200
        assert saved.json()["source"] == "stored"
        assert saved.json()["encryptedAtRest"] is True
        assert saved.json()["maskedKey"].endswith("6789")
        assert secret not in saved.text
        assert secret not in status.text
        assert deleted.json()["configured"] is False
    finally:
        app.dependency_overrides.clear()
