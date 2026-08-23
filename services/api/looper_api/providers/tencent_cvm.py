from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from looper_core.canonical import canonical_digest, canonical_json, utc_now
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
from looper_api.models import TargetRecord
from looper_api.providers.base import CloudProvider, CloudProviderError
from looper_api.providers.utils import (
    ambiguous_create_error,
    attr,
    cloud_target_id,
    decimal_value,
    environment_credentials,
    filter_images,
    filter_instance_types,
    legacy_cloud_target_ids,
    optional_environment,
    parse_datetime,
    sdk_installed,
    to_plain,
)


class TencentInventoryError(CloudProviderError):
    pass


_REQUIRED_ENV = ["TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"]


def _credentials() -> tuple[str, str, str | None]:
    values, missing = environment_credentials(_REQUIRED_ENV)
    if missing:
        raise TencentInventoryError(
            f"missing Tencent Cloud credentials: {', '.join(missing)}",
            code="credentials_missing",
        )
    return (
        values["TENCENTCLOUD_SECRET_ID"],
        values["TENCENTCLOUD_SECRET_KEY"],
        optional_environment("TENCENTCLOUD_SESSION_TOKEN"),
    )


def _tag_map(tags: Any) -> dict[str, str]:
    return {
        str(attr(tag, "Key")): str(attr(tag, "Value", default="") or "")
        for tag in list(tags or [])
        if attr(tag, "Key")
    }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _tencent_architecture(instance_type: str, cpu_type: Any) -> str:
    family = instance_type.split(".", 1)[0].upper()
    normalized_cpu = str(cpu_type or "").casefold().replace("-", "")
    if family.startswith("SR") or "arm" in normalized_cpu:
        return "ARM"
    return "X86"


def _local_disk_details(item: Any) -> tuple[str | None, float | None, list[dict[str, Any]]]:
    details: list[dict[str, Any]] = []
    for disk in list(attr(item, "LocalDiskTypeList", default=[]) or []):
        disk_type = str(attr(disk, "Type", default="") or "")
        minimum = _number(attr(disk, "MinSize"))
        maximum = _number(attr(disk, "MaxSize"))
        details.append(
            {
                "type": disk_type,
                "minSizeGib": minimum,
                "maxSizeGib": maximum,
                "required": str(attr(disk, "Required", default="") or "").upper()
                == "REQUIRED",
            }
        )
    categories = sorted({str(detail["type"]) for detail in details if detail["type"]})
    capacities = [float(detail["maxSizeGib"]) for detail in details if detail["maxSizeGib"]]
    return ", ".join(categories) or None, max(capacities, default=None), details


def _quota_capability(item: Any) -> dict[str, Any]:
    status = str(attr(item, "Status", default="") or "").upper()
    disk_category, disk_capacity, disk_details = _local_disk_details(item)
    gpu_count = _number(attr(item, "GpuCount"))
    if gpu_count is None:
        gpu_count = _number(attr(item, "Gpu")) or 0
    return {
        "zone": str(attr(item, "Zone", default="") or ""),
        "status": status or None,
        "available": True if status == "SELL" else False if status == "SOLD_OUT" else None,
        "statusCategory": attr(item, "StatusCategory"),
        "soldOutReason": attr(item, "SoldOutReason"),
        "gpu": gpu_count,
        "networkBandwidthGbps": _number(attr(item, "InstanceBandwidth")),
        "networkPps": (
            (_number(attr(item, "InstancePps")) or 0) * 10_000
            if attr(item, "InstancePps") is not None
            else None
        ),
        "localStorageCategory": disk_category,
        "localStorageCapacityGib": disk_capacity,
        "localStorageCount": int(attr(item, "StorageBlockAmount", default=0) or 0),
        "localDiskTypes": disk_details,
    }


def _maximum(capabilities: list[dict[str, Any]], key: str) -> float | None:
    values = [float(value) for item in capabilities if (value := item.get(key)) is not None]
    return max(values, default=None)


class TencentCvmProvider(CloudProvider):
    id = ProviderId.TENCENT
    display_name = "腾讯云 CVM"
    sdk_package = "tencentcloud-sdk-python-cvm + tencentcloud-sdk-python-vpc"

    def info(self, *, live_purchase_enabled: bool) -> ProviderInfo:
        _, missing = environment_credentials(_REQUIRED_ENV)
        installed = sdk_installed("tencentcloud.cvm") and sdk_installed("tencentcloud.vpc")
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
                "vpcs",
                "subnets",
                "managed-subnet",
                "security-groups",
                "key-pairs",
                "managed-security-group",
                "hourly-quote",
                "postpaid-purchase",
                "client-token",
                "inventory",
            ],
            livePurchaseEnabled=live_purchase_enabled and installed and not missing,
            message=None if installed and not missing else "安装 SDK 并配置显式环境变量后可用",
        )

    def _client(self, region: str):
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.cvm.v20170312 import cvm_client
        except ImportError as error:
            raise CloudProviderError(f"install {self.sdk_package}", code="sdk_missing") from error
        secret_id, secret_key, token = _credentials()
        cred = credential.Credential(secret_id, secret_key, token)
        http_profile = HttpProfile()
        http_profile.endpoint = "cvm.tencentcloudapi.com"
        profile = ClientProfile(httpProfile=http_profile)
        return cvm_client.CvmClient(cred, region, profile)

    def _vpc_client(self, region: str):
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.vpc.v20170312 import vpc_client
        except ImportError as error:
            raise CloudProviderError(f"install {self.sdk_package}", code="sdk_missing") from error
        secret_id, secret_key, token = _credentials()
        cred = credential.Credential(secret_id, secret_key, token)
        http_profile = HttpProfile()
        http_profile.endpoint = "vpc.tencentcloudapi.com"
        profile = ClientProfile(httpProfile=http_profile)
        return vpc_client.VpcClient(cred, region, profile)

    def _call(self, method: str, region: str, request: Any) -> Any:
        client = self._client(region)
        try:
            return getattr(client, method)(request)
        except Exception as error:
            provider_code = attr(error, "code", default=None)
            code = provider_code or error.__class__.__name__
            message = attr(error, "message", default=str(error))
            request_id = attr(error, "request_id", "requestId")
            raise CloudProviderError(
                f"Tencent Cloud {method} failed: {message}",
                code=str(code),
                retryable=str(code) in {"RequestLimitExceeded", "InternalError"},
                ambiguous=method == "RunInstances" and ambiguous_create_error(provider_code, error),
                details={"requestId": request_id} if request_id else {},
            ) from error

    def _vpc_call(self, method: str, region: str, request: Any) -> Any:
        client = self._vpc_client(region)
        try:
            return getattr(client, method)(request)
        except Exception as error:
            provider_code = attr(error, "code", default=None)
            code = provider_code or error.__class__.__name__
            message = attr(error, "message", default=str(error))
            request_id = attr(error, "request_id", "requestId")
            raise CloudProviderError(
                f"Tencent Cloud VPC {method} failed: {message}",
                code=str(code),
                retryable=str(code) in {"RequestLimitExceeded", "InternalError"},
                details={"requestId": request_id} if request_id else {},
            ) from error

    def list_regions(self) -> list[RegionInfo]:
        from tencentcloud.cvm.v20170312 import models

        response = self._call("DescribeRegions", "ap-guangzhou", models.DescribeRegionsRequest())
        return [
            RegionInfo(
                provider=self.id,
                id=str(item.Region),
                name=str(item.RegionName or item.Region),
                available=str(item.RegionState or "").upper() == "AVAILABLE",
            )
            for item in list(response.RegionSet or [])
        ]

    def list_zones(self, region: str) -> list[ZoneInfo]:
        from tencentcloud.cvm.v20170312 import models

        response = self._call("DescribeZones", region, models.DescribeZonesRequest())
        return [
            ZoneInfo(
                provider=self.id,
                region=region,
                id=str(item.Zone),
                name=str(item.ZoneName or item.Zone),
                available=str(item.ZoneState or "").upper() == "AVAILABLE",
            )
            for item in list(response.ZoneSet or [])
        ]

    def list_vpcs(self, region: str) -> list[VpcInfo]:
        from tencentcloud.vpc.v20170312 import models

        items: list[VpcInfo] = []
        offset = 0
        while True:
            request = models.DescribeVpcsRequest()
            request.from_json_string(canonical_json({"Offset": str(offset), "Limit": "100"}))
            response = self._vpc_call("DescribeVpcs", region, request)
            rows = list(response.VpcSet or [])
            items.extend(
                VpcInfo(
                    provider=self.id,
                    region=region,
                    id=str(item.VpcId),
                    name=str(item.VpcName or item.VpcId),
                    cidrBlock=attr(item, "CidrBlock"),
                    isDefault=bool(attr(item, "IsDefault", default=False)),
                )
                for item in rows
            )
            offset += len(rows)
            total = attr(response, "TotalCount")
            if not rows or len(rows) < 100 or (total is not None and offset >= int(total)):
                break
        return items

    def list_subnets(self, region: str, zone: str, vpc_id: str) -> list[SubnetInfo]:
        return self._list_subnets(region, vpc_id=vpc_id, zone=zone)

    def list_vpc_subnets(self, region: str, vpc_id: str) -> list[SubnetInfo]:
        return self._list_subnets(region, vpc_id=vpc_id, zone=None)

    def _list_subnets(
        self, region: str, *, vpc_id: str, zone: str | None
    ) -> list[SubnetInfo]:
        from tencentcloud.vpc.v20170312 import models

        filters = [{"Name": "vpc-id", "Values": [vpc_id]}]
        if zone:
            filters.append({"Name": "zone", "Values": [zone]})
        items: list[SubnetInfo] = []
        offset = 0
        while True:
            request = models.DescribeSubnetsRequest()
            request.from_json_string(
                canonical_json(
                    {"Filters": filters, "Offset": str(offset), "Limit": "100"}
                )
            )
            response = self._vpc_call("DescribeSubnets", region, request)
            rows = list(response.SubnetSet or [])
            for item in rows:
                tags = _tag_map(attr(item, "TagSet", default=[]))
                items.append(SubnetInfo(
                    provider=self.id,
                    region=region,
                    zone=str(item.Zone),
                    vpcId=str(item.VpcId),
                    id=str(item.SubnetId),
                    name=str(item.SubnetName or item.SubnetId),
                    cidrBlock=attr(item, "CidrBlock"),
                    availableIpCount=attr(item, "AvailableIpAddressCount"),
                    isDefault=bool(attr(item, "IsDefault", default=False)),
                    tags=tags,
                    managed=tags.get("managedBy", "").casefold() == "looper",
                ))
            offset += len(rows)
            total = attr(response, "TotalCount")
            if not rows or len(rows) < 100 or (total is not None and offset >= int(total)):
                break
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
        del client_token
        from tencentcloud.vpc.v20170312 import models

        request = models.CreateSubnetRequest()
        request.from_json_string(
            canonical_json(
                {
                    "VpcId": vpc_id,
                    "SubnetName": name,
                    "CidrBlock": cidr_block,
                    "Zone": zone,
                    "Tags": [
                        {"Key": "managedBy", "Value": "looper"},
                        {"Key": "purpose", "Value": "cloud-purchase"},
                    ],
                }
            )
        )
        response = self._vpc_call("CreateSubnet", region, request)
        item = response.Subnet
        if item is None or not attr(item, "SubnetId"):
            raise CloudProviderError(
                "Tencent Cloud created a subnet without returning its id",
                code="ambiguous_response",
                ambiguous=True,
            )
        tags = _tag_map(attr(item, "TagSet", default=[]))
        tags.setdefault("managedBy", "looper")
        tags.setdefault("purpose", "cloud-purchase")
        return SubnetInfo(
            provider=self.id,
            region=region,
            zone=str(attr(item, "Zone", default=zone)),
            vpcId=str(attr(item, "VpcId", default=vpc_id)),
            id=str(item.SubnetId),
            name=str(attr(item, "SubnetName", default=name) or name),
            cidrBlock=attr(item, "CidrBlock", default=cidr_block),
            availableIpCount=attr(item, "AvailableIpAddressCount"),
            isDefault=bool(attr(item, "IsDefault", default=False)),
            tags=tags,
            managed=True,
        )

    def list_security_groups(self, region: str) -> list[SecurityGroupInfo]:
        from tencentcloud.vpc.v20170312 import models

        items: list[SecurityGroupInfo] = []
        offset = 0
        while True:
            request = models.DescribeSecurityGroupsRequest()
            request.from_json_string(canonical_json({"Offset": str(offset), "Limit": "100"}))
            response = self._vpc_call("DescribeSecurityGroups", region, request)
            rows = list(response.SecurityGroupSet or [])
            for item in rows:
                tags = _tag_map(attr(item, "TagSet", default=[]))
                name = str(item.SecurityGroupName or item.SecurityGroupId)
                recommended = (
                    tags.get("managedBy", "").lower() == "looper"
                    or name.lower().startswith("looper")
                )
                items.append(
                    SecurityGroupInfo(
                        provider=self.id,
                        region=region,
                        id=str(item.SecurityGroupId),
                        name=name,
                        description=attr(item, "SecurityGroupDesc"),
                        isDefault=bool(attr(item, "IsDefault", default=False)),
                        recommended=recommended,
                        tags=tags,
                    )
                )
            offset += len(rows)
            total = attr(response, "TotalCount")
            if not rows or len(rows) < 100 or (total is not None and offset >= int(total)):
                break
        return items

    def list_key_pairs(self, region: str) -> list[KeyPairInfo]:
        from tencentcloud.cvm.v20170312 import models

        items: list[KeyPairInfo] = []
        offset = 0
        while True:
            request = models.DescribeKeyPairsRequest()
            request.from_json_string(canonical_json({"Offset": offset, "Limit": 100}))
            response = self._call("DescribeKeyPairs", region, request)
            rows = list(response.KeyPairSet or [])
            items.extend(
                KeyPairInfo(
                    provider=self.id,
                    region=region,
                    id=str(item.KeyId),
                    name=str(item.KeyName or item.KeyId),
                    description=attr(item, "Description"),
                    createdAt=parse_datetime(attr(item, "CreatedTime")),
                    associatedInstanceCount=len(
                        list(attr(item, "AssociatedInstanceIds", default=[]) or [])
                    ),
                )
                for item in rows
            )
            offset += len(rows)
            total = attr(response, "TotalCount")
            if not rows or len(rows) < 100 or (total is not None and offset >= int(total)):
                break
        return items

    def ensure_managed_security_group(self, region: str) -> SecurityGroupInfo:
        groups = self.list_security_groups(region)
        recommended = [item for item in groups if item.recommended]
        if recommended:
            return sorted(
                recommended,
                key=lambda item: (item.name != "looper-private-outbound", item.id),
            )[0]

        from tencentcloud.vpc.v20170312 import models

        request = models.CreateSecurityGroupWithPoliciesRequest()
        request.from_json_string(
            canonical_json(
                {
                    "GroupName": "looper-private-outbound",
                    "GroupDescription": "Looper managed: no ingress, allow IPv4 egress",
                    "SecurityGroupPolicySet": {
                        "Ingress": [],
                        "Egress": [
                            {
                                "Protocol": "ALL",
                                "Port": "all",
                                "CidrBlock": "0.0.0.0/0",
                                "Action": "ACCEPT",
                                "PolicyDescription": "Looper managed outbound IPv4",
                            }
                        ],
                    },
                    "Tags": [{"Key": "managedBy", "Value": "looper"}],
                }
            )
        )
        response = self._vpc_call("CreateSecurityGroupWithPolicies", region, request)
        item = response.SecurityGroup
        tags = _tag_map(attr(item, "TagSet", default=[]))
        tags.setdefault("managedBy", "looper")
        return SecurityGroupInfo(
            provider=self.id,
            region=region,
            id=str(item.SecurityGroupId),
            name=str(item.SecurityGroupName or item.SecurityGroupId),
            description=attr(item, "SecurityGroupDesc"),
            isDefault=bool(attr(item, "IsDefault", default=False)),
            recommended=True,
            tags=tags,
        )

    def search_instance_types(self, filters: CatalogFilters) -> list[InstanceTypeInfo]:
        if not filters.region:
            raise CloudProviderError("region is required", code="invalid_request")
        from tencentcloud.cvm.v20170312 import models

        provider_filters = [
            {"Name": "instance-charge-type", "Values": ["POSTPAID_BY_HOUR"]}
        ]
        if filters.zone:
            provider_filters.append({"Name": "zone", "Values": [filters.zone]})
        request = models.DescribeZoneInstanceConfigInfosRequest()
        request.from_json_string(canonical_json({"Filters": provider_filters}))
        response = self._call("DescribeZoneInstanceConfigInfos", filters.region, request)
        rows = list(attr(response, "InstanceTypeQuotaSet", default=[]) or [])

        grouped: dict[str, list[Any]] = {}
        for item in rows:
            instance_type = str(attr(item, "InstanceType", default="") or "")
            if instance_type:
                grouped.setdefault(instance_type, []).append(item)

        items: list[InstanceTypeInfo] = []
        for instance_type, quota_items in grouped.items():
            representative = quota_items[0]
            capabilities = [_quota_capability(item) for item in quota_items]
            available_capabilities = [
                capability for capability in capabilities if capability["available"] is True
            ]
            metric_capabilities = available_capabilities or capabilities
            statuses = {capability["available"] for capability in capabilities}
            available = True if True in statuses else False if statuses == {False} else None
            gpu = _maximum(metric_capabilities, "gpu") or 0
            bandwidth = _maximum(metric_capabilities, "networkBandwidthGbps")
            pps = _maximum(metric_capabilities, "networkPps")
            disk_categories = sorted(
                {
                    str(capability["localStorageCategory"])
                    for capability in metric_capabilities
                    if capability.get("localStorageCategory")
                }
            )
            gpu_model = attr(representative, "GpuType", "GPUType", "GpuModel")
            items.append(
                InstanceTypeInfo(
                    provider=self.id,
                    region=filters.region,
                    id=instance_type,
                    family=attr(representative, "InstanceFamily"),
                    cpu=int(attr(representative, "Cpu", "CPU", default=0) or 0),
                    memoryGib=float(attr(representative, "Memory", default=0) or 0),
                    gpu=gpu,
                    gpuModel=str(gpu_model) if gpu_model else None,
                    architecture=_tencent_architecture(
                        instance_type, attr(representative, "CpuType", "CPUType")
                    ),
                    networkBandwidthRxGbps=bandwidth,
                    networkBandwidthTxGbps=bandwidth,
                    networkPpsRx=int(pps) if pps is not None else None,
                    networkPpsTx=int(pps) if pps is not None else None,
                    localStorageCapacityGib=_maximum(
                        metric_capabilities, "localStorageCapacityGib"
                    ),
                    localStorageCount=(
                        int(_maximum(metric_capabilities, "localStorageCount") or 0) or None
                    ),
                    localStorageCategory=", ".join(disk_categories) or None,
                    zones=sorted(
                        {
                            str(capability["zone"])
                            for capability in capabilities
                            if capability.get("zone")
                        }
                    ),
                    available=available,
                    attributes={
                        "typeName": attr(representative, "TypeName"),
                        "cpuType": attr(representative, "CpuType", "CPUType"),
                        "fpga": int(attr(representative, "Fpga", "FPGA", default=0) or 0),
                        "gpuCores": _number(attr(representative, "Gpu", "GPU")) or 0,
                        "zoneCapabilities": capabilities,
                    },
                )
            )
        return filter_instance_types(items, filters)

    def search_images(self, filters: CatalogFilters) -> list[ImageInfo]:
        if not filters.region:
            raise CloudProviderError("region is required", code="invalid_request")
        from tencentcloud.cvm.v20170312 import models

        provider_filters = [
            {
                "Name": "image-type",
                "Values": [filters.image_type or "PUBLIC_IMAGE"],
            }
        ]
        if filters.query:
            provider_filters.append({"Name": "image-name", "Values": [filters.query]})
        if filters.platform:
            provider_filters.append({"Name": "platform", "Values": [filters.platform]})
        items: list[ImageInfo] = []
        offset = 0
        page_size = 100
        while True:
            request = models.DescribeImagesRequest()
            payload: dict[str, Any] = {
                "Filters": provider_filters,
                "Offset": offset,
                "Limit": page_size,
            }
            if filters.instance_type:
                payload["InstanceType"] = filters.instance_type
            request.from_json_string(canonical_json(payload))
            response = self._call("DescribeImages", filters.region, request)
            rows = list(response.ImageSet or [])
            items.extend(
                ImageInfo(
                    provider=self.id,
                    region=filters.region,
                    id=str(item.ImageId),
                    name=str(item.ImageName or item.OsName or item.ImageId),
                    platform=attr(item, "Platform", "OsName"),
                    architecture=attr(item, "Architecture"),
                    imageType=attr(item, "ImageType"),
                    sizeGib=float(attr(item, "ImageSize", default=0) or 0),
                    createdAt=parse_datetime(attr(item, "CreatedTime")),
                    available=str(attr(item, "ImageState", default="NORMAL")).upper()
                    in {"NORMAL", "AVAILABLE"},
                )
                for item in rows
            )
            offset += len(rows)
            total = attr(response, "TotalCount")
            if not rows or len(rows) < page_size or (total is not None and offset >= int(total)):
                break
        return filter_images(items, filters)

    def _run_payload(self, spec: CloudPurchaseSpec) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "Placement": {"Zone": spec.zone},
            "ImageId": spec.image_id,
            "InstanceType": spec.instance_type,
            "InstanceChargeType": "POSTPAID_BY_HOUR",
            "InstanceCount": spec.count,
            "SystemDisk": {
                "DiskType": spec.system_disk_type or "CLOUD_PREMIUM",
                "DiskSize": spec.system_disk_gib,
            },
            "VirtualPrivateCloud": {"VpcId": spec.vpc_id, "SubnetId": spec.subnet_id},
            "SecurityGroupIds": spec.security_group_ids,
            "InternetAccessible": {
                "InternetChargeType": "BANDWIDTH_POSTPAID_BY_HOUR",
                "InternetMaxBandwidthOut": spec.internet_bandwidth_mbps if spec.public_ip else 0,
                "PublicIpAssigned": spec.public_ip,
            },
        }
        if spec.key_pair_id:
            payload["LoginSettings"] = {"KeyIds": [spec.key_pair_id]}
        if spec.tags:
            payload["TagSpecification"] = [
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": key, "Value": value} for key, value in spec.tags.items()],
                }
            ]
        return payload

    @staticmethod
    def _hourly_amount(price: Any, label: str) -> Decimal:
        if price is None:
            return decimal_value(0)
        charge_unit = attr(price, "ChargeUnit")
        if charge_unit and str(charge_unit).upper() != "HOUR":
            raise CloudProviderError(
                f"Tencent Cloud {label} price uses {charge_unit}, not an hourly charge",
                code="non_hourly_price",
            )
        value = attr(price, "UnitPriceDiscount")
        if value is None:
            value = attr(price, "UnitPrice")
        return decimal_value(value, default=decimal_value(0))

    def quote(self, spec: CloudPurchaseSpec) -> ProviderQuote:
        from tencentcloud.cvm.v20170312 import models

        request = models.InquiryPriceRunInstancesRequest()
        request.from_json_string(canonical_json(self._run_payload(spec)))
        response = self._call("InquiryPriceRunInstances", spec.region, request)
        price = response.Price
        instance_price = attr(price, "InstancePrice")
        bandwidth_price = attr(price, "BandwidthPrice")
        instance_amount = self._hourly_amount(instance_price, "instance")
        bandwidth_amount = self._hourly_amount(bandwidth_price, "bandwidth")
        amount = instance_amount + bandwidth_amount
        if amount <= 0:
            raise CloudProviderError("Tencent Cloud quote did not include an hourly price")
        return ProviderQuote(
            providerQuoteId=attr(response, "RequestId"),
            amount=amount,
            currency="CNY",
            estimated=False,
            expiresAt=utc_now() + timedelta(minutes=5),
            details={
                "requestId": attr(response, "RequestId"),
                "instancePrice": to_plain(instance_price),
                "bandwidthPrice": to_plain(bandwidth_price),
            },
        )

    def purchase(self, spec: CloudPurchaseSpec, *, client_token: str) -> ProviderPurchaseResult:
        from tencentcloud.cvm.v20170312 import models

        payload = self._run_payload(spec)
        payload.update({"ClientToken": client_token, "InstanceName": spec.instance_name})
        request = models.RunInstancesRequest()
        request.from_json_string(canonical_json(payload))
        response = self._call("RunInstances", spec.region, request)
        instance_ids = [str(item) for item in list(response.InstanceIdSet or [])]
        if not instance_ids:
            raise CloudProviderError(
                "Tencent Cloud accepted the request without returning instance ids",
                code="ambiguous_response",
                ambiguous=True,
            )
        details = {"requestId": attr(response, "RequestId")}
        provisioned = [
            ProvisionedInstance(
                id=instance_id,
                name=spec.instance_name,
                region=spec.region,
                zone=spec.zone,
                status="PENDING",
            )
            for instance_id in instance_ids
        ]
        try:
            describe_request = models.DescribeInstancesRequest()
            describe_request.from_json_string(canonical_json({"InstanceIds": instance_ids}))
            described = self._call("DescribeInstances", spec.region, describe_request)
            by_id = {str(item.InstanceId): item for item in list(described.InstanceSet or [])}
            provisioned = [
                _provisioned_instance(spec.region, by_id[instance_id], spec.instance_name)
                if instance_id in by_id
                else item
                for instance_id, item in zip(instance_ids, provisioned, strict=True)
            ]
        except Exception as error:
            details["inventoryWarning"] = f"post-create inventory pending: {error}"
        return ProviderPurchaseResult(
            providerOrderId=attr(response, "RequestId"),
            requestId=attr(response, "RequestId"),
            instances=provisioned,
            details=details,
        )

    def destroy(self, *, region: str, instance_ids: list[str]) -> ProviderDestroyResult:
        from tencentcloud.cvm.v20170312 import models

        normalized = [value.strip() for value in instance_ids if value and value.strip()]
        if not normalized or len(normalized) != len(set(normalized)):
            raise CloudProviderError(
                "instance ids must be non-empty and unique", code="invalid_request"
            )
        request = models.TerminateInstancesRequest()
        request.from_json_string(canonical_json({"InstanceIds": normalized}))
        response = self._call("TerminateInstances", region, request)
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
                    kind="local-disk",
                    id=f"{instance_id}:local-disk",
                    note="挂载的本地盘（含机械盘/SSD）随实例一并释放",
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
            request_id=attr(response, "RequestId"),
            instance_ids=normalized,
            released_resources=released,
            details={"requestId": attr(response, "RequestId")},
        )

    def cleanup_managed_network(
        self,
        *,
        region: str,
        vpc_id: str | None,
        subnet_id: str | None,
        security_group_ids: list[str],
    ) -> list[DestroyedResource]:
        from tencentcloud.vpc.v20170312 import models

        released: list[DestroyedResource] = []
        if subnet_id and vpc_id:
            released.append(self._delete_managed_subnet(region, vpc_id, subnet_id, models))
        for security_group_id in security_group_ids:
            if security_group_id:
                released.append(
                    self._delete_managed_security_group(region, security_group_id, models)
                )
        return released

    @staticmethod
    def _delete_managed_subnet(
        region: str, vpc_id: str, subnet_id: str, models: Any
    ) -> DestroyedResource:
        provider = TencentCvmProvider()
        try:
            describe = models.DescribeSubnetsRequest()
            describe.from_json_string(
                canonical_json({"Filters": [{"Name": "subnet-id", "Values": [subnet_id]}]})
            )
            response = provider._vpc_call("DescribeSubnets", region, describe)
            rows = list(response.SubnetSet or [])
            tags = _tag_map(attr(rows[0], "TagSet", default=[])) if rows else {}
            if tags.get("managedBy", "").casefold() != "looper":
                return DestroyedResource(
                    kind="subnet",
                    id=subnet_id,
                    released=False,
                    note="非 Looper 纳管子网，保留不动",
                )
            delete = models.DeleteSubnetRequest()
            delete.from_json_string(
                canonical_json({"SubnetId": subnet_id, "VpcId": vpc_id})
            )
            provider._vpc_call("DeleteSubnet", region, delete)
            return DestroyedResource(kind="subnet", id=subnet_id, note="Looper 纳管子网已删除")
        except CloudProviderError as error:
            return DestroyedResource(
                kind="subnet",
                id=subnet_id,
                released=False,
                note=f"子网清理暂缓：{error}",
            )

    @staticmethod
    def _delete_managed_security_group(
        region: str, security_group_id: str, models: Any
    ) -> DestroyedResource:
        provider = TencentCvmProvider()
        try:
            describe = models.DescribeSecurityGroupsRequest()
            describe.from_json_string(
                canonical_json(
                    {"Filters": [{"Name": "security-group-id", "Values": [security_group_id]}]}
                )
            )
            response = provider._vpc_call("DescribeSecurityGroups", region, describe)
            rows = list(response.SecurityGroupSet or [])
            name = str(attr(rows[0], "SecurityGroupName", default="") or "") if rows else ""
            tags = _tag_map(attr(rows[0], "TagSet", default=[])) if rows else {}
            if tags.get("managedBy", "").casefold() != "looper" and not name.casefold().startswith(
                "looper"
            ):
                return DestroyedResource(
                    kind="security-group",
                    id=security_group_id,
                    released=False,
                    note="非 Looper 纳管安全组，保留不动",
                )
            delete = models.DeleteSecurityGroupRequest()
            delete.from_json_string(canonical_json({"SecurityGroupId": security_group_id}))
            provider._vpc_call("DeleteSecurityGroup", region, delete)
            return DestroyedResource(
                kind="security-group", id=security_group_id, note="Looper 纳管安全组已删除"
            )
        except CloudProviderError as error:
            return DestroyedResource(
                kind="security-group",
                id=security_group_id,
                released=False,
                note=f"安全组清理暂缓：{error}",
            )


def _provisioned_instance(
    region: str, instance: Any, fallback_name: str | None = None
) -> ProvisionedInstance:
    private_ips = list(instance.PrivateIpAddresses or [])
    public_ips = list(instance.PublicIpAddresses or [])
    return ProvisionedInstance(
        id=str(instance.InstanceId),
        name=str(instance.InstanceName or fallback_name or instance.InstanceId),
        region=region,
        zone=str(instance.Placement.Zone) if instance.Placement else None,
        status=str(instance.InstanceState or "PENDING"),
        privateIp=private_ips[0] if private_ips else None,
        publicIp=public_ips[0] if public_ips else None,
        publicIpPresent=bool(public_ips),
    )


def sync_cvm_inventory(
    session: Session, region: str, instance_ids: list[str] | None = None
) -> list[TargetRecord]:
    if not region or not region.startswith("ap-"):
        raise TencentInventoryError("a valid Tencent Cloud region is required")
    provider = TencentCvmProvider()
    from tencentcloud.cvm.v20170312 import models

    if instance_ids:
        normalized_ids = [value.strip() for value in instance_ids]
        if len(normalized_ids) > 100 or len(normalized_ids) != len(set(normalized_ids)):
            raise TencentInventoryError(
                "instance ids must be unique and contain at most 100 values"
            )
        if any(not value.startswith("ins-") or len(value) > 64 for value in normalized_ids):
            raise TencentInventoryError("invalid Tencent Cloud instance id")
        request = models.DescribeInstancesRequest()
        request.from_json_string(canonical_json({"InstanceIds": normalized_ids}))
        response = provider._call("DescribeInstances", region, request)
        return [
            _upsert_instance(session, region, item) for item in list(response.InstanceSet or [])
        ]

    offset = 0
    page_size = 100
    imported: list[TargetRecord] = []
    while True:
        request = models.DescribeInstancesRequest()
        request.from_json_string(canonical_json({"Offset": offset, "Limit": page_size}))
        response = provider._call("DescribeInstances", region, request)
        instances = list(response.InstanceSet or [])
        for instance in instances:
            imported.append(_upsert_instance(session, region, instance))
        if len(instances) < page_size:
            break
        offset += page_size
    _reconcile_missing_inventory(session, region, {record.id for record in imported})
    return imported


_ARCHIVE_AFTER_AUTHORITATIVE_MISSES = 3


def _reconcile_missing_inventory(
    session: Session, region: str, seen_target_ids: set[str]
) -> None:
    """Retain historical targets while removing absent instances from the active pool.

    This is called only after a complete, successful regional inventory traversal. A
    missing row is evidence that the instance was not visible to this credential and
    region, not proof that it was destroyed.
    """
    now = utc_now()
    records = list(
        session.scalars(select(TargetRecord).where(TargetRecord.provider == "tencent"))
    )
    for record in records:
        inventory = record.inventory_json or {}
        if inventory.get("region") != region or record.id in seen_target_ids:
            continue
        misses = record.inventory_miss_count + 1
        record.lifecycle_status = (
            "archived"
            if misses >= _ARCHIVE_AFTER_AUTHORITATIVE_MISSES
            else "missing"
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
    instance_id = str(instance.InstanceId)
    target_id = cloud_target_id("tencent", region, instance_id)
    public_ips = list(instance.PublicIpAddresses or [])
    private_ips = list(instance.PrivateIpAddresses or [])
    inventory = {
        "region": region,
        "zone": instance.Placement.Zone if instance.Placement else None,
        "instance_id": instance_id,
        "instance_name": instance.InstanceName,
        "instance_state": instance.InstanceState,
        "image_id": instance.ImageId,
        "vpc_id": instance.VirtualPrivateCloud.VpcId if instance.VirtualPrivateCloud else None,
        "subnet_id": instance.VirtualPrivateCloud.SubnetId
        if instance.VirtualPrivateCloud
        else None,
        "private_ip": private_ips[0] if private_ips else None,
        "public_ip_present": bool(public_ips),
    }
    fingerprint = {
        "provider": "tencent",
        "region": region,
        "zone": inventory["zone"],
        "instance_type": instance.InstanceType,
        "cpu": instance.CPU,
        "memory_gib": instance.Memory,
        "image_id": instance.ImageId,
        "os_name": instance.OsName,
    }
    capabilities = ["tencent-cvm", "inventory"]
    snapshot = {
        "provider": "tencent",
        "capabilities": capabilities,
        "fingerprint": fingerprint,
    }
    record = session.get(TargetRecord, target_id)
    if record is None:
        for legacy_id in legacy_cloud_target_ids("tencent", region, instance_id):
            record = session.get(TargetRecord, legacy_id)
            if record is not None:
                break
    values: dict[str, Any] = {
        "name": instance.InstanceName or instance_id,
        "provider": "tencent",
        "status": "inventory-only",
        "capabilities_json": capabilities,
        "inventory_json": inventory,
        "fingerprint_json": fingerprint,
        "snapshot_digest": canonical_digest(snapshot),
        "runnable": False,
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
