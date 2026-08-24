from __future__ import annotations

import asyncio

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from looper_api.app import app, require_operator
from looper_api.cloud_contracts import ProviderId
from looper_api.cloud_service import CloudWorkflowError, provider_enabled, purchase_readiness
from looper_api.config import Settings, get_settings
from looper_api.database import get_session
from looper_api.providers.registry import CloudProviderRegistry


def credentials(value: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=value)


def test_operator_auth_is_optional_while_live_purchase_is_off(tmp_path) -> None:
    app_settings = Settings(data_dir=tmp_path, live_purchase_enabled=False)
    assert require_operator(None, app_settings) == "local-readonly"
    assert provider_enabled(app_settings, ProviderId.TENCENT) is False


def test_live_purchase_requires_distinct_operator_authentication(tmp_path) -> None:
    app_settings = Settings(
        data_dir=tmp_path,
        live_purchase_enabled=True,
        live_purchase_providers="tencent",
        purchase_confirmation_secret="c" * 48,
    )
    with pytest.raises(CloudWorkflowError) as missing_configuration:
        require_operator(None, app_settings)
    assert missing_configuration.value.code == "operator_auth_not_configured"
    assert provider_enabled(app_settings, ProviderId.TENCENT) is False

    app_settings.operator_token = "o" * 48
    with pytest.raises(CloudWorkflowError) as unauthenticated:
        require_operator(credentials("wrong-token"), app_settings)
    assert unauthenticated.value.status_code == 401
    assert provider_enabled(app_settings, ProviderId.TENCENT) is True
    assert require_operator(credentials("o" * 48), app_settings) == "operator"


def test_local_operator_session_is_loopback_only(tmp_path) -> None:
    token = "o" * 48

    async def exercise(settings: Settings, client_host: str):
        app.dependency_overrides[get_settings] = lambda: settings
        async with AsyncClient(
            transport=ASGITransport(app=app, client=(client_host, 43210)),
            base_url="http://testserver",
        ) as client:
            status = await client.get("/api/v1/operator/session")
            issued = await client.post("/api/v1/operator/local-session")
            return status, issued

    try:
        local_status, local_issued = asyncio.run(
            exercise(
                Settings(data_dir=tmp_path, host="127.0.0.1", operator_token=token),
                "127.0.0.1",
            )
        )
        assert local_status.json()["localBootstrapAvailable"] is True
        assert local_issued.status_code == 200
        assert local_issued.json() == {"token": token}

        remote_status, remote_issued = asyncio.run(
            exercise(Settings(data_dir=tmp_path, host="0.0.0.0", operator_token=token), "127.0.0.1")
        )
        assert remote_status.json()["localBootstrapAvailable"] is False
        assert remote_issued.status_code == 403
        assert remote_issued.json()["code"] == "local_operator_bootstrap_forbidden"
    finally:
        app.dependency_overrides.clear()


def test_cloud_ssh_defaults_are_authenticated_and_not_cacheable(tmp_path) -> None:
    token = "o" * 48
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        operator_token=token,
        default_ssh_auth_method="password",
        default_ssh_password="StrongPassword1#",
    )

    async def exercise():
        app.dependency_overrides[get_settings] = lambda: settings
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            denied = await client.get("/api/v1/cloud/ssh-defaults")
            allowed = await client.get(
                "/api/v1/cloud/ssh-defaults",
                headers={"Authorization": f"Bearer {token}"},
            )
            return denied, allowed

    try:
        denied, allowed = asyncio.run(exercise())
        assert denied.status_code == 401
        assert allowed.status_code == 200
        assert allowed.headers["cache-control"] == "no-store, private"
        assert allowed.json() == {
            "username": "root",
            "port": 22,
            "authMethod": "password",
            "password": "StrongPassword1#",
            "passwordConfigured": True,
            "privateKeyConfigured": False,
        }
    finally:
        app.dependency_overrides.clear()


def test_purchase_readiness_explains_and_clears_tencent_blockers(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TENCENTCLOUD_SECRET_ID", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_KEY", raising=False)
    registry = CloudProviderRegistry()
    disabled = purchase_readiness(Settings(data_dir=tmp_path), registry)
    tencent = next(item for item in disabled["providers"] if item["provider"] == "tencent")
    blocked = {check["code"] for check in tencent["checks"] if not check["ready"]}
    assert {"credentials", "operator-token", "allowlist", "global-switch"}.issubset(blocked)
    assert tencent["ready"] is False

    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "test-secret-id")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "test-secret-key")
    enabled = purchase_readiness(
        Settings(
            data_dir=tmp_path,
            live_purchase_enabled=True,
            live_purchase_providers="tencent",
            operator_token="o" * 48,
            purchase_confirmation_secret="c" * 48,
        ),
        registry,
    )
    tencent = next(item for item in enabled["providers"] if item["provider"] == "tencent")
    assert tencent["ready"] is True
    assert all(check["ready"] for check in tencent["checks"])


def test_order_http_routes_enforce_operator_bearer(db_session, tmp_path) -> None:
    app_settings = Settings(
        data_dir=tmp_path,
        operator_token="o" * 48,
        purchase_confirmation_secret="c" * 48,
    )

    def session_override():
        yield db_session

    app.dependency_overrides[get_settings] = lambda: app_settings
    app.dependency_overrides[get_session] = session_override

    async def exercise_routes():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            operator_session = await client.get("/api/v1/operator/session")
            authenticated_session = await client.get(
                "/api/v1/operator/session",
                headers={"Authorization": f"Bearer {'o' * 48}"},
            )
            readiness = await client.get("/api/v1/cloud/purchase-readiness")
            denied = await client.get("/api/v1/cloud/orders")
            allowed = await client.get(
                "/api/v1/cloud/orders",
                headers={"Authorization": f"Bearer {'o' * 48}"},
            )
            denied_catalog = await client.get(
                "/api/v1/cloud/catalog/tencent/vpc?region=ap-test"
            )
            denied_managed_group = await client.post(
                "/api/v1/cloud/network/tencent/managed-security-group?region=ap-test"
            )
            denied_sync = await client.post(
                "/api/v1/targets/tencent-cvm/sync?instance_id=ins-test"
            )
            denied_alibaba_sync = await client.post(
                "/api/v1/targets/alibaba-ecs/sync?instance_id=i-test"
            )
            denied_evidence = await client.get("/api/v1/cloud/orders/missing/evidence")
            denied_purchase = await client.post(
                "/api/v1/cloud/orders/purchase",
                json={"quoteId": "missing-quote"},
            )
            allowed_evidence = await client.get(
                "/api/v1/cloud/orders/missing/evidence",
                headers={"Authorization": f"Bearer {'o' * 48}"},
            )
            return (
                operator_session,
                authenticated_session,
                readiness,
                denied,
                allowed,
                denied_catalog,
                denied_managed_group,
                denied_sync,
                denied_alibaba_sync,
                denied_evidence,
                denied_purchase,
                allowed_evidence,
            )

    try:
        (
            operator_session,
            authenticated_session,
            readiness,
            denied,
            allowed,
            denied_catalog,
            denied_managed_group,
            denied_sync,
            denied_alibaba_sync,
            denied_evidence,
            denied_purchase,
            allowed_evidence,
        ) = asyncio.run(exercise_routes())
        assert operator_session.json()["authenticated"] is False
        assert authenticated_session.json()["authenticated"] is True
        assert readiness.status_code == 200
        assert {item["provider"] for item in readiness.json()["providers"]} == {
            "tencent",
            "alibaba",
            "volcengine",
            "baidu",
        }
        assert denied.status_code == 401
        assert denied.json()["code"] == "operator_auth_required"
        assert allowed.status_code == 200
        assert allowed.json() == {"items": [], "total": 0}
        assert denied_catalog.status_code == 401
        assert denied_managed_group.status_code == 401
        assert denied_sync.status_code == 401
        assert denied_alibaba_sync.status_code == 401
        assert denied_evidence.status_code == 401
        assert denied_purchase.status_code == 401
        assert allowed_evidence.status_code == 404
        assert allowed_evidence.json()["code"] == "order_not_found"
    finally:
        app.dependency_overrides.clear()
