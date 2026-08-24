from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from looper_api.cloud_contracts import CatalogFilters, ImageInfo, InstanceTypeInfo


def sdk_installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _explicit_environment(names: list[str]) -> dict[str, str]:
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if not missing:
        return values
    env_file = Path(os.environ.get("LOOPER_ENV_FILE", ".env"))
    if not env_file.is_file():
        return values
    file_values = dotenv_values(env_file)
    for name in missing:
        values[name] = str(file_values.get(name) or "").strip()
    return values


def environment_credentials(required: list[str]) -> tuple[dict[str, str], list[str]]:
    values = _explicit_environment(required)
    missing = [name for name, value in values.items() if not value]
    return values, missing


def cloud_target_id(provider: str, region: str, instance_id: str) -> str:
    return f"cloud:{provider}:{region}:{instance_id}"


def has_online_worker_for_target(session: Any, target_id: str) -> bool:
    """Return True when a live Worker currently binds this target.

    Inventory syncs are authoritative for provider facts but must not reset a
    target to not-runnable while its worker is online and registered.
    """
    from sqlalchemy import select

    from looper_api.models import WorkerRecord

    capability = f'target.{target_id}'
    match = session.scalars(
        select(WorkerRecord.id).where(
            WorkerRecord.status == "online",
            WorkerRecord.capabilities_json.like(f'%"{capability}"%'),
        )
    ).first()
    return match is not None


def legacy_cloud_target_ids(provider: str, region: str, instance_id: str) -> list[str]:
    values = [f"{provider}:{instance_id}"]
    if provider == "tencent":
        values.append(f"cvm:{region}:{instance_id}")
    return values


def optional_environment(name: str) -> str | None:
    return _explicit_environment([name])[name] or None


def ambiguous_create_error(provider_code: Any, error: Exception) -> bool:
    if provider_code is None:
        return True
    text = f"{provider_code} {error.__class__.__name__} {error}".casefold()
    markers = (
        "timeout",
        "internalerror",
        "serviceunavailable",
        "serverunavailable",
        "clientnetworkerror",
        "networkerror",
        "connectionerror",
        "connectionreset",
        "gatewayerror",
        "badgateway",
        "eof",
        "http500",
        "http502",
        "http503",
        "http504",
    )
    normalized = "".join(character for character in text if character.isalnum())
    return any(marker in normalized for marker in markers)


def attr(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def nested(value: Any, *path: tuple[str, ...], default: Any = None) -> Any:
    current = value
    for alternatives in path:
        current = attr(current, *alternatives, default=None)
        if current is None:
            return default
    return current


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def to_plain(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "<maximum-depth>"
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_plain(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [to_plain(item, depth=depth + 1) for item in value]
    if hasattr(value, "to_map"):
        return to_plain(value.to_map(), depth=depth + 1)
    if hasattr(value, "to_json_string"):
        try:
            import json

            return to_plain(json.loads(value.to_json_string()), depth=depth + 1)
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            str(key).lstrip("_"): to_plain(item, depth=depth + 1)
            for key, item in vars(value).items()
            if not str(key).startswith("__")
        }
    return str(value)


def decimal_value(value: Any, *, default: Decimal | None = None) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        if default is not None:
            return default
        raise ValueError(f"invalid decimal value: {value}") from None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def instance_type_family_token(item: InstanceTypeInfo) -> str:
    source = (item.family or item.id).strip()
    if source.casefold().startswith("ecs."):
        source = source[4:]
    return source.split(".", 1)[0]


def instance_type_family_kind(item: InstanceTypeInfo) -> str:
    token = instance_type_family_token(item).casefold()
    if item.provider.value == "alibaba":
        for prefix, kind in (
            (("ebmgn", "vgn", "gn", "ga"), "heterogeneous"),
            (("hfc",), "compute"),
            (("s",), "shared"),
            (("t",), "burstable"),
            (("e",), "economy"),
            (("u",), "universal"),
            (("g",), "general"),
            (("c",), "compute"),
            (("r",), "memory"),
            (("i",), "storage"),
            (("d",), "big-data"),
        ):
            if token.startswith(prefix):
                return kind
        return "other"

    if item.provider.value != "tencent":
        return "other"
    type_name = str(item.attributes.get("typeName") or "").casefold()
    upper = token.upper()
    if upper.startswith(("GN", "GI", "GA", "GT", "PNV", "GPU")) or "gpu" in type_name:
        return "heterogeneous"
    if upper.startswith(("IT", "IA")) or any(
        marker in type_name for marker in ("高io", "高 io", "存储")
    ):
        return "storage"
    if upper.startswith(("MA", "M")) or "内存" in type_name:
        return "memory"
    if upper.startswith("D") or "大数据" in type_name:
        return "big-data"
    if upper.startswith(("CN", "C")) or "计算" in type_name:
        return "compute"
    if "NE" in upper or "网络" in type_name:
        return "network"
    if upper.startswith("B") or "批量" in type_name:
        return "batch"
    if upper.startswith(("SA", "SR", "S")) or "标准" in type_name:
        return "standard"
    return "other"


def instance_type_labels(item: InstanceTypeInfo) -> tuple[str, str]:
    token = instance_type_family_token(item) or item.family or item.id
    if item.provider.value not in {"alibaba", "tencent"}:
        return "", ""
    kind = instance_type_family_kind(item)
    if item.provider.value == "alibaba":
        labels = {
            "shared": ("共享型", "共享标准型"),
            "burstable": ("突发性能型", "突发性能型"),
            "economy": ("经济型", "经济型"),
            "universal": ("通用算力型", "通用算力型"),
            "general": ("通用型", "通用型"),
            "compute": (
                "计算型",
                "高主频计算型" if token.casefold().startswith("hfc") else "计算型",
            ),
            "memory": ("内存型", "内存型"),
            "storage": ("本地存储型", "本地 SSD 型"),
            "big-data": ("大数据型", "大数据型"),
            "heterogeneous": ("GPU/异构型", "GPU/异构计算型"),
        }
    else:
        labels = {
            "standard": ("标准型", "标准型"),
            "compute": ("计算型", "计算型"),
            "memory": ("内存型", "内存型"),
            "storage": ("存储型", "高 IO/存储型"),
            "big-data": ("大数据型", "大数据型"),
            "network": ("网络型", "网络优化型"),
            "batch": ("批量型", "批量计算型"),
            "heterogeneous": ("GPU/异构型", "GPU/异构计算型"),
        }
    if kind not in labels:
        return "其他类型", f"规格族 {token}"
    type_label, family_prefix = labels[kind]
    type_name = str(item.attributes.get("typeName") or "").strip()
    if item.provider.value == "tencent" and type_name:
        family_label = (
            type_name if token.casefold() in type_name.casefold() else f"{type_name} {token}"
        )
    else:
        family_label = f"{family_prefix} {token}"
    return type_label, family_label


def enrich_instance_type_labels(item: InstanceTypeInfo) -> InstanceTypeInfo:
    type_label, family_label = instance_type_labels(item)
    if not type_label or not family_label:
        return item
    return item.model_copy(update={"type_label": type_label, "family_label": family_label})


def instance_type_search_text(item: InstanceTypeInfo) -> str:
    type_label, family_label = instance_type_labels(item)
    return " ".join(
        value
        for value in (
            item.id,
            item.family or "",
            item.architecture or "",
            item.type_label or type_label,
            item.family_label or family_label,
            str(item.attributes.get("typeName") or ""),
        )
        if value
    ).casefold()


def filter_instance_types(
    items: list[InstanceTypeInfo], filters: CatalogFilters
) -> list[InstanceTypeInfo]:
    query = (filters.query or "").casefold()
    result = []
    for original in items:
        item = enrich_instance_type_labels(original)
        text = instance_type_search_text(item)
        if query and query not in text:
            continue
        if filters.zone and item.zones and filters.zone not in item.zones:
            continue
        if filters.min_cpu is not None and item.cpu < filters.min_cpu:
            continue
        if filters.max_cpu is not None and item.cpu > filters.max_cpu:
            continue
        if filters.min_memory_gib is not None and item.memory_gib < filters.min_memory_gib:
            continue
        if filters.max_memory_gib is not None and item.memory_gib > filters.max_memory_gib:
            continue
        result.append(item)
    return result


def filter_images(items: list[ImageInfo], filters: CatalogFilters) -> list[ImageInfo]:
    query = (filters.query or "").casefold()
    platform = (filters.platform or "").casefold()
    image_type = (filters.image_type or "").casefold()
    result = []
    for item in items:
        text = f"{item.id} {item.name} {item.platform or ''}".casefold()
        if query and query not in text:
            continue
        if platform and platform not in (item.platform or "").casefold():
            continue
        if image_type and image_type != (item.image_type or "").casefold():
            continue
        result.append(item)
    return result
