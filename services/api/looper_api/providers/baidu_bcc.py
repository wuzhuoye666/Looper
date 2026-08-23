from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any

from looper_core.canonical import canonical_digest, utc_now

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
    decimal_value,
    environment_credentials,
    filter_images,
    filter_instance_types,
    optional_environment,
    parse_datetime,
    sdk_installed,
    to_plain,
)

_REQUIRED_ENV = ["BAIDU_BCE_ACCESS_KEY_ID", "BAIDU_BCE_SECRET_ACCESS_KEY"]
_DEFAULT_ENDPOINTS = {
    "bj": "bcc.bj.baidubce.com",
    "gz": "bcc.gz.baidubce.com",
    "su": "bcc.su.baidubce.com",
    "sh": "bcc.sh.baidubce.com",
    "hkg": "bcc.hkg.baidubce.com",
    "fwh": "bcc.fwh.baidubce.com",
    "global": "bcc.baidubce.com",
}


class BaiduBccProvider(CloudProvider):
    id = ProviderId.BAIDU
    display_name = "百度智能云 BCC"
    sdk_package = "bce-python-sdk"

    def info(self, *, live_purchase_enabled: bool) -> ProviderInfo:
        _, missing = environment_credentials(_REQUIRED_ENV)
        installed = sdk_installed("baidubce")
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
                "stock-unknown",
                "flavor-price-estimate",
                "postpaid-create-sdk",
                "purchase-blocked-without-complete-price",
                "client-token",
                "no-dry-run",
            ],
            livePurchaseEnabled=False,
            message=(
                "BCC SDK only exposes a flavor estimate, so purchase remains blocked until a "
                "complete launch-spec price can be verified"
                if installed and not missing
                else "安装 SDK 并配置显式环境变量后可用"
            ),
        )

    @staticmethod
    def _endpoint(region: str) -> str:
        configured = os.environ.get("BAIDU_BCC_ENDPOINTS", "").strip()
        if configured:
            try:
                mapping = json.loads(configured)
                if isinstance(mapping, dict) and region in mapping:
                    return str(mapping[region])
            except json.JSONDecodeError:
                pass
        if region in _DEFAULT_ENDPOINTS:
            return _DEFAULT_ENDPOINTS[region]
        return f"bcc.{region}.baidubce.com"

    def _client(self, region: str):
        try:
            from baidubce.auth.bce_credentials import BceCredentials
            from baidubce.bce_client_configuration import BceClientConfiguration
            from baidubce.services.bcc.bcc_client import BccClient
        except ImportError as error:
            raise CloudProviderError(f"install {self.sdk_package}", code="sdk_missing") from error
        values, missing = environment_credentials(_REQUIRED_ENV)
        if missing:
            raise CloudProviderError(
                f"missing Baidu Cloud credentials: {', '.join(missing)}",
                code="credentials_missing",
            )
        credentials = BceCredentials(
            values["BAIDU_BCE_ACCESS_KEY_ID"], values["BAIDU_BCE_SECRET_ACCESS_KEY"]
        )
        config = BceClientConfiguration(
            credentials=credentials,
            endpoint=self._endpoint(region),
            security_token=optional_environment("BAIDU_BCE_SECURITY_TOKEN"),
        )
        return BccClient(config)

    def _call(self, method: str, region: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self._client(region), method)(*args, **kwargs)
        except Exception as error:
            provider_code = attr(error, "code", default=None)
            code = provider_code or error.__class__.__name__
            message = attr(error, "message", default=str(error))
            request_id = attr(error, "request_id", "requestId")
            raise CloudProviderError(
                f"Baidu BCC {method} failed: {message}",
                code=str(code),
                retryable=str(code) in {"REQUEST_LIMIT_EXCEEDED", "InternalError"},
                ambiguous=method == "create_instance_by_spec"
                and ambiguous_create_error(provider_code, error),
                details={"requestId": request_id} if request_id else {},
            ) from error

    def list_regions(self) -> list[RegionInfo]:
        configured = os.environ.get("BAIDU_BCC_REGIONS", "").strip()
        if configured:
            try:
                rows = json.loads(configured)
                if isinstance(rows, list):
                    return [
                        RegionInfo(
                            provider=self.id,
                            id=str(item.get("id", item)) if isinstance(item, dict) else str(item),
                            name=str(
                                item.get("name", item.get("id")) if isinstance(item, dict) else item
                            ),
                            endpoint=(item.get("endpoint") if isinstance(item, dict) else None),
                            available=None,
                        )
                        for item in rows
                    ]
            except json.JSONDecodeError:
                pass
        return [
            RegionInfo(provider=self.id, id=region, name=region, endpoint=endpoint, available=None)
            for region, endpoint in _DEFAULT_ENDPOINTS.items()
            if region != "global"
        ]

    def list_zones(self, region: str) -> list[ZoneInfo]:
        response = self._call("list_zones", region)
        rows = attr(response, "zones", "zone", default=[])
        return [
            ZoneInfo(
                provider=self.id,
                region=region,
                id=str(attr(item, "name", "zone_name", "zoneName")),
                name=str(attr(item, "name", "zone_name", "zoneName")),
                available=True,
            )
            for item in as_list(rows)
            if attr(item, "name", "zone_name", "zoneName")
        ]

    def search_instance_types(self, filters: CatalogFilters) -> list[InstanceTypeInfo]:
        if not filters.region:
            raise CloudProviderError("region is required", code="invalid_request")
        if not filters.zone:
            raise CloudProviderError(
                "zone is required for Baidu flavor search", code="invalid_request"
            )
        response = self._call("list_flavor_spec", filters.region, zone_name=filters.zone)
        rows = attr(response, "flavor_specs", "flavorSpec", "specs", default=[])
        items: list[InstanceTypeInfo] = []
        for item in as_list(rows):
            item_id = attr(item, "spec", "spec_id", "specId", "name")
            if not item_id:
                continue
            cpu = int(attr(item, "cpu_count", "cpuCount", "cpu", default=0) or 0)
            memory = float(
                attr(item, "memory_capacity_in_gb", "memoryCapacityInGb", "memory", default=0) or 0
            )
            items.append(
                InstanceTypeInfo(
                    provider=self.id,
                    region=filters.region,
                    id=str(item_id),
                    family=attr(item, "instance_type", "instanceType"),
                    cpu=cpu,
                    memoryGib=memory,
                    zones=[filters.zone],
                    available=None,
                    attributes={"raw": to_plain(item)},
                )
            )
        return filter_instance_types(items, filters)

    def search_images(self, filters: CatalogFilters) -> list[ImageInfo]:
        if not filters.region:
            raise CloudProviderError("region is required", code="invalid_request")
        marker: str | None = None
        rows: list[Any] = []
        seen_markers: set[str] = set()
        while True:
            response = self._call(
                "list_images",
                filters.region,
                image_type=filters.image_type or "System",
                marker=marker,
                max_keys=100,
                image_name=filters.query,
            )
            batch = as_list(attr(response, "images", "image", default=[]))
            rows.extend(batch)
            marker = attr(response, "next_marker", "nextMarker")
            if not marker or not batch:
                break
            if marker in seen_markers:
                raise CloudProviderError(
                    "Baidu image pagination repeated a marker",
                    code="pagination_stalled",
                )
            seen_markers.add(marker)
        items = [
            ImageInfo(
                provider=self.id,
                region=filters.region,
                id=str(attr(item, "id", "image_id", "imageId")),
                name=str(attr(item, "name", "image_name", "imageName", default="")),
                platform=attr(item, "os_name", "osName", "platform"),
                architecture=attr(item, "architecture"),
                imageType=attr(item, "image_type", "imageType"),
                sizeGib=float(attr(item, "size_in_gb", "sizeInGb", "size", default=0) or 0),
                createdAt=parse_datetime(attr(item, "create_time", "created_at")),
                available=str(attr(item, "status", default="available")).casefold()
                in {"available", "normal"},
                attributes={"raw": to_plain(item)},
            )
            for item in as_list(rows)
            if attr(item, "id", "image_id", "imageId")
        ]
        return filter_images(items, filters)

    def quote(self, spec: CloudPurchaseSpec) -> ProviderQuote:
        if spec.public_ip:
            raise CloudProviderError(
                "Baidu BCC flavor pricing does not include metered public traffic; "
                "public IP purchase is blocked instead of presenting an incomplete hourly quote",
                code="public_ip_price_not_supported",
            )
        response = self._call(
            "get_price_by_spec",
            spec.region,
            spec=spec.instance_type,
            payment_timing="postpay",
            zone_name=spec.zone,
            purchase_num=spec.count,
            client_token=canonical_digest(spec.model_dump(mode="json")).removeprefix("sha256:")[
                :63
            ],
        )
        price = attr(response, "price", "price_detail", "priceDetail", default=response)
        amount = decimal_value(
            attr(price, "price", "trade_price", "amount", "unit_price", default=0)
        )
        if amount <= 0:
            raise CloudProviderError("Baidu BCC quote did not include an hourly price")
        return ProviderQuote(
            providerQuoteId=attr(response, "request_id", "requestId"),
            amount=amount,
            currency=str(attr(price, "currency", default="CNY")),
            estimated=True,
            expiresAt=utc_now() + timedelta(minutes=5),
            details={
                "requestId": attr(response, "request_id", "requestId"),
                "stock": "unknown",
                "priceScope": "instance-flavor",
                "warning": (
                    "The BCC SDK flavor quote excludes parts of the launch spec and cannot "
                    "authorize a Looper purchase"
                ),
            },
        )

    def purchase(self, spec: CloudPurchaseSpec, *, client_token: str) -> ProviderPurchaseResult:
        if spec.public_ip:
            raise CloudProviderError(
                "Baidu BCC public IP purchase requires a separately priced EIP workflow",
                code="public_ip_price_not_supported",
            )
        if not spec.security_group_ids:
            raise CloudProviderError(
                "at least one security group is required for Baidu BCC",
                code="invalid_request",
            )
        try:
            from baidubce.services.bcc import bcc_model
        except ImportError as error:
            raise CloudProviderError(f"install {self.sdk_package}", code="sdk_missing") from error
        billing = bcc_model.Billing("Postpaid")
        tags = [bcc_model.TagModel(tagKey=key, tagValue=value) for key, value in spec.tags.items()]
        response = self._call(
            "create_instance_by_spec",
            spec.region,
            spec=spec.instance_type,
            image_id=spec.image_id,
            billing=billing,
            root_disk_size_in_gb=spec.system_disk_gib,
            root_disk_storage_type=spec.system_disk_type,
            purchase_count=spec.count,
            name=spec.instance_name,
            zone_name=spec.zone,
            subnet_id=spec.subnet_id,
            security_group_ids=spec.security_group_ids,
            key_pair_id=spec.key_pair_id,
            tags=tags or None,
            client_token=client_token,
        )
        ids = attr(response, "instance_ids", "instanceIds", "instances", default=[])
        if not ids:
            raise CloudProviderError(
                "Baidu BCC accepted the request without returning instance ids",
                code="ambiguous_response",
                ambiguous=True,
            )
        request_id = attr(response, "request_id", "requestId")
        return ProviderPurchaseResult(
            providerOrderId=request_id,
            requestId=request_id,
            instances=[
                ProvisionedInstance(
                    id=str(item),
                    name=spec.instance_name,
                    region=spec.region,
                    zone=spec.zone,
                    status="PENDING",
                )
                for item in as_list(ids)
            ],
            details={"requestId": request_id},
        )

    def destroy(self, *, region: str, instance_ids: list[str]) -> ProviderDestroyResult:
        raise CloudProviderError(
            "instance destroy is not supported for Baidu BCC yet",
            code="unsupported_operation",
        )
