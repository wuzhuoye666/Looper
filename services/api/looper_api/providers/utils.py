from __future__ import annotations

import importlib.util
import os
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from looper_api.cloud_contracts import (
    CatalogFilters,
    ImageInfo,
    InstanceTypeArchitectureFacet,
    InstanceTypeFacets,
    InstanceTypeFamilyFacet,
    InstanceTypeInfo,
    InstanceTypeKindFacet,
)


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
        if token.startswith("scc"):
            return "hpc"
        if token.startswith("ebm"):
            return "bare-metal"
        if token.startswith(("hfc", "hfg", "hfr")):
            return "high-frequency"
        if token.startswith(("g", "c", "r")) and re.search(r"ne(?:x)?$", token):
            return "network-enhanced"
        if token.startswith(("g", "c", "r")) and token.endswith("se"):
            return "storage-enhanced"
        if token.startswith(("g", "c", "r")) and re.search(r"\d+t$", token):
            return "security-enhanced"
        if token.startswith("re"):
            return "memory-enhanced"
        for prefix, kind in (
            (("ebmgn", "vgn", "gn", "ga"), "heterogeneous"),
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
    if upper.startswith(("HCC", "HPC", "SCC")) or any(
        marker in type_name for marker in ("高性能计算集群", "超级计算集群")
    ):
        return "hpc"
    if upper.startswith(("BMS", "CBM", "BM")) or "裸金属" in type_name:
        return "bare-metal"
    if upper.startswith(("GN", "GI", "GA", "GT", "PNV", "GPU")) or "gpu" in type_name:
        return "heterogeneous"
    if "高主频" in type_name:
        return "high-frequency"
    if "安全" in type_name:
        return "security-enhanced"
    if "存储增强" in type_name:
        return "storage-enhanced"
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
            "high-frequency": ("高主频型", "高主频型"),
            "memory": ("内存型", "内存型"),
            "memory-enhanced": ("内存增强型", "内存增强型"),
            "storage": ("本地存储型", "本地 SSD 型"),
            "big-data": ("大数据型", "大数据型"),
            "network-enhanced": ("网络增强型", "网络增强型"),
            "storage-enhanced": ("存储增强型", "存储增强型"),
            "security-enhanced": ("安全增强型", "安全增强型"),
            "heterogeneous": ("GPU/异构型", "GPU/异构计算型"),
            "bare-metal": ("裸金属型", "弹性裸金属型"),
            "hpc": ("高性能计算型", "高性能计算集群"),
        }
    else:
        labels = {
            "standard": ("标准型", "标准型"),
            "compute": ("计算型", "计算型"),
            "high-frequency": ("高主频型", "高主频型"),
            "memory": ("内存型", "内存型"),
            "storage": ("存储型", "高 IO/存储型"),
            "big-data": ("大数据型", "大数据型"),
            "network": ("网络型", "网络优化型"),
            "storage-enhanced": ("存储增强型", "存储增强型"),
            "security-enhanced": ("安全增强型", "安全增强型"),
            "batch": ("批量型", "批量计算型"),
            "heterogeneous": ("GPU/异构型", "GPU/异构计算型"),
            "bare-metal": ("裸金属型", "裸金属服务器"),
            "hpc": ("高性能计算型", "高性能计算集群"),
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
    if not type_label or not family_label or item.provider.value not in {"alibaba", "tencent"}:
        return item
    token = instance_type_family_token(item)
    kind = instance_type_family_kind(item)
    type_name = str(item.attributes.get("typeName") or "").casefold()
    normalized_architecture = (item.architecture or "").casefold().replace("-", "_")
    if kind == "hpc":
        selection_class = "hpc"
    elif kind == "bare-metal":
        selection_class = "bare-metal"
    elif kind == "heterogeneous" or (item.gpu or 0) > 0 or "gpu" in type_name:
        selection_class = "heterogeneous"
    elif "arm" in normalized_architecture or "aarch" in normalized_architecture:
        selection_class = "arm"
    elif any(
        marker in normalized_architecture for marker in ("x86", "amd64", "i386", "i686")
    ):
        selection_class = "x86"
    else:
        selection_class = "other"
    return item.model_copy(
        update={
            "type_label": type_label,
            "family_label": family_label,
            "selection_class": selection_class,
            "type_kind": kind,
            "family_token": token,
        }
    )


_SELECTION_CLASS_LABELS = {
    "x86": "X86 计算",
    "arm": "ARM 计算",
    "heterogeneous": "异构计算",
    "bare-metal": "裸金属服务器",
    "hpc": "高性能计算集群",
    "other": "其他架构",
}
_SELECTION_CLASS_ORDER = tuple(_SELECTION_CLASS_LABELS)
_TYPE_KIND_ORDER = (
    "standard", "shared", "burstable", "economy", "universal", "general", "compute",
    "memory", "high-frequency", "big-data", "storage", "memory-enhanced",
    "network", "network-enhanced", "storage-enhanced", "security-enhanced", "batch",
    "heterogeneous", "bare-metal", "hpc", "other",
)


def instance_type_generation(item: InstanceTypeInfo) -> int | None:
    match = re.search(r"\d+", item.family_token or instance_type_family_token(item))
    return int(match.group()) if match else None


def _natural_text_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )


def build_instance_type_facets(items: list[InstanceTypeInfo]) -> InstanceTypeFacets:
    enriched = [enrich_instance_type_labels(item) for item in items]
    architectures: list[InstanceTypeArchitectureFacet] = []
    for architecture in _SELECTION_CLASS_ORDER:
        architecture_items = [item for item in enriched if item.selection_class == architecture]
        if not architecture_items:
            continue
        kinds: list[InstanceTypeKindFacet] = []
        available_kinds = {item.type_kind or "other" for item in architecture_items}
        ordered_kinds = [kind for kind in _TYPE_KIND_ORDER if kind in available_kinds]
        ordered_kinds.extend(sorted(available_kinds.difference(ordered_kinds)))
        for kind in ordered_kinds:
            kind_items = [
                item for item in architecture_items if (item.type_kind or "other") == kind
            ]
            family_groups: dict[str, list[InstanceTypeInfo]] = {}
            for item in kind_items:
                family_groups.setdefault(
                    item.family_token or instance_type_family_token(item), []
                ).append(item)
            families = [
                InstanceTypeFamilyFacet(
                    value=token,
                    label=group[0].family_label or f"规格族 {token}",
                    count=len(group),
                    generation=instance_type_generation(group[0]),
                )
                for token, group in family_groups.items()
            ]
            families.sort(
                key=lambda facet: (
                    facet.generation is None,
                    -(facet.generation or 0),
                    _natural_text_key(facet.value),
                )
            )
            kinds.append(
                InstanceTypeKindFacet(
                    value=kind,
                    label=kind_items[0].type_label or "其他类型",
                    count=len(kind_items),
                    families=families,
                )
            )
        architectures.append(
            InstanceTypeArchitectureFacet(
                value=architecture,
                label=_SELECTION_CLASS_LABELS[architecture],
                count=len(architecture_items),
                types=kinds,
            )
        )
    return InstanceTypeFacets(architectures=architectures)


def matches_instance_type_facets(item: InstanceTypeInfo, filters: CatalogFilters) -> bool:
    enriched = enrich_instance_type_labels(item)
    return not (
        (filters.architecture_class and enriched.selection_class != filters.architecture_class)
        or (filters.type_kind and enriched.type_kind != filters.type_kind)
        or (
            filters.family_token
            and (enriched.family_token or "").casefold()
            != filters.family_token.casefold()
        )
    )


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
        if not matches_instance_type_facets(item, filters):
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
