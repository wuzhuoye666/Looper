from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace

from looper_api.cloud_contracts import ProviderId
from looper_api.providers.base import CloudProviderError
from looper_api.providers.tencent_cvm import TencentCvmProvider, sync_cvm_inventory

app_module = import_module("looper_api.app")


def test_sync_all_cloud_inventory_visits_every_tencent_and_alibaba_region(monkeypatch) -> None:
    regions = {
        ProviderId.TENCENT: [SimpleNamespace(id="ap-shanghai"), SimpleNamespace(id="ap-guangzhou")],
        ProviderId.ALIBABA: [SimpleNamespace(id="cn-hangzhou"), SimpleNamespace(id="cn-beijing")],
    }
    registry = SimpleNamespace(
        get=lambda provider_id: SimpleNamespace(list_regions=lambda: regions[provider_id])
    )
    calls: list[tuple[str, str, object]] = []
    credential_store = object()

    def sync_tencent(session, region, *, credential_store):
        calls.append(("tencent", region, credential_store))
        return []

    def sync_alibaba(session, region, *, credential_store):
        calls.append(("alibaba", region, credential_store))
        return []

    monkeypatch.setattr(app_module, "sync_cvm_inventory", sync_tencent)
    monkeypatch.setattr(app_module, "sync_ecs_inventory", sync_alibaba)

    result = app_module._sync_all_cloud_inventory(object(), registry, credential_store)

    assert calls == [
        ("tencent", "ap-guangzhou", credential_store),
        ("tencent", "ap-shanghai", credential_store),
        ("alibaba", "cn-beijing", credential_store),
        ("alibaba", "cn-hangzhou", credential_store),
    ]
    assert result == {
        "items": [],
        "total": 0,
        "regions": {
            "tencent": ["ap-guangzhou", "ap-shanghai"],
            "alibaba": ["cn-beijing", "cn-hangzhou"],
        },
        "completedRegions": {
            "tencent": ["ap-guangzhou", "ap-shanghai"],
            "alibaba": ["cn-beijing", "cn-hangzhou"],
        },
        "errors": [],
    }


def test_sync_all_cloud_inventory_keeps_later_regions_after_one_failure(monkeypatch) -> None:
    regions = {
        ProviderId.TENCENT: [SimpleNamespace(id="ap-guangzhou")],
        ProviderId.ALIBABA: [
            SimpleNamespace(id="cn-beijing"),
            SimpleNamespace(id="cn-qingdao"),
        ],
    }
    registry = SimpleNamespace(
        get=lambda provider_id: SimpleNamespace(list_regions=lambda: regions[provider_id])
    )
    session = SimpleNamespace(rollback_calls=0)
    session.rollback = lambda: setattr(session, "rollback_calls", session.rollback_calls + 1)
    checkpoints: list[str] = []

    monkeypatch.setattr(app_module, "sync_cvm_inventory", lambda *_args, **_kwargs: [])

    def sync_alibaba(_session, region, **_kwargs):
        if region == "cn-beijing":
            raise CloudProviderError("temporary region failure", code="region_unavailable")
        return []

    monkeypatch.setattr(app_module, "sync_ecs_inventory", sync_alibaba)

    result = app_module._sync_all_cloud_inventory(
        session,
        registry,
        object(),
        checkpoint=lambda: checkpoints.append("committed"),
    )

    assert session.rollback_calls == 1
    assert checkpoints == ["committed", "committed"]
    assert result["completedRegions"] == {
        "tencent": ["ap-guangzhou"],
        "alibaba": ["cn-qingdao"],
    }
    assert result["errors"] == [
        {
            "provider": "alibaba",
            "region": "cn-beijing",
            "code": "region_unavailable",
            "message": "temporary region failure",
        }
    ]


def test_tencent_inventory_accepts_a_global_region(monkeypatch, db_session) -> None:
    calls: list[tuple[str, str]] = []

    def call(_provider, method: str, region: str, _request: object):
        calls.append((method, region))
        return SimpleNamespace(InstanceSet=[])

    monkeypatch.setattr(TencentCvmProvider, "_call", call)

    assert sync_cvm_inventory(db_session, "eu-frankfurt") == []
    assert calls == [("DescribeInstances", "eu-frankfurt")]
