from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from looper_core.canonical import canonical_digest, canonical_json, new_id, utc_now
from sqlalchemy import Text, cast, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from looper_api.cloud_contracts import (
    CatalogFilters,
    CatalogResponse,
    CloudPurchaseSpec,
    ImageInfo,
    InstanceTypeInfo,
    OrderConfirmRequest,
    OrderResolveRequest,
    ProviderId,
    ProvisionedInstance,
    SearchResult,
)
from looper_api.config import Settings
from looper_api.events import append_event
from looper_api.models import (
    BenchmarkRecord,
    CloudCatalogCacheRecord,
    CloudOrderRecord,
    CloudQuoteRecord,
    EventRecord,
    ExperimentRecord,
    TargetRecord,
)
from looper_api.providers.base import CloudProviderError
from looper_api.providers.registry import CloudProviderRegistry
from looper_api.providers.utils import (
    cloud_target_id,
    filter_images,
    filter_instance_types,
    legacy_cloud_target_ids,
    to_plain,
)

CatalogKind = Literal[
    "region",
    "zone",
    "instance-type",
    "image",
    "vpc",
    "subnet",
    "security-group",
    "key-pair",
]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso_utc(value: datetime) -> str:
    return _aware(value).astimezone(UTC).isoformat()


def _money(value: Decimal | str) -> str:
    rendered = format(Decimal(str(value)), "f")
    normalized = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    return normalized or "0"


class CloudWorkflowError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 409, code: str = "cloud_workflow_error"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def operator_auth_required(settings: Settings) -> bool:
    return settings.live_purchase_enabled or bool(settings.operator_token)


def operator_token_ready(settings: Settings) -> bool:
    return len(settings.operator_token) >= 32


def confirmation_secret_ready(settings: Settings) -> bool:
    return (
        len(settings.purchase_confirmation_secret) >= 32
        and settings.purchase_confirmation_secret != "change-me-before-enabling-live-purchase"
        and settings.purchase_confirmation_secret != settings.operator_token
    )


def provider_enabled(settings: Settings, provider: ProviderId) -> bool:
    secret_ready = confirmation_secret_ready(settings)
    return (
        settings.live_purchase_enabled
        and operator_token_ready(settings)
        and secret_ready
        and provider.value in settings.enabled_purchase_providers
    )


def provider_views(settings: Settings, registry: CloudProviderRegistry) -> list[dict[str, Any]]:
    result = []
    for provider_id in ProviderId:
        provider = registry.get(provider_id)
        result.append(
            provider.info(live_purchase_enabled=provider_enabled(settings, provider_id)).model_dump(
                by_alias=True, mode="json"
            )
        )
    return result


def purchase_readiness(settings: Settings, registry: CloudProviderRegistry) -> dict[str, Any]:
    operator_ready = operator_token_ready(settings)
    confirmation_ready = confirmation_secret_ready(settings)
    providers = []
    for provider_id in ProviderId:
        info = registry.get(provider_id).info(live_purchase_enabled=False)
        exact_quote_ready = "hourly-quote" in info.capabilities
        create_ready = "postpaid-purchase" in info.capabilities
        allowlisted = provider_id.value in settings.enabled_purchase_providers
        checks = [
            {
                "code": "sdk",
                "label": "SDK",
                "ready": info.sdk_installed,
                "detail": info.sdk_package,
            },
            {
                "code": "credentials",
                "label": "API 凭证",
                "ready": info.credentials_configured,
                "detail": "已配置"
                if info.credentials_configured
                else f"缺少：{'、'.join(info.missing_environment)}",
            },
            {
                "code": "price-contract",
                "label": "完整小时报价",
                "ready": exact_quote_ready,
                "detail": "Provider 支持精确小时询价"
                if exact_quote_ready
                else (info.message or "尚无完整价格契约"),
            },
            {
                "code": "create-adapter",
                "label": "创建适配器",
                "ready": create_ready,
                "detail": "按量创建路径已实现"
                if create_ready
                else (info.message or "按量创建未开放"),
            },
            {
                "code": "operator-token",
                "label": "Operator token",
                "ready": operator_ready,
                "detail": "已配置" if operator_ready else "需要独立的 32+ 字符 Bearer token",
            },
            {
                "code": "confirmation-secret",
                "label": "确认签名密钥",
                "ready": confirmation_ready,
                "detail": "已配置"
                if confirmation_ready
                else "需要与 Operator token 不同的 32+ 字符密钥",
            },
            {
                "code": "allowlist",
                "label": "Provider allowlist",
                "ready": allowlisted,
                "detail": "已允许" if allowlisted else f"未允许 {provider_id.value}",
            },
            {
                "code": "global-switch",
                "label": "真实购买开关",
                "ready": settings.live_purchase_enabled,
                "detail": "已开启" if settings.live_purchase_enabled else "当前关闭",
            },
        ]
        providers.append(
            {
                "provider": provider_id.value,
                "name": info.name,
                "ready": all(check["ready"] for check in checks),
                "checks": checks,
                "missingEnvironment": info.missing_environment,
            }
        )
    return {
        "livePurchaseEnabled": settings.live_purchase_enabled,
        "operatorTokenReady": operator_ready,
        "confirmationSecretReady": confirmation_ready,
        "maxHourlyAmount": _money(settings.max_live_hourly_amount),
        "providers": providers,
    }


def ensure_managed_security_group(
    session: Session,
    registry: CloudProviderRegistry,
    provider_id: ProviderId,
    region: str,
) -> dict[str, Any]:
    group = registry.get(provider_id).ensure_managed_security_group(region)
    session.execute(
        delete(CloudCatalogCacheRecord).where(
            CloudCatalogCacheRecord.provider == provider_id.value,
            CloudCatalogCacheRecord.resource_type == "security-group",
            CloudCatalogCacheRecord.region == region,
        )
    )
    return group.model_dump(mode="json", by_alias=True)


_FULL_CATALOG_CACHE_VERSION = 3
_PAGED_CATALOG_KINDS = {"instance-type", "image"}


@dataclass(frozen=True)
class _CatalogSnapshot:
    items: list[dict[str, Any]]
    source: Literal["live", "cache", "stale-cache"]
    fetched_at: datetime
    expires_at: datetime
    stale: bool = False
    warning: str | None = None


def _snapshot_filters(kind: CatalogKind, filters: CatalogFilters) -> CatalogFilters:
    if kind == "instance-type":
        return CatalogFilters(region=filters.region, zone=filters.zone)
    if kind == "image":
        return CatalogFilters(region=filters.region, imageType=filters.image_type)
    return CatalogFilters(region=filters.region, zone=filters.zone, vpcId=filters.vpc_id)


def _cache_key(provider: ProviderId, kind: CatalogKind, filters: CatalogFilters) -> str:
    return canonical_digest(
        {
            "version": _FULL_CATALOG_CACHE_VERSION,
            "provider": provider.value,
            "kind": kind,
            "filters": filters.model_dump(
                mode="json", exclude_none=True, exclude={"offset", "limit"}
            ),
        }
    )


def _catalog_call(provider: Any, kind: CatalogKind, filters: CatalogFilters) -> list[Any]:
    if kind == "region":
        return provider.list_regions()
    if kind == "zone":
        if not filters.region:
            raise CloudWorkflowError("region is required", status_code=422, code="region_required")
        return provider.list_zones(filters.region)
    if kind == "instance-type":
        return provider.search_instance_types(filters)
    if kind == "image":
        return provider.search_images(filters)
    if not filters.region:
        raise CloudWorkflowError("region is required", status_code=422, code="region_required")
    if kind == "vpc":
        return provider.list_vpcs(filters.region)
    if kind == "subnet":
        if not filters.zone:
            raise CloudWorkflowError("zone is required", status_code=422, code="zone_required")
        if not filters.vpc_id:
            raise CloudWorkflowError("vpc_id is required", status_code=422, code="vpc_required")
        return provider.list_subnets(filters.region, filters.zone, filters.vpc_id)
    if kind == "security-group":
        return provider.list_security_groups(filters.region)
    return provider.list_key_pairs(filters.region)


def _deduplicate_catalog(kind: CatalogKind, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if kind not in _PAGED_CATALOG_KINDS:
        return items
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        result.append(item)
    return result


def _catalog_snapshot(
    session: Session,
    settings: Settings,
    registry: CloudProviderRegistry,
    provider_id: ProviderId,
    kind: CatalogKind,
    filters: CatalogFilters,
) -> _CatalogSnapshot:
    provider = registry.get(provider_id)
    now = utc_now()
    snapshot_filters = _snapshot_filters(kind, filters)
    key = _cache_key(provider_id, kind, snapshot_filters)
    record = session.get(CloudCatalogCacheRecord, key)
    if record and _aware(record.expires_at) > now:
        return _CatalogSnapshot(
            items=record.payload_json,
            source="cache",
            fetched_at=record.fetched_at,
            expires_at=record.expires_at,
            stale=False,
        )
    if (
        record
        and filters.offset > 0
        and _aware(record.fetched_at) + timedelta(seconds=settings.cloud_stale_cache_seconds) > now
    ):
        return _CatalogSnapshot(
            items=record.payload_json,
            source="stale-cache",
            fetched_at=record.fetched_at,
            expires_at=_aware(record.fetched_at)
            + timedelta(seconds=settings.cloud_stale_cache_seconds),
            stale=True,
            warning="为保持分页顺序一致，继续使用当前目录快照",
        )

    try:
        models = _catalog_call(provider, kind, snapshot_filters)
        payload = _deduplicate_catalog(
            kind, [model.model_dump(mode="json", by_alias=True) for model in models]
        )
        expires = now + timedelta(seconds=settings.cloud_catalog_ttl_seconds)
        if record is None:
            record = CloudCatalogCacheRecord(
                key=key,
                provider=provider_id.value,
                resource_type=kind,
                region=snapshot_filters.region,
                zone=snapshot_filters.zone,
                query_json={
                    "version": _FULL_CATALOG_CACHE_VERSION,
                    **snapshot_filters.model_dump(
                        mode="json", exclude_none=True, exclude={"offset", "limit"}
                    ),
                },
                payload_json=payload,
                fetched_at=now,
                expires_at=expires,
                last_error=None,
            )
            session.add(record)
        else:
            record.payload_json = payload
            record.fetched_at = now
            record.expires_at = expires
            record.last_error = None
        session.flush()
        return _CatalogSnapshot(
            items=payload,
            source="live",
            fetched_at=now,
            expires_at=expires,
            stale=False,
        )
    except (CloudProviderError, CloudWorkflowError) as error:
        if (
            record
            and _aware(record.fetched_at) + timedelta(seconds=settings.cloud_stale_cache_seconds)
            > now
        ):
            record.last_error = str(error)
            stale_expires = _aware(record.fetched_at) + timedelta(
                seconds=settings.cloud_stale_cache_seconds
            )
            return _CatalogSnapshot(
                items=record.payload_json,
                source="stale-cache",
                fetched_at=record.fetched_at,
                expires_at=stale_expires,
                stale=True,
                warning=f"实时查询失败，显示缓存：{error}",
            )
        raise


def catalog_inventory(
    session: Session,
    settings: Settings,
    registry: CloudProviderRegistry,
    provider_id: ProviderId,
    kind: CatalogKind,
    filters: CatalogFilters,
) -> CatalogResponse:
    snapshot = _catalog_snapshot(session, settings, registry, provider_id, kind, filters)
    return CatalogResponse(
        provider=provider_id,
        resourceType=kind,
        items=snapshot.items,
        total=len(snapshot.items),
        offset=0,
        limit=max(len(snapshot.items), 1),
        nextOffset=None,
        source=snapshot.source,
        fetchedAt=snapshot.fetched_at,
        expiresAt=snapshot.expires_at,
        stale=snapshot.stale,
        warning=snapshot.warning,
    )


def _natural_catalog_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )


def _inventory_sort_rank(available: bool | None) -> int:
    if available is True:
        return 0
    if available is False:
        return 1
    return 2


def catalog_search(
    session: Session,
    settings: Settings,
    registry: CloudProviderRegistry,
    provider_id: ProviderId,
    kind: CatalogKind,
    filters: CatalogFilters,
) -> CatalogResponse:
    snapshot = _catalog_snapshot(session, settings, registry, provider_id, kind, filters)
    items = snapshot.items
    if kind == "instance-type":
        models = filter_instance_types(
            [InstanceTypeInfo.model_validate(item) for item in items], filters
        )
        if provider_id in {ProviderId.TENCENT, ProviderId.ALIBABA}:
            models.sort(
                key=lambda item: (
                    _inventory_sort_rank(item.available),
                    _natural_catalog_key(item.id),
                    item.id,
                )
            )
        else:
            models.sort(key=lambda item: (_natural_catalog_key(item.id), item.id))
        items = [item.model_dump(mode="json", by_alias=True) for item in models]
    elif kind == "image":
        models = filter_images([ImageInfo.model_validate(item) for item in items], filters)
        items = [item.model_dump(mode="json", by_alias=True) for item in models]

    total = len(items)
    if kind in _PAGED_CATALOG_KINDS:
        page = items[filters.offset : filters.offset + filters.limit]
        next_offset = filters.offset + filters.limit
        next_offset = next_offset if next_offset < total else None
    else:
        page = items
        next_offset = None
    return CatalogResponse(
        provider=provider_id,
        resourceType=kind,
        items=page,
        total=total,
        offset=filters.offset if kind in _PAGED_CATALOG_KINDS else 0,
        limit=filters.limit,
        nextOffset=next_offset,
        source=snapshot.source,
        fetchedAt=snapshot.fetched_at,
        expiresAt=snapshot.expires_at,
        stale=snapshot.stale,
        warning=snapshot.warning,
    )


def _public_spec(spec_json: dict[str, Any]) -> dict[str, Any]:
    return CloudPurchaseSpec.model_validate(spec_json).model_dump(mode="json", by_alias=True)


def _quote_view(record: CloudQuoteRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "provider": record.provider,
        "status": record.status,
        "spec": _public_spec(record.spec_json),
        "specDigest": record.spec_digest,
        "providerQuoteId": record.provider_quote_id,
        "hourlyAmount": _money(record.hourly_amount),
        "currency": record.currency,
        "estimated": record.estimated,
        "quoteDigest": record.quote_digest,
        "details": record.provider_details_json,
        "expiresAt": _iso_utc(record.expires_at),
        "createdAt": _iso_utc(record.created_at),
    }


def create_quote(
    session: Session,
    settings: Settings,
    registry: CloudProviderRegistry,
    spec: CloudPurchaseSpec,
    idempotency_key: str,
) -> dict[str, Any]:
    if len(idempotency_key) < 8 or len(idempotency_key) > 160:
        raise CloudWorkflowError("Idempotency-Key must contain 8-160 characters", status_code=422)
    existing = session.scalar(
        select(CloudQuoteRecord).where(CloudQuoteRecord.idempotency_key == idempotency_key)
    )
    if existing:
        if existing.spec_digest != canonical_digest(spec.model_dump(mode="json")):
            raise CloudWorkflowError(
                "Idempotency-Key is already bound to another quote", code="idempotency_conflict"
            )
        return _quote_view(existing)
    provider = registry.get(spec.provider)
    try:
        quote = provider.quote(spec)
    except CloudProviderError:
        raise
    spec_json = spec.model_dump(mode="json")
    spec_digest = canonical_digest(spec_json)
    provider_expires = quote.expires_at
    if provider_expires.tzinfo is None:
        provider_expires = provider_expires.replace(tzinfo=UTC)
    expires_at = min(
        provider_expires,
        utc_now() + timedelta(seconds=settings.cloud_quote_ttl_seconds),
    )
    quote_payload = {
        "provider": spec.provider.value,
        "specDigest": spec_digest,
        "providerQuoteId": quote.provider_quote_id,
        "amount": str(quote.amount),
        "currency": quote.currency,
        "expiresAt": expires_at.isoformat(),
        "details": to_plain(quote.details),
    }
    record = CloudQuoteRecord(
        id=new_id("quote"),
        provider=spec.provider.value,
        status="valid",
        idempotency_key=idempotency_key,
        spec_json=spec_json,
        spec_digest=spec_digest,
        provider_quote_id=quote.provider_quote_id,
        hourly_amount=quote.amount,
        currency=quote.currency,
        estimated=quote.estimated,
        quote_digest=canonical_digest(quote_payload),
        provider_details_json=to_plain(quote.details),
        expires_at=expires_at,
        created_at=utc_now(),
    )
    try:
        session.add(record)
        append_event(
            session,
            experiment_id=None,
            event_type="cloud.quote.created",
            entity_type="cloud_quote",
            entity_id=record.id,
            idempotency_key=f"cloud-quote-created:{record.id}",
            payload={
                "provider": record.provider,
                "quoteDigest": record.quote_digest,
                "amount": _money(record.hourly_amount),
                "currency": record.currency,
            },
        )
        session.flush()
    except IntegrityError as error:
        session.rollback()
        winner = session.scalar(
            select(CloudQuoteRecord).where(CloudQuoteRecord.idempotency_key == idempotency_key)
        )
        if winner is None:
            raise CloudWorkflowError(
                "quote idempotency reservation conflicted; retry with the same key",
                code="idempotency_in_progress",
            ) from error
        if winner.spec_digest != spec_digest:
            raise CloudWorkflowError(
                "Idempotency-Key is already bound to another quote",
                code="idempotency_conflict",
            ) from error
        return _quote_view(winner)
    return _quote_view(record)


def _token_sign(settings: Settings, payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(canonical_json(payload).encode()).decode().rstrip("=")
    signature = hmac.new(
        settings.purchase_confirmation_secret.encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    return f"{body}.{signature}"


def _token_verify(settings: Settings, token: str) -> dict[str, Any]:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(
            settings.purchase_confirmation_secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        padded = body + "=" * (-len(body) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CloudWorkflowError(
            "confirmation token is invalid", code="invalid_confirmation"
        ) from error


def _hash_phrase(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _confirmation_phrase(record: CloudOrderRecord) -> str:
    return (
        f"确认购买 {record.spec_json['provider']} {record.spec_json['instance_name']} "
        f"每小时 {_money(record.hourly_amount)} {record.currency}"
    )


def _confirmation_token(settings: Settings, record: CloudOrderRecord) -> str:
    expires = _aware(record.confirmation_expires_at)
    return _token_sign(
        settings,
        {
            "orderId": record.id,
            "quoteDigest": record.quote_digest,
            "expires": expires.timestamp(),
        },
    )


def _order_view(
    record: CloudOrderRecord, *, token: str | None = None, phrase: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": record.id,
        "quoteId": record.quote_id,
        "provider": record.provider,
        "status": record.status,
        "spec": _public_spec(record.spec_json),
        "specDigest": record.spec_digest,
        "quoteDigest": record.quote_digest,
        "hourlyAmount": _money(record.hourly_amount),
        "currency": record.currency,
        "providerOrderId": record.provider_order_id,
        "instanceIds": record.provider_instance_ids_json,
        "providerResponse": record.provider_response_json,
        "errorCode": record.error_code,
        "errorMessage": record.error_message,
        "confirmationExpiresAt": _iso_utc(record.confirmation_expires_at),
        "createdAt": _iso_utc(record.created_at),
        "updatedAt": _iso_utc(record.updated_at),
        "confirmedAt": _iso_utc(record.confirmed_at) if record.confirmed_at else None,
        "submittedAt": _iso_utc(record.submitted_at) if record.submitted_at else None,
    }
    if token is not None:
        result["confirmationToken"] = token
    if phrase is not None:
        result["acknowledgement"] = phrase
    return result


def prepare_order(
    session: Session,
    settings: Settings,
    quote_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    if len(idempotency_key) < 8 or len(idempotency_key) > 160:
        raise CloudWorkflowError("Idempotency-Key must contain 8-160 characters", status_code=422)
    quote = session.get(CloudQuoteRecord, quote_id)
    if quote is None:
        raise CloudWorkflowError("quote not found", status_code=404, code="quote_not_found")
    existing = session.scalar(
        select(CloudOrderRecord).where(CloudOrderRecord.idempotency_key == idempotency_key)
    )
    if existing:
        if existing.quote_id != quote.id:
            raise CloudWorkflowError(
                "Idempotency-Key is already bound to another order",
                code="idempotency_conflict",
            )
        if existing.status == "awaiting_confirmation":
            token = _confirmation_token(settings, existing)
            return _order_view(existing, token=token, phrase=_confirmation_phrase(existing))
        return _order_view(existing)
    quote_order = session.scalar(
        select(CloudOrderRecord).where(CloudOrderRecord.quote_id == quote.id)
    )
    if quote_order:
        raise CloudWorkflowError(
            "quote is already bound to another order",
            code="quote_already_prepared",
        )
    now = utc_now()
    quote_expires = _aware(quote.expires_at)
    if quote.status != "valid":
        raise CloudWorkflowError(
            f"quote cannot prepare an order from {quote.status}",
            code="invalid_quote_state",
        )
    if quote.estimated:
        raise CloudWorkflowError(
            "estimated quotes cannot be used for live purchase; obtain a complete provider quote",
            code="estimated_quote_not_purchasable",
        )
    if quote_expires <= now:
        quote.status = "expired"
        session.commit()
        raise CloudWorkflowError("quote has expired", code="quote_expired")
    phrase = (
        f"确认购买 {quote.spec_json['provider']} {quote.spec_json['instance_name']} "
        f"每小时 {_money(quote.hourly_amount)} {quote.currency}"
    )
    expires = now + timedelta(seconds=settings.purchase_confirmation_seconds)
    client_token = secrets.token_urlsafe(32)[:64]
    record = CloudOrderRecord(
        id=new_id("order"),
        quote_id=quote.id,
        provider=quote.provider,
        status="awaiting_confirmation",
        idempotency_key=idempotency_key,
        client_token=client_token,
        spec_json=quote.spec_json,
        spec_digest=quote.spec_digest,
        quote_digest=quote.quote_digest,
        hourly_amount=quote.hourly_amount,
        currency=quote.currency,
        confirmation_phrase_hash=_hash_phrase(phrase),
        confirmation_expires_at=expires,
        provider_instance_ids_json=[],
        provider_response_json={},
        created_at=now,
        updated_at=now,
    )
    try:
        session.add(record)
        append_event(
            session,
            experiment_id=None,
            event_type="cloud.order.awaiting_confirmation",
            entity_type="cloud_order",
            entity_id=record.id,
            idempotency_key=f"cloud-order-prepared:{record.id}",
            payload={"quoteId": quote.id, "provider": quote.provider},
        )
        session.flush()
    except IntegrityError as error:
        session.rollback()
        winner = session.scalar(
            select(CloudOrderRecord).where(CloudOrderRecord.idempotency_key == idempotency_key)
        )
        if winner:
            if winner.quote_id != quote_id:
                raise CloudWorkflowError(
                    "Idempotency-Key is already bound to another order",
                    code="idempotency_conflict",
                ) from error
            if winner.status == "awaiting_confirmation":
                return _order_view(
                    winner,
                    token=_confirmation_token(settings, winner),
                    phrase=_confirmation_phrase(winner),
                )
            return _order_view(winner)
        quote_winner = session.scalar(
            select(CloudOrderRecord).where(CloudOrderRecord.quote_id == quote_id)
        )
        if quote_winner:
            raise CloudWorkflowError(
                "quote is already bound to another order",
                code="quote_already_prepared",
            ) from error
        raise CloudWorkflowError(
            "order idempotency reservation conflicted; retry with the same key",
            code="idempotency_in_progress",
        ) from error
    return _order_view(
        record,
        token=_confirmation_token(settings, record),
        phrase=_confirmation_phrase(record),
    )


def renew_order_confirmation(
    session: Session,
    settings: Settings,
    registry: CloudProviderRegistry,
    order_id: str,
) -> dict[str, Any]:
    order = session.get(CloudOrderRecord, order_id)
    if order is None:
        raise CloudWorkflowError("order not found", status_code=404, code="order_not_found")
    if order.status not in {"awaiting_confirmation", "expired"}:
        raise CloudWorkflowError(
            f"order confirmation cannot be renewed from {order.status}",
            code="invalid_order_state",
        )
    now = utc_now()
    bound_quote = session.get(CloudQuoteRecord, order.quote_id)
    if bound_quote is None:
        raise CloudWorkflowError("bound quote no longer exists", code="quote_binding_invalid")
    if bound_quote.status == "superseded":
        raise CloudWorkflowError(
            "price changed previously; request a fresh quote", code="fresh_quote_required"
        )
    if (
        bound_quote.provider != order.provider
        or bound_quote.spec_digest != order.spec_digest
        or bound_quote.quote_digest != order.quote_digest
        or Decimal(str(bound_quote.hourly_amount)) != Decimal(str(order.hourly_amount))
        or bound_quote.currency != order.currency
    ):
        raise CloudWorkflowError(
            "order no longer matches its immutable quote", code="quote_binding_invalid"
        )

    provider_id = ProviderId(order.provider)
    if not provider_enabled(settings, provider_id):
        raise CloudWorkflowError(
            "live purchase is disabled; confirmation cannot be renewed",
            code="live_purchase_disabled",
        )
    confirmed_amount = Decimal(str(order.hourly_amount))
    if confirmed_amount > settings.max_live_hourly_amount:
        raise CloudWorkflowError(
            "quote exceeds the configured live hourly spend limit", code="spend_limit"
        )
    provider = registry.get(provider_id)
    provider_status = provider.info(live_purchase_enabled=True)
    if not provider_status.live_purchase_enabled:
        raise CloudWorkflowError(
            provider_status.message or "provider purchase path is not ready",
            code="provider_purchase_not_ready",
        )

    spec = CloudPurchaseSpec.model_validate(order.spec_json)
    refreshed_quote = provider.quote(spec)
    refreshed_amount = Decimal(str(refreshed_quote.amount))
    price_matches = (
        not refreshed_quote.estimated
        and _aware(refreshed_quote.expires_at) > now
        and refreshed_amount == confirmed_amount
        and refreshed_quote.currency == order.currency
    )
    if not price_matches:
        bound_quote.status = "superseded"
        order.status = "expired"
        order.updated_at = now
        payload = {
            "confirmedAmount": _money(confirmed_amount),
            "confirmedCurrency": order.currency,
            "currentAmount": _money(refreshed_amount),
            "currentCurrency": refreshed_quote.currency,
            "currentProviderQuoteId": refreshed_quote.provider_quote_id,
            "estimated": refreshed_quote.estimated,
        }
        append_event(
            session,
            experiment_id=None,
            event_type="cloud.order.price_changed",
            entity_type="cloud_order",
            entity_id=order.id,
            idempotency_key=(
                f"cloud-order-renewal-price-changed:{order.id}:"
                f"{canonical_digest(payload)[7:23]}"
            ),
            payload=payload,
        )
        session.commit()
        raise CloudWorkflowError(
            "provider price changed or is no longer exact; request a fresh quote",
            code="price_changed",
        )

    renewed_expiry = now + timedelta(seconds=settings.purchase_confirmation_seconds)
    previous_expiry = order.confirmation_expires_at
    order.status = "awaiting_confirmation"
    order.confirmation_expires_at = renewed_expiry
    order.updated_at = now
    append_event(
        session,
        experiment_id=None,
        event_type="cloud.order.confirmation_renewed",
        entity_type="cloud_order",
        entity_id=order.id,
        idempotency_key=f"cloud-order-confirmation-renewed:{order.id}:{int(now.timestamp())}",
        payload={
            "previousExpiresAt": _iso_utc(previous_expiry),
            "confirmationExpiresAt": _iso_utc(renewed_expiry),
            "amount": _money(confirmed_amount),
            "currency": order.currency,
            "providerQuoteId": refreshed_quote.provider_quote_id,
        },
    )
    session.flush()
    return _order_view(
        order,
        token=_confirmation_token(settings, order),
        phrase=_confirmation_phrase(order),
    )


def _upsert_provisioned_target(session: Session, order: CloudOrderRecord, instance: Any) -> None:
    target_id = cloud_target_id(order.provider, instance.region, instance.id)
    record = session.get(TargetRecord, target_id)
    if record is None:
        for legacy_id in legacy_cloud_target_ids(order.provider, instance.region, instance.id):
            record = session.get(TargetRecord, legacy_id)
            if record is not None:
                break
    now = utc_now()
    inventory = {
        "source": "cloud-order",
        "order_id": order.id,
        "provider_instance_id": instance.id,
        "region": instance.region,
        "zone": instance.zone,
        "status": instance.status,
        "private_ip": instance.private_ip,
        "public_ip_present": instance.public_ip_present,
    }
    fingerprint = {
        "provider": order.provider,
        "region": instance.region,
        "zone": instance.zone,
        "instance_id": instance.id,
        "instance_type": order.spec_json["instance_type"],
        "image_id": order.spec_json["image_id"],
    }
    values = {
        "name": instance.name or instance.id,
        "provider": order.provider,
        "status": "inventory-only"
        if str(instance.status).upper() == "RUNNING"
        else "provisioning",
        "capabilities_json": [order.provider, "cloud-instance", "inventory"],
        "inventory_json": inventory,
        "fingerprint_json": fingerprint,
        "snapshot_digest": canonical_digest({"fingerprint": fingerprint, "inventory": inventory}),
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
        session.add(TargetRecord(id=target_id, created_at=now, **values))
    else:
        for field, value in values.items():
            setattr(record, field, value)


def confirm_order(
    session: Session,
    settings: Settings,
    registry: CloudProviderRegistry,
    order_id: str,
    request: OrderConfirmRequest,
) -> dict[str, Any]:
    order = session.get(CloudOrderRecord, order_id)
    if order is None:
        raise CloudWorkflowError("order not found", status_code=404, code="order_not_found")
    if order.status in {"submitted", "succeeded"}:
        return _order_view(order)
    if order.status == "submitting":
        raise CloudWorkflowError(
            "order submission is already in progress; do not retry with a new token",
            code="submission_in_progress",
        )
    if order.status == "failed":
        raise CloudWorkflowError(
            "failed orders are terminal; request a fresh quote before another attempt",
            code="fresh_quote_required",
        )
    if order.status != "awaiting_confirmation":
        raise CloudWorkflowError(
            f"order cannot be confirmed from {order.status}", code="invalid_order_state"
        )
    token_payload = _token_verify(settings, request.confirmation_token)
    if (
        token_payload.get("orderId") != order.id
        or token_payload.get("quoteDigest") != order.quote_digest
    ):
        raise CloudWorkflowError(
            "confirmation token does not match this order", code="invalid_confirmation"
        )
    confirmation_expires = _aware(order.confirmation_expires_at)
    if (
        float(token_payload.get("expires", 0)) < utc_now().timestamp()
        or confirmation_expires <= utc_now()
    ):
        order.status = "expired"
        session.commit()
        raise CloudWorkflowError("confirmation window has expired", code="confirmation_expired")
    if _hash_phrase(request.acknowledgement) != order.confirmation_phrase_hash:
        raise CloudWorkflowError(
            "acknowledgement text does not match", code="acknowledgement_mismatch"
        )
    if request.expected_hourly_amount != Decimal(str(order.hourly_amount)):
        raise CloudWorkflowError(
            "hourly amount changed; request a fresh quote", code="amount_mismatch"
        )
    quote = session.get(CloudQuoteRecord, order.quote_id)
    if quote is None:
        raise CloudWorkflowError("bound quote no longer exists", code="quote_binding_invalid")
    if quote.status not in {"valid", "expired"}:
        raise CloudWorkflowError(
            "bound quote is no longer confirmable", code="fresh_quote_required"
        )
    if (
        quote.provider != order.provider
        or quote.spec_digest != order.spec_digest
        or quote.quote_digest != order.quote_digest
        or Decimal(str(quote.hourly_amount)) != Decimal(str(order.hourly_amount))
        or quote.currency != order.currency
    ):
        raise CloudWorkflowError(
            "order no longer matches its immutable quote",
            code="quote_binding_invalid",
        )
    provider_id = ProviderId(order.provider)
    if not provider_enabled(settings, provider_id):
        raise CloudWorkflowError(
            "live purchase is disabled; enable the server-side purchase gate "
            "and provider explicitly",
            code="live_purchase_disabled",
        )
    if Decimal(str(order.hourly_amount)) > settings.max_live_hourly_amount:
        raise CloudWorkflowError(
            "quote exceeds the configured live hourly spend limit", code="spend_limit"
        )

    provider = registry.get(provider_id)
    provider_status = provider.info(live_purchase_enabled=True)
    if not provider_status.live_purchase_enabled:
        raise CloudWorkflowError(
            provider_status.message or "provider purchase path is not ready",
            code="provider_purchase_not_ready",
        )
    spec = CloudPurchaseSpec.model_validate(order.spec_json)
    refreshed_quote = provider.quote(spec)
    refreshed_amount = Decimal(str(refreshed_quote.amount))
    confirmed_amount = Decimal(str(order.hourly_amount))
    if (
        refreshed_quote.estimated
        or _aware(refreshed_quote.expires_at) <= utc_now()
        or refreshed_amount != confirmed_amount
        or refreshed_quote.currency != order.currency
    ):
        quote.status = "superseded"
        order.status = "expired"
        order.updated_at = utc_now()
        append_event(
            session,
            experiment_id=None,
            event_type="cloud.order.price_changed",
            entity_type="cloud_order",
            entity_id=order.id,
            idempotency_key=f"cloud-order-price-changed:{order.id}",
            payload={
                "confirmedAmount": _money(confirmed_amount),
                "confirmedCurrency": order.currency,
                "currentAmount": _money(refreshed_amount),
                "currentCurrency": refreshed_quote.currency,
                "currentProviderQuoteId": refreshed_quote.provider_quote_id,
                "estimated": refreshed_quote.estimated,
            },
        )
        session.commit()
        raise CloudWorkflowError(
            "provider price changed or is no longer exact; request and confirm a fresh quote",
            code="price_changed",
        )
    if confirmation_expires <= utc_now():
        order.status = "expired"
        order.updated_at = utc_now()
        session.commit()
        raise CloudWorkflowError(
            "confirmation expired while refreshing the provider price",
            code="confirmation_expired",
        )

    submitted_at = utc_now()
    claimed = session.execute(
        update(CloudOrderRecord)
        .where(
            CloudOrderRecord.id == order.id,
            CloudOrderRecord.status == "awaiting_confirmation",
        )
        .values(
            status="submitting",
            confirmed_at=submitted_at,
            submitted_at=submitted_at,
            updated_at=submitted_at,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        session.rollback()
        current = session.get(CloudOrderRecord, order_id)
        if current and current.status in {"submitted", "succeeded"}:
            return _order_view(current)
        raise CloudWorkflowError(
            "order submission is already in progress; do not retry with a new token",
            code="submission_in_progress",
        )
    session.commit()
    session.refresh(order)

    try:
        result = provider.purchase(spec, client_token=order.client_token)
    except CloudProviderError as error:
        session.refresh(order)
        order.status = "unknown" if error.ambiguous else "failed"
        order.error_code = error.code
        order.error_message = str(error)
        order.provider_response_json = error.details
        order.updated_at = utc_now()
        append_event(
            session,
            experiment_id=None,
            event_type="cloud.order.unknown" if error.ambiguous else "cloud.order.failed",
            entity_type="cloud_order",
            entity_id=order.id,
            idempotency_key=f"cloud-order-result:{order.id}:{order.status}",
            payload={"code": error.code, "message": str(error), "ambiguous": error.ambiguous},
        )
        session.commit()
        raise CloudWorkflowError(
            "云厂商调用结果不明确，请先查询订单和云账户，不要重复购买"
            if error.ambiguous
            else str(error),
            code="purchase_ambiguous" if error.ambiguous else error.code,
        ) from error
    except Exception as error:
        session.refresh(order)
        order.status = "unknown"
        order.error_code = "unexpected_provider_error"
        order.error_message = str(error)
        order.updated_at = utc_now()
        session.commit()
        raise CloudWorkflowError(
            "云厂商调用结果不明确，请先查询订单和云账户，不要重复购买",
            code="purchase_ambiguous",
        ) from error

    session.refresh(order)
    order.status = "submitted"
    order.provider_order_id = result.provider_order_id
    order.provider_instance_ids_json = [item.id for item in result.instances]
    order.provider_response_json = result.model_dump(mode="json", by_alias=True)
    order.error_code = None
    order.error_message = None
    order.updated_at = utc_now()
    for instance in result.instances:
        _upsert_provisioned_target(session, order, instance)
    append_event(
        session,
        experiment_id=None,
        event_type="cloud.order.submitted",
        entity_type="cloud_order",
        entity_id=order.id,
        idempotency_key=f"cloud-order-result:{order.id}:submitted",
        payload={
            "providerOrderId": result.provider_order_id,
            "instanceIds": order.provider_instance_ids_json,
        },
    )
    session.commit()
    return _order_view(order)


def recover_interrupted_orders(session: Session) -> int:
    orders = list(
        session.scalars(select(CloudOrderRecord).where(CloudOrderRecord.status == "submitting"))
    )
    now = utc_now()
    for order in orders:
        order.status = "unknown"
        order.error_code = "control_plane_restarted"
        order.error_message = (
            "control plane restarted during provider submission; reconcile with the provider "
            "using the persisted client token"
        )
        order.updated_at = now
        append_event(
            session,
            experiment_id=None,
            event_type="cloud.order.unknown",
            entity_type="cloud_order",
            entity_id=order.id,
            idempotency_key=f"cloud-order-recovered:{order.id}",
            payload={"code": order.error_code, "ambiguous": True},
        )
    return len(orders)


def resolve_unknown_order(
    session: Session,
    order_id: str,
    request: OrderResolveRequest,
) -> dict[str, Any]:
    order = session.get(CloudOrderRecord, order_id)
    if order is None:
        raise CloudWorkflowError("order not found", status_code=404, code="order_not_found")
    if order.status != "unknown":
        raise CloudWorkflowError(
            "only unknown orders can be reconciled manually",
            code="invalid_order_state",
        )
    now = utc_now()
    resolution = {
        "resolution": request.resolution,
        "note": request.note,
        "resolvedAt": now.isoformat(),
    }
    if request.resolution == "submitted":
        order.status = "submitted"
        order.provider_order_id = request.provider_order_id
        order.provider_instance_ids_json = request.instance_ids
        order.error_code = None
        order.error_message = None
        for instance_id in request.instance_ids:
            _upsert_provisioned_target(
                session,
                order,
                ProvisionedInstance(
                    id=instance_id,
                    name=order.spec_json["instance_name"],
                    region=order.spec_json["region"],
                    zone=order.spec_json["zone"],
                    status="MANUALLY_RECONCILED",
                ),
            )
    else:
        order.status = "failed"
        order.error_code = "manual_reconciled_not_created"
        order.error_message = request.note
    order.provider_response_json = {
        **(order.provider_response_json or {}),
        "manualResolution": resolution,
    }
    order.updated_at = now
    append_event(
        session,
        experiment_id=None,
        event_type="cloud.order.reconciled",
        entity_type="cloud_order",
        entity_id=order.id,
        idempotency_key=f"cloud-order-reconciled:{order.id}",
        payload={
            "resolution": request.resolution,
            "instanceIds": request.instance_ids,
            "providerOrderId": request.provider_order_id,
            "note": request.note,
        },
    )
    session.flush()
    return _order_view(order)


def get_quote(session: Session, quote_id: str) -> dict[str, Any]:
    quote = session.get(CloudQuoteRecord, quote_id)
    if quote is None:
        raise CloudWorkflowError("quote not found", status_code=404, code="quote_not_found")
    if quote.status == "valid" and _aware(quote.expires_at) <= utc_now():
        quote.status = "expired"
        session.flush()
    return _quote_view(quote)


def get_order(session: Session, settings: Settings, order_id: str) -> dict[str, Any]:
    order = session.get(CloudOrderRecord, order_id)
    if order is None:
        raise CloudWorkflowError("order not found", status_code=404, code="order_not_found")
    if order.status == "awaiting_confirmation":
        if _aware(order.confirmation_expires_at) <= utc_now():
            order.status = "expired"
            order.updated_at = utc_now()
            session.flush()
            return _order_view(order)
        return _order_view(
            order,
            token=_confirmation_token(settings, order),
            phrase=_confirmation_phrase(order),
        )
    return _order_view(order)


def list_orders(
    session: Session, *, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    statement = select(CloudOrderRecord).order_by(CloudOrderRecord.created_at.desc()).limit(limit)
    if status:
        statement = statement.where(CloudOrderRecord.status == status)
    return [_order_view(item) for item in session.scalars(statement)]


def _event_view(record: EventRecord, *, include_idempotency_key: bool = False) -> dict[str, Any]:
    result = {
        "id": record.id,
        "sequence": record.sequence,
        "eventType": record.event_type,
        "entityType": record.entity_type,
        "entityId": record.entity_id,
        "payload": record.payload_json,
        "createdAt": _iso_utc(record.created_at),
    }
    if include_idempotency_key:
        result["idempotencyKey"] = record.idempotency_key
    return result


def _order_events(session: Session, order: CloudOrderRecord) -> list[EventRecord]:
    statement = (
        select(EventRecord)
        .where(
            or_(
                (EventRecord.entity_type == "cloud_order") & (EventRecord.entity_id == order.id),
                (EventRecord.entity_type == "cloud_quote")
                & (EventRecord.entity_id == order.quote_id),
            )
        )
        .order_by(EventRecord.created_at, EventRecord.sequence, EventRecord.id)
    )
    return list(session.scalars(statement))


def list_order_events(session: Session, order_id: str) -> list[dict[str, Any]]:
    order = session.get(CloudOrderRecord, order_id)
    if order is None:
        raise CloudWorkflowError("order not found", status_code=404, code="order_not_found")
    return [_event_view(event) for event in _order_events(session, order)]


def get_order_reconciliation_context(session: Session, order_id: str) -> dict[str, Any]:
    order = session.get(CloudOrderRecord, order_id)
    if order is None:
        raise CloudWorkflowError("order not found", status_code=404, code="order_not_found")
    if order.status != "unknown":
        raise CloudWorkflowError(
            "reconciliation context is only available for unknown orders",
            code="invalid_order_state",
        )
    return {
        "orderId": order.id,
        "provider": order.provider,
        "status": order.status,
        "clientToken": order.client_token,
        "providerOrderId": order.provider_order_id,
        "providerRequestId": order.provider_response_json.get("requestId"),
        "instanceIds": order.provider_instance_ids_json,
        "instanceName": order.spec_json.get("instanceName"),
        "region": order.spec_json.get("region"),
        "submittedAt": _iso_utc(order.submitted_at) if order.submitted_at else None,
        "createdAt": _iso_utc(order.created_at),
    }


def get_order_evidence(session: Session, order_id: str) -> dict[str, Any]:
    order = session.get(CloudOrderRecord, order_id)
    if order is None:
        raise CloudWorkflowError("order not found", status_code=404, code="order_not_found")
    quote = session.get(CloudQuoteRecord, order.quote_id)
    if quote is None:
        raise CloudWorkflowError(
            "order references a missing quote", status_code=409, code="quote_not_found"
        )
    order_payload = _order_view(order)
    client_token_hash = hashlib.sha256(order.client_token.encode("utf-8")).hexdigest()
    order_payload.update(
        {
            "idempotencyKey": order.idempotency_key,
            "clientTokenDigest": f"sha256:{client_token_hash}",
            "confirmationPhraseHash": order.confirmation_phrase_hash,
        }
    )
    quote_payload = _quote_view(quote)
    quote_payload["idempotencyKey"] = quote.idempotency_key
    manifest = {
        "schemaVersion": "looper.cloud-order-evidence/v1",
        "generatedAt": utc_now().isoformat(),
        "quote": quote_payload,
        "order": order_payload,
        "events": [
            _event_view(event, include_idempotency_key=True)
            for event in _order_events(session, order)
        ],
    }
    return {**manifest, "evidenceDigest": canonical_digest(manifest)}


def global_search(session: Session, query: str, limit: int = 30) -> list[dict[str, Any]]:
    needle = query.strip()
    if not needle:
        return []
    pattern = f"%{needle}%"
    results: list[SearchResult] = []
    for item in session.scalars(
        select(ExperimentRecord)
        .where(ExperimentRecord.name.ilike(pattern) | ExperimentRecord.id.ilike(pattern))
        .order_by(ExperimentRecord.updated_at.desc())
        .limit(limit)
    ):
        results.append(
            SearchResult(
                type="experiment",
                id=item.id,
                title=item.name,
                subtitle=item.description,
                status=item.status,
                url=f"/experiments/{item.id}",
                updatedAt=item.updated_at,
            )
        )
    for item in session.scalars(
        select(BenchmarkRecord)
        .where(BenchmarkRecord.name.ilike(pattern) | BenchmarkRecord.benchmark_id.ilike(pattern))
        .order_by(BenchmarkRecord.installed_at.desc())
        .limit(limit)
    ):
        results.append(
            SearchResult(
                type="benchmark",
                id=item.key,
                title=item.name,
                subtitle=f"{item.benchmark_id}@{item.version}",
                status="trusted" if item.trusted else "review",
                url="/benchmarks",
                updatedAt=item.installed_at,
            )
        )
    for item in session.scalars(
        select(TargetRecord)
        .where(TargetRecord.name.ilike(pattern) | TargetRecord.id.ilike(pattern))
        .order_by(TargetRecord.updated_at.desc())
        .limit(limit)
    ):
        results.append(
            SearchResult(
                type="target",
                id=item.id,
                title=item.name,
                subtitle=item.provider,
                status=item.status,
                url="/targets",
                updatedAt=item.updated_at,
            )
        )
    for item in session.scalars(
        select(CloudQuoteRecord)
        .where(
            CloudQuoteRecord.id.ilike(pattern)
            | CloudQuoteRecord.provider.ilike(pattern)
            | cast(CloudQuoteRecord.spec_json, Text).ilike(pattern)
        )
        .order_by(CloudQuoteRecord.created_at.desc())
        .limit(limit)
    ):
        results.append(
            SearchResult(
                type="quote",
                id=item.id,
                title=f"报价 {item.provider} {_money(item.hourly_amount)} {item.currency}/小时",
                subtitle=item.spec_json.get("instance_name"),
                status=item.status,
                url=f"/cloud/quotes/{item.id}",
                updatedAt=item.created_at,
            )
        )
    for item in session.scalars(
        select(CloudOrderRecord)
        .where(
            CloudOrderRecord.id.ilike(pattern)
            | CloudOrderRecord.provider.ilike(pattern)
            | cast(CloudOrderRecord.spec_json, Text).ilike(pattern)
        )
        .order_by(CloudOrderRecord.updated_at.desc())
        .limit(limit)
    ):
        results.append(
            SearchResult(
                type="order",
                id=item.id,
                title=f"订单 {item.provider} {item.status}",
                subtitle=item.spec_json.get("instance_name"),
                status=item.status,
                url=f"/cloud/orders/{item.id}",
                updatedAt=item.updated_at,
            )
        )
    return [item.model_dump(mode="json", by_alias=True) for item in results[:limit]]
