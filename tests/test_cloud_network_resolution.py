from __future__ import annotations

import pytest
from looper_api.cloud_contracts import (
    CatalogFilters,
    CloudPurchaseSpec,
    ImageInfo,
    InstanceNetworkResolveRequest,
    InstanceTypeInfo,
    ProviderId,
    ProviderInfo,
    ProviderPurchaseResult,
    ProviderQuote,
    RegionInfo,
    SubnetInfo,
    VpcInfo,
    ZoneInfo,
)
from looper_api.cloud_service import CloudWorkflowError, resolve_instance_network
from looper_api.config import Settings
from looper_api.providers.base import CloudProvider
from looper_api.providers.registry import CloudProviderRegistry


class NetworkProvider(CloudProvider):
    id = ProviderId.TENCENT
    display_name = "Network test provider"
    sdk_package = "fake"

    def __init__(self, *, subnets: list[SubnetInfo] | None = None) -> None:
        self.subnets = subnets or []
        self.created: list[tuple[str, str, str]] = []

    def info(self, *, live_purchase_enabled: bool) -> ProviderInfo:
        return ProviderInfo(
            id=self.id,
            name=self.display_name,
            sdkPackage=self.sdk_package,
            sdkInstalled=True,
            credentialsConfigured=True,
            capabilities=["instance-types", "managed-subnet"],
            livePurchaseEnabled=live_purchase_enabled,
        )

    def list_regions(self) -> list[RegionInfo]:
        return [RegionInfo(provider=self.id, id="ap-test", name="Test")]

    def list_zones(self, region: str) -> list[ZoneInfo]:
        return [
            ZoneInfo(provider=self.id, region=region, id="ap-test-1", name="One"),
            ZoneInfo(provider=self.id, region=region, id="ap-test-2", name="Two"),
        ]

    def search_instance_types(self, filters: CatalogFilters) -> list[InstanceTypeInfo]:
        return [
            InstanceTypeInfo(
                provider=self.id,
                region=filters.region or "ap-test",
                id="S9.TEST",
                cpu=4,
                memoryGib=8,
                available=True,
                zones=["ap-test-1", "ap-test-2"],
                attributes={
                    "zoneCapabilities": [
                        {"zone": "ap-test-1", "available": True},
                        {"zone": "ap-test-2", "available": True},
                    ]
                },
            )
        ]

    def search_images(self, filters: CatalogFilters) -> list[ImageInfo]:
        return []

    def list_vpcs(self, region: str) -> list[VpcInfo]:
        return [
            VpcInfo(
                provider=self.id,
                region=region,
                id="vpc-a",
                name="A",
                cidrBlock="10.0.0.0/16",
            ),
            VpcInfo(
                provider=self.id,
                region=region,
                id="vpc-z",
                name="Default",
                cidrBlock="172.16.0.0/16",
                isDefault=True,
            ),
        ]

    def list_vpc_subnets(self, region: str, vpc_id: str) -> list[SubnetInfo]:
        return [item for item in self.subnets if item.vpc_id == vpc_id]

    def create_managed_subnet(
        self,
        *,
        region: str,
        zone: str,
        vpc_id: str,
        cidr_block: str,
        name: str,
        client_token: str,
    ) -> SubnetInfo:
        self.created.append((zone, vpc_id, cidr_block))
        item = SubnetInfo(
            provider=self.id,
            region=region,
            zone=zone,
            vpcId=vpc_id,
            id="subnet-created",
            name=name,
            cidrBlock=cidr_block,
            availableIpCount=250,
            tags={"managedBy": "looper", "purpose": "instance-network"},
            managed=True,
        )
        self.subnets.append(item)
        return item

    def quote(self, spec: CloudPurchaseSpec) -> ProviderQuote:
        raise NotImplementedError

    def purchase(self, spec: CloudPurchaseSpec, *, client_token: str) -> ProviderPurchaseResult:
        raise NotImplementedError


def resolve(db_session, tmp_path, provider: NetworkProvider, **overrides: str):
    request = InstanceNetworkResolveRequest(
        region="ap-test",
        instanceType="S9.TEST",
        **overrides,
    )
    registry = CloudProviderRegistry({ProviderId.TENCENT: lambda: provider})
    return resolve_instance_network(
        db_session,
        Settings(_env_file=None, data_dir=tmp_path),
        registry,
        ProviderId.TENCENT,
        request,
        idempotency_key="network-resolution-test-key",
    )


def test_resolver_prefers_zone_with_existing_subnet_and_default_vpc(db_session, tmp_path) -> None:
    provider = NetworkProvider(
        subnets=[
            SubnetInfo(
                provider="tencent",
                region="ap-test",
                zone="ap-test-2",
                vpcId="vpc-z",
                id="subnet-existing",
                name="Existing",
                cidrBlock="172.16.1.0/24",
                availableIpCount=20,
            )
        ]
    )

    result = resolve(db_session, tmp_path, provider)

    assert result.zone == "ap-test-2"
    assert result.vpc.id == "vpc-z"
    assert result.subnet.id == "subnet-existing"
    assert result.subnet_action == "reused"
    assert provider.created == []


def test_resolver_creates_lowest_free_managed_subnet(db_session, tmp_path) -> None:
    provider = NetworkProvider(
        subnets=[
            SubnetInfo(
                provider="tencent",
                region="ap-test",
                zone="ap-test-2",
                vpcId="vpc-a",
                id="subnet-used",
                name="Used",
                cidrBlock="10.0.0.0/24",
                availableIpCount=0,
            )
        ]
    )

    result = resolve(db_session, tmp_path, provider, vpc_id="vpc-a")

    assert result.zone == "ap-test-1"
    assert result.subnet_action == "created"
    assert result.subnet.cidr_block == "10.0.1.0/24"
    assert result.subnet.managed is True
    assert provider.created == [("ap-test-1", "vpc-a", "10.0.1.0/24")]


def test_resolver_rejects_unavailable_explicit_zone(db_session, tmp_path) -> None:
    provider = NetworkProvider()

    with pytest.raises(CloudWorkflowError, match="指定可用区不可售") as raised:
        resolve(db_session, tmp_path, provider, zone="ap-test-9")

    assert raised.value.code == "instance_type_zone_unavailable"
