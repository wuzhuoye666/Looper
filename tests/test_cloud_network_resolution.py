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
    SecurityGroupInfo,
    SubnetInfo,
    VpcInfo,
    ZoneInfo,
)
from looper_api.cloud_service import CloudWorkflowError, resolve_instance_network
from looper_api.config import Settings
from looper_api.providers.base import CloudProvider, CloudProviderError
from looper_api.providers.registry import CloudProviderRegistry


class NetworkProvider(CloudProvider):
    id = ProviderId.TENCENT
    display_name = "Network test provider"
    sdk_package = "fake"

    def __init__(
        self,
        *,
        vpcs: list[VpcInfo] | None = None,
        subnets: list[SubnetInfo] | None = None,
    ) -> None:
        self.vpcs = vpcs if vpcs is not None else [
            VpcInfo(
                provider=self.id,
                region="ap-test",
                id="vpc-a",
                name="A",
                cidrBlock="10.0.0.0/16",
            ),
            VpcInfo(
                provider=self.id,
                region="ap-test",
                id="vpc-z",
                name="Default",
                cidrBlock="172.16.0.0/16",
                isDefault=True,
            ),
        ]
        self.subnets = subnets or []
        self.created: list[tuple[str, str, str]] = []
        self.created_vpcs: list[tuple[str, str, str]] = []
        self.security_groups = [
            SecurityGroupInfo(
                provider=self.id,
                region="ap-test",
                id="sg-existing",
                name="Default",
                isDefault=True,
            )
        ]
        self.created_security_groups: list[tuple[str | None, str | None]] = []

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
        return list(self.vpcs)

    def create_managed_vpc(
        self,
        *,
        region: str,
        cidr_block: str,
        name: str,
        client_token: str,
    ) -> VpcInfo:
        self.created_vpcs.append((cidr_block, name, client_token))
        item = VpcInfo(
            provider=self.id,
            region=region,
            id="vpc-created",
            name=name,
            cidrBlock=cidr_block,
            tags={"managedBy": "looper", "purpose": "cloud-purchase"},
            managed=True,
        )
        self.vpcs.append(item)
        return item

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

    def list_security_groups(self, region: str) -> list[SecurityGroupInfo]:
        return list(self.security_groups)

    def ensure_managed_security_group(
        self,
        region: str,
        *,
        vpc_id: str | None = None,
        client_token: str | None = None,
    ) -> SecurityGroupInfo:
        self.created_security_groups.append((vpc_id, client_token))
        item = SecurityGroupInfo(
            provider=self.id,
            region=region,
            id="sg-created",
            name="looper-ssh-access",
            recommended=True,
            tags={
                "managedBy": "looper",
                "purpose": "cloud-purchase",
                "policyVersion": "ssh-v1",
            },
            managed=True,
        )
        self.security_groups.append(item)
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
    registry = CloudProviderRegistry({provider.id: lambda: provider})
    return resolve_instance_network(
        db_session,
        Settings(_env_file=None, data_dir=tmp_path),
        registry,
        provider.id,
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
    assert result.vpc_action == "reused"
    assert result.subnet_action == "reused"
    assert result.security_group.id == "sg-existing"
    assert result.security_group_action == "reused"
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


def test_resolver_creates_vpc_and_subnet_when_region_has_no_vpc(db_session, tmp_path) -> None:
    provider = NetworkProvider(vpcs=[])

    result = resolve(db_session, tmp_path, provider)

    assert result.vpc_action == "created"
    assert result.vpc.id == "vpc-created"
    assert result.vpc.cidr_block == "10.0.0.0/16"
    assert result.vpc.managed is True
    assert result.subnet_action == "created"
    assert result.subnet.cidr_block == "10.0.0.0/24"
    assert len(provider.created_vpcs) == 1


def test_resolver_recovers_vpc_after_ambiguous_create_response(db_session, tmp_path) -> None:
    class AmbiguousNetworkProvider(NetworkProvider):
        def create_managed_vpc(self, **kwargs) -> VpcInfo:
            super().create_managed_vpc(**kwargs)
            raise CloudProviderError(
                "simulated response loss", code="ambiguous_response", ambiguous=True
            )

    provider = AmbiguousNetworkProvider(vpcs=[])

    result = resolve(db_session, tmp_path, provider)

    assert result.vpc_action == "created"
    assert result.vpc.id == "vpc-created"
    assert any("创建响应不明确" in warning for warning in result.warnings)
    assert len(provider.created_vpcs) == 1
    assert provider.created_vpcs[0][1].startswith("looper-vpc-")
    assert provider.created == [("ap-test-1", "vpc-created", "10.0.0.0/24")]

    retried = resolve(db_session, tmp_path, provider)
    assert retried.vpc_action == "reused"
    assert retried.vpc.id == "vpc-created"
    assert retried.subnet_action == "reused"
    assert len(provider.created_vpcs) == 1


def test_resolver_rejects_unavailable_explicit_zone(db_session, tmp_path) -> None:
    provider = NetworkProvider()

    with pytest.raises(CloudWorkflowError, match="指定可用区不可售") as raised:
        resolve(db_session, tmp_path, provider, zone="ap-test-9")

    assert raised.value.code == "instance_type_zone_unavailable"


def test_resolver_creates_and_selects_security_group_when_region_has_none(
    db_session, tmp_path
) -> None:
    provider = NetworkProvider()
    provider.security_groups = []

    result = resolve(db_session, tmp_path, provider)

    assert result.security_group_action == "created"
    assert result.security_group is not None
    assert result.security_group.id == "sg-created"
    assert provider.created_security_groups[0][0] == result.vpc.id


def test_resolver_leaves_multiple_plain_security_groups_for_manual_selection(
    db_session, tmp_path
) -> None:
    provider = NetworkProvider()
    provider.security_groups = [
        SecurityGroupInfo(provider="tencent", region="ap-test", id="sg-a", name="A"),
        SecurityGroupInfo(provider="tencent", region="ap-test", id="sg-b", name="B"),
    ]

    result = resolve(db_session, tmp_path, provider)

    assert result.security_group is None
    assert result.security_group_action == "selection-required"
    assert provider.created_security_groups == []


def test_alibaba_resolver_ignores_security_groups_from_another_vpc(
    db_session, tmp_path
) -> None:
    class AlibabaNetworkProvider(NetworkProvider):
        id = ProviderId.ALIBABA

    provider = AlibabaNetworkProvider()
    provider.security_groups = [
        SecurityGroupInfo(
            provider="alibaba",
            region="ap-test",
            id="sg-other-vpc",
            name="Other VPC",
            vpcId="vpc-a",
        )
    ]

    result = resolve(db_session, tmp_path, provider, vpc_id="vpc-z")

    assert result.security_group_action == "created"
    assert result.security_group is not None
    assert provider.created_security_groups[0][0] == "vpc-z"
