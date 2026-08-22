from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from looper_api.cloud_contracts import CatalogFilters, CloudPurchaseSpec
from looper_api.providers.alibaba_ecs import AlibabaEcsProvider
from looper_api.providers.baidu_bcc import BaiduBccProvider
from looper_api.providers.base import CloudProviderError
from looper_api.providers.tencent_cvm import TencentCvmProvider, sync_cvm_inventory
from looper_api.providers.utils import ambiguous_create_error
from looper_api.providers.volcengine_ecs import VolcengineEcsProvider


def purchase_spec(provider: str = "tencent", *, public_ip: bool = True) -> CloudPurchaseSpec:
    return CloudPurchaseSpec(
        provider=provider,
        region="region-test",
        zone="zone-test-1",
        instanceType="family.small",
        cpu=2,
        memoryGib=4,
        imageId="img-test",
        instanceName="looper-contract",
        vpcId="vpc-test",
        subnetId="subnet-test",
        securityGroupIds=["sg-one", "sg-two"],
        keyPairId="key-test",
        systemDiskType="disk-test",
        systemDiskGib=60,
        publicIp=public_ip,
        internetBandwidthMbps=5 if public_ip else 0,
        tags={"managedBy": "looper", "purpose": "contract"},
    )


def test_tencent_quote_and_run_share_launch_payload(monkeypatch) -> None:
    provider = TencentCvmProvider()
    spec = purchase_spec()
    calls: list[tuple[str, object]] = []

    def call(method: str, _region: str, request: object):
        calls.append((method, request))
        if method == "InquiryPriceRunInstances":
            return SimpleNamespace(
                RequestId="quote-request",
                Price=SimpleNamespace(
                    InstancePrice=SimpleNamespace(UnitPrice=Decimal("0.4")),
                    BandwidthPrice=SimpleNamespace(UnitPrice=Decimal("0.1")),
                ),
            )
        if method == "DescribeInstances":
            return SimpleNamespace(
                InstanceSet=[
                    SimpleNamespace(
                        InstanceId="ins-test",
                        InstanceName="looper-contract",
                        InstanceState="RUNNING",
                        PrivateIpAddresses=["10.0.0.8"],
                        PublicIpAddresses=["203.0.113.8"],
                        Placement=SimpleNamespace(Zone="zone-test-1"),
                    )
                ]
            )
        return SimpleNamespace(RequestId="run-request", InstanceIdSet=["ins-test"])

    monkeypatch.setattr(provider, "_call", call)
    quote = provider.quote(spec)
    result = provider.purchase(spec, client_token="stable-token")

    quote_map = calls[0][1].to_json_string()
    run_map = calls[1][1].to_json_string()
    assert quote.amount == Decimal("0.5")
    assert result.instances[0].id == "ins-test"
    assert result.instances[0].status == "RUNNING"
    assert result.instances[0].private_ip == "10.0.0.8"
    assert result.instances[0].public_ip_present is True
    assert '"InstanceChargeType": "POSTPAID_BY_HOUR"' in quote_map
    assert '"DiskSize": 60' in run_map
    assert '"ClientToken": "stable-token"' in run_map
    assert '"PublicIpAssigned": true' in run_map
    assert '"InternetChargeType": "BANDWIDTH_POSTPAID_BY_HOUR"' in run_map
    assert '"TagSpecification"' in run_map


def test_tencent_network_catalog_maps_defaults_filters_and_recommendations(
    monkeypatch,
) -> None:
    provider = TencentCvmProvider()
    vpc_calls: list[tuple[str, object]] = []
    cvm_calls: list[tuple[str, object]] = []

    def vpc_call(method: str, _region: str, request: object):
        vpc_calls.append((method, request))
        if method == "DescribeVpcs":
            return SimpleNamespace(
                TotalCount=1,
                VpcSet=[
                    SimpleNamespace(
                        VpcId="vpc-default",
                        VpcName="Default-VPC",
                        CidrBlock="172.16.0.0/16",
                        IsDefault=True,
                    )
                ],
            )
        if method == "DescribeSubnets":
            return SimpleNamespace(
                TotalCount=1,
                SubnetSet=[
                    SimpleNamespace(
                        VpcId="vpc-default",
                        SubnetId="subnet-default",
                        SubnetName="Default-Subnet",
                        CidrBlock="172.16.1.0/24",
                        IsDefault=True,
                        Zone="ap-test-1",
                        AvailableIpAddressCount=250,
                    )
                ],
            )
        return SimpleNamespace(
            TotalCount=1,
            SecurityGroupSet=[
                SimpleNamespace(
                    SecurityGroupId="sg-looper",
                    SecurityGroupName="looper-private",
                    SecurityGroupDesc="managed",
                    IsDefault=False,
                    TagSet=[SimpleNamespace(Key="managedBy", Value="looper")],
                )
            ],
        )

    def cvm_call(method: str, _region: str, request: object):
        cvm_calls.append((method, request))
        return SimpleNamespace(
            TotalCount=1,
            KeyPairSet=[
                SimpleNamespace(
                    KeyId="skey-test",
                    KeyName="operator-key",
                    Description="test key",
                    CreatedTime="2026-08-20T00:00:00Z",
                    AssociatedInstanceIds=["ins-one"],
                )
            ],
        )

    monkeypatch.setattr(provider, "_vpc_call", vpc_call)
    monkeypatch.setattr(provider, "_call", cvm_call)

    vpcs = provider.list_vpcs("ap-test")
    subnets = provider.list_subnets("ap-test", "ap-test-1", "vpc-default")
    groups = provider.list_security_groups("ap-test")
    keys = provider.list_key_pairs("ap-test")

    assert vpcs[0].is_default is True
    assert subnets[0].available_ip_count == 250
    assert groups[0].recommended is True
    assert groups[0].tags == {"managedBy": "looper"}
    assert keys[0].associated_instance_count == 1
    subnet_payload = vpc_calls[1][1].to_json_string()
    assert '"Name": "vpc-id", "Values": ["vpc-default"]' in subnet_payload
    assert '"Name": "zone", "Values": ["ap-test-1"]' in subnet_payload
    assert '"Offset": "0"' in subnet_payload
    assert cvm_calls[0][0] == "DescribeKeyPairs"


def test_tencent_ensure_security_group_uses_safe_atomic_policy(monkeypatch) -> None:
    provider = TencentCvmProvider()
    calls: list[tuple[str, object]] = []

    def call(method: str, _region: str, request: object):
        calls.append((method, request))
        if method == "DescribeSecurityGroups":
            return SimpleNamespace(TotalCount=0, SecurityGroupSet=[])
        return SimpleNamespace(
            SecurityGroup=SimpleNamespace(
                SecurityGroupId="sg-created",
                SecurityGroupName="looper-private-outbound",
                SecurityGroupDesc="managed",
                IsDefault=False,
                TagSet=[SimpleNamespace(Key="managedBy", Value="looper")],
            )
        )

    monkeypatch.setattr(provider, "_vpc_call", call)
    group = provider.ensure_managed_security_group("ap-test")

    assert group.id == "sg-created"
    assert group.recommended is True
    assert [method for method, _request in calls] == [
        "DescribeSecurityGroups",
        "CreateSecurityGroupWithPolicies",
    ]
    payload = calls[1][1].to_json_string()
    assert '"Ingress": []' in payload
    assert '"CidrBlock": "0.0.0.0/0"' in payload
    assert '"Action": "ACCEPT"' in payload
    assert '"managedBy"' in payload


def test_tencent_targeted_inventory_sync_only_requests_selected_instances(
    monkeypatch, db_session
) -> None:
    calls: list[object] = []

    def call(_provider, method: str, _region: str, request: object):
        assert method == "DescribeInstances"
        calls.append(request)
        return SimpleNamespace(
            InstanceSet=[
                SimpleNamespace(
                    InstanceId="ins-selected",
                    InstanceName="selected-instance",
                    InstanceState="RUNNING",
                    InstanceType="SA9.MEDIUM2",
                    CPU=2,
                    Memory=2,
                    ImageId="img-test",
                    OsName="Ubuntu Server 24.04 LTS",
                    PrivateIpAddresses=["10.0.0.9"],
                    PublicIpAddresses=[],
                    Placement=SimpleNamespace(Zone="ap-test-1"),
                    VirtualPrivateCloud=SimpleNamespace(VpcId="vpc-test", SubnetId="subnet-test"),
                )
            ]
        )

    monkeypatch.setattr(TencentCvmProvider, "_call", call)
    records = sync_cvm_inventory(db_session, "ap-test", ["ins-selected"])

    assert len(records) == 1
    assert records[0].id == "cloud:tencent:ap-test:ins-selected"
    assert records[0].inventory_json["instance_state"] == "RUNNING"
    payload = calls[0].to_json_string()
    assert '"InstanceIds": ["ins-selected"]' in payload
    assert '"Offset": null' in payload


def test_alibaba_quote_and_run_use_postpaid_network_disk_and_token(monkeypatch) -> None:
    provider = AlibabaEcsProvider()
    spec = purchase_spec("alibaba")
    calls: list[tuple[str, object]] = []

    def call(method: str, _region: str, request: object):
        calls.append((method, request))
        if method == "describe_price":
            return SimpleNamespace(
                body=SimpleNamespace(
                    request_id="quote-request",
                    price_info=SimpleNamespace(
                        price=SimpleNamespace(
                            trade_price=Decimal("0.8"),
                            original_price=Decimal("1.0"),
                            currency="CNY",
                        )
                    ),
                )
            )
        return SimpleNamespace(
            body=SimpleNamespace(
                request_id="run-request",
                instance_id_sets=SimpleNamespace(instance_id_set=["i-test"]),
            )
        )

    monkeypatch.setattr(provider, "_call", call)
    quote = provider.quote(spec)
    result = provider.purchase(spec, client_token="stable-token")

    quote_request = calls[0][1]
    run_request = calls[1][1]
    assert quote.amount == Decimal("0.8")
    assert result.instances[0].id == "i-test"
    assert quote_request.price_unit == "Hour"
    assert quote_request.system_disk.size == 60
    assert run_request.instance_charge_type == "PostPaid"
    assert quote_request.internet_charge_type == "PayByBandwidth"
    assert run_request.internet_charge_type == "PayByBandwidth"
    assert run_request.system_disk.size == "60"
    assert run_request.client_token == "stable-token"
    assert run_request.security_group_ids == ["sg-one", "sg-two"]
    assert [(item.key, item.value) for item in run_request.tag] == [
        ("managedBy", "looper"),
        ("purpose", "contract"),
    ]


def test_alibaba_catalog_search_scans_later_pages(monkeypatch) -> None:
    provider = AlibabaEcsProvider()
    calls: list[tuple[str, object]] = []
    first_types = [
        SimpleNamespace(
            instance_type_id=f"ecs.dummy.{index}",
            instance_type_family="dummy",
            cpu_core_count=2,
            memory_size=4,
        )
        for index in range(100)
    ]
    first_images = [
        SimpleNamespace(
            image_id=f"img-dummy-{index}",
            image_name=f"Unrelated {index}",
            status="Available",
        )
        for index in range(100)
    ]

    def call(method: str, _region: str, request: object):
        calls.append((method, request))
        if method == "describe_instance_types":
            rows = (
                first_types
                if request.next_token is None
                else [
                    SimpleNamespace(
                        instance_type_id="ecs.target.large",
                        instance_type_family="target",
                        cpu_core_count=4,
                        memory_size=8,
                    )
                ]
            )
            return SimpleNamespace(
                body=SimpleNamespace(
                    next_token="page-two" if request.next_token is None else None,
                    instance_types=SimpleNamespace(instance_type=rows),
                )
            )
        rows = (
            first_images
            if request.page_number == 1
            else [
                SimpleNamespace(
                    image_id="img-target",
                    image_name="Target Linux",
                    status="Available",
                )
            ]
        )
        return SimpleNamespace(body=SimpleNamespace(images=SimpleNamespace(image=rows)))

    monkeypatch.setattr(provider, "_call", call)
    type_results = provider.search_instance_types(
        CatalogFilters(region="cn-test", query="target", limit=10)
    )
    image_results = provider.search_images(
        CatalogFilters(region="cn-test", query="target", limit=10)
    )

    assert [item.id for item in type_results] == ["ecs.target.large"]
    assert [item.id for item in image_results] == ["img-target"]
    assert len([method for method, _ in calls if method == "describe_instance_types"]) == 2
    assert len([method for method, _ in calls if method == "describe_images"]) == 2


def test_tencent_image_search_scans_later_pages(monkeypatch) -> None:
    provider = TencentCvmProvider()
    offsets: list[int] = []

    def image(image_id: str, name: str) -> SimpleNamespace:
        return SimpleNamespace(
            ImageId=image_id,
            ImageName=name,
            OsName="Linux",
            ImageState="NORMAL",
        )

    first = [image(f"img-dummy-{index}", f"Unrelated {index}") for index in range(100)]

    def call(_method: str, _region: str, request: object):
        offsets.append(request.Offset)
        rows = first if request.Offset == 0 else [image("img-target", "Target Linux")]
        return SimpleNamespace(ImageSet=rows, TotalCount=101)

    monkeypatch.setattr(provider, "_call", call)
    results = provider.search_images(CatalogFilters(region="ap-test", query="target", limit=10))
    assert [item.id for item in results] == ["img-target"]
    assert offsets == [0, 100]


def test_volcengine_catalog_search_scans_later_pages(monkeypatch) -> None:
    provider = VolcengineEcsProvider()
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(provider, "_availability", lambda _filters: {})

    def instance(item_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            instance_type_id=item_id,
            instance_type_family="family",
            processor=SimpleNamespace(cpus=2),
            memory=SimpleNamespace(size=4),
        )

    def image(image_id: str, name: str) -> SimpleNamespace:
        return SimpleNamespace(
            image_id=image_id,
            image_name=name,
            status="available",
        )

    first_types = [instance(f"ecs.dummy.{index}") for index in range(100)]
    first_images = [image(f"img-dummy-{index}", f"Unrelated {index}") for index in range(100)]

    def call(method: str, _region: str, request: object):
        calls.append((method, request.next_token))
        if method == "describe_instance_types":
            rows = first_types if request.next_token is None else [instance("ecs.target.large")]
            return SimpleNamespace(
                instance_types=rows,
                next_token="type-page-two" if request.next_token is None else None,
            )
        rows = first_images if request.next_token is None else [image("img-target", "Target")]
        return SimpleNamespace(
            images=rows,
            next_token="image-page-two" if request.next_token is None else None,
        )

    monkeypatch.setattr(provider, "_call", call)
    types = provider.search_instance_types(
        CatalogFilters(region="cn-test", query="target", limit=10)
    )
    images = provider.search_images(CatalogFilters(region="cn-test", query="target", limit=10))
    assert [item.id for item in types] == ["ecs.target.large"]
    assert [item.id for item in images] == ["img-target"]
    assert calls == [
        ("describe_instance_types", None),
        ("describe_instance_types", "type-page-two"),
        ("describe_images", None),
        ("describe_images", "image-page-two"),
    ]


def test_baidu_image_search_scans_markers(monkeypatch) -> None:
    provider = BaiduBccProvider()
    markers: list[str | None] = []
    first = [
        SimpleNamespace(id=f"img-dummy-{index}", name=f"Unrelated {index}", status="available")
        for index in range(100)
    ]

    def call(_method: str, _region: str, **kwargs):
        markers.append(kwargs["marker"])
        if kwargs["marker"] is None:
            return SimpleNamespace(images=first, next_marker="page-two")
        return SimpleNamespace(
            images=[SimpleNamespace(id="img-target", name="Target Linux", status="available")],
            next_marker=None,
        )

    monkeypatch.setattr(provider, "_call", call)
    results = provider.search_images(CatalogFilters(region="bj", query="target", limit=10))
    assert [item.id for item in results] == ["img-target"]
    assert markers == [None, "page-two"]


def test_volcengine_run_shape_and_unsupported_public_ip(monkeypatch) -> None:
    provider = VolcengineEcsProvider()
    monkeypatch.setenv("VOLCENGINE_ACCESS_KEY", "test-ak")
    monkeypatch.setenv("VOLCENGINE_SECRET_KEY", "test-sk")
    status = provider.info(live_purchase_enabled=True)
    assert status.live_purchase_enabled is False
    assert "quote-blocked-price-mapping" in status.capabilities
    with pytest.raises(CloudProviderError, match="existing EIP"):
        provider.purchase(purchase_spec("volcengine"), client_token="stable-token")

    captured: dict[str, object] = {}

    def call(method: str, region: str, request: object):
        captured.update(method=method, region=region, request=request)
        return SimpleNamespace(instance_ids=["i-volc-test"], request_id="run-request")

    monkeypatch.setattr(provider, "_call", call)
    result = provider.purchase(
        purchase_spec("volcengine", public_ip=False), client_token="stable-token"
    )
    request = captured["request"]
    assert result.instances[0].id == "i-volc-test"
    assert request.instance_charge_type == "PostPaid"
    assert request.client_token == "stable-token"
    assert request.dry_run is False
    assert request.network_interfaces[0].security_group_ids == ["sg-one", "sg-two"]
    assert request.volumes[0].size == 60
    assert [(item.key, item.value) for item in request.tags] == [
        ("managedBy", "looper"),
        ("purpose", "contract"),
    ]


def test_baidu_quote_and_flavor_run_forward_network_tags_and_tokens(monkeypatch) -> None:
    provider = BaiduBccProvider()
    monkeypatch.setenv("BAIDU_BCE_ACCESS_KEY_ID", "test-ak")
    monkeypatch.setenv("BAIDU_BCE_SECRET_ACCESS_KEY", "test-sk")
    assert provider.info(live_purchase_enabled=True).live_purchase_enabled is False
    with pytest.raises(CloudProviderError) as public_ip_error:
        provider.quote(purchase_spec("baidu"))
    assert public_ip_error.value.code == "public_ip_price_not_supported"
    spec = purchase_spec("baidu", public_ip=False)
    calls: list[tuple[str, dict[str, object]]] = []

    def call(method: str, _region: str, *args: object, **kwargs: object):
        calls.append((method, kwargs))
        if method == "get_price_by_spec":
            return SimpleNamespace(
                request_id="quote-request",
                price=SimpleNamespace(price=Decimal("0.6"), currency="CNY"),
            )
        return SimpleNamespace(instance_ids=["i-baidu-test"], request_id="run-request")

    monkeypatch.setattr(provider, "_call", call)
    quote = provider.quote(spec)
    result = provider.purchase(spec, client_token="stable-token")

    quote_call = calls[0][1]
    run = calls[1][1]
    assert quote.amount == Decimal("0.6")
    assert result.instances[0].id == "i-baidu-test"
    assert calls[1][0] == "create_instance_by_spec"
    assert run["spec"] == "family.small"
    assert "cpu_count" not in run
    assert "memory_capacity_in_gb" not in run
    assert run["security_group_ids"] == ["sg-one", "sg-two"]
    assert run["key_pair_id"] == "key-test"
    assert len(str(quote_call["client_token"])) == 63
    assert run["client_token"] == "stable-token"
    assert [(tag.tagKey, tag.tagValue) for tag in run["tags"]] == [
        ("managedBy", "looper"),
        ("purpose", "contract"),
    ]


class _TransportFailureClient:
    def __getattr__(self, _name: str):
        def fail(*_args: object, **_kwargs: object) -> None:
            raise TimeoutError("socket timed out after request write")

        return fail


@pytest.mark.parametrize(
    ("provider", "client_attribute", "method"),
    [
        (TencentCvmProvider(), "_client", "RunInstances"),
        (AlibabaEcsProvider(), "_client", "run_instances"),
        (VolcengineEcsProvider(), "_api", "run_instances"),
        (BaiduBccProvider(), "_client", "create_instance_by_spec"),
    ],
)
def test_transport_failures_during_create_are_ambiguous(
    monkeypatch, provider, client_attribute: str, method: str
) -> None:
    monkeypatch.setattr(provider, client_attribute, lambda _region: _TransportFailureClient())
    with pytest.raises(CloudProviderError) as caught:
        provider._call(method, "region-test", object())
    assert caught.value.ambiguous is True


def test_explicit_business_rejection_is_not_ambiguous() -> None:
    assert (
        ambiguous_create_error("InvalidParameter.InstanceType", RuntimeError("rejected")) is False
    )
    assert ambiguous_create_error("InternalError", RuntimeError("provider failed")) is True
