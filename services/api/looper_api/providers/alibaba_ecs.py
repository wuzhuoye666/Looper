from __future__ import annotations

from datetime import timedelta
from typing import Any

from looper_core.canonical import utc_now

from looper_api.cloud_contracts import (
    CatalogFilters,
    CloudPurchaseSpec,
    ImageInfo,
    InstanceTypeInfo,
    KeyPairInfo,
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
from looper_api.providers.base import CloudProvider, CloudProviderError
from looper_api.providers.utils import (
    ambiguous_create_error,
    as_list,
    attr,
    decimal_value,
    environment_credentials,
    filter_images,
    filter_instance_types,
    nested,
    optional_environment,
    parse_datetime,
    sdk_installed,
    to_plain,
)

_REQUIRED_ENV = ["ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET"]


class AlibabaEcsProvider(CloudProvider):
    id = ProviderId.ALIBABA
    display_name = "阿里云 ECS"
    sdk_package = "alibabacloud_ecs20140526"

    def info(self, *, live_purchase_enabled: bool) -> ProviderInfo:
        _, missing = environment_credentials(_REQUIRED_ENV)
        installed = sdk_installed("alibabacloud_ecs20140526")
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
                            "statusCategory": (
                                str(status_category) if status_category else None
                            ),
                        }
                        existing = by_instance.setdefault(str(value), {}).get(zone_id)
                        status_rank = {None: 0, False: 1, True: 2}
                        if existing is None or status_rank[capability["available"]] > status_rank[
                            existing["available"]
                        ]:
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
                available=aggregate_available,
                attributes={
                    "networkEniQuantity": attr(item, "eni_quantity"),
                    "physicalProcessorModel": attr(item, "physical_processor_model"),
                    "zoneCapabilities": zone_capabilities,
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
                        attr(item, "vpc_name", default=attr(item, "vpc_id"))
                        or attr(item, "vpc_id")
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
            rows = as_list(
                nested(response, ("body",), ("v_switches",), ("v_switch",), default=[])
            )
            items.extend(
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
                )
                for item in rows
                if attr(item, "v_switch_id")
            )
            total = int(attr(nested(response, ("body",)), "total_count", default=0) or 0)
            if not rows or len(items) >= total:
                break
            page_number += 1
        return items

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
            rows = as_list(
                nested(response, ("body",), ("key_pairs",), ("key_pair",), default=[])
            )
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

    @staticmethod
    def _system_disk(models: Any, spec: CloudPurchaseSpec, *, quote: bool) -> Any:
        class_name = "DescribePriceRequestSystemDisk" if quote else "RunInstancesRequestSystemDisk"
        disk_class = getattr(models, class_name)
        return disk_class(
            category=spec.system_disk_type or "cloud_essd",
            size=spec.system_disk_gib if quote else str(spec.system_disk_gib),
        )

    def quote(self, spec: CloudPurchaseSpec) -> ProviderQuote:
        from alibabacloud_ecs20140526 import models

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
            system_disk=self._system_disk(models, spec, quote=True),
            internet_charge_type="PayByBandwidth",
            internet_max_bandwidth_out=spec.internet_bandwidth_mbps if spec.public_ip else 0,
        )
        response = self._call("describe_price", spec.region, request)
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

    def purchase(self, spec: CloudPurchaseSpec, *, client_token: str) -> ProviderPurchaseResult:
        if not spec.security_group_ids:
            raise CloudProviderError(
                "at least one security group is required for Alibaba Cloud",
                code="invalid_request",
            )
        from alibabacloud_ecs20140526 import models

        tags = [
            models.RunInstancesRequestTag(key=key, value=value) for key, value in spec.tags.items()
        ]
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
            system_disk=self._system_disk(models, spec, quote=False),
            internet_charge_type="PayByBandwidth",
            internet_max_bandwidth_out=spec.internet_bandwidth_mbps if spec.public_ip else 0,
            instance_name=spec.instance_name,
            tag=tags or None,
        )
        response = self._call("run_instances", spec.region, request)
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
        return ProviderPurchaseResult(
            providerOrderId=request_id,
            requestId=request_id,
            instances=[
                ProvisionedInstance(
                    id=str(instance_id),
                    name=spec.instance_name,
                    region=spec.region,
                    zone=spec.zone,
                    status="Pending",
                )
                for instance_id in instance_ids
            ],
            details={"requestId": request_id},
        )
