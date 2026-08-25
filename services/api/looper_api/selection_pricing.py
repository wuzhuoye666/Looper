from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Literal

from looper_core.canonical import utc_now

from looper_api.cloud_contracts import (
    ApiModel,
    CatalogFilters,
    ImageInfo,
    InstanceTypeInfo,
    ProviderId,
)
from looper_api.price_catalog import AlibabaPriceTable
from looper_api.providers.base import CloudProvider, CloudProviderError
from looper_api.providers.registry import CloudProviderRegistry
from looper_api.providers.utils import (
    enrich_instance_type_labels,
    instance_type_generation,
)

QUOTE_CACHE_TTL_SECONDS = 300.0
QUOTE_TIMEOUT_SECONDS = 2.0
SELECTION_SYSTEM_DISK_GIB = 50
SELECTION_PUBLIC_BANDWIDTH_MBPS = 1

_QUOTE_CACHE: dict[str, tuple[float, "SelectionPriceQuote"]] = {}
_QUOTE_CACHE_LOCK = threading.Lock()

_PROVIDER_RATES: dict[ProviderId, dict[str, float]] = {
    ProviderId.TENCENT: {
        "cpu": 0.046,
        "memory": 0.014,
        "gpu": 1.65,
        "localStorageGib": 0.00014,
        "systemDiskGib": 0.00055,
        "publicIp": 0.012,
        "publicBandwidthMbps": 0.018,
    },
    ProviderId.ALIBABA: {
        "cpu": 0.049,
        "memory": 0.015,
        "gpu": 1.75,
        "localStorageGib": 0.00015,
        "systemDiskGib": 0.00058,
        "publicIp": 0.013,
        "publicBandwidthMbps": 0.020,
    },
    ProviderId.VOLCENGINE: {
        "cpu": 0.044,
        "memory": 0.013,
        "gpu": 1.6,
        "localStorageGib": 0.00013,
        "systemDiskGib": 0.00052,
        "publicIp": 0.011,
        "publicBandwidthMbps": 0.017,
    },
    ProviderId.BAIDU: {
        "cpu": 0.047,
        "memory": 0.014,
        "gpu": 1.68,
        "localStorageGib": 0.00014,
        "systemDiskGib": 0.00056,
        "publicIp": 0.012,
        "publicBandwidthMbps": 0.019,
    },
}
_PRICE_CALIBRATION_FACTOR = 2.0


class SelectionPriceQuoteRequest(ApiModel):
    item: InstanceTypeInfo
    zone: str | None = None
    image_id: str | None = None


class SelectionPriceQuote(ApiModel):
    provider: ProviderId
    region: str
    zone: str | None = None
    instance_type: str
    hourly_amount: str
    monthly_amount: str | None = None
    currency: str = "CNY"
    source: Literal["live", "estimated"] = "estimated"
    fetched_at: str | None = None
    expires_at: str | None = None
    warning: str | None = None


class PriceInfo(ApiModel):
    hourly_amount: str
    monthly_amount: str
    source: Literal["price-table", "live", "unavailable"] = "unavailable"
    currency: str = "CNY"
    fetched_at: str | None = None
    warning: str | None = None


def _instance_class_multiplier(item: InstanceTypeInfo) -> float:
    kind = " ".join(
        value or ""
        for value in (item.type_kind, item.type_label, item.family_label)
    ).casefold()
    if any(marker in kind for marker in ("bare", "metal", "裸金属", "hpc", "高性能计算")):
        return 1.2
    if any(marker in kind for marker in ("gpu", "异构", "加速")):
        return 1.14
    if any(marker in kind for marker in ("local", "storage", "本地", "存储")):
        return 1.12
    if any(marker in kind for marker in ("memory", "内存")):
        return 1.1
    if any(marker in kind for marker in ("compute", "计算")):
        return 1.08
    if any(marker in kind for marker in ("burst", "突发")):
        return 0.9
    return 1.0


def estimate_instance_hourly(item: InstanceTypeInfo) -> float:
    """Estimate an hourly price with the same preview model as the web client."""
    enriched = enrich_instance_type_labels(item)
    rates = _PROVIDER_RATES[item.provider]
    generation = instance_type_generation(enriched)
    generation_multiplier = (
        1.0
        if generation is None
        else min(1.2, max(0.95, 0.9 + generation * 0.025))
    )
    instance = max(
        item.cpu * rates["cpu"]
        + item.memory_gib * rates["memory"]
        + max(item.gpu or 0, 0) * rates["gpu"]
        + max(item.local_storage_capacity_gib or 0, 0) * rates["localStorageGib"],
        0.01,
    )
    instance *= (
        generation_multiplier
        * _instance_class_multiplier(enriched)
        * _PRICE_CALIBRATION_FACTOR
    )
    system_disk = (
        SELECTION_SYSTEM_DISK_GIB * rates["systemDiskGib"] * _PRICE_CALIBRATION_FACTOR
    )
    public_ip = (
        rates["publicIp"]
        + SELECTION_PUBLIC_BANDWIDTH_MBPS * rates["publicBandwidthMbps"]
    ) * _PRICE_CALIBRATION_FACTOR
    return round(instance + system_disk + public_ip, 3)


def _architecture_group(value: str | None) -> str | None:
    normalized = (value or "").casefold().replace("-", "_")
    if "arm" in normalized or "aarch" in normalized:
        return "arm"
    if any(token in normalized for token in ("x86", "amd64", "i386", "i686")):
        return "x86"
    return None


def _image_preference_key(image: ImageInfo) -> tuple[int, str]:
    text = f"{image.name} {image.id} {image.platform or ''}".casefold()
    if "ubuntu" in text:
        preference = 0
    elif any(
        marker in text
        for marker in (
            "linux",
            "debian",
            "centos",
            "rocky",
            "almalinux",
            "fedora",
            "opensuse",
            "tencentos",
            "alinux",
            "anolis",
        )
    ):
        preference = 1
    elif "windows" in text:
        preference = 2
    else:
        preference = 3
    return preference, image.id.casefold()


def _resolve_image(
    provider: CloudProvider,
    *,
    region: str,
    instance_type: str,
    architecture: str | None,
) -> ImageInfo | None:
    try:
        images = provider.search_images(
            CatalogFilters(region=region, instance_type=instance_type)
        )
    except CloudProviderError:
        return None
    if not images:
        return None
    available = [item for item in images if item.available is not False] or images
    architecture_group = _architecture_group(architecture)
    if architecture_group:
        matching = [
            item
            for item in available
            if _architecture_group(item.architecture) == architecture_group
        ]
        if matching:
            available = matching
    return sorted(available, key=_image_preference_key)[0]


def _cache_key(
    provider: ProviderId,
    region: str,
    zone: str,
    instance_type: str,
    image_id: str,
) -> str:
    return "|".join(
        (provider.value, region, zone, instance_type, image_id)
    )


def _estimated_quote(
    item: InstanceTypeInfo,
    *,
    zone: str | None,
    warning: str | None = None,
) -> "SelectionPriceQuote":
    hourly = estimate_instance_hourly(item)
    return SelectionPriceQuote(
        provider=item.provider,
        region=item.region,
        zone=zone,
        instance_type=item.id,
        hourly_amount=f"{hourly:.3f}",
        monthly_amount=f"{hourly * 730:.2f}",
        currency="CNY",
        source="estimated",
        warning=warning,
    )


def selection_instance_quote(
    request: SelectionPriceQuoteRequest,
    registry: CloudProviderRegistry,
) -> "SelectionPriceQuote":
    item = request.item
    zone = request.zone or (item.zones[0] if item.zones else None)
    if not zone:
        return _estimated_quote(
            item,
            zone=zone,
            warning="未选择可用区，无法实时询价",
        )
    provider = registry.get(item.provider)
    image_id = request.image_id
    if not image_id:
        image = _resolve_image(
            provider,
            region=item.region,
            instance_type=item.id,
            architecture=item.architecture,
        )
        image_id = image.id if image else None
    if not image_id:
        return _estimated_quote(
            item,
            zone=zone,
            warning="未找到兼容镜像，无法实时询价",
        )

    cache_key = _cache_key(item.provider, item.region, zone, item.id, image_id)
    now = time.time()
    with _QUOTE_CACHE_LOCK:
        cached = _QUOTE_CACHE.get(cache_key)
        if cached is not None and now - cached[0] < QUOTE_CACHE_TTL_SECONDS:
            return cached[1]

    try:
        quote_method = getattr(provider, "quote_instance_type", None)
        if not callable(quote_method):
            raise CloudProviderError(
                "provider does not support selection price inquiry",
                code="unsupported_selection_quote",
            )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            quote_method,
            region=item.region,
            zone=zone,
            instance_type=item.id,
            image_id=image_id,
            system_disk_gib=SELECTION_SYSTEM_DISK_GIB,
            system_disk_category=None,
            public_ip=True,
            public_bandwidth_mbps=SELECTION_PUBLIC_BANDWIDTH_MBPS,
        )
        try:
            quote = future.result(timeout=QUOTE_TIMEOUT_SECONDS)
        finally:
            executor.shutdown(wait=False)
        hourly_amount = float(quote.amount)
        price = SelectionPriceQuote(
            provider=item.provider,
            region=item.region,
            zone=zone,
            instance_type=item.id,
            hourly_amount=str(quote.amount),
            monthly_amount=f"{hourly_amount * 730:.2f}",
            currency=quote.currency,
            source="live",
            fetched_at=utc_now().isoformat(),
            expires_at=quote.expires_at.isoformat(),
        )
    except Exception as error:
        warning = str(error).strip().replace("\n", " ")[:240] or error.__class__.__name__
        price = _estimated_quote(
            item,
            zone=zone,
            warning=f"实时询价失败，已回退估算价：{warning}",
        )

    with _QUOTE_CACHE_LOCK:
        _QUOTE_CACHE[cache_key] = (time.time(), price)
    return price


def resolve_item_price(
    item: InstanceTypeInfo,
    *,
    registry: CloudProviderRegistry | None = None,
    price_table: AlibabaPriceTable | None = None,
) -> "PriceInfo | None":
    """Return the authoritative price for an instance, never a guessed estimate."""
    if item.provider == ProviderId.ALIBABA and price_table is not None:
        entry = price_table.get(item.region, item.id)
        if entry is None:
            return None
        return PriceInfo(
            hourly_amount=f"{entry.hourly_discounted:.3f}",
            monthly_amount=f"{entry.monthly_discounted:.3f}",
            source="price-table",
        )
    if item.provider == ProviderId.TENCENT:
        attributes = item.attributes or {}
        hourly = attributes.get("hourlyPrice") or attributes.get("hourly_amount")
        if hourly is None:
            return None
        monthly = attributes.get("monthlyPrice") or attributes.get("monthly_amount")
        if monthly is None:
            monthly = float(hourly) * 730.0
        return PriceInfo(
            hourly_amount=str(hourly),
            monthly_amount=f"{float(monthly):.3f}",
            source="live",
        )
    return None
