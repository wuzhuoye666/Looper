from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from itertools import product
from typing import Literal

from pydantic import Field, model_validator

from looper_api.cloud_contracts import (
    ApiModel,
    CatalogFilters,
    InstanceSelectionClass,
    InstanceTypeFacets,
    InstanceTypeInfo,
    ProviderId,
)
from looper_api.providers.utils import (
    build_instance_type_facets,
    enrich_instance_type_labels,
    instance_type_family_token,
    instance_type_search_text,
    matches_instance_type_facets,
)
from looper_api.selection_pricing import PriceInfo

ScenarioId = Literal[
    "web-api",
    "microservices-rpc",
    "database",
    "cache",
    "search-logs",
    "big-data-messaging",
    "game",
    "video",
    "ai",
    "development-test",
    "other",
]

SCENARIO_LABELS: dict[str, str] = {
    "web-api": "Web / API",
    "microservices-rpc": "微服务 / RPC",
    "database": "数据库",
    "cache": "缓存",
    "search-logs": "搜索与日志",
    "big-data-messaging": "大数据与消息",
    "game": "游戏",
    "video": "视频",
    "ai": "AI",
    "development-test": "开发测试",
    "other": "其他",
}


class SelectionAdvisorRequest(ApiModel):
    provider: Literal["alibaba", "tencent"] = "alibaba"
    region: str = Field(min_length=2, max_length=64)
    zone: str | None = Field(default=None, max_length=64)
    primary_scenario: ScenarioId
    co_located_components: list[ScenarioId] = Field(default_factory=list, max_length=5)
    sizing_mode: Literal["exact", "unknown"] = "unknown"
    exact_cpu: int | None = Field(default=None, ge=1, le=1024)
    exact_memory_gib: float | None = Field(default=None, ge=0.25, le=65536)
    workload_scale: str | None = Field(default=None, max_length=160)
    minimum_gpu_count: float = Field(default=0, ge=0, le=64)
    local_storage: Literal["required", "not-required", "unknown"] = "unknown"
    minimum_network_bandwidth_gbps: float | None = Field(default=None, ge=0, le=10000)
    minimum_network_pps: int | None = Field(default=None, ge=0, le=2_000_000_000)
    code_availability: Literal["available", "unavailable", "unknown"] = "unknown"
    architecture: Literal["x86", "arm", "unknown"] = "unknown"
    budget_monthly_cny: float | None = Field(default=None, ge=0, le=1_000_000)
    query: str | None = Field(default=None, max_length=120)
    architecture_class: InstanceSelectionClass | None = None
    type_kind: str | None = Field(default=None, max_length=60)
    family_token: str | None = Field(default=None, max_length=80)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_exact_sizing(self) -> SelectionAdvisorRequest:
        if self.sizing_mode == "exact" and (
            self.exact_cpu is None or self.exact_memory_gib is None
        ):
            raise ValueError("exact sizing requires both exactCpu and exactMemoryGib")
        if len(self.co_located_components) != len(set(self.co_located_components)):
            raise ValueError("co-located components must be unique")
        if self.primary_scenario in self.co_located_components:
            raise ValueError("primary scenario cannot also be a co-located component")
        return self


class ExclusionStage(ApiModel):
    code: str
    label: str
    before: int
    after: int
    removed: int


class AdvisedInstanceType(InstanceTypeInfo):
    match_tier: Literal["preferred", "suitable", "other"]
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    price: PriceInfo | None = None


class RecommendationScores(ApiModel):
    scenario_rank: int
    scenario_fit: float
    performance: float
    cost_efficiency: float
    cost_control: float
    category_score: float
    hourly_price: float
    value_per_yuan: float


class RecommendedInstanceType(ApiModel):
    category: Literal["balanced", "value", "performance"]
    label: str
    reason: str
    scores: RecommendationScores
    item: AdvisedInstanceType
    price: PriceInfo | None = None


class SelectionAdvisorResponse(ApiModel):
    provider: ProviderId
    region: str
    zone: str | None = None
    items: list[AdvisedInstanceType]
    total: int
    eligible_total: int
    offset: int
    limit: int
    next_offset: int | None = None
    exclusion_stages: list[ExclusionStage]
    most_restrictive_stage: ExclusionStage | None = None
    source: Literal["live", "cache", "stale-cache"]
    fetched_at: str
    expires_at: str
    stale: bool = False
    warning: str | None = None
    instance_type_facets: InstanceTypeFacets | None = None
    top_picks: list[RecommendedInstanceType] = Field(default_factory=list)


_ALIBABA_FAMILY_ORDERS: dict[str, list[set[str]]] = {
    "web-api": [{"g"}, {"c", "u"}],
    "microservices-rpc": [{"g", "network"}, {"c"}],
    "database": [{"i"}, {"r"}, {"g"}],
    "cache": [{"r"}, {"g", "i"}],
    "search-logs": [{"i"}, {"d"}, {"r"}, {"g"}],
    "big-data-messaging": [{"d"}, {"i"}, {"g"}],
    "game": [{"hfc"}, {"g"}],
    "video": [{"c"}, {"hfc"}, {"gn"}],
    "ai": [{"gn"}, {"c", "g"}],
    "development-test": [{"u", "e", "t"}, {"g"}],
    "other": [{"g", "u"}],
}

_TENCENT_FAMILY_ORDERS: dict[str, list[set[str]]] = {
    "web-api": [{"standard"}, {"compute", "network"}],
    "microservices-rpc": [{"standard", "network"}, {"compute"}],
    "database": [{"storage", "memory"}, {"standard"}],
    "cache": [{"memory"}, {"standard"}],
    "search-logs": [{"storage", "big-data", "memory"}, {"standard"}],
    "big-data-messaging": [{"big-data", "storage", "batch"}, {"standard"}],
    "game": [{"compute", "network"}, {"standard"}],
    "video": [{"compute", "network"}, {"heterogeneous"}, {"standard"}],
    "ai": [{"heterogeneous"}, {"compute", "standard"}],
    "development-test": [{"standard", "batch"}, {"compute"}],
    "other": [{"standard"}, {"compute", "memory"}],
}

_SCENARIO_RESOURCE_WEIGHTS: dict[str, dict[str, float]] = {
    "web-api": {"cpu": 0.35, "memory": 0.20, "bandwidth": 0.25, "pps": 0.20},
    "microservices-rpc": {"cpu": 0.30, "memory": 0.15, "bandwidth": 0.25, "pps": 0.30},
    "database": {
        "cpu": 0.25,
        "memory": 0.35,
        "local_storage": 0.25,
        "bandwidth": 0.10,
        "pps": 0.05,
    },
    "cache": {"cpu": 0.20, "memory": 0.55, "bandwidth": 0.10, "pps": 0.15},
    "search-logs": {
        "cpu": 0.20,
        "memory": 0.25,
        "local_storage": 0.30,
        "bandwidth": 0.15,
        "pps": 0.10,
    },
    "big-data-messaging": {
        "cpu": 0.25,
        "memory": 0.25,
        "local_storage": 0.25,
        "bandwidth": 0.20,
        "pps": 0.05,
    },
    "game": {"cpu": 0.35, "memory": 0.20, "bandwidth": 0.20, "pps": 0.25},
    "video": {"cpu": 0.25, "memory": 0.15, "gpu": 0.30, "bandwidth": 0.25, "pps": 0.05},
    "ai": {"cpu": 0.15, "memory": 0.20, "gpu": 0.50, "bandwidth": 0.10, "pps": 0.05},
    "development-test": {"cpu": 0.40, "memory": 0.40, "bandwidth": 0.10, "pps": 0.10},
    "other": {"cpu": 0.35, "memory": 0.30, "bandwidth": 0.20, "pps": 0.15},
}

_CATEGORY_WEIGHTS: dict[str, dict[str, float]] = {
    "balanced": {"scenario": 0.35, "performance": 0.25, "efficiency": 0.25, "cost": 0.15},
    "value": {"scenario": 0.20, "performance": 0.25, "efficiency": 0.45, "cost": 0.10},
    "performance": {"scenario": 0.20, "performance": 0.40, "efficiency": 0.30, "cost": 0.10},
}


@dataclass(frozen=True)
class _CandidateScore:
    item: InstanceTypeInfo
    scenario_rank: int
    scenario_fit: float
    performance: float
    hourly_price: float | None
    cost_efficiency: float
    cost_control: float
    category_scores: dict[str, float]


def _family_token(item: InstanceTypeInfo) -> str:
    return instance_type_family_token(item).casefold()


def _alibaba_family_kind(item: InstanceTypeInfo) -> str:
    token = _family_token(item)
    for prefix in ("hfc", "gn", "g", "c", "u", "i", "r", "d", "e", "t"):
        if token.startswith(prefix):
            return prefix
    return "other"


def _tencent_family_kind(item: InstanceTypeInfo) -> str:
    token = _family_token(item).upper()
    type_name = str(item.attributes.get("typeName") or "").casefold()
    if token.startswith(("GN", "GI", "GA", "GT", "PNV", "GPU")) or "gpu" in type_name:
        return "heterogeneous"
    if token.startswith(("IT", "IA")) or any(
        marker in type_name for marker in ("高io", "高 io", "存储")
    ):
        return "storage"
    if token.startswith(("MA", "M")) or "内存" in type_name:
        return "memory"
    if token.startswith("D") or "大数据" in type_name:
        return "big-data"
    if token.startswith(("CN", "C")) or "计算" in type_name:
        return "compute"
    if "NE" in token or "网络" in type_name:
        return "network"
    if token.startswith("B") or "批量" in type_name:
        return "batch"
    if token.startswith(("SA", "SR", "S")) or "标准" in type_name:
        return "standard"
    return "other"


def _generation(item: InstanceTypeInfo) -> int:
    match = re.search(r"\d+", _family_token(item))
    return int(match.group()) if match else 0


def _architecture_kind(value: str | None) -> str:
    normalized = (value or "").casefold().replace("_", "").replace("-", "")
    if "arm" in normalized or "aarch64" in normalized:
        return "arm"
    if "x86" in normalized or "amd64" in normalized:
        return "x86"
    return "unknown"


def _scenario_family_rank(
    item: InstanceTypeInfo, scenario: str, *, gpu: bool, provider: str
) -> int:
    if provider == "tencent":
        kind = _tencent_family_kind(item)
        order = _TENCENT_FAMILY_ORDERS[scenario]
        if scenario in {"ai", "video", "game"} and gpu:
            order = [{"heterogeneous"}, {"compute", "network", "standard"}]
    else:
        kind = _alibaba_family_kind(item)
        order = _ALIBABA_FAMILY_ORDERS[scenario]
        if scenario in {"ai", "video"} and gpu:
            order = [{"gn"}, {"c", "hfc", "g"}]
    for index, group in enumerate(order):
        if kind in group:
            return index
        if "network" in group and "ne" in _family_token(item):
            return index
    return len(order) + 1


def _combined_family_rank(item: InstanceTypeInfo, request: SelectionAdvisorRequest) -> int:
    gpu = request.minimum_gpu_count > 0
    rank = (
        _scenario_family_rank(item, request.primary_scenario, gpu=gpu, provider=request.provider)
        * 2
    )
    rank += sum(
        _scenario_family_rank(item, component, gpu=gpu, provider=request.provider)
        for component in request.co_located_components
    )
    return rank


def _resource_value(item: InstanceTypeInfo, resource: str) -> float:
    if resource == "cpu":
        return float(item.cpu)
    if resource == "memory":
        return float(item.memory_gib)
    if resource == "gpu":
        return float(max(item.gpu or 0, 0))
    if resource == "bandwidth":
        return float(
            min(
                item.network_bandwidth_rx_gbps or 0,
                item.network_bandwidth_tx_gbps or 0,
            )
        )
    if resource == "pps":
        return float(min(item.network_pps_rx or 0, item.network_pps_tx or 0))
    if resource == "local_storage":
        return float(
            max(
                item.local_storage_capacity_gib or 0,
                (item.local_storage_count or 0) * 100,
            )
        )
    raise ValueError(f"unknown selection resource: {resource}")


def _resource_weights(request: SelectionAdvisorRequest) -> dict[str, float]:
    combined: dict[str, float] = {}
    scenarios = [
        (request.primary_scenario, 2.0),
        *[(item, 1.0) for item in request.co_located_components],
    ]
    for scenario, scenario_weight in scenarios:
        for resource, resource_weight in _SCENARIO_RESOURCE_WEIGHTS[scenario].items():
            combined[resource] = combined.get(resource, 0.0) + (scenario_weight * resource_weight)
    total = sum(combined.values()) or 1.0
    return {resource: weight / total for resource, weight in combined.items()}


def _percentile_scores(values: dict[str, float]) -> dict[str, float]:
    unique = sorted(set(values.values()))
    if len(unique) == 1:
        neutral = 50.0 if unique[0] > 0 else 0.0
        return {item_id: neutral for item_id in values}
    ranks = {value: index * 100.0 / (len(unique) - 1) for index, value in enumerate(unique)}
    return {item_id: ranks[value] for item_id, value in values.items()}


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[round((len(ordered) - 1) * fraction)]


def _hourly_price(
    item: InstanceTypeInfo,
    price_reader: Callable[[InstanceTypeInfo], PriceInfo | None],
) -> float | None:
    price = price_reader(item)
    if price is None:
        return None
    try:
        hourly = float(price.hourly_amount)
    except (TypeError, ValueError):
        return None
    return hourly if hourly > 0 else None


def _score_candidates(
    candidates: list[InstanceTypeInfo],
    request: SelectionAdvisorRequest,
    price_reader: Callable[[InstanceTypeInfo], PriceInfo | None],
) -> list[_CandidateScore]:
    resource_weights = _resource_weights(request)
    resource_percentiles = {
        resource: _percentile_scores(
            {item.id: _resource_value(item, resource) for item in candidates}
        )
        for resource in resource_weights
    }
    generation_percentiles = _percentile_scores(
        {item.id: float(_generation(item)) for item in candidates}
    )
    performance = {
        item.id: 0.9
        * sum(
            resource_weights[resource] * resource_percentiles[resource][item.id]
            for resource in resource_weights
        )
        + 0.1 * generation_percentiles[item.id]
        for item in candidates
    }
    prices = {item.id: _hourly_price(item, price_reader) for item in candidates}
    priced = {item_id: price for item_id, price in prices.items() if price is not None}
    price_percentiles = _percentile_scores(priced) if priced else {}
    efficiency_raw = {
        item.id: performance[item.id] / prices[item.id]
        for item in candidates
        if prices[item.id] is not None
    }
    efficiency_percentiles = _percentile_scores(efficiency_raw) if efficiency_raw else {}

    scored: list[_CandidateScore] = []
    suitable_limit = _suitable_rank_limit(request)
    for item in candidates:
        scenario_rank = _combined_family_rank(item, request)
        scenario_fit = max(20.0, 100.0 - 20.0 * scenario_rank)
        if request.architecture == "unknown" and _architecture_kind(item.architecture) == "arm":
            scenario_fit = max(0.0, scenario_fit - 10.0)
        hourly = prices[item.id]
        if priced:
            cost_control = (
                100.0 - price_percentiles[item.id] if item.id in price_percentiles else 0.0
            )
            cost_efficiency = efficiency_percentiles.get(item.id, 0.0)
        else:
            cost_control = 50.0
            cost_efficiency = 50.0
        if scenario_rank > suitable_limit:
            scenario_fit = min(scenario_fit, 35.0)
        components = {
            "scenario": scenario_fit,
            "performance": performance[item.id],
            "efficiency": cost_efficiency,
            "cost": cost_control,
        }
        category_scores = {
            category: sum(components[name] * weight for name, weight in weights.items())
            for category, weights in _CATEGORY_WEIGHTS.items()
        }
        scored.append(
            _CandidateScore(
                item=item,
                scenario_rank=scenario_rank,
                scenario_fit=scenario_fit,
                performance=performance[item.id],
                hourly_price=hourly,
                cost_efficiency=cost_efficiency,
                cost_control=cost_control,
                category_scores=category_scores,
            )
        )
    return scored


def _suitable_rank_limit(request: SelectionAdvisorRequest) -> int:
    return 2 + len(request.co_located_components)


def _zone_capabilities(item: InstanceTypeInfo) -> list[dict[str, object]]:
    value = item.attributes.get("zoneCapabilities")
    if not isinstance(value, list):
        return []
    return [capability for capability in value if isinstance(capability, dict)]


def _number(capability: dict[str, object], name: str) -> float:
    value = capability.get(name)
    return float(value) if isinstance(value, int | float) else 0


def _capability_matches(
    capability: dict[str, object],
    request: SelectionAdvisorRequest,
    *,
    gpu: bool = False,
    local_storage: bool = False,
    bandwidth: bool = False,
    pps: bool = False,
) -> bool:
    available = capability.get("available")
    if request.provider in {"tencent", "alibaba"} and request.zone is None:
        if available is not True:
            return False
    elif available is False:
        return False
    if gpu and _number(capability, "gpu") < request.minimum_gpu_count:
        return False
    if local_storage and not (
        capability.get("localStorageCategory") or capability.get("localStorageCapacityGib")
    ):
        return False
    if bandwidth and _number(capability, "networkBandwidthGbps") < (
        request.minimum_network_bandwidth_gbps or 0
    ):
        return False
    return not (pps and _number(capability, "networkPps") < (request.minimum_network_pps or 0))


def _matches_resources(
    item: InstanceTypeInfo,
    request: SelectionAdvisorRequest,
    *,
    gpu: bool = False,
    local_storage: bool = False,
    bandwidth: bool = False,
    pps: bool = False,
) -> bool:
    if item.attributes.get("purchaseCompatible") is False:
        return False
    capabilities = _zone_capabilities(item)
    if capabilities:
        return any(
            _capability_matches(
                capability,
                request,
                gpu=gpu,
                local_storage=local_storage,
                bandwidth=bandwidth,
                pps=pps,
            )
            for capability in capabilities
        )
    if item.available is False:
        return False
    if gpu and (item.gpu or 0) < request.minimum_gpu_count:
        return False
    if local_storage and not (item.local_storage_count or item.local_storage_category):
        return False
    if bandwidth and min(
        item.network_bandwidth_rx_gbps or 0,
        item.network_bandwidth_tx_gbps or 0,
    ) < (request.minimum_network_bandwidth_gbps or 0):
        return False
    return not (
        pps
        and min(item.network_pps_rx or 0, item.network_pps_tx or 0)
        < (request.minimum_network_pps or 0)
    )


def _eligible_zones(item: InstanceTypeInfo, request: SelectionAdvisorRequest) -> list[str]:
    return sorted(
        str(capability["zone"])
        for capability in _zone_capabilities(item)
        if capability.get("zone")
        and _capability_matches(
            capability,
            request,
            gpu=request.minimum_gpu_count > 0,
            local_storage=request.local_storage == "required",
            bandwidth=request.minimum_network_bandwidth_gbps is not None,
            pps=request.minimum_network_pps is not None,
        )
    )


def _filter_stage(
    items: list[InstanceTypeInfo],
    code: str,
    label: str,
    predicate: Callable[[InstanceTypeInfo], bool],
) -> tuple[list[InstanceTypeInfo], ExclusionStage]:
    filtered = [item for item in items if predicate(item)]
    stage = ExclusionStage(
        code=code,
        label=label,
        before=len(items),
        after=len(filtered),
        removed=len(items) - len(filtered),
    )
    return filtered, stage


def _reasons_and_warnings(
    item: InstanceTypeInfo, request: SelectionAdvisorRequest, family_rank: int
) -> tuple[list[str], list[str], Literal["preferred", "suitable", "other"]]:
    reasons: list[str] = []
    warnings: list[str] = []
    label = SCENARIO_LABELS[request.primary_scenario]
    if family_rank == 0:
        reasons.append(f"规格族优先匹配{label}场景")
        tier: Literal["preferred", "suitable", "other"] = "preferred"
    elif family_rank <= 2 + len(request.co_located_components):
        reasons.append(f"规格族适合{label}场景")
        tier = "suitable"
    else:
        reasons.append("满足硬约束，场景匹配度较低")
        tier = "other"
    if request.sizing_mode == "exact":
        reasons.append(f"精确匹配 {item.cpu} vCPU / {item.memory_gib:g} GiB")
    if request.minimum_gpu_count:
        reasons.append(f"提供 {item.gpu or 0:g} 块 GPU")
    if request.local_storage == "required":
        reasons.append("提供本地盘")
    eligible_zones = _eligible_zones(item, request)
    if request.provider in {"tencent", "alibaba"} and request.zone is None and eligible_zones:
        reasons.append(f"地域内 {len(eligible_zones)} 个可用区满足硬约束")
        warnings.append(f"地域聚合结果，需选择可用区确认；当前匹配：{'、'.join(eligible_zones)}")
    elif item.available is True:
        reasons.append("当前可用区库存可用")
    elif item.available is None:
        warnings.append("库存状态未知，选择后仍需实时确认")
    if any(
        str(capability.get("statusCategory") or "").casefold() == "understock"
        for capability in _zone_capabilities(item)
        if str(capability.get("zone") or "") in eligible_zones
    ):
        warnings.append("部分匹配可用区库存即将售罄")
    if any(
        str(capability.get("statusCategory") or "").casefold() == "closedwithstock"
        for capability in _zone_capabilities(item)
        if str(capability.get("zone") or "") in eligible_zones
    ):
        warnings.append("部分匹配可用区当前有库存，但不会继续补充")
    if request.architecture == "unknown" and _architecture_kind(item.architecture) == "arm":
        warnings.append("ARM 兼容性未经代码分析验证")
    if request.code_availability != "available":
        warnings.append("未提供代码，无法验证运行时与原生依赖兼容性")
    if request.sizing_mode == "unknown":
        warnings.append("配置未明确，建议选择后通过压测验证容量")
    return reasons, warnings, tier


def _to_advised(
    item: InstanceTypeInfo,
    request: SelectionAdvisorRequest,
    price: PriceInfo | None = None,
) -> tuple[AdvisedInstanceType, int]:
    rank = _combined_family_rank(item, request)
    reasons, warnings, tier = _reasons_and_warnings(item, request, rank)
    enriched = enrich_instance_type_labels(item)
    advised = AdvisedInstanceType(
        **enriched.model_dump(),
        matchTier=tier,
        reasons=reasons,
        warnings=warnings,
        price=price,
    )
    return advised, rank


def _category_candidates(
    scored: list[_CandidateScore],
    category: Literal["balanced", "value", "performance"],
) -> list[_CandidateScore]:
    candidates = scored
    if category == "balanced":
        floor = max(25.0, _quantile([item.performance for item in scored], 0.30))
        qualified = [item for item in scored if item.performance >= floor]
        candidates = qualified or scored
    elif category == "value":
        candidates = [item for item in scored if item.hourly_price is not None]
        if not candidates:
            return []
        floor = max(25.0, _quantile([item.performance for item in candidates], 0.30))
        qualified = [item for item in candidates if item.performance >= floor]
        candidates = qualified or candidates
    elif category == "performance":
        floor = _quantile([item.performance for item in scored], 0.70)
        qualified = [item for item in scored if item.performance >= floor]
        candidates = qualified or scored
    return sorted(
        candidates,
        key=lambda item: (
            -item.category_scores[category],
            -item.scenario_fit,
            -item.performance,
            item.hourly_price if item.hourly_price is not None else float("inf"),
            item.item.id,
        ),
    )


def _assign_distinct_categories(
    scored: list[_CandidateScore],
) -> list[tuple[Literal["balanced", "value", "performance"], _CandidateScore]]:
    category_order: list[Literal["balanced", "value", "performance"]] = [
        "balanced",
        "value",
        "performance",
    ]
    rankings = {category: _category_candidates(scored, category) for category in category_order}
    categories = [category for category in category_order if rankings[category]]
    categories = categories[: min(len(scored), len(categories))]
    if not categories:
        return []

    choices = [rankings[category][:20] for category in categories]
    best: tuple[_CandidateScore, ...] | None = None
    best_key: tuple[float, float, float] | None = None
    for combination in product(*choices):
        if len({item.item.id for item in combination}) != len(combination):
            continue
        category_total = sum(
            item.category_scores[category]
            for category, item in zip(categories, combination, strict=True)
        )
        key = (
            category_total,
            sum(item.scenario_fit for item in combination),
            sum(item.performance for item in combination),
        )
        if best_key is None or key > best_key:
            best = combination
            best_key = key
    if best is None:
        selected: list[_CandidateScore] = []
        for category in categories:
            alternative = next(
                (
                    item
                    for item in rankings[category]
                    if item.item.id not in {chosen.item.id for chosen in selected}
                ),
                None,
            )
            if alternative is not None:
                selected.append(alternative)
        best = tuple(selected)
        categories = categories[: len(best)]
    return list(zip(categories, best, strict=True))


def _recommendation_reason(
    category: Literal["balanced", "value", "performance"],
    score: _CandidateScore,
    request: SelectionAdvisorRequest,
    *,
    eligible_total: int,
    relaxed: bool,
) -> str:
    scenario = SCENARIO_LABELS[request.primary_scenario]
    total = score.category_scores[category]
    if category == "balanced":
        reason = (
            f"综合分 {total:.0f}：场景匹配 {score.scenario_fit:.0f}、容量性能 "
            f"{score.performance:.0f}、单位价格效率 {score.cost_efficiency:.0f}、"
            "成本控制 "
            f"{score.cost_control:.0f}（权重 35% / 25% / 25% / 15%）。"
        )
    elif category == "value":
        reason = (
            f"性价比分 {total:.0f}：单位价格效率权重 45%，同时要求容量性能 "
            f"{score.performance:.0f} 达到候选基准，避免只因价格最低而入选。"
        )
    else:
        reason = (
            f"性能优先分 {total:.0f}：容量性能 {score.performance:.0f}（权重 40%），"
            f"并计入「{scenario}」场景匹配 {score.scenario_fit:.0f} 和单位价格效率 "
            f"{score.cost_efficiency:.0f}，不直接选择最大或最贵规格。"
        )
    reason += f"本次基于 {eligible_total} 个满足硬约束的候选比较。"
    if relaxed:
        reason += "（场景匹配候选不足，已放宽到全部合格候选）"
    return reason


def _build_top_picks(
    ranked: list[InstanceTypeInfo],
    request: SelectionAdvisorRequest,
    price_reader: Callable[[InstanceTypeInfo], PriceInfo | None],
) -> list[RecommendedInstanceType]:
    if not ranked:
        return []
    eligible_total = len(ranked)
    suitable_limit = _suitable_rank_limit(request)
    suitable_pool = [
        item for item in ranked if _combined_family_rank(item, request) <= suitable_limit
    ]
    pool = suitable_pool if len(suitable_pool) >= 3 else ranked
    relaxed = pool is not suitable_pool

    labels = {
        "balanced": "均衡型",
        "value": "性价比型",
        "performance": "性能型",
    }
    scored = _score_candidates(pool, request, price_reader)
    picks: list[RecommendedInstanceType] = []
    for category, chosen in _assign_distinct_categories(scored):
        price = price_reader(chosen.item)
        advised, scenario_rank = _to_advised(chosen.item, request, price)
        hourly = chosen.hourly_price or 0.0
        value_per_yuan = chosen.performance / hourly if hourly > 0 else 0.0
        picks.append(
            RecommendedInstanceType(
                category=category,
                label=labels[category],
                reason=_recommendation_reason(
                    category,
                    chosen,
                    request,
                    eligible_total=eligible_total,
                    relaxed=relaxed,
                ),
                scores=RecommendationScores(
                    scenario_rank=scenario_rank,
                    scenario_fit=round(chosen.scenario_fit, 2),
                    performance=round(chosen.performance, 2),
                    cost_efficiency=round(chosen.cost_efficiency, 2),
                    cost_control=round(chosen.cost_control, 2),
                    category_score=round(chosen.category_scores[category], 2),
                    hourly_price=hourly,
                    value_per_yuan=round(value_per_yuan, 2),
                ),
                item=advised,
                price=price,
            )
        )
    return picks


def advise_instance_types(
    request: SelectionAdvisorRequest,
    items: list[InstanceTypeInfo],
    *,
    source: Literal["live", "cache", "stale-cache"],
    fetched_at: str,
    expires_at: str,
    stale: bool = False,
    warning: str | None = None,
    price_reader: Callable[[InstanceTypeInfo], PriceInfo | None] | None = None,
) -> SelectionAdvisorResponse:
    remaining = list(items)
    stages: list[ExclusionStage] = []

    remaining, stage = _filter_stage(
        remaining,
        "availability",
        "可用区库存",
        lambda item: _matches_resources(item, request),
    )
    stages.append(stage)
    if request.sizing_mode == "exact":
        remaining, stage = _filter_stage(
            remaining,
            "exact-spec",
            "精确 CPU / 内存",
            lambda item: (
                item.cpu == request.exact_cpu and item.memory_gib == request.exact_memory_gib
            ),
        )
        stages.append(stage)
    if request.architecture != "unknown":
        remaining, stage = _filter_stage(
            remaining,
            "architecture",
            "CPU 架构",
            lambda item: _architecture_kind(item.architecture) == request.architecture,
        )
        stages.append(stage)
    if request.minimum_gpu_count:
        remaining, stage = _filter_stage(
            remaining,
            "gpu",
            "GPU 数量",
            lambda item: _matches_resources(item, request, gpu=True),
        )
        stages.append(stage)
    if request.local_storage == "required":
        remaining, stage = _filter_stage(
            remaining,
            "local-storage",
            "本地盘",
            lambda item: _matches_resources(
                item,
                request,
                gpu=request.minimum_gpu_count > 0,
                local_storage=True,
            ),
        )
        stages.append(stage)
    if request.minimum_network_bandwidth_gbps is not None:
        remaining, stage = _filter_stage(
            remaining,
            "network-bandwidth",
            "内网带宽",
            lambda item: _matches_resources(
                item,
                request,
                gpu=request.minimum_gpu_count > 0,
                local_storage=request.local_storage == "required",
                bandwidth=True,
            ),
        )
        stages.append(stage)
    if request.minimum_network_pps is not None:
        remaining, stage = _filter_stage(
            remaining,
            "network-pps",
            "网络 PPS",
            lambda item: _matches_resources(
                item,
                request,
                gpu=request.minimum_gpu_count > 0,
                local_storage=request.local_storage == "required",
                bandwidth=request.minimum_network_bandwidth_gbps is not None,
                pps=True,
            ),
        )
        stages.append(stage)

    if request.budget_monthly_cny is not None and price_reader is not None:

        def _within_budget(item: InstanceTypeInfo) -> bool:
            price = price_reader(item)
            if price is None:
                return False
            try:
                return float(price.monthly_amount) <= request.budget_monthly_cny
            except (TypeError, ValueError):
                return False

        remaining, stage = _filter_stage(
            remaining,
            "budget",
            "月预算",
            _within_budget,
        )
        stages.append(stage)

    ranked = sorted(
        remaining,
        key=lambda item: (
            _combined_family_rank(item, request),
            1
            if request.architecture == "unknown" and _architecture_kind(item.architecture) == "arm"
            else 0,
            -_generation(item),
            item.id,
        ),
    )
    eligible_total = len(ranked)
    _price_reader = price_reader or (lambda _item: None)
    top_picks = _build_top_picks(ranked, request, _price_reader)
    instance_type_facets = build_instance_type_facets(ranked)
    query = (request.query or "").casefold()
    facet_filters = CatalogFilters(
        architectureClass=request.architecture_class,
        typeKind=request.type_kind,
        familyToken=request.family_token,
    )
    searched = [
        item
        for item in ranked
        if (not query or query in instance_type_search_text(item))
        and matches_instance_type_facets(item, facet_filters)
    ]
    advised: list[AdvisedInstanceType] = []
    for item in searched[request.offset : request.offset + request.limit]:
        advised_item, _rank = _to_advised(item, request, _price_reader(item))
        advised.append(advised_item)
    next_offset = request.offset + request.limit
    most_restrictive = max(stages, key=lambda item: item.removed, default=None)
    return SelectionAdvisorResponse(
        provider=ProviderId(request.provider),
        region=request.region,
        zone=request.zone,
        items=advised,
        total=len(searched),
        eligibleTotal=eligible_total,
        offset=request.offset,
        limit=request.limit,
        nextOffset=next_offset if next_offset < len(searched) else None,
        exclusionStages=stages,
        mostRestrictiveStage=(
            most_restrictive if most_restrictive and most_restrictive.removed else None
        ),
        source=source,
        fetchedAt=fetched_at,
        expiresAt=expires_at,
        stale=stale,
        warning=warning,
        instanceTypeFacets=instance_type_facets,
        topPicks=top_picks,
    )
