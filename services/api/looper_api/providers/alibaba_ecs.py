from __future__ import annotations

from datetime import timedelta
from typing import Any

from looper_core.canonical import utc_now

from looper_api.cloud_contracts import (
    CatalogFilters,
    CloudPurchaseSpec,
    ImageInfo,
    InstanceTypeInfo,
    ProviderId,
    ProviderInfo,
    ProviderPurchaseResult,
    ProviderQuote,
    ProvisionedInstance,
    RegionInfo,
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
        ]

    def _availability(self, filters: CatalogFilters) -> dict[str, bool]:
        if not filters.region or not filters.zone:
            return {}
        from alibabacloud_ecs20140526 import models

        request = models.DescribeAvailableResourceRequest(
            region_id=filters.region,
            zone_id=filters.zone,
            destination_resource="InstanceType",
            resource_type="instance",
            instance_charge_type="PostPaid",
            spot_strategy="NoSpot",
            io_optimized="optimized",
        )
        response = self._call("describe_available_resource", filters.region, request)
        result: dict[str, bool] = {}
        zones = as_list(
            nested(response, ("body",), ("available_zones",), ("available_zone",), default=[])
        )
        for zone in zones:
            resources = as_list(attr(zone, "available_resources", default=[]))
            if not isinstance(attr(zone, "available_resources"), list):
                resources = as_list(
                    attr(attr(zone, "available_resources"), "available_resource", default=[])
                )
            for resource in resources:
                supported = attr(resource, "supported_resources", default=[])
                if not isinstance(supported, list):
                    supported = attr(supported, "supported_resource", default=[])
                for item in as_list(supported):
                    value = attr(item, "value")
                    status = str(attr(item, "status", default="")).casefold()
                    if value:
                        result[str(value)] = status in {"available", "sufficient", "normal"}
        return result

    def search_instance_types(self, filters: CatalogFilters) -> list[InstanceTypeInfo]:
        if not filters.region:
            raise CloudProviderError("region is required", code="invalid_request")
        from alibabacloud_ecs20140526 import models

        available = self._availability(filters)
        rows: list[Any] = []
        next_token: str | None = None
        scan_limit = max(filters.limit, 1000 if filters.query else filters.limit)
        while len(rows) < scan_limit:
            request = models.DescribeInstanceTypesRequest(
                minimum_cpu_core_count=filters.min_cpu,
                maximum_cpu_core_count=filters.max_cpu,
                minimum_memory_size=filters.min_memory_gib,
                maximum_memory_size=filters.max_memory_gib,
                max_results=min(100, scan_limit - len(rows)),
                next_token=next_token,
            )
            response = self._call("describe_instance_types", filters.region, request)
            body = nested(response, ("body",))
            batch = as_list(nested(body, ("instance_types",), ("instance_type",), default=[]))
            rows.extend(batch)
            next_token = attr(body, "next_token")
            if not next_token or not batch:
                break
        items = [
            InstanceTypeInfo(
                provider=self.id,
                region=filters.region,
                id=str(attr(item, "instance_type_id")),
                family=attr(item, "instance_type_family"),
                cpu=int(attr(item, "cpu_core_count", default=0) or 0),
                memoryGib=float(attr(item, "memory_size", default=0) or 0),
                gpu=int(attr(item, "gpuamount", "gpu_amount", default=0) or 0),
                gpuModel=attr(item, "gpuspec", "gpu_spec"),
                gpuMemoryGib=attr(item, "gpumemory_size", "gpu_memory_size"),
                architecture=attr(item, "cpu_architecture"),
                networkBandwidthRxGbps=(
                    float(attr(item, "instance_bandwidth_rx")) / 1_000_000
                    if attr(item, "instance_bandwidth_rx") is not None
                    else None
                ),
                networkBandwidthTxGbps=(
                    float(attr(item, "instance_bandwidth_tx")) / 1_000_000
                    if attr(item, "instance_bandwidth_tx") is not None
                    else None
                ),
                networkPpsRx=attr(item, "instance_pps_rx"),
                networkPpsTx=attr(item, "instance_pps_tx"),
                localStorageCount=attr(item, "local_storage_amount"),
                localStorageCapacityGib=attr(item, "local_storage_capacity"),
                localStorageCategory=attr(item, "local_storage_category"),
                zones=[filters.zone] if filters.zone else [],
                available=available.get(str(attr(item, "instance_type_id"))),
                attributes={
                    "networkEniQuantity": attr(item, "eni_quantity"),
                    "physicalProcessorModel": attr(item, "physical_processor_model"),
                },
            )
            for item in rows
            if attr(item, "instance_type_id")
        ]
        return filter_instance_types(items, filters)

    def search_images(self, filters: CatalogFilters) -> list[ImageInfo]:
        if not filters.region:
            raise CloudProviderError("region is required", code="invalid_request")
        from alibabacloud_ecs20140526 import models

        rows: list[Any] = []
        page_number = 1
        scan_limit = max(filters.limit, 1000 if filters.query else filters.limit)
        while len(rows) < scan_limit:
            page_size = min(100, scan_limit - len(rows))
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
