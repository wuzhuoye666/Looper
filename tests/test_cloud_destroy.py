from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from looper_api.cloud_contracts import (
    CatalogFilters,
    CloudPurchaseSpec,
    DestroyedResource,
    ImageInfo,
    InstanceTypeInfo,
    OrderConfirmRequest,
    ProviderDestroyResult,
    ProviderId,
    ProviderInfo,
    ProviderPurchaseResult,
    ProviderQuote,
    ProvisionedInstance,
    RegionInfo,
    TargetDestroyRequest,
    ZoneInfo,
)
from looper_api.cloud_service import (
    CloudWorkflowError,
    confirm_order,
    create_quote,
    destroy_target,
    destroy_target_preview,
    prepare_order,
)
from looper_api.config import Settings
from looper_api.models import EventRecord, TargetRecord
from looper_api.providers.base import CloudProvider
from looper_api.providers.registry import CloudProviderRegistry
from looper_core.canonical import canonical_digest, utc_now
from sqlalchemy import select


class FakeProvider(CloudProvider):
    id = ProviderId.TENCENT
    display_name = "Fake Tencent"
    sdk_package = "fake"

    def __init__(self) -> None:
        self.destroy_calls: list[str] = []
        self.vpc_cleanup_calls: list[tuple[str, str]] = []

    def info(self, *, live_purchase_enabled: bool) -> ProviderInfo:
        return ProviderInfo(
            id=self.id,
            name=self.display_name,
            sdkPackage=self.sdk_package,
            sdkInstalled=True,
            credentialsConfigured=True,
            capabilities=["hourly-quote", "postpaid-purchase"],
            livePurchaseEnabled=live_purchase_enabled,
        )

    def list_regions(self) -> list[RegionInfo]:
        return [RegionInfo(provider=self.id, id="ap-test", name="Test")]

    def list_zones(self, region: str) -> list[ZoneInfo]:
        return [ZoneInfo(provider=self.id, region=region, id="ap-test-1", name="Test Zone")]

    def search_instance_types(self, filters: CatalogFilters) -> list[InstanceTypeInfo]:
        return [
            InstanceTypeInfo(
                provider=self.id, region="ap-test", id="S5.SMALL2", cpu=2, memoryGib=2
            )
        ]

    def search_images(self, filters: CatalogFilters) -> list[ImageInfo]:
        return [ImageInfo(provider=self.id, region="ap-test", id="img-test", name="Test Linux")]

    def quote(self, spec: CloudPurchaseSpec) -> ProviderQuote:
        return ProviderQuote(
            providerQuoteId="fake-price-1",
            amount=Decimal("0.42"),
            currency="CNY",
            estimated=False,
            expiresAt=utc_now() + timedelta(minutes=5),
        )

    def purchase(self, spec: CloudPurchaseSpec, *, client_token: str) -> ProviderPurchaseResult:
        return ProviderPurchaseResult(
            providerOrderId="fake-order-1",
            requestId="fake-request-1",
            instances=[
                ProvisionedInstance(
                    id="ins-fake-1",
                    name=spec.instance_name,
                    region=spec.region,
                    zone=spec.zone,
                    status="PENDING",
                )
            ],
        )

    def destroy(self, *, region: str, instance_ids: list[str]) -> ProviderDestroyResult:
        self.destroy_calls.extend(instance_ids)
        return ProviderDestroyResult(
            request_id="fake-destroy-request-1",
            instance_ids=list(instance_ids),
            released_resources=[
                DestroyedResource(kind="instance", id=instance_id, note="fake destroyed")
                for instance_id in instance_ids
            ],
        )

    def delete_vpc_if_empty(self, *, region: str, vpc_id: str) -> DestroyedResource:
        self.vpc_cleanup_calls.append((region, vpc_id))
        return DestroyedResource(kind="vpc", id=vpc_id, note="fake empty VPC deleted")


def _spec() -> CloudPurchaseSpec:
    return CloudPurchaseSpec(
        provider="tencent",
        region="ap-test",
        zone="ap-test-1",
        instanceType="S5.SMALL2",
        imageId="img-test",
        instanceName="workflow-test",
        vpcId="vpc-test",
        subnetId="subnet-test",
        securityGroupIds=["sg-test"],
    )


def _settings(tmp_path, *, live: bool = False) -> Settings:
    return Settings(
        data_dir=tmp_path,
        live_purchase_enabled=live,
        live_purchase_providers="tencent" if live else "",
        purchase_confirmation_secret="x" * 48,
        operator_token="o" * 48 if live else "",
    )


def _purchased_target_id(db_session, tmp_path, fake: FakeProvider) -> str:
    registry = CloudProviderRegistry({ProviderId.TENCENT: lambda: fake})
    app_settings = _settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, registry, _spec(), "quote-key-destroy")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-destroy")
    confirm_order(
        db_session,
        app_settings,
        registry,
        prepared["id"],
        OrderConfirmRequest(
            confirmationToken=str(prepared["confirmationToken"]),
            acknowledgement=str(prepared["acknowledgement"]),
            expectedHourlyAmount="0.42",
        ),
    )
    db_session.commit()
    return "cloud:tencent:ap-test:ins-fake-1"


def test_destroy_preview_reports_instance_and_accompanying_resources(db_session, tmp_path) -> None:
    fake = FakeProvider()
    target_id = _purchased_target_id(db_session, tmp_path, fake)

    preview = destroy_target_preview(db_session, _settings(tmp_path, live=True), target_id)
    assert preview["instanceId"] == "ins-fake-1"
    assert preview["region"] == "ap-test"
    assert preview["provider"] == "tencent"
    assert "确认销毁 腾讯云 CVM 实例 workflow-test（ins-fake-1）" in preview["acknowledgement"]
    kinds = {item["kind"] for item in preview["resources"]}
    expected_kinds = {
        "instance", "system-disk", "local-disk", "public-ip", "vpc", "subnet", "security-group"
    }
    assert expected_kinds <= kinds
    assert "空闲非默认 VPC 也可能被删除" in preview["acknowledgement"]


def test_destroy_requires_exact_acknowledgement_before_provider_call(db_session, tmp_path) -> None:
    fake = FakeProvider()
    target_id = _purchased_target_id(db_session, tmp_path, fake)
    registry = CloudProviderRegistry({ProviderId.TENCENT: lambda: fake})
    app_settings = _settings(tmp_path, live=True)

    with pytest.raises(CloudWorkflowError) as mismatch:
        destroy_target(
            db_session,
            app_settings,
            registry,
            target_id,
            TargetDestroyRequest(acknowledgement="确认销毁别的资源"),
        )
    assert mismatch.value.code == "acknowledgement_mismatch"
    assert fake.destroy_calls == []


def test_destroy_terminates_instance_archives_target_and_writes_event(db_session, tmp_path) -> None:
    fake = FakeProvider()
    target_id = _purchased_target_id(db_session, tmp_path, fake)
    registry = CloudProviderRegistry({ProviderId.TENCENT: lambda: fake})
    app_settings = _settings(tmp_path, live=True)

    preview = destroy_target_preview(db_session, app_settings, target_id)
    result = destroy_target(
        db_session,
        app_settings,
        registry,
        target_id,
        TargetDestroyRequest(acknowledgement=preview["acknowledgement"]),
    )
    db_session.commit()

    assert fake.destroy_calls == ["ins-fake-1"]
    assert fake.vpc_cleanup_calls == [("ap-test", "vpc-test")]
    assert result["status"] == "destroyed"
    assert result["instanceId"] == "ins-fake-1"
    assert any(item["kind"] == "vpc" and item["released"] for item in result["resources"])

    target = db_session.get(TargetRecord, target_id)
    assert target is not None
    assert target.lifecycle_status == "archived"
    assert target.archive_reason == "destroyed"
    assert target.status == "offline"
    assert target.inventory_json.get("instance_state") == "TERMINATED"

    event = db_session.scalar(
        select(EventRecord).where(
            EventRecord.entity_type == "target",
            EventRecord.entity_id == target_id,
            EventRecord.event_type == "cloud.target.destroyed",
        )
    )
    assert event is not None
    assert event.payload_json["instanceId"] == "ins-fake-1"


def test_destroy_rejects_non_cloud_target(db_session, tmp_path) -> None:
    now = utc_now()
    db_session.add(
        TargetRecord(
            id="external:host1",
            name="external-host",
            provider="external",
            status="available",
            capabilities_json=["external"],
            inventory_json={"source": "manual", "endpoint": "10.0.0.1"},
            fingerprint_json={"provider": "external"},
            snapshot_digest=canonical_digest({"provider": "external"}),
            runnable=False,
            lifecycle_status="active",
            created_at=now,
            updated_at=now,
        )
    )
    db_session.commit()

    with pytest.raises(CloudWorkflowError) as blocked:
        destroy_target_preview(db_session, _settings(tmp_path, live=True), "external:host1")
    assert blocked.value.code == "not_a_destroyable_instance"


def test_destroy_rejects_already_destroyed_target(db_session, tmp_path) -> None:
    fake = FakeProvider()
    target_id = _purchased_target_id(db_session, tmp_path, fake)
    registry = CloudProviderRegistry({ProviderId.TENCENT: lambda: fake})
    app_settings = _settings(tmp_path, live=True)

    preview = destroy_target_preview(db_session, app_settings, target_id)
    destroy_target(
        db_session,
        app_settings,
        registry,
        target_id,
        TargetDestroyRequest(acknowledgement=preview["acknowledgement"]),
    )
    db_session.commit()

    with pytest.raises(CloudWorkflowError) as duplicate:
        destroy_target(
            db_session,
            app_settings,
            registry,
            target_id,
            TargetDestroyRequest(acknowledgement=preview["acknowledgement"]),
        )
    assert duplicate.value.code == "target_already_destroyed"
    assert fake.destroy_calls == ["ins-fake-1"]
