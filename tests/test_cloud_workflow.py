from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from looper_api.cloud_contracts import (
    CatalogFilters,
    CloudPurchaseSpec,
    CloudSshCredentials,
    DestroyedResource,
    ImageInfo,
    InstanceTypeInfo,
    OrderConfirmRequest,
    OrderResolveRequest,
    ProviderDestroyResult,
    ProviderId,
    ProviderInfo,
    ProviderPurchaseResult,
    ProviderQuote,
    ProvisionedInstance,
    RegionInfo,
    SubnetInfo,
    ZoneInfo,
)
from looper_api.cloud_service import (
    CloudWorkflowError,
    catalog_search,
    confirm_order,
    create_quote,
    delete_order,
    get_order_evidence,
    get_order_reconciliation_context,
    global_search,
    list_order_events,
    prepare_order,
    purchase_quote,
    recover_interrupted_orders,
    renew_order_confirmation,
    resolve_unknown_order,
)
from looper_api.config import Settings
from looper_api.models import (
    CloudCatalogCacheRecord,
    CloudOrderRecord,
    CloudQuoteRecord,
    EventRecord,
    TargetRecord,
)
from looper_api.providers.base import CloudProvider, CloudProviderError
from looper_api.providers.registry import CloudProviderRegistry
from looper_api.serialization import target_view
from looper_core.canonical import canonical_digest, utc_now
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session


class FakeProvider(CloudProvider):
    id = ProviderId.TENCENT
    display_name = "Fake Tencent"
    sdk_package = "fake"

    def __init__(self, *, ambiguous: bool = False) -> None:
        self.catalog_calls = 0
        self.instance_items: list[InstanceTypeInfo] | None = None
        self.image_items: list[ImageInfo] | None = None
        self.purchase_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self.ambiguous = ambiguous
        self.fail_catalog = False
        self.quote_amount = Decimal("0.42")

    def info(self, *, live_purchase_enabled: bool) -> ProviderInfo:
        return ProviderInfo(
            id=self.id,
            name=self.display_name,
            sdkPackage=self.sdk_package,
            sdkInstalled=True,
            credentialsConfigured=True,
            capabilities=["regions", "zones", "instance-types", "images", "hourly-quote"],
            livePurchaseEnabled=live_purchase_enabled,
        )

    def list_regions(self) -> list[RegionInfo]:
        self.catalog_calls += 1
        if self.fail_catalog:
            raise CloudProviderError("catalog unavailable", code="unavailable", retryable=True)
        return [RegionInfo(provider=self.id, id="ap-test", name="Test")]

    def list_zones(self, region: str) -> list[ZoneInfo]:
        self.catalog_calls += 1
        return [ZoneInfo(provider=self.id, region=region, id="ap-test-1", name="Test Zone")]

    def list_subnets(self, region: str, zone: str, vpc_id: str) -> list[SubnetInfo]:
        self.catalog_calls += 1
        return [
            SubnetInfo(
                provider=self.id,
                region=region,
                zone=zone,
                vpcId=vpc_id,
                id=f"subnet-{vpc_id}",
                name="Test Subnet",
            )
        ]

    def search_instance_types(self, filters: CatalogFilters) -> list[InstanceTypeInfo]:
        self.catalog_calls += 1
        if self.instance_items is not None:
            return self.instance_items
        return [
            InstanceTypeInfo(
                provider=self.id,
                region=filters.region or "ap-test",
                id="S5.SMALL2",
                cpu=2,
                memoryGib=2,
                zones=[filters.zone] if filters.zone else [],
            )
        ]

    def search_images(self, filters: CatalogFilters) -> list[ImageInfo]:
        self.catalog_calls += 1
        if self.image_items is not None:
            return self.image_items
        return [
            ImageInfo(
                provider=self.id,
                region=filters.region or "ap-test",
                id="img-test",
                name="Test Linux",
                platform="Linux",
            )
        ]

    def quote(self, spec: CloudPurchaseSpec) -> ProviderQuote:
        return ProviderQuote(
            providerQuoteId="fake-price-1",
            amount=self.quote_amount,
            currency="CNY",
            estimated=False,
            expiresAt=utc_now() + timedelta(minutes=5),
            details={"stock": "advisory"},
        )

    def purchase(self, spec: CloudPurchaseSpec, *, client_token: str) -> ProviderPurchaseResult:
        self.purchase_calls.append(client_token)
        if self.ambiguous:
            raise CloudProviderError("simulated timeout", code="timeout", ambiguous=True)
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



def test_cloud_ssh_credentials_accept_camel_case_remember_flag() -> None:
    credentials = CloudSshCredentials.model_validate(
        {
            "username": "ubuntu",
            "authMethod": "password",
            "password": "one-time-secret",
            "rememberCredentials": False,
        }
    )

    assert credentials.remember_credentials is False
    assert credentials.model_dump(by_alias=True)["rememberCredentials"] is False

def spec() -> CloudPurchaseSpec:
    return CloudPurchaseSpec(
        provider="tencent",
        region="ap-test",
        zone="ap-test-1",
        instanceType="S5.SMALL2",
        cpu=2,
        memoryGib=2,
        imageId="img-test",
        instanceName="workflow-test",
        vpcId="vpc-test",
        subnetId="subnet-test",
        securityGroupIds=["sg-test"],
    )


def confirmation(prepared: dict[str, object]) -> OrderConfirmRequest:
    return OrderConfirmRequest(
        confirmationToken=str(prepared["confirmationToken"]),
        acknowledgement=str(prepared["acknowledgement"]),
        expectedHourlyAmount="0.42",
    )


def registry(fake: FakeProvider) -> CloudProviderRegistry:
    return CloudProviderRegistry({fake.id: lambda: fake})


def settings(tmp_path, *, live: bool = False) -> Settings:
    return Settings(
        data_dir=tmp_path,
        live_purchase_enabled=live,
        live_purchase_providers="tencent" if live else "",
        purchase_confirmation_secret="x" * 48,
        operator_token="o" * 48 if live else "",
    )


def test_catalog_cache_quote_idempotency_and_default_provider_gate(db_session, tmp_path) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path)

    first = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "region",
        CatalogFilters(),
    )
    second = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "region",
        CatalogFilters(),
    )
    assert first.source == "live"
    assert second.source == "cache"
    assert fake.catalog_calls == 1

    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-1")
    replay = create_quote(db_session, app_settings, reg, spec(), "quote-key-1")
    assert quote["id"] == replay["id"]
    assert quote["spec"]["instanceType"] == "S5.SMALL2"
    assert "instance_type" not in quote["spec"]
    with pytest.raises(CloudWorkflowError) as conflict:
        create_quote(
            db_session,
            app_settings,
            reg,
            spec().model_copy(update={"instance_name": "different-instance"}),
            "quote-key-1",
        )
    assert conflict.value.code == "idempotency_conflict"
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-1")
    prepared_replay = prepare_order(db_session, app_settings, quote["id"], "order-key-1")
    assert prepared["id"] == prepared_replay["id"]
    assert prepared["status"] == "awaiting_confirmation"
    assert prepared["spec"]["imageId"] == "img-test"
    assert "image_id" not in prepared["spec"]
    with pytest.raises(CloudWorkflowError, match="disabled"):
        confirm_order(
            db_session,
            app_settings,
            reg,
            prepared["id"],
            confirmation(prepared),
        )


def test_full_catalog_paginates_searches_and_naturally_sorts_from_one_snapshot(
    db_session, tmp_path
) -> None:
    fake = FakeProvider()
    fake.instance_items = [
        InstanceTypeInfo(
            provider=ProviderId.TENCENT,
            region="ap-test",
            id=f"S9.TEST.{index}",
            family="S9",
            cpu=4,
            memoryGib=8,
        )
        for index in range(620, 0, -1)
    ]
    fake.image_items = [
        ImageInfo(
            provider=ProviderId.TENCENT,
            region="ap-test",
            id=f"img-{index}",
            name="Needle Image" if index == 205 else f"Image {index}",
            platform="Linux",
        )
        for index in range(1, 206)
    ]
    reg = registry(fake)
    app_settings = settings(tmp_path)
    legacy_filters = {"region": "ap-test"}
    db_session.add(
        CloudCatalogCacheRecord(
            key=canonical_digest(
                {
                    "version": 2,
                    "provider": "tencent",
                    "kind": "instance-type",
                    "filters": legacy_filters,
                }
            ),
            provider="tencent",
            resource_type="instance-type",
            region="ap-test",
            zone=None,
            query_json={"version": 2, **legacy_filters},
            payload_json=[fake.instance_items[0].model_dump(mode="json", by_alias=True)],
            fetched_at=utc_now(),
            expires_at=utc_now() + timedelta(minutes=5),
            last_error=None,
        )
    )
    db_session.commit()

    first = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "instance-type",
        CatalogFilters(region="ap-test", limit=20),
    )
    second = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "instance-type",
        CatalogFilters(region="ap-test", offset=20, limit=20),
    )
    searched = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "instance-type",
        CatalogFilters(region="ap-test", query=".620", limit=20),
    )

    assert first.total == 620
    assert first.next_offset == 20
    assert [item["id"] for item in first.items[:3]] == [
        "S9.TEST.1",
        "S9.TEST.2",
        "S9.TEST.3",
    ]
    assert second.items[0]["id"] == "S9.TEST.21"
    assert searched.total == 1
    assert searched.items[0]["id"] == "S9.TEST.620"
    assert fake.catalog_calls == 1

    full_record = next(
        record
        for record in db_session.scalars(
            select(CloudCatalogCacheRecord).where(
                CloudCatalogCacheRecord.provider == "tencent",
                CloudCatalogCacheRecord.resource_type == "instance-type",
            )
        )
        if record.query_json.get("version") == 4
    )
    full_record.expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    continued = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "instance-type",
        CatalogFilters(region="ap-test", offset=40, limit=20),
    )
    assert continued.source == "stale-cache"
    assert continued.items[0]["id"] == "S9.TEST.41"
    assert fake.catalog_calls == 1

    images = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "image",
        CatalogFilters(region="ap-test", query="needle", limit=20),
    )
    assert images.total == 1
    assert images.items[0]["id"] == "img-205"
    assert fake.catalog_calls == 2


def test_catalog_orders_instance_sizes_and_prefers_modern_ubuntu_images(
    db_session, tmp_path
) -> None:
    fake = FakeProvider()
    fake.instance_items = [
        InstanceTypeInfo(
            provider=ProviderId.TENCENT,
            region="ap-test",
            id=instance_id,
            family="S9",
            cpu=cpu,
            memoryGib=memory,
            available=available,
            attributes=attributes,
        )
        for instance_id, cpu, memory, available, attributes in [
            ("S9.4C16G", 4, 16, True, {}),
            ("S9.2C8G", 2, 8, True, {}),
            ("S9.2C4G.UNKNOWN", 2, 4, None, {}),
            ("S9.2C4G.UNAVAILABLE", 2, 4, False, {}),
            (
                "S9.2C4G.A-INCOMPATIBLE",
                2,
                4,
                True,
                {"purchaseCompatible": False},
            ),
            ("S9.2C4G.Z-COMPATIBLE", 2, 4, True, {}),
            ("S9.1C1G.UNAVAILABLE", 1, 1, False, {}),
            ("S9.8C8G", 8, 8, True, {}),
        ]
    ]
    fake.image_items = [
        ImageInfo(
            provider=ProviderId.TENCENT,
            region="ap-test",
            id=image_id,
            name=name,
            platform=platform,
            available=available,
        )
        for image_id, name, platform, available in [
            ("img-windows", "Windows Server 2022", "Windows", True),
            ("img-linux", "TencentOS Server 4", "Linux", True),
            ("img-ubuntu-2204", "ubuntu_22_04_x64_20G_alibase.vhd", "Ubuntu", True),
            ("img-ubuntu-2404", "ubuntu_24_04_x64_20G_alibase.vhd", "Ubuntu", True),
            ("img-ubuntu-unavailable", "Ubuntu Server 24.04 unavailable", "Ubuntu", False),
        ]
    ]
    reg = registry(fake)
    app_settings = settings(tmp_path)

    instances = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "instance-type",
        CatalogFilters(region="ap-test"),
    )
    images = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "image",
        CatalogFilters(region="ap-test"),
    )

    assert [item["id"] for item in instances.items] == [
        "S9.2C4G.Z-COMPATIBLE",
        "S9.2C8G",
        "S9.4C16G",
        "S9.8C8G",
        "S9.2C4G.A-INCOMPATIBLE",
        "S9.1C1G.UNAVAILABLE",
        "S9.2C4G.UNAVAILABLE",
        "S9.2C4G.UNKNOWN",
    ]
    assert [item["id"] for item in images.items] == [
        "img-ubuntu-2404",
        "img-ubuntu-2204",
        "img-linux",
        "img-windows",
        "img-ubuntu-unavailable",
    ]


@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [
        (
            ProviderId.TENCENT,
            ["C6.2XLARGE", "C6.13XLARGE", "C6.1XLARGE", "C6.3XLARGE", "C6.0XLARGE"],
        ),
        (
            ProviderId.ALIBABA,
            ["C6.2XLARGE", "C6.13XLARGE", "C6.1XLARGE", "C6.3XLARGE", "C6.0XLARGE"],
        ),
        (
            ProviderId.VOLCENGINE,
            ["C6.2XLARGE", "C6.13XLARGE", "C6.1XLARGE", "C6.3XLARGE", "C6.0XLARGE"],
        ),
        (
            ProviderId.BAIDU,
            ["C6.2XLARGE", "C6.13XLARGE", "C6.1XLARGE", "C6.3XLARGE", "C6.0XLARGE"],
        ),
    ],
)
def test_instance_catalog_groups_available_before_unavailable_and_unknown(
    db_session,
    tmp_path,
    provider_id: ProviderId,
    expected: list[str],
) -> None:
    fake = FakeProvider()
    fake.id = provider_id
    fake.instance_items = [
        InstanceTypeInfo(
            provider=provider_id,
            region="test-region",
            id=instance_id,
            family="C6",
            cpu=4,
            memoryGib=8,
            available=available,
        )
        for instance_id, available in [
            ("C6.13XLARGE", True),
            ("C6.0XLARGE", None),
            ("C6.3XLARGE", False),
            ("C6.2XLARGE", True),
            ("C6.1XLARGE", False),
        ]
    ]
    reg = registry(fake)
    app_settings = settings(tmp_path)

    first = catalog_search(
        db_session,
        app_settings,
        reg,
        provider_id,
        "instance-type",
        CatalogFilters(region="test-region", limit=3),
    )
    second = catalog_search(
        db_session,
        app_settings,
        reg,
        provider_id,
        "instance-type",
        CatalogFilters(region="test-region", offset=3, limit=3),
    )

    assert [item["id"] for item in first.items + second.items] == expected
    assert first.total == 5
    assert first.next_offset == 3
    assert second.next_offset is None


def test_subnet_catalog_requires_dependencies_and_separates_vpc_cache_keys(
    db_session, tmp_path
) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path)

    with pytest.raises(CloudWorkflowError) as missing_zone:
        catalog_search(
            db_session,
            app_settings,
            reg,
            ProviderId.TENCENT,
            "subnet",
            CatalogFilters(region="ap-test", vpcId="vpc-a"),
        )
    assert missing_zone.value.code == "zone_required"
    with pytest.raises(CloudWorkflowError) as missing_vpc:
        catalog_search(
            db_session,
            app_settings,
            reg,
            ProviderId.TENCENT,
            "subnet",
            CatalogFilters(region="ap-test", zone="ap-test-1"),
        )
    assert missing_vpc.value.code == "vpc_required"

    first = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "subnet",
        CatalogFilters(region="ap-test", zone="ap-test-1", vpcId="vpc-a"),
    )
    second = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "subnet",
        CatalogFilters(region="ap-test", zone="ap-test-1", vpcId="vpc-b"),
    )
    replay = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "subnet",
        CatalogFilters(region="ap-test", zone="ap-test-1", vpcId="vpc-a"),
    )

    assert first.items[0]["vpcId"] == "vpc-a"
    assert second.items[0]["vpcId"] == "vpc-b"
    assert replay.source == "cache"
    assert fake.catalog_calls == 2


def test_confirm_purchase_is_idempotent_and_naturalizes_target(db_session, tmp_path) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-success")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-success")
    db_session.expire_all()
    request = confirmation(prepared)
    result = confirm_order(db_session, app_settings, reg, prepared["id"], request)
    replay = confirm_order(db_session, app_settings, reg, prepared["id"], request)
    assert result["status"] == "submitted"
    assert replay["instanceIds"] == ["ins-fake-1"]
    assert fake.purchase_calls == [fake.purchase_calls[0]]
    target_count = db_session.execute(
        text("select count(*) from targets where id='cloud:tencent:ap-test:ins-fake-1'")
    ).scalar_one()
    assert target_count == 1
    target = db_session.get(TargetRecord, "cloud:tencent:ap-test:ins-fake-1")
    assert target is not None
    view = target_view(target)
    assert view["endpoint"] == "—"
    assert "2 vCPU" in view["hardware"]
    assert "2 GiB" in view["hardware"]
    assert view["framework"] == "镜像 img-test"


def test_delete_order_removes_unsubmitted_order_events_and_rejects_submitted_order(
    db_session, tmp_path
) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-delete")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-delete")

    delete_order(db_session, prepared["id"])

    assert db_session.get(CloudOrderRecord, prepared["id"]) is None
    assert db_session.get(CloudQuoteRecord, quote["id"]) is not None
    assert not list(
        db_session.scalars(
            select(EventRecord).where(
                EventRecord.entity_type == "cloud_order",
                EventRecord.entity_id == prepared["id"],
            )
        )
    )

    submitted_quote = create_quote(
        db_session, app_settings, reg, spec(), "quote-key-delete-submitted"
    )
    submitted = purchase_quote(
        db_session,
        app_settings,
        reg,
        submitted_quote["id"],
        "order-key-delete-submitted",
    )
    with pytest.raises(CloudWorkflowError) as exc_info:
        delete_order(db_session, submitted["id"])
    assert exc_info.value.status_code == 409
    assert db_session.get(CloudOrderRecord, submitted["id"]) is not None


def test_purchase_quote_completes_order_without_browser_confirmation(db_session, tmp_path) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-one-click")

    result = purchase_quote(
        db_session,
        app_settings,
        reg,
        quote["id"],
        "order-key-one-click",
    )
    replay = purchase_quote(
        db_session,
        app_settings,
        reg,
        quote["id"],
        "order-key-one-click",
    )

    assert result["status"] == "submitted"
    assert result["instanceIds"] == ["ins-fake-1"]
    assert replay["id"] == result["id"]
    assert len(fake.purchase_calls) == 1


def test_ambiguous_provider_result_is_not_automatically_retried(db_session, tmp_path) -> None:
    fake = FakeProvider(ambiguous=True)
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-ambiguous")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-ambiguous")
    with pytest.raises(CloudWorkflowError, match="不明确"):
        confirm_order(
            db_session,
            app_settings,
            reg,
            prepared["id"],
            confirmation(prepared),
        )
    assert len(fake.purchase_calls) == 1
    resolved = resolve_unknown_order(
        db_session,
        prepared["id"],
        OrderResolveRequest(
            resolution="submitted",
            instanceIds=["ins-reconciled-1"],
            providerOrderId="provider-order-reconciled",
            note="verified in the provider control plane",
        ),
    )
    assert resolved["status"] == "submitted"
    assert resolved["instanceIds"] == ["ins-reconciled-1"]
    assert len(fake.purchase_calls) == 1
    target = db_session.get(TargetRecord, "cloud:tencent:ap-test:ins-reconciled-1")
    assert target is not None


def test_order_audit_timeline_reconciliation_context_and_evidence(db_session, tmp_path) -> None:
    fake = FakeProvider(ambiguous=True)
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-audit")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-audit")
    with pytest.raises(CloudWorkflowError):
        confirm_order(
            db_session,
            app_settings,
            reg,
            prepared["id"],
            confirmation(prepared),
        )

    context = get_order_reconciliation_context(db_session, prepared["id"])
    assert context["clientToken"]
    assert context["provider"] == "tencent"
    event_types = [item["eventType"] for item in list_order_events(db_session, prepared["id"])]
    assert event_types == [
        "cloud.quote.created",
        "cloud.order.awaiting_confirmation",
        "cloud.order.unknown",
    ]

    resolve_unknown_order(
        db_session,
        prepared["id"],
        OrderResolveRequest(
            resolution="not_created",
            note="provider support confirmed that no instance was created",
        ),
    )
    evidence = get_order_evidence(db_session, prepared["id"])
    digest = evidence.pop("evidenceDigest")
    assert digest == canonical_digest(evidence)
    assert evidence["schemaVersion"] == "looper.cloud-order-evidence/v1"
    assert evidence["events"][-1]["eventType"] == "cloud.order.reconciled"
    assert context["clientToken"] not in str(evidence)
    with pytest.raises(CloudWorkflowError) as resolved_context:
        get_order_reconciliation_context(db_session, prepared["id"])
    assert resolved_context.value.code == "invalid_order_state"


def test_stale_catalog_fallback_marks_warning(db_session, tmp_path) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path)
    filters = CatalogFilters()
    catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "region",
        filters,
    )
    record = db_session.scalar(select(CloudCatalogCacheRecord))
    assert record is not None
    record.expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    fake.fail_catalog = True

    result = catalog_search(
        db_session,
        app_settings,
        reg,
        ProviderId.TENCENT,
        "region",
        filters,
    )
    assert result.source == "stale-cache"
    assert result.stale is True
    assert "catalog unavailable" in (result.warning or "")
    assert fake.catalog_calls == 2


def test_confirmation_rejects_tampering_before_provider_call(db_session, tmp_path) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-tamper")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-tamper")

    requests = [
        OrderConfirmRequest(
            confirmationToken="x" * 32,
            acknowledgement=str(prepared["acknowledgement"]),
            expectedHourlyAmount="0.42",
        ),
        OrderConfirmRequest(
            confirmationToken=str(prepared["confirmationToken"]),
            acknowledgement="确认购买别的资源",
            expectedHourlyAmount="0.42",
        ),
        OrderConfirmRequest(
            confirmationToken=str(prepared["confirmationToken"]),
            acknowledgement=str(prepared["acknowledgement"]),
            expectedHourlyAmount="0.43",
        ),
    ]
    for request in requests:
        with pytest.raises(CloudWorkflowError):
            confirm_order(db_session, app_settings, reg, prepared["id"], request)
    assert fake.purchase_calls == []


def test_quote_binds_one_order_and_expired_snapshot_is_revalidated_at_confirmation(
    db_session, tmp_path
) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-binding")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-binding")
    with pytest.raises(CloudWorkflowError) as duplicate:
        prepare_order(db_session, app_settings, quote["id"], "order-key-another")
    assert duplicate.value.code == "quote_already_prepared"

    quote_record = db_session.get(CloudQuoteRecord, quote["id"])
    assert quote_record is not None
    quote_record.expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    result = confirm_order(
        db_session,
        app_settings,
        reg,
        prepared["id"],
        confirmation(prepared),
    )
    assert result["status"] == "submitted"
    assert len(fake.purchase_calls) == 1


def test_expired_order_can_renew_exact_confirmation_and_then_submit(
    db_session, tmp_path
) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-renew")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-renew")
    order = db_session.get(CloudOrderRecord, prepared["id"])
    quote_record = db_session.get(CloudQuoteRecord, quote["id"])
    assert order is not None and quote_record is not None
    order.confirmation_expires_at = utc_now() - timedelta(seconds=1)
    quote_record.expires_at = utc_now() - timedelta(seconds=1)
    quote_record.status = "expired"
    db_session.commit()

    renewed = renew_order_confirmation(
        db_session, app_settings, reg, prepared["id"]
    )
    db_session.commit()

    assert renewed["status"] == "awaiting_confirmation"
    assert renewed["confirmationToken"] != prepared["confirmationToken"]
    assert renewed["acknowledgement"] == prepared["acknowledgement"]
    assert str(renewed["confirmationExpiresAt"]).endswith("+00:00")
    renewed_expiry = datetime.fromisoformat(str(renewed["confirmationExpiresAt"]))
    assert renewed_expiry > utc_now() + timedelta(minutes=25)
    event_types = [item["eventType"] for item in list_order_events(db_session, prepared["id"])]
    assert "cloud.order.confirmation_renewed" in event_types

    result = confirm_order(
        db_session,
        app_settings,
        reg,
        prepared["id"],
        confirmation(renewed),
    )
    assert result["status"] == "submitted"
    assert len(fake.purchase_calls) == 1


def test_expired_order_renewal_rejects_changed_price_without_purchase(
    db_session, tmp_path
) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-renew-price")
    prepared = prepare_order(
        db_session, app_settings, quote["id"], "order-key-renew-price"
    )
    order = db_session.get(CloudOrderRecord, prepared["id"])
    assert order is not None
    order.confirmation_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    fake.quote_amount = Decimal("0.43")

    with pytest.raises(CloudWorkflowError) as changed:
        renew_order_confirmation(db_session, app_settings, reg, prepared["id"])

    assert changed.value.code == "price_changed"
    db_session.refresh(order)
    assert order.status == "expired"
    quote_record = db_session.get(CloudQuoteRecord, quote["id"])
    assert quote_record is not None and quote_record.status == "superseded"
    assert fake.purchase_calls == []


def test_global_search_finds_cloud_instance_name_and_specific_order_url(
    db_session, tmp_path
) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-search")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-search")

    results = global_search(db_session, "workflow-test")
    result_types = {item["type"] for item in results}
    assert {"quote", "order"}.issubset(result_types)
    quote_result = next(item for item in results if item["type"] == "quote")
    order_result = next(item for item in results if item["type"] == "order")
    assert quote_result["url"] == f"/cloud/quotes/{quote['id']}"
    assert order_result["url"] == f"/cloud/orders/{prepared['id']}"


def test_estimated_quote_cannot_prepare_a_purchase(db_session, tmp_path) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-estimate")
    record = db_session.get(CloudQuoteRecord, quote["id"])
    assert record is not None
    record.estimated = True
    db_session.commit()

    with pytest.raises(CloudWorkflowError) as blocked:
        prepare_order(db_session, app_settings, quote["id"], "order-key-estimate")
    assert blocked.value.code == "estimated_quote_not_purchasable"


def test_atomic_submission_claim_blocks_a_stale_concurrent_confirmer(db_session, tmp_path) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-concurrent")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-concurrent")
    db_session.commit()

    with Session(db_session.get_bind()) as concurrent:
        concurrent.execute(
            update(CloudOrderRecord)
            .where(CloudOrderRecord.id == prepared["id"])
            .values(status="submitting")
        )
        concurrent.commit()

    with pytest.raises(CloudWorkflowError) as conflict:
        confirm_order(
            db_session,
            app_settings,
            reg,
            prepared["id"],
            confirmation(prepared),
        )
    assert conflict.value.code == "submission_in_progress"
    assert fake.purchase_calls == []


def test_failed_order_is_terminal_and_cannot_call_provider_again(db_session, tmp_path) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-failed-terminal")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-failed-terminal")
    order = db_session.get(CloudOrderRecord, prepared["id"])
    assert order is not None
    order.status = "failed"
    db_session.commit()

    with pytest.raises(CloudWorkflowError) as terminal:
        confirm_order(
            db_session,
            app_settings,
            reg,
            prepared["id"],
            confirmation(prepared),
        )
    assert terminal.value.code == "fresh_quote_required"
    assert fake.purchase_calls == []


def test_confirmation_requotes_and_blocks_changed_provider_price(db_session, tmp_path) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path, live=True)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-reprice")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-reprice")
    fake.quote_amount = Decimal("0.43")

    with pytest.raises(CloudWorkflowError) as changed:
        confirm_order(
            db_session,
            app_settings,
            reg,
            prepared["id"],
            confirmation(prepared),
        )
    assert changed.value.code == "price_changed"
    assert fake.purchase_calls == []
    order = db_session.get(CloudOrderRecord, prepared["id"])
    bound_quote = db_session.get(CloudQuoteRecord, quote["id"])
    assert order is not None and order.status == "expired"
    assert bound_quote is not None and bound_quote.status == "superseded"


def test_startup_recovery_marks_interrupted_submission_unknown(db_session, tmp_path) -> None:
    fake = FakeProvider()
    reg = registry(fake)
    app_settings = settings(tmp_path)
    quote = create_quote(db_session, app_settings, reg, spec(), "quote-key-recovery")
    prepared = prepare_order(db_session, app_settings, quote["id"], "order-key-recovery")
    order = db_session.get(CloudOrderRecord, prepared["id"])
    assert order is not None
    order.status = "submitting"
    db_session.commit()

    assert recover_interrupted_orders(db_session) == 1
    db_session.commit()
    db_session.refresh(order)
    assert order.status == "unknown"
    assert order.error_code == "control_plane_restarted"
