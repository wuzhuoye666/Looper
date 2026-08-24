from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any

from looper_core.canonical import canonical_digest, utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.cloud_contracts import (
    CatalogFilters,
    CloudPurchaseSpec,
    DestroyedResource,
    ImageInfo,
    InstanceTypeInfo,
    KeyPairInfo,
    ProviderDestroyResult,
    ProviderId,
    ProviderInfo,
    ProviderPurchaseResult,
    ProviderQuote,
    ProvisionedInstance,
    RegionInfo,
    SecurityGroupInfo,
    SubnetInfo,
    VpcInfo,
    ZoneInfo,
)
from looper_api.external_targets import reconcile_external_duplicate
from looper_api.models import TargetRecord
from looper_api.providers.base import CloudProvider, CloudProviderError
from looper_api.providers.utils import (
    ambiguous_create_error,
    as_list,
    attr,
    cloud_target_id,
    decimal_value,
    environment_credentials,
    filter_images,
    filter_instance_types,
    has_online_worker_for_target,
    legacy_cloud_target_ids,
    nested,
    optional_environment,
    parse_datetime,
    sdk_installed,
    to_plain,
)

_REQUIRED_ENV = ["ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET"]


class AlibabaInventoryError(CloudProviderError):
    pass


_SYSTEM_DISK_CATEGORY_PREFERENCE = (
    "cloud_essd",
    "cloud_essd_entry",
    "cloud_auto",
    "cloud_efficiency",
    "cloud_ssd",
    "cloud",
)

# Alibaba Cloud generation-I families cannot be launched into a VPC because
# they do not support ENIs.  Looper's purchase contract always requires a
# vSwitch, so exposing these SKUs as purchasable only defers an inevitable
# provider rejection until after the user submits the order.
_VPC_INCOMPATIBLE_INSTANCE_TYPE = re.compile(r"^ecs\.(?:t1|s[123]|m[12]|c[12])(?:\.|$)", re.I)


def _supports_vpc_launch(instance_type: str, eni_quantity: Any = None) -> bool:
    if _VPC_INCOMPATIBLE_INSTANCE_TYPE.match(instance_type):
        return False
    if eni_quantity is not None:
        try:
            return int(eni_quantity) > 0
        except (TypeError, ValueError):
            pass
    return True


class AlibabaEcsProvider(CloudProvider):
    id = ProviderId.ALIBABA
    display_name = "阿里云 ECS"
    sdk_package = "alibabacloud_ecs20140526 + alibabacloud_vpc20160428"

    def info(self, *, live_purchase_enabled: bool) -> ProviderInfo:
        _, missing = environment_credentials(_REQUIRED_ENV)
        installed = sdk_installed("alibabacloud_ecs20140526") and sdk_installed(
            "alibabacloud_vpc20160428"
        )
        return ProviderInfo(
            id=self.id,
            name=self.display_name,
            sdkPackage=self.sdk_package,
            sdkInstalled=installed,
            credentialsConfigured=not missing,
            missingEnvironment=missing,
            capabilities=[
                "regions",
                "zones",
                "instance-types",
                "images",
                "stock-advisory",
                "vpcs",
                "subnets",
                "managed-subnet",
                "security-groups",
                "key-pairs",
                "hourly-quote",
                "postpaid-purchase",
                "client-token",
                "dry-run",
            ],
            livePurchaseEnabled=live_purchase_enabled and installed and not missing,
            message=None if installed and not missing else "安装 SDK 并配置显式环境变量后可用",
        )

    def _client(self, region: str):
        try:
            from alibabacloud_ecs20140526.client import Client
            from alibabacloud_tea_openapi.models import Config
        except ImportError as error:
            raise CloudProviderError(f"install {self.sdk_package}", code="sdk_missing") from error
        values, missing = environment_credentials(_REQUIRED_ENV)
        if missing:
            raise CloudProviderError(
                f"missing Alibaba Cloud credentials: {', '.join(missing)}",
                code="credentials_missing",
            )
        config = Config(
            access_key_id=values["ALIBABA_CLOUD_ACCESS_KEY_ID"],
            access_key_secret=values["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
            security_token=optional_environment("ALIBABA_CLOUD_SECURITY_TOKEN"),
            region_id=region,
        )
        return Client(config)

    def _call(self, method: str, region: str, request: Any) -> Any:
        try:
            return getattr(self._client(region), method)(request)
        except Exception as error:
            provider_code = attr(error, "code", default=None)
            code = provider_code or error.__class__.__name__
            message = attr(error, "message", default=str(error))
            data = attr(error, "data", default={}) or {}
            request_id = attr(data, "RequestId", "requestId", default=None)
            raise CloudProviderError(
                f"Alibaba Cloud {method} failed: {message}",
                code=str(code),
                retryable=str(code) in {"Throttling", "ServiceUnavailable", "InternalError"},
                ambiguous=method == "run_instances"
                and ambiguous_create_error(provider_code, error),
                details={"requestId": request_id} if request_id else {},
            ) from error

    def _vpc_client(self, region: str):
        try:
            from alibabacloud_tea_openapi.models import Config
            from alibabacloud_vpc20160428.client import Client
        except ImportError as error:
            raise CloudProviderError(
                "install alibabacloud_vpc20160428", code="sdk_missing"
            ) from error
        values, missing = environment_credentials(_REQUIRED_ENV)
        if missing:
            raise CloudProviderError(
                f"missing Alibaba Cloud credentials: {', '.join(missing)}",
                code="credentials_missing",
            )
        config = Config(
            access_key_id=values["ALIBABA_CLOUD_ACCESS_KEY_ID"],
            access_key_secret=values["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
            security_token=optional_environment("ALIBABA_CLOUD_SECURITY_TOKEN"),
            region_id=region,
        )
        return Client(config)

    def _vpc_call(self, method: str, region: str, request: Any) -> Any:
        try:
            return getattr(self._vpc_client(region), method)(request)
        except Exception as error:
            provider_code = attr(error, "code", default=None)
            code = provider_code or error.__class__.__name__
            message = attr(error, "message", default=str(error))
            data = attr(error, "data", default={}) or {}
            request_id = attr(data, "RequestId", "requestId", default=None)
            raise CloudProviderError(
                f"Alibaba Cloud VPC {method} failed: {message}",
                code=str(code),
                retryable=str(code) in {"Throttling", "ServiceUnavailable", "InternalError"},
                ambiguous=method == "create_vswitch"
                and ambiguous_create_error(provider_code, error),
                details={"requestId": request_id} if request_id else {},
            ) from error

    def list_regions(self) -> list[RegionInfo]:
        from alibabacloud_ecs20140526 import models

        request = models.DescribeRegionsRequest(
            instance_charge_type="PostPaid", resource_type="instance"
        )
        response = self._call("describe_regions", "cn-hangzhou", request)
        rows = as_list(nested(response, ("body",), ("regions",), ("region",), default=[]))
        return [
            RegionInfo(
                provider=self.id,
                id=str(attr(item, "region_id")),
                name=str(attr(item, "local_name", default=attr(item, "region_id"))),
                endpoint=attr(item, "region_endpoint"),
                available=True,
            )
            for item in rows
            if attr(item, "region_id")
        ]

    def list_zones(self, region: str) -> list[ZoneInfo]:
        from alibabacloud_ecs20140526 import models

        request = models.DescribeZonesRequest(
            region_id=region,
            instance_charge_type="PostPaid",
            spot_strategy="NoSpot",
            verbose=True,
        )
        response = self._call("describe_zones", region, request)
        rows = as_list(nested(response, ("body",), ("zones",), ("zone",), default=[]))
        return [
            ZoneInfo(
                provider=self.id,
                region=region,
                id=str(attr(item, "zone_id")),
                name=str(attr(item, "local_name", default=attr(item, "zone_id"))),
                available=str(attr(item, "zone_type", default="AvailabilityZone")) != "Unavailable",
            )
            for item in rows
            if attr(item, "zone_id")
            and str(attr(item, "zone_type", default="AvailabilityZone") or "AvailabilityZone")
            == "AvailabilityZone"
        ]

    @staticmethod
    def _stock_status(status: Any, status_category: Any) -> bool | None:
        normalized = str(status or "").casefold()
        if normalized == "available":
            return True
        if normalized == "soldout":
            return False
        category = str(status_category or "").casefold()
        if category in {"withstock", "closedwithstock"}:
            return True
        if category in {"withoutstock", "closedwithoutstock"}:
            return False
        return None

    def _availability(self, filters: CatalogFilters) -> dict[str, list[dict[str, Any]]]:
        if not filters.region:
            return {}
        from alibabacloud_ecs20140526 import models

        zone_ids = (
            [filters.zone]
            if filters.zone
            else [zone.id for zone in self.list_zones(filters.region) if zone.id]
        )
        by_instance: dict[str, dict[str, dict[str, Any]]] = {}
        for requested_zone in zone_ids:
            request = models.DescribeAvailableResourceRequest(
                region_id=filters.region,
                zone_id=requested_zone,
                destination_resource="InstanceType",
                resource_type="instance",
                instance_charge_type="PostPaid",
                spot_strategy="NoSpot",
                io_optimized="optimized",
            )
            response = self._call("describe_available_resource", filters.region, request)
            zones = as_list(
                nested(
                    response,
                    ("body",),
                    ("available_zones",),
                    ("available_zone",),
                    default=[],
                )
            )
            for zone in zones:
                zone_id = str(attr(zone, "zone_id", default=requested_zone) or requested_zone)
                resources = as_list(attr(zone, "available_resources", default=[]))
                if not isinstance(attr(zone, "available_resources"), list):
                    resources = as_list(
                        attr(
                            attr(zone, "available_resources"),
                            "available_resource",
                            default=[],
                        )
                    )
                for resource in resources:
                    supported = attr(resource, "supported_resources", default=[])
                    if not isinstance(supported, list):
                        supported = attr(supported, "supported_resource", default=[])
                    for item in as_list(supported):
                        value = attr(item, "value")
                        if not value:
                            continue
                        status = attr(item, "status")
                        status_category = attr(item, "status_category")
                        capability = {
                            "zone": zone_id,
                            "available": self._stock_status(status, status_category),
                            "status": str(status) if status else None,
                            "statusCategory": (str(status_category) if status_category else None),
                        }
                        existing = by_instance.setdefault(str(value), {}).get(zone_id)
                        status_rank = {None: 0, False: 1, True: 2}
                        if (
                            existing is None
                            or status_rank[capability["available"]]
                            > status_rank[existing["available"]]
                        ):
                            by_instance[str(value)][zone_id] = capability
        return {
            instance_type: [capabilities[zone] for zone in sorted(capabilities)]
            for instance_type, capabilities in by_instance.items()
        }

    def search_instance_types(self, filters: CatalogFilters) -> list[InstanceTypeInfo]:
        if not filters.region:
            raise CloudProviderError("region is required", code="invalid_request")
        from alibabacloud_ecs20140526 import models

        available = self._availability(filters)
        rows: list[Any] = []
        next_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            request = models.DescribeInstanceTypesRequest(
                minimum_cpu_core_count=filters.min_cpu,
                maximum_cpu_core_count=filters.max_cpu,
                minimum_memory_size=filters.min_memory_gib,
                maximum_memory_size=filters.max_memory_gib,
                max_results=100,
                next_token=next_token,
            )
            response = self._call("describe_instance_types", filters.region, request)
            body = nested(response, ("body",))
            batch = as_list(nested(body, ("instance_types",), ("instance_type",), default=[]))
            rows.extend(batch)
            next_token = attr(body, "next_token")
            if not next_token or not batch:
                break
            if next_token in seen_tokens:
                raise CloudProviderError(
                    "Alibaba Cloud instance type pagination repeated a token",
                    code="pagination_stalled",
                )
            seen_tokens.add(next_token)
        items: list[InstanceTypeInfo] = []
        for item in rows:
            instance_type = str(attr(item, "instance_type_id", default="") or "")
            capabilities = available.get(instance_type, [])
            if not instance_type or not capabilities:
                continue
            eni_quantity = attr(item, "eni_quantity")
            purchase_compatible = _supports_vpc_launch(instance_type, eni_quantity)
            gpu = float(attr(item, "gpuamount", "gpu_amount", default=0) or 0)
            bandwidth_rx = (
                float(attr(item, "instance_bandwidth_rx")) / 1_000_000
                if attr(item, "instance_bandwidth_rx") is not None
                else None
            )
            bandwidth_tx = (
                float(attr(item, "instance_bandwidth_tx")) / 1_000_000
                if attr(item, "instance_bandwidth_tx") is not None
                else None
            )
            pps_rx = attr(item, "instance_pps_rx")
            pps_tx = attr(item, "instance_pps_tx")
            local_storage_count = attr(item, "local_storage_amount")
            local_storage_capacity = attr(item, "local_storage_capacity")
            local_storage_category = attr(item, "local_storage_category")
            zone_capabilities = [
                {
                    **capability,
                    "gpu": gpu,
                    "networkBandwidthGbps": (
                        min(bandwidth_rx, bandwidth_tx)
                        if bandwidth_rx is not None and bandwidth_tx is not None
                        else bandwidth_rx or bandwidth_tx
                    ),
                    "networkPps": (
                        min(pps_rx, pps_tx)
                        if pps_rx is not None and pps_tx is not None
                        else pps_rx or pps_tx
                    ),
                    "localStorageCount": local_storage_count,
                    "localStorageCapacityGib": local_storage_capacity,
                    "localStorageCategory": local_storage_category,
                }
                for capability in capabilities
            ]
            statuses = {capability["available"] for capability in zone_capabilities}
            aggregate_available = (
                True if True in statuses else False if statuses == {False} else None
            )
            items.append(
                InstanceTypeInfo(
                    provider=self.id,
                    region=filters.region,
                    id=instance_type,
                    family=attr(item, "instance_type_family"),
                    cpu=int(attr(item, "cpu_core_count", default=0) or 0),
                    memoryGib=float(attr(item, "memory_size", default=0) or 0),
                    gpu=gpu,
                    gpuModel=attr(item, "gpuspec", "gpu_spec"),
                    gpuMemoryGib=attr(item, "gpumemory_size", "gpu_memory_size"),
                    architecture=attr(item, "cpu_architecture"),
                    networkBandwidthRxGbps=bandwidth_rx,
                    networkBandwidthTxGbps=bandwidth_tx,
                    networkPpsRx=pps_rx,
                    networkPpsTx=pps_tx,
                    localStorageCount=local_storage_count,
                    localStorageCapacityGib=local_storage_capacity,
                    localStorageCategory=local_storage_category,
                    zones=sorted(
                        {
                            str(capability["zone"])
                            for capability in zone_capabilities
                            if capability.get("zone")
                        }
                    ),
                    available=aggregate_available if purchase_compatible else False,
                    attributes={
                        "networkEniQuantity": eni_quantity,
                        "physicalProcessorModel": attr(item, "physical_processor_model"),
                        "zoneCapabilities": zone_capabilities,
                        "purchaseCompatible": purchase_compatible,
                        "purchaseBlockReason": (
                            None
                            if purchase_compatible
                            else "该旧规格不支持 VPC 弹性网卡，无法用于当前购买流程"
                        ),
                    },
                )
            )
        return filter_instance_types(items, filters)

    def search_images(self, filters: CatalogFilters) -> list[ImageInfo]:
        if not filters.region:
            raise CloudProviderError("region is required", code="invalid_request")
        from alibabacloud_ecs20140526 import models

        rows: list[Any] = []
        page_number = 1
        seen_pages: set[tuple[str, ...]] = set()
        while True:
            page_size = 100
            request = models.DescribeImagesRequest(
                region_id=filters.region,
                image_owner_alias="system",
                status="Available",
                usable=True,
                page_number=page_number,
                page_size=page_size,
                architecture=None,
                ostype=filters.platform,
                instance_type=filters.instance_type,
            )
            response = self._call("describe_images", filters.region, request)
            batch = as_list(nested(response, ("body",), ("images",), ("image",), default=[]))
            signature = tuple(str(attr(item, "image_id", default="")) for item in batch)
            if batch and signature in seen_pages:
                raise CloudProviderError(
                    "Alibaba Cloud image pagination repeated a page",
                    code="pagination_stalled",
                )
            if batch:
                seen_pages.add(signature)
            rows.extend(batch)
            if len(batch) < page_size:
                break
            page_number += 1
        items = [
            ImageInfo(
                provider=self.id,
                region=filters.region,
                id=str(attr(item, "image_id")),
                name=str(attr(item, "image_name", default=attr(item, "image_id"))),
                platform=attr(item, "platform", "os_name"),
                architecture=attr(item, "architecture"),
                imageType=attr(item, "image_owner_alias"),
                sizeGib=float(attr(item, "size", default=0) or 0),
                createdAt=parse_datetime(attr(item, "creation_time")),
                available=str(attr(item, "status", default="Available")) == "Available",
            )
            for item in rows
            if attr(item, "image_id")
        ]
        return filter_images(items, filters)

    def list_vpcs(self, region: str) -> list[VpcInfo]:
        from alibabacloud_ecs20140526 import models

        items: list[VpcInfo] = []
        page_number = 1
        while True:
            request = models.DescribeVpcsRequest(
                region_id=region,
                page_number=page_number,
                page_size=50,
            )
            response = self._call("describe_vpcs", region, request)
            rows = as_list(nested(response, ("body",), ("vpcs",), ("vpc",), default=[]))
            items.extend(
                VpcInfo(
                    provider=self.id,
                    region=region,
                    id=str(attr(item, "vpc_id")),
                    name=str(
                        attr(item, "vpc_name", default=attr(item, "vpc_id")) or attr(item, "vpc_id")
                    ),
                    cidrBlock=attr(item, "cidr_block"),
                    isDefault=bool(attr(item, "is_default", default=False)),
                )
                for item in rows
                if attr(item, "vpc_id")
            )
            total = int(attr(nested(response, ("body",)), "total_count", default=0) or 0)
            if not rows or len(items) >= total:
                break
            page_number += 1
        return items

    def list_subnets(self, region: str, zone: str, vpc_id: str) -> list[SubnetInfo]:
        return self._list_vswitches(region, vpc_id=vpc_id, zone=zone)

    def list_vpc_subnets(self, region: str, vpc_id: str) -> list[SubnetInfo]:
        return self._list_vswitches(region, vpc_id=vpc_id, zone=None)

    def _list_vswitches(self, region: str, *, vpc_id: str, zone: str | None) -> list[SubnetInfo]:
        from alibabacloud_ecs20140526 import models

        items: list[SubnetInfo] = []
        page_number = 1
        while True:
            request = models.DescribeVSwitchesRequest(
                region_id=region,
                zone_id=zone,
                vpc_id=vpc_id,
                page_number=page_number,
                page_size=50,
            )
            response = self._call("describe_vswitches", region, request)
            rows = as_list(nested(response, ("body",), ("v_switches",), ("v_switch",), default=[]))
            for item in rows:
                if not attr(item, "v_switch_id"):
                    continue
                tag_rows = as_list(nested(item, ("tags",), ("tag",), default=[]))
                tags = {
                    str(attr(tag, "tag_key")): str(attr(tag, "tag_value", default=""))
                    for tag in tag_rows
                    if attr(tag, "tag_key")
                }
                items.append(
                    SubnetInfo(
                        provider=self.id,
                        region=region,
                        zone=str(attr(item, "zone_id")),
                        vpcId=str(attr(item, "vpc_id")),
                        id=str(attr(item, "v_switch_id")),
                        name=str(
                            attr(item, "v_switch_name", default=attr(item, "v_switch_id"))
                            or attr(item, "v_switch_id")
                        ),
                        cidrBlock=attr(item, "cidr_block"),
                        availableIpCount=attr(item, "available_ip_address_count"),
                        isDefault=bool(attr(item, "is_default", default=False)),
                        tags=tags,
                        managed=tags.get("managedBy", "").casefold() == "looper",
                    )
                )
            total = int(attr(nested(response, ("body",)), "total_count", default=0) or 0)
            if not rows or len(items) >= total:
                break
            page_number += 1
        return items

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
        from alibabacloud_vpc20160428 import models

        request = models.CreateVSwitchRequest(
            region_id=region,
            zone_id=zone,
            vpc_id=vpc_id,
            cidr_block=cidr_block,
            v_switch_name=name,
            client_token=client_token,
            tag=[
                models.CreateVSwitchRequestTag(key="managedBy", value="looper"),
                models.CreateVSwitchRequestTag(key="purpose", value="cloud-purchase"),
            ],
        )
        response = self._vpc_call("create_vswitch", region, request)
        v_switch_id = attr(nested(response, ("body",)), "v_switch_id")
        if not v_switch_id:
            raise CloudProviderError(
                "Alibaba Cloud created a vSwitch without returning its id",
                code="ambiguous_response",
                ambiguous=True,
            )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            describe = models.DescribeVSwitchAttributesRequest(
                region_id=region,
                v_switch_id=str(v_switch_id),
            )
            item = nested(
                self._vpc_call("describe_vswitch_attributes", region, describe),
                ("body",),
            )
            status = str(attr(item, "status", default="") or "")
            if status.casefold() == "available":
                tags = {"managedBy": "looper", "purpose": "cloud-purchase"}
                return SubnetInfo(
                    provider=self.id,
                    region=region,
                    zone=str(attr(item, "zone_id", default=zone)),
                    vpcId=str(attr(item, "vpc_id", default=vpc_id)),
                    id=str(v_switch_id),
                    name=str(attr(item, "v_switch_name", default=name) or name),
                    cidrBlock=attr(item, "cidr_block", default=cidr_block),
                    availableIpCount=attr(item, "available_ip_address_count"),
                    tags=tags,
                    managed=True,
                )
            if status and status.casefold() not in {"pending", "creating"}:
                raise CloudProviderError(
                    f"Alibaba Cloud vSwitch entered unexpected state {status}",
                    code="subnet_create_failed",
                )
            time.sleep(1)
        raise CloudProviderError(
            "Alibaba Cloud vSwitch creation is still pending",
            code="ambiguous_response",
            ambiguous=True,
        )

    def list_security_groups(self, region: str) -> list[SecurityGroupInfo]:
        from alibabacloud_ecs20140526 import models

        items: list[SecurityGroupInfo] = []
        page_number = 1
        while True:
            request = models.DescribeSecurityGroupsRequest(
                region_id=region,
                page_number=page_number,
                page_size=50,
                network_type="vpc",
            )
            response = self._call("describe_security_groups", region, request)
            rows = as_list(
                nested(
                    response,
                    ("body",),
                    ("security_groups",),
                    ("security_group",),
                    default=[],
                )
            )
            for item in rows:
                tag_rows = as_list(nested(item, ("tags",), ("tag",), default=[]))
                tags = {
                    str(attr(tag, "tag_key")): str(attr(tag, "tag_value", default=""))
                    for tag in tag_rows
                    if attr(tag, "tag_key")
                }
                group_id = attr(item, "security_group_id")
                if not group_id:
                    continue
                name = str(attr(item, "security_group_name", default=group_id) or group_id)
                items.append(
                    SecurityGroupInfo(
                        provider=self.id,
                        region=region,
                        id=str(group_id),
                        name=name,
                        description=attr(item, "description"),
                        recommended=(
                            tags.get("managedBy", "").casefold() == "looper"
                            or name.casefold().startswith("looper")
                        ),
                        tags=tags,
                    )
                )
            total = int(attr(nested(response, ("body",)), "total_count", default=0) or 0)
            if not rows or len(items) >= total:
                break
            page_number += 1
        return items

    def list_key_pairs(self, region: str) -> list[KeyPairInfo]:
        from alibabacloud_ecs20140526 import models

        items: list[KeyPairInfo] = []
        page_number = 1
        while True:
            request = models.DescribeKeyPairsRequest(
                region_id=region,
                page_number=page_number,
                page_size=50,
            )
            response = self._call("describe_key_pairs", region, request)
            rows = as_list(nested(response, ("body",), ("key_pairs",), ("key_pair",), default=[]))
            items.extend(
                KeyPairInfo(
                    provider=self.id,
                    region=region,
                    id=str(attr(item, "key_pair_name")),
                    name=str(attr(item, "key_pair_name")),
                    createdAt=parse_datetime(attr(item, "creation_time")),
                )
                for item in rows
                if attr(item, "key_pair_name")
            )
            total = int(attr(nested(response, ("body",)), "total_count", default=0) or 0)
            if not rows or len(items) >= total:
                break
            page_number += 1
        return items

    def _available_system_disk_categories(self, spec: CloudPurchaseSpec) -> list[str]:
        from alibabacloud_ecs20140526 import models

        request = models.DescribeAvailableResourceRequest(
            region_id=spec.region,
            zone_id=spec.zone,
            destination_resource="SystemDisk",
            instance_type=spec.instance_type,
            instance_charge_type="PostPaid",
            spot_strategy="NoSpot",
        )
        response = self._call("describe_available_resource", spec.region, request)
        categories: list[str] = []
        zones = as_list(
            nested(response, ("body",), ("available_zones",), ("available_zone",), default=[])
        )
        for zone in zones:
            resources = as_list(
                nested(zone, ("available_resources",), ("available_resource",), default=[])
            )
            for resource in resources:
                if str(attr(resource, "type", default="")) != "SystemDisk":
                    continue
                supported = as_list(
                    nested(resource, ("supported_resources",), ("supported_resource",), default=[])
                )
                for item in supported:
                    value = attr(item, "value")
                    if value:
                        categories.append(str(value))
        return categories

    def _system_disk_candidates(self, spec: CloudPurchaseSpec) -> list[str]:
        if spec.system_disk_type:
            return [spec.system_disk_type]
        try:
            available = self._available_system_disk_categories(spec)
        except Exception:
            available = []
        ordered: list[str] = []
        for candidate in _SYSTEM_DISK_CATEGORY_PREFERENCE:
            if candidate in available and candidate not in ordered:
                ordered.append(candidate)
        for candidate in available:
            if candidate not in ordered:
                ordered.append(candidate)
        for candidate in _SYSTEM_DISK_CATEGORY_PREFERENCE:
            if candidate not in ordered:
                ordered.append(candidate)
        return ordered or ["cloud_essd"]

    @staticmethod
    def _is_disk_category_error(error: CloudProviderError) -> bool:
        code = str(getattr(error, "code", "") or "").lower()
        message = str(error).lower()
        if "diskcategory" in code or "disk_category" in code or "notsupportdisk" in code:
            return True
        return (
            "disk category" in message
            or "systemdisk.category" in message
            or "category is not valid" in message
        )

    @staticmethod
    def _system_disk(models: Any, spec: CloudPurchaseSpec, *, quote: bool, category: str) -> Any:
        class_name = "DescribePriceRequestSystemDisk" if quote else "RunInstancesRequestSystemDisk"
        disk_class = getattr(models, class_name)
        return disk_class(
            category=category,
            size=spec.system_disk_gib if quote else str(spec.system_disk_gib),
        )

    def quote(self, spec: CloudPurchaseSpec) -> ProviderQuote:
        from alibabacloud_ecs20140526 import models

        if not _supports_vpc_launch(spec.instance_type):
            raise CloudProviderError(
                "所选阿里云旧规格不支持 VPC 弹性网卡，请选择较新的 ECS 规格",
                code="instance_type_vpc_incompatible",
            )

        last_error: CloudProviderError | None = None
        for category in self._system_disk_candidates(spec):
            request = models.DescribePriceRequest(
                region_id=spec.region,
                zone_id=spec.zone,
                resource_type="instance",
                instance_type=spec.instance_type,
                image_id=spec.image_id,
                amount=spec.count,
                price_unit="Hour",
                period=1,
                spot_strategy="NoSpot",
                system_disk=self._system_disk(models, spec, quote=True, category=category),
                internet_charge_type="PayByBandwidth",
                internet_max_bandwidth_out=spec.internet_bandwidth_mbps if spec.public_ip else 0,
            )
            try:
                response = self._call("describe_price", spec.region, request)
            except CloudProviderError as error:
                if self._is_disk_category_error(error):
                    last_error = error
                    continue
                raise
            price = nested(response, ("body",), ("price_info",), ("price",))
            amount = decimal_value(attr(price, "trade_price", "original_price"))
            if amount <= 0:
                raise CloudProviderError("Alibaba Cloud quote did not include an hourly price")
            return ProviderQuote(
                providerQuoteId=attr(nested(response, ("body",)), "request_id"),
                amount=amount,
                currency=str(attr(price, "currency", default="CNY")),
                estimated=False,
                expiresAt=utc_now() + timedelta(minutes=5),
                details={
                    "requestId": attr(nested(response, ("body",)), "request_id"),
                    "originalPrice": to_plain(attr(price, "original_price")),
                    "tradePrice": to_plain(attr(price, "trade_price")),
                },
            )
        if last_error is not None:
            raise last_error
        raise CloudProviderError(
            "no usable system disk category", code="system_disk_category_unresolved"
        )

    def purchase(self, spec: CloudPurchaseSpec, *, client_token: str) -> ProviderPurchaseResult:
        if not _supports_vpc_launch(spec.instance_type):
            raise CloudProviderError(
                "所选阿里云旧规格不支持 VPC 弹性网卡，请选择较新的 ECS 规格",
                code="instance_type_vpc_incompatible",
            )
        if not spec.security_group_ids:
            raise CloudProviderError(
                "at least one security group is required for Alibaba Cloud",
                code="invalid_request",
            )
        from alibabacloud_ecs20140526 import models

        tags = [
            models.RunInstancesRequestTag(key=key, value=value) for key, value in spec.tags.items()
        ]
        last_error: CloudProviderError | None = None
        response: Any = None
        for category in self._system_disk_candidates(spec):
            request = models.RunInstancesRequest(
                region_id=spec.region,
                zone_id=spec.zone,
                image_id=spec.image_id,
                instance_type=spec.instance_type,
                security_group_ids=spec.security_group_ids,
                v_switch_id=spec.subnet_id,
                instance_charge_type="PostPaid",
                amount=spec.count,
                spot_strategy="NoSpot",
                key_pair_name=spec.key_pair_id,
                client_token=client_token,
                dry_run=False,
                system_disk=self._system_disk(models, spec, quote=False, category=category),
                internet_charge_type="PayByBandwidth",
                internet_max_bandwidth_out=spec.internet_bandwidth_mbps if spec.public_ip else 0,
                instance_name=spec.instance_name,
                tag=tags or None,
            )
            try:
                response = self._call("run_instances", spec.region, request)
            except CloudProviderError as error:
                if self._is_disk_category_error(error):
                    last_error = error
                    continue
                raise
            break
        if response is None:
            if last_error is not None:
                raise last_error
            raise CloudProviderError(
                "no usable system disk category", code="system_disk_category_unresolved"
            )
        body = nested(response, ("body",))
        instance_ids = as_list(
            nested(body, ("instance_id_sets",), ("instance_id_set",), default=[])
        )
        if not instance_ids:
            raise CloudProviderError(
                "Alibaba Cloud accepted the request without returning instance ids",
                code="ambiguous_response",
                ambiguous=True,
            )
        request_id = attr(body, "request_id")

        # VPC RunInstances may return before a public address is attached.
        # Allocate one explicitly when the purchase requested internet access.
        public_ip_warning: str | None = None
        if spec.public_ip:
            for instance_id in instance_ids:
                try:
                    self._call(
                        "allocate_public_ip_address",
                        spec.region,
                        models.AllocatePublicIpAddressRequest(instance_id=str(instance_id)),
                    )
                except CloudProviderError as error:
                    # An existing public IP is harmless; describe below remains authoritative.
                    public_ip_warning = str(error)

        # Fetch instance details to get public IP
        provisioned_instances = []
        for instance_id in instance_ids:
            public_ip = None
            private_ip = None
            try:
                describe_request = models.DescribeInstancesRequest(
                    region_id=spec.region,
                    instance_ids=[str(instance_id)],
                )
                describe_response = self._call("describe_instances", spec.region, describe_request)
                instances = nested(
                    describe_response,
                    ("body",),
                    ("instances",),
                    ("instance",),
                    default=[],
                )
                if instances:
                    inst = instances[0]
                    # Get public IP
                    public_ip_attr = nested(
                        inst, ("public_ip_address",), ("ip_address",), default=[]
                    )
                    if public_ip_attr:
                        public_ip = (
                            str(public_ip_attr[0])
                            if isinstance(public_ip_attr, list)
                            else str(public_ip_attr)
                        )
                    # Get private IP
                    vpc_attr = nested(
                        inst,
                        ("vpc_attributes",),
                        ("private_ip_address",),
                        ("ip_address",),
                        default=[],
                    )
                    if vpc_attr:
                        private_ip = (
                            str(vpc_attr[0]) if isinstance(vpc_attr, list) else str(vpc_attr)
                        )
            except Exception:
                pass

            provisioned_instances.append(
                ProvisionedInstance(
                    id=str(instance_id),
                    name=spec.instance_name,
                    region=spec.region,
                    zone=spec.zone,
                    status="Pending",
                    private_ip=private_ip,
                    public_ip=public_ip,
                    public_ip_present=public_ip is not None,
                )
            )

        return ProviderPurchaseResult(
            providerOrderId=request_id,
            requestId=request_id,
            instances=provisioned_instances,
            details={
                "requestId": request_id,
                **({"publicIpWarning": public_ip_warning} if public_ip_warning else {}),
            },
        )

    def destroy(self, *, region: str, instance_ids: list[str]) -> ProviderDestroyResult:
        from alibabacloud_ecs20140526 import models

        normalized = [value.strip() for value in instance_ids if value and value.strip()]
        if not normalized or len(normalized) != len(set(normalized)):
            raise CloudProviderError(
                "instance ids must be non-empty and unique", code="invalid_request"
            )
        request = models.DeleteInstancesRequest(
            region_id=region,
            instance_id=normalized,
            force=True,
        )
        response = self._call("delete_instances", region, request)
        request_id = attr(nested(response, ("body",)), "request_id")
        released: list[DestroyedResource] = []
        for instance_id in normalized:
            released.append(
                DestroyedResource(kind="instance", id=instance_id, note="按量实例已销毁")
            )
            released.append(
                DestroyedResource(
                    kind="system-disk",
                    id=f"{instance_id}:system-disk",
                    note="系统盘随实例一并释放",
                )
            )
            released.append(
                DestroyedResource(
                    kind="public-ip",
                    id=f"{instance_id}:public-ip",
                    note="按量公网 IP 与带宽随实例释放",
                )
            )
        return ProviderDestroyResult(
            request_id=request_id,
            instance_ids=normalized,
            released_resources=released,
            details={"requestId": request_id},
        )

    def cleanup_managed_network(
        self,
        *,
        region: str,
        vpc_id: str | None,
        subnet_id: str | None,
        security_group_ids: list[str],
    ) -> list[DestroyedResource]:
        del vpc_id, security_group_ids
        if not subnet_id:
            return []
        from alibabacloud_vpc20160428 import models

        try:
            describe = models.DescribeVSwitchAttributesRequest(
                region_id=region,
                v_switch_id=subnet_id,
            )
            item = nested(
                self._vpc_call("describe_vswitch_attributes", region, describe),
                ("body",),
            )
            tag_rows = as_list(nested(item, ("tags",), ("tag",), default=[]))
            tags = {
                str(attr(tag, "key", "tag_key")): str(attr(tag, "value", "tag_value", default=""))
                for tag in tag_rows
                if attr(tag, "key", "tag_key")
            }
            if tags.get("managedBy", "").casefold() != "looper":
                return [
                    DestroyedResource(
                        kind="subnet",
                        id=subnet_id,
                        released=False,
                        note="非 Looper 纳管 vSwitch，保留不动",
                    )
                ]
            delete_request = models.DeleteVSwitchRequest(
                region_id=region,
                v_switch_id=subnet_id,
            )
            self._vpc_call("delete_vswitch", region, delete_request)
            return [
                DestroyedResource(kind="subnet", id=subnet_id, note="Looper 纳管 vSwitch 已删除")
            ]
        except CloudProviderError as error:
            return [
                DestroyedResource(
                    kind="subnet",
                    id=subnet_id,
                    released=False,
                    note=f"vSwitch 清理暂缓：{error}",
                )
            ]


_ARCHIVE_AFTER_AUTHORITATIVE_MISSES = 3


def sync_ecs_inventory(
    session: Session,
    region: str,
    instance_ids: list[str] | None = None,
    *,
    credential_store: Any | None = None,
) -> list[TargetRecord]:
    """Sync Alibaba Cloud ECS instances for a region into the target inventory."""
    if not region:
        raise AlibabaInventoryError("a valid Alibaba Cloud region is required")
    provider = AlibabaEcsProvider()
    from alibabacloud_ecs20140526 import models

    if instance_ids:
        normalized_ids = [value.strip() for value in instance_ids if value and value.strip()]
        if len(normalized_ids) > 100 or len(normalized_ids) != len(set(normalized_ids)):
            raise AlibabaInventoryError(
                "instance ids must be unique and contain at most 100 values"
            )
        if any(not value.startswith("i-") or len(value) > 64 for value in normalized_ids):
            raise AlibabaInventoryError("invalid Alibaba Cloud instance id")
        request = models.DescribeInstancesRequest(
            region_id=region, instance_ids=normalized_ids, page_size=len(normalized_ids)
        )
        response = provider._call("describe_instances", region, request)
        instances = as_list(nested(response, ("body",), ("instances",), ("instance",), default=[]))
        imported = [_upsert_instance(session, region, item) for item in instances]
        for record in imported:
            reconcile_external_duplicate(session, record, credential_store=credential_store)
        return imported

    page_number = 1
    page_size = 100
    imported: list[TargetRecord] = []
    while True:
        request = models.DescribeInstancesRequest(
            region_id=region, page_number=page_number, page_size=page_size
        )
        response = provider._call("describe_instances", region, request)
        body = nested(response, ("body",))
        instances = as_list(nested(body, ("instances",), ("instance",), default=[]))
        for instance in instances:
            record = _upsert_instance(session, region, instance)
            reconcile_external_duplicate(session, record, credential_store=credential_store)
            imported.append(record)
        total = int(attr(body, "total_count", default=0) or 0)
        if not instances or len(imported) >= total:
            break
        page_number += 1
    _reconcile_missing_inventory(session, region, {record.id for record in imported})
    return imported


def _reconcile_missing_inventory(session: Session, region: str, seen_target_ids: set[str]) -> None:
    """Retain historical targets while removing absent instances from the active pool."""
    now = utc_now()
    records = list(session.scalars(select(TargetRecord).where(TargetRecord.provider == "alibaba")))
    for record in records:
        inventory = record.inventory_json or {}
        if inventory.get("region") != region or record.id in seen_target_ids:
            continue
        misses = record.inventory_miss_count + 1
        record.lifecycle_status = (
            "archived" if misses >= _ARCHIVE_AFTER_AUTHORITATIVE_MISSES else "missing"
        )
        record.inventory_missing_since = record.inventory_missing_since or now
        record.inventory_miss_count = misses
        record.runnable = False
        record.updated_at = now
        if record.lifecycle_status == "archived":
            record.archived_at = record.archived_at or now
            record.archive_reason = "absent-after-authoritative-inventory-syncs"


def _upsert_instance(session: Session, region: str, instance: Any) -> TargetRecord:
    now = utc_now()
    instance_id = str(attr(instance, "instance_id") or "")
    target_id = cloud_target_id("alibaba", region, instance_id)
    public_ips = as_list(nested(instance, ("public_ip_address",), ("ip_address",), default=[]))
    private_ips = as_list(
        nested(
            instance,
            ("vpc_attributes",),
            ("private_ip_address",),
            ("ip_address",),
            default=[],
        )
    )
    vpc = nested(instance, ("vpc_attributes",), default=None)
    record = session.get(TargetRecord, target_id)
    if record is None:
        for legacy_id in legacy_cloud_target_ids("alibaba", region, instance_id):
            record = session.get(TargetRecord, legacy_id)
            if record is not None:
                break
    existing_inventory = record.inventory_json if record is not None else {}
    public_ip = public_ips[0] if public_ips else None
    private_ip = private_ips[0] if private_ips else None
    inventory = {
        "source": existing_inventory.get("source", "cloud-inventory"),
        "region": region,
        "zone": attr(instance, "zone_id"),
        "instance_id": instance_id,
        "instance_name": attr(instance, "instance_name"),
        "instance_state": attr(instance, "status"),
        "image_id": attr(instance, "image_id"),
        "vpc_id": attr(vpc, "vpc_id") if vpc else None,
        "subnet_id": attr(vpc, "vswitch_id") if vpc else None,
        "private_ip": private_ip,
        "public_ip": public_ip,
        "endpoint": public_ip or private_ip,
        "public_ip_present": bool(public_ip),
    }
    for key in ("order_id", "autoSsh"):
        if key in existing_inventory:
            inventory[key] = existing_inventory[key]
    memory_mib = attr(instance, "memory", default=0) or 0
    memory_gib = (float(memory_mib) / 1024) if memory_mib else None
    # Keep SSH-verified facts (host key, probe inventory) inherited from an
    # external twin when duplicate records are folded; provider facts below
    # stay authoritative for their own keys.
    preserved_fingerprint = record.fingerprint_json if record is not None else {}
    fingerprint = {
        **preserved_fingerprint,
        "provider": "alibaba",
        "region": region,
        "zone": inventory["zone"],
        "instance_type": attr(instance, "instance_type"),
        "cpu": attr(instance, "cpu"),
        "memory_gib": memory_gib,
        "image_id": attr(instance, "image_id"),
        "os_name": attr(instance, "os_name"),
    }
    capabilities = sorted(
        (set(record.capabilities_json) if record is not None else set())
        | {"alibaba-ecs", 'inventory'}
    )
    snapshot = {
        "provider": "alibaba",
        "capabilities": capabilities,
        "fingerprint": fingerprint,
    }
    runnable_now = (
        inventory.get("autoSsh", {}).get("status") == "connected"
        or (record is not None and has_online_worker_for_target(session, record.id))
    )
    values: dict[str, Any] = {
        "name": attr(instance, "instance_name") or instance_id,
        "provider": "alibaba",
        "status": "available" if runnable_now else "inventory-only",
        "capabilities_json": capabilities,
        "inventory_json": inventory,
        "fingerprint_json": fingerprint,
        "snapshot_digest": canonical_digest(snapshot),
        "runnable": runnable_now,
        "lifecycle_status": "active",
        "last_inventory_seen_at": now,
        "inventory_missing_since": None,
        "inventory_miss_count": 0,
        "archived_at": None,
        "archive_reason": None,
        "updated_at": now,
    }
    if record is None:
        record = TargetRecord(id=target_id, created_at=now, **values)
        session.add(record)
    else:
        for field, value in values.items():
            setattr(record, field, value)
    return record
