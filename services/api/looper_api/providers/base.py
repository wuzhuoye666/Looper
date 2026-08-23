from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

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
    RegionInfo,
    SecurityGroupInfo,
    SubnetInfo,
    VpcInfo,
    ZoneInfo,
)


class CloudProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        retryable: bool = False,
        ambiguous: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.details = details or {}


class CloudProvider(ABC):
    id: ProviderId
    display_name: str
    sdk_package: str

    @abstractmethod
    def info(self, *, live_purchase_enabled: bool) -> ProviderInfo:
        raise NotImplementedError

    @abstractmethod
    def list_regions(self) -> list[RegionInfo]:
        raise NotImplementedError

    @abstractmethod
    def list_zones(self, region: str) -> list[ZoneInfo]:
        raise NotImplementedError

    @abstractmethod
    def search_instance_types(self, filters: CatalogFilters) -> list[InstanceTypeInfo]:
        raise NotImplementedError

    @abstractmethod
    def search_images(self, filters: CatalogFilters) -> list[ImageInfo]:
        raise NotImplementedError

    def list_vpcs(self, region: str) -> list[VpcInfo]:
        raise CloudProviderError("VPC catalog is not supported", code="unsupported_catalog")

    def list_subnets(self, region: str, zone: str, vpc_id: str) -> list[SubnetInfo]:
        raise CloudProviderError("subnet catalog is not supported", code="unsupported_catalog")

    def list_security_groups(self, region: str) -> list[SecurityGroupInfo]:
        raise CloudProviderError(
            "security group catalog is not supported", code="unsupported_catalog"
        )

    def list_key_pairs(self, region: str) -> list[KeyPairInfo]:
        raise CloudProviderError("key pair catalog is not supported", code="unsupported_catalog")

    def ensure_managed_security_group(self, region: str) -> SecurityGroupInfo:
        raise CloudProviderError(
            "managed security group creation is not supported", code="unsupported_operation"
        )

    @abstractmethod
    def quote(self, spec: CloudPurchaseSpec) -> ProviderQuote:
        raise NotImplementedError

    @abstractmethod
    def purchase(self, spec: CloudPurchaseSpec, *, client_token: str) -> ProviderPurchaseResult:
        raise NotImplementedError

    def destroy(self, *, region: str, instance_ids: list[str]) -> ProviderDestroyResult:
        """Terminate postpaid instances, releasing their system disk, local disks and public IP."""
        raise CloudProviderError("destroy is not supported", code="unsupported_operation")

    def cleanup_managed_network(
        self,
        *,
        region: str,
        vpc_id: str | None,
        subnet_id: str | None,
        security_group_ids: list[str],
    ) -> list[DestroyedResource]:
        """Best-effort removal of Looper-managed subnet/security-group resources.

        Providers that cannot inspect ownership tags leave these resources in place
        by returning an empty list. Implementations must fail closed: never delete a
        resource that is not verifiably Looper-managed and not still referenced by
        other instances.
        """
        return []
