from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from pydantic import Field, model_validator

from looper_api.cloud_contracts import ApiModel, InstanceTypeInfo, ProviderId

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


class SelectionAdvisorResponse(ApiModel):
    provider: ProviderId
    region: str
    zone: str | None = None
    items: list[AdvisedInstanceType]
    total: int
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


def _family_token(item: InstanceTypeInfo) -> str:
    source = (item.family or item.id).casefold()
    if source.startswith("ecs."):
        source = source[4:]
    return source.split(".", 1)[0]


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
    rank = _scenario_family_rank(
        item, request.primary_scenario, gpu=gpu, provider=request.provider
    ) * 2
    rank += sum(
        _scenario_family_rank(item, component, gpu=gpu, provider=request.provider)
        for component in request.co_located_components
    )
    return rank


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
    if request.provider == "tencent" and request.zone is None:
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
    return not (
        pps and _number(capability, "networkPps") < (request.minimum_network_pps or 0)
    )


def _matches_resources(
    item: InstanceTypeInfo,
    request: SelectionAdvisorRequest,
    *,
    gpu: bool = False,
    local_storage: bool = False,
    bandwidth: bool = False,
    pps: bool = False,
) -> bool:
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
    if request.provider == "tencent" and request.zone is None and eligible_zones:
        reasons.append(f"地域内 {len(eligible_zones)} 个可用区满足硬约束")
        warnings.append(
            f"地域聚合结果，需选择可用区确认；当前匹配：{'、'.join(eligible_zones)}"
        )
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
    if request.architecture == "unknown" and _architecture_kind(item.architecture) == "arm":
        warnings.append("ARM 兼容性未经代码分析验证")
    if request.code_availability != "available":
        warnings.append("未提供代码，无法验证运行时与原生依赖兼容性")
    if request.sizing_mode == "unknown":
        warnings.append("配置未明确，建议选择后通过压测验证容量")
    return reasons, warnings, tier


def advise_instance_types(
    request: SelectionAdvisorRequest,
    items: list[InstanceTypeInfo],
    *,
    source: Literal["live", "cache", "stale-cache"],
    fetched_at: str,
    expires_at: str,
    stale: bool = False,
    warning: str | None = None,
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
            lambda item: item.cpu == request.exact_cpu
            and item.memory_gib == request.exact_memory_gib,
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

    ranked = sorted(
        remaining,
        key=lambda item: (
            _combined_family_rank(item, request),
            1
            if request.architecture == "unknown"
            and _architecture_kind(item.architecture) == "arm"
            else 0,
            -_generation(item),
            item.id,
        ),
    )
    advised: list[AdvisedInstanceType] = []
    for item in ranked[request.offset : request.offset + request.limit]:
        rank = _combined_family_rank(item, request)
        reasons, warnings, tier = _reasons_and_warnings(item, request, rank)
        advised.append(
            AdvisedInstanceType(
                **item.model_dump(), matchTier=tier, reasons=reasons, warnings=warnings
            )
        )
    next_offset = request.offset + request.limit
    most_restrictive = max(stages, key=lambda item: item.removed, default=None)
    return SelectionAdvisorResponse(
        provider=ProviderId(request.provider),
        region=request.region,
        zone=request.zone,
        items=advised,
        total=len(ranked),
        offset=request.offset,
        limit=request.limit,
        nextOffset=next_offset if next_offset < len(ranked) else None,
        exclusionStages=stages,
        mostRestrictiveStage=(
            most_restrictive if most_restrictive and most_restrictive.removed else None
        ),
        source=source,
        fetchedAt=fetched_at,
        expiresAt=expires_at,
        stale=stale,
        warning=warning,
    )
