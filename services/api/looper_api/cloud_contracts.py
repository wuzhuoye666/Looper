from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class ProviderId(StrEnum):
    TENCENT = "tencent"
    ALIBABA = "alibaba"
    VOLCENGINE = "volcengine"
    BAIDU = "baidu"


class ProviderInfo(ApiModel):
    id: ProviderId
    name: str
    sdk_package: str
    sdk_installed: bool
    credentials_configured: bool
    missing_environment: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    live_purchase_enabled: bool = False
    message: str | None = None


class RegionInfo(ApiModel):
    provider: ProviderId
    id: str
    name: str
    endpoint: str | None = None
    available: bool | None = None


class ZoneInfo(ApiModel):
    provider: ProviderId
    region: str
    id: str
    name: str
    available: bool | None = None


class VpcInfo(ApiModel):
    provider: ProviderId
    region: str
    id: str
    name: str
    cidr_block: str | None = None
    is_default: bool = False


class SubnetInfo(ApiModel):
    provider: ProviderId
    region: str
    zone: str
    vpc_id: str
    id: str
    name: str
    cidr_block: str | None = None
    available_ip_count: int | None = None
    is_default: bool = False
    tags: dict[str, str] = Field(default_factory=dict)
    managed: bool = False


class SecurityGroupInfo(ApiModel):
    provider: ProviderId
    region: str
    id: str
    name: str
    description: str | None = None
    is_default: bool = False
    recommended: bool = False
    tags: dict[str, str] = Field(default_factory=dict)


class KeyPairInfo(ApiModel):
    provider: ProviderId
    region: str
    id: str
    name: str
    description: str | None = None
    created_at: datetime | None = None
    associated_instance_count: int = 0


class InstanceTypeInfo(ApiModel):
    provider: ProviderId
    region: str
    id: str
    family: str | None = None
    cpu: int
    memory_gib: float
    gpu: float | None = None
    gpu_model: str | None = None
    gpu_memory_gib: float | None = None
    architecture: str | None = None
    network_bandwidth_rx_gbps: float | None = None
    network_bandwidth_tx_gbps: float | None = None
    network_pps_rx: int | None = None
    network_pps_tx: int | None = None
    local_storage_count: int | None = None
    local_storage_capacity_gib: float | None = None
    local_storage_category: str | None = None
    zones: list[str] = Field(default_factory=list)
    available: bool | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class ImageInfo(ApiModel):
    provider: ProviderId
    region: str
    id: str
    name: str
    platform: str | None = None
    architecture: str | None = None
    image_type: str | None = None
    size_gib: float | None = None
    created_at: datetime | None = None
    available: bool | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class CatalogResponse(ApiModel):
    provider: ProviderId
    resource_type: Literal[
        "region",
        "zone",
        "instance-type",
        "image",
        "vpc",
        "subnet",
        "security-group",
        "key-pair",
    ]
    items: list[dict[str, Any]]
    total: int
    offset: int = 0
    limit: int = 100
    next_offset: int | None = None
    source: Literal["live", "cache", "stale-cache"]
    fetched_at: datetime
    expires_at: datetime
    stale: bool = False
    warning: str | None = None


class CatalogFilters(ApiModel):
    region: str | None = Field(default=None, max_length=64)
    zone: str | None = Field(default=None, max_length=64)
    vpc_id: str | None = Field(default=None, max_length=120)
    query: str | None = Field(default=None, max_length=120)
    min_cpu: int | None = Field(default=None, ge=1, le=1024)
    max_cpu: int | None = Field(default=None, ge=1, le=1024)
    min_memory_gib: float | None = Field(default=None, ge=0.25, le=65536)
    max_memory_gib: float | None = Field(default=None, ge=0.25, le=65536)
    image_type: str | None = Field(default=None, max_length=60)
    platform: str | None = Field(default=None, max_length=80)
    instance_type: str | None = Field(default=None, max_length=120)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


class InstanceNetworkResolveRequest(ApiModel):
    region: str = Field(min_length=2, max_length=64)
    instance_type: str = Field(min_length=1, max_length=120)
    zone: str | None = Field(default=None, max_length=64)
    vpc_id: str | None = Field(default=None, max_length=120)
    subnet_id: str | None = Field(default=None, max_length=120)


class InstanceNetworkResolution(ApiModel):
    provider: ProviderId
    region: str
    instance_type: str
    zone: str
    eligible_zones: list[str]
    vpc: VpcInfo
    subnet: SubnetInfo
    zone_automatically_selected: bool = False
    subnet_action: Literal["reused", "created"]
    warnings: list[str] = Field(default_factory=list)


class CloudPurchaseSpec(ApiModel):
    provider: ProviderId
    region: str = Field(min_length=2, max_length=64)
    zone: str = Field(min_length=2, max_length=64)
    instance_type: str = Field(min_length=1, max_length=120)
    cpu: int | None = Field(default=None, ge=1, le=1024)
    memory_gib: float | None = Field(default=None, ge=0.25, le=65536)
    image_id: str = Field(min_length=1, max_length=180)
    instance_name: str = Field(min_length=1, max_length=128)
    count: int = Field(default=1, ge=1, le=5)
    billing_mode: Literal["postpaid"] = "postpaid"
    vpc_id: str = Field(min_length=1, max_length=120)
    subnet_id: str = Field(min_length=1, max_length=120)
    security_group_ids: list[str] = Field(min_length=1, max_length=5)
    key_pair_id: str | None = Field(default=None, max_length=180)
    system_disk_type: str | None = Field(default=None, max_length=80)
    system_disk_gib: int = Field(default=50, ge=20, le=2048)
    public_ip: bool = False
    internet_bandwidth_mbps: int = Field(default=0, ge=0, le=1000)
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("security_group_ids")
    @classmethod
    def unique_security_groups(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("security group ids must be unique")
        if any(not item.strip() for item in value):
            raise ValueError("security group ids cannot be empty")
        return value

    @field_validator("tags")
    @classmethod
    def bounded_tags(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("at most 20 tags are allowed")
        if any(not key or len(key) > 128 or len(item) > 256 for key, item in value.items()):
            raise ValueError("tag keys and values exceed allowed lengths")
        return value


class ProviderQuote(ApiModel):
    provider_quote_id: str | None = None
    amount: Decimal = Field(ge=0, max_digits=20, decimal_places=8)
    currency: str = Field(min_length=3, max_length=8)
    unit: Literal["hour"] = "hour"
    estimated: bool = True
    expires_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class ProvisionedInstance(ApiModel):
    id: str
    name: str | None = None
    region: str
    zone: str | None = None
    status: str
    private_ip: str | None = None
    public_ip: str | None = None
    public_ip_present: bool = False


class ProviderPurchaseResult(ApiModel):
    provider_order_id: str | None = None
    request_id: str | None = None
    instances: list[ProvisionedInstance]
    details: dict[str, Any] = Field(default_factory=dict)


class QuoteCreateRequest(ApiModel):
    spec: CloudPurchaseSpec


class OrderPrepareRequest(ApiModel):
    quote_id: str = Field(min_length=8, max_length=100)


class OrderConfirmRequest(ApiModel):
    confirmation_token: str = Field(min_length=32, max_length=4096)
    acknowledgement: str = Field(min_length=8, max_length=300)
    expected_hourly_amount: Decimal = Field(ge=0, max_digits=20, decimal_places=8)


class OrderResolveRequest(ApiModel):
    resolution: Literal["submitted", "not_created"]
    instance_ids: list[str] = Field(default_factory=list, max_length=100)
    provider_order_id: str | None = Field(default=None, max_length=160)
    note: str = Field(min_length=8, max_length=500)

    @field_validator("instance_ids")
    @classmethod
    def validate_instance_ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 160 for value in values):
            raise ValueError("provider instance ids must contain 1-160 characters")
        if len(set(values)) != len(values):
            raise ValueError("provider instance ids must be unique")
        return values

    @model_validator(mode="after")
    def validate_resolution(self) -> OrderResolveRequest:
        if self.resolution == "submitted" and not self.instance_ids:
            raise ValueError("submitted resolution requires at least one provider instance id")
        if self.resolution == "not_created" and self.instance_ids:
            raise ValueError("not_created resolution cannot include provider instance ids")
        return self


class SearchResult(ApiModel):
    type: Literal["experiment", "benchmark", "target", "quote", "order"]
    id: str
    title: str
    subtitle: str | None = None
    status: str | None = None
    url: str
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DestroyedResource(ApiModel):
    kind: Literal[
        "instance", "system-disk", "local-disk", "public-ip", "subnet", "security-group"
    ]
    id: str = Field(min_length=1, max_length=180)
    released: bool = True
    note: str | None = Field(default=None, max_length=500)


class ProviderDestroyResult(ApiModel):
    request_id: str | None = None
    instance_ids: list[str] = Field(default_factory=list)
    released_resources: list[DestroyedResource] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class TargetDestroyRequest(ApiModel):
    acknowledgement: str = Field(min_length=8, max_length=300)


class TargetDestroyPreview(ApiModel):
    target_id: str
    provider: ProviderId
    region: str
    instance_id: str
    instance_name: str
    acknowledgement: str
    resources: list[DestroyedResource]
