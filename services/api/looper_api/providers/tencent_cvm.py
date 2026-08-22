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
    image_scan_limit,
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
        from tencentcloud.vpc.v20170312 import models

        filters = [
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "zone", "Values": [zone]},
        ]
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
            items.extend(
                SubnetInfo(
                    provider=self.id,
                    region=region,
                    zone=str(item.Zone),
                    vpcId=str(item.VpcId),
                    id=str(item.SubnetId),
                    name=str(item.SubnetName or item.SubnetId),
                    cidrBlock=attr(item, "CidrBlock"),
                    availableIpCount=attr(item, "AvailableIpAddressCount"),
                    isDefault=bool(attr(item, "IsDefault", default=False)),
                )
                for item in rows
            )
            offset += len(rows)
            total = attr(response, "TotalCount")
            if not rows or len(rows) < 100 or (total is not None and offset >= int(total)):
                break
        return items

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

        request = models.DescribeInstanceTypeConfigsRequest()
        response = self._call("DescribeInstanceTypeConfigs", filters.region, request)
        items = []
        for item in list(response.InstanceTypeConfigSet or []):
            zone = attr(item, "Zone")
            items.append(
                InstanceTypeInfo(
                    provider=self.id,
                    region=filters.region,
                    id=str(item.InstanceType),
                    family=attr(item, "InstanceFamily"),
                    cpu=int(item.CPU),
                    memoryGib=float(item.Memory),
                    gpu=int(attr(item, "GPU", default=0) or 0),
                    zones=[str(zone)] if zone else [],
                    available=None,
                    attributes={"fpga": int(attr(item, "FPGA", default=0) or 0)},
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
        scan_limit = image_scan_limit(filters)
        while len(items) < scan_limit:
            page_size = min(100, scan_limit - len(items))
            request = models.DescribeImagesRequest()
            request.from_json_string(
                canonical_json({"Filters": provider_filters, "Offset": offset, "Limit": page_size})
            )
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
