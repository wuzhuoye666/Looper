from __future__ import annotations

from typing import Any

from looper_api.cloud_contracts import (
    CatalogFilters,
    CloudPurchaseSpec,
    ImageInfo,
    InstanceTypeInfo,
    ProviderDestroyResult,
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
    environment_credentials,
    filter_images,
    filter_instance_types,
    image_scan_limit,
    instance_scan_limit,
    optional_environment,
    parse_datetime,
    sdk_installed,
)

_REQUIRED_ENV = ["VOLCENGINE_ACCESS_KEY", "VOLCENGINE_SECRET_KEY"]


class VolcengineEcsProvider(CloudProvider):
    id = ProviderId.VOLCENGINE
    display_name = "火山引擎 ECS"
    sdk_package = "volcengine-python-sdk"

    def info(self, *, live_purchase_enabled: bool) -> ProviderInfo:
        _, missing = environment_credentials(_REQUIRED_ENV)
        installed = sdk_installed("volcenginesdkecs")
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
                "postpaid-create-sdk",
                "client-token",
                "dry-run",
                "quote-blocked-price-mapping",
            ],
            livePurchaseEnabled=False,
            message=(
                "ECS SDK does not map launch specs to Billing configuration codes; "
                "live quotes remain blocked until account codes are configured"
            )
            if installed and not missing
            else "安装 SDK 并配置显式环境变量后可用",
        )

    def _api(self, region: str):
        try:
            from volcenginesdkcore.api_client import ApiClient
            from volcenginesdkcore.configuration import Configuration
            from volcenginesdkecs.api.ecs_api import ECSApi
        except ImportError as error:
            raise CloudProviderError(f"install {self.sdk_package}", code="sdk_missing") from error
        values, missing = environment_credentials(_REQUIRED_ENV)
        if missing:
            raise CloudProviderError(
                f"missing Volcengine credentials: {', '.join(missing)}",
                code="credentials_missing",
            )
        configuration = Configuration()
        configuration.ak = values["VOLCENGINE_ACCESS_KEY"]
        configuration.sk = values["VOLCENGINE_SECRET_KEY"]
        configuration.session_token = optional_environment("VOLCENGINE_SESSION_TOKEN") or ""
        configuration.region = region
        configuration.connect_timeout = 15.0
        configuration.read_timeout = 30.0
        configuration.auto_retry = False
        return ECSApi(ApiClient(configuration))

    def _call(self, method: str, region: str, request: Any) -> Any:
        try:
            return getattr(self._api(region), method)(request)
        except Exception as error:
            body = attr(error, "body", default={}) or {}
            provider_code = attr(body, "Code", "code", default=None)
            code = provider_code or error.__class__.__name__
            message = attr(body, "Message", "message", default=str(error))
            request_id = attr(body, "RequestId", "request_id")
            raise CloudProviderError(
                f"Volcengine {method} failed: {message}",
                code=str(code),
                retryable=str(code) in {"InternalError", "RequestLimitExceeded"},
                ambiguous=method == "run_instances"
                and ambiguous_create_error(provider_code, error),
                details={"requestId": request_id} if request_id else {},
            ) from error

    def list_regions(self) -> list[RegionInfo]:
        from volcenginesdkecs import DescribeRegionsRequest

        token: str | None = None
        rows: list[Any] = []
        while True:
            response = self._call(
                "describe_regions",
                "cn-beijing",
                DescribeRegionsRequest(max_results=100, next_token=token),
            )
            rows.extend(as_list(attr(response, "regions", default=[])))
            token = attr(response, "next_token")
            if not token:
                break
        return [
            RegionInfo(
                provider=self.id,
                id=str(attr(item, "region_id")),
                name=str(attr(item, "region_name", default=attr(item, "region_id"))),
                available=True,
            )
            for item in rows
            if attr(item, "region_id")
        ]

    def list_zones(self, region: str) -> list[ZoneInfo]:
        from volcenginesdkecs import DescribeZonesRequest

        response = self._call("describe_zones", region, DescribeZonesRequest())
        return [
            ZoneInfo(
                provider=self.id,
                region=region,
                id=str(attr(item, "zone_id")),
                name=str(attr(item, "zone_name", default=attr(item, "zone_id"))),
                available=True,
            )
            for item in as_list(attr(response, "zones", default=[]))
            if attr(item, "zone_id")
        ]

    def _availability(self, filters: CatalogFilters) -> dict[str, bool]:
        if not filters.region or not filters.zone:
            return {}
        from volcenginesdkecs import DescribeAvailableResourceRequest

        request = DescribeAvailableResourceRequest(
            destination_resource="InstanceType",
            instance_charge_type="PostPaid",
            zone_id=filters.zone,
        )
        response = self._call("describe_available_resource", filters.region, request)
        result: dict[str, bool] = {}
        for zone in as_list(attr(response, "available_zones", default=[])):
            for resource in as_list(attr(zone, "available_resources", default=[])):
                for item in as_list(attr(resource, "supported_resources", default=[])):
                    value = attr(item, "value")
                    status = str(attr(item, "status", default="")).casefold()
                    if value:
                        result[str(value)] = status in {"available", "sufficient", "normal"}
        return result

    def search_instance_types(self, filters: CatalogFilters) -> list[InstanceTypeInfo]:
        if not filters.region:
            raise CloudProviderError("region is required", code="invalid_request")
        from volcenginesdkecs import DescribeInstanceTypesRequest

        available = self._availability(filters)
        token: str | None = None
        rows: list[Any] = []
        scan_limit = instance_scan_limit(filters)
        while len(rows) < scan_limit:
            response = self._call(
                "describe_instance_types",
                filters.region,
                DescribeInstanceTypesRequest(
                    max_results=min(100, scan_limit - len(rows)), next_token=token
                ),
            )
            rows.extend(as_list(attr(response, "instance_types", default=[])))
            token = attr(response, "next_token")
            if not token:
                break
        items = []
        for item in rows:
            item_id = attr(item, "instance_type_id")
            processor = attr(item, "processor")
            memory = attr(item, "memory")
            gpu = attr(item, "gpu")
            if not item_id:
                continue
            items.append(
                InstanceTypeInfo(
                    provider=self.id,
                    region=filters.region,
                    id=str(item_id),
                    family=attr(item, "instance_type_family"),
                    cpu=int(attr(processor, "cpus", default=0) or 0),
                    memoryGib=float(attr(memory, "size", default=0) or 0),
                    gpu=int(attr(gpu, "gpus", "count", default=0) or 0),
                    architecture=attr(processor, "architecture", "model"),
                    zones=[filters.zone] if filters.zone else [],
                    available=available.get(str(item_id)),
                    attributes={
                        "processorModel": attr(processor, "model"),
                        "rawMemorySize": attr(memory, "size"),
                        "memoryUnit": "GiB per ECS product specification",
                    },
                )
            )
        return filter_instance_types(items, filters)

    def search_images(self, filters: CatalogFilters) -> list[ImageInfo]:
        if not filters.region:
            raise CloudProviderError("region is required", code="invalid_request")
        from volcenginesdkecs import DescribeImagesRequest

        token: str | None = None
        rows: list[Any] = []
        scan_limit = image_scan_limit(filters)
        while len(rows) < scan_limit:
            response = self._call(
                "describe_images",
                filters.region,
                DescribeImagesRequest(
                    image_name=filters.query,
                    instance_type_id=None,
                    max_results=min(100, scan_limit - len(rows)),
                    next_token=token,
                    os_type=filters.platform,
                    status=["available"],
                    visibility="public",
                ),
            )
            rows.extend(as_list(attr(response, "images", default=[])))
            token = attr(response, "next_token")
            if not token:
                break
        items = [
            ImageInfo(
                provider=self.id,
                region=filters.region,
                id=str(attr(item, "image_id")),
                name=str(attr(item, "image_name", default=attr(item, "image_id"))),
                platform=attr(item, "platform", "os_name"),
                architecture=attr(item, "architecture"),
                imageType=attr(item, "visibility"),
                sizeGib=float(attr(item, "size", default=0) or 0),
                createdAt=parse_datetime(attr(item, "created_at", "creation_time")),
                available=str(attr(item, "status", default="available")).casefold() == "available",
                attributes={"cloudInit": attr(item, "is_support_cloud_init")},
            )
            for item in rows
            if attr(item, "image_id")
        ]
        return filter_images(items, filters)

    def quote(self, spec: CloudPurchaseSpec) -> ProviderQuote:
        raise CloudProviderError(
            "Volcengine ECS launch specifications cannot be mapped safely to account Billing "
            "product/configuration codes; the generic Billing query exists, but its ECS mapping "
            "is undocumented, so price inquiry is disabled instead of guessed",
            code="price_mapping_required",
            details={
                "provider": self.id.value,
                "region": spec.region,
                "instanceType": spec.instance_type,
                "billingMethod": "BILLINGApi.query_price_for_pay_as_you_go",
                "requiredConfiguration": "account-validated Billing product and charge item codes",
            },
        )

    def purchase(self, spec: CloudPurchaseSpec, *, client_token: str) -> ProviderPurchaseResult:
        if spec.public_ip:
            raise CloudProviderError(
                "Volcengine RunInstances requires an existing EIP address; automatic public IP "
                "allocation is not represented by the current launch contract",
                code="public_ip_not_supported",
            )
        if not spec.security_group_ids:
            raise CloudProviderError(
                "at least one security group is required for Volcengine",
                code="invalid_request",
            )
        from volcenginesdkecs import (
            NetworkInterfaceForRunInstancesInput,
            RunInstancesRequest,
            TagForRunInstancesInput,
            VolumeForRunInstancesInput,
        )

        request = RunInstancesRequest(
            image_id=spec.image_id,
            instance_name=spec.instance_name,
            zone_id=spec.zone,
            instance_type_id=spec.instance_type,
            instance_charge_type="PostPaid",
            count=spec.count,
            min_count=spec.count,
            network_interfaces=[
                NetworkInterfaceForRunInstancesInput(
                    subnet_id=spec.subnet_id,
                    security_group_ids=spec.security_group_ids,
                )
            ],
            volumes=[
                VolumeForRunInstancesInput(
                    volume_type=spec.system_disk_type or "ESSD_PL0",
                    size=spec.system_disk_gib,
                    delete_with_instance="true",
                )
            ],
            key_pair_name=spec.key_pair_id,
            tags=[TagForRunInstancesInput(key=key, value=value) for key, value in spec.tags.items()]
            or None,
            client_token=client_token,
            dry_run=False,
        )
        response = self._call("run_instances", spec.region, request)
        instance_ids = [str(item) for item in as_list(attr(response, "instance_ids", default=[]))]
        if not instance_ids:
            raise CloudProviderError(
                "Volcengine accepted the request without returning instance ids",
                code="ambiguous_response",
                ambiguous=True,
            )
        request_id = attr(response, "request_id")
        return ProviderPurchaseResult(
            providerOrderId=request_id,
            requestId=request_id,
            instances=[
                ProvisionedInstance(
                    id=instance_id,
                    name=spec.instance_name,
                    region=spec.region,
                    zone=spec.zone,
                    status="PENDING",
                )
                for instance_id in instance_ids
            ],
            details={"requestId": request_id},
        )

    def destroy(self, *, region: str, instance_ids: list[str]) -> ProviderDestroyResult:
        raise CloudProviderError(
            "instance destroy is not supported for Volcengine yet",
            code="unsupported_operation",
        )
