from __future__ import annotations

import re
from collections import Counter
from typing import Any

from looper_api.benchmark_runtime import (
    deployment_capabilities,
    provisioned_capabilities,
)
from looper_api.models import TargetRecord


class BenchmarkTargetCompatibilityError(ValueError):
    status_code = 422
    code = "benchmark_target_incompatible"

    def __init__(self, message: str, constraints: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.constraints = constraints


def single_node_contract(manifest: dict[str, Any]) -> dict[str, Any] | None:
    infrastructure = manifest.get("spec", {}).get("infrastructure") or {}
    node_groups = infrastructure.get("nodeGroups") or []
    if len(node_groups) != 1:
        return None
    node_group = node_groups[0]
    count = node_group.get("count") or {}
    if any(count.get(key) != 1 for key in ("minimum", "default", "maximum")):
        return None
    return node_group


def require_single_node_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    node_group = single_node_contract(manifest)
    if node_group is None:
        raise BenchmarkTargetCompatibilityError(
            "当前版本只支持恰好需要一台物理机器的 Benchmark",
            [
                {
                    "code": "single_node_required",
                    "field": "infrastructure.nodeGroups",
                    "required": "一个机器组且 minimum/default/maximum 均为 1",
                    "actual": manifest.get("spec", {}).get("infrastructure"),
                    "message": "该 Benchmark 需要多机或未声明可验证的单机合同",
                }
            ],
        )
    return node_group


def _constraint(
    code: str,
    field: str,
    required: Any,
    actual: Any,
    message: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "field": field,
        "required": required,
        "actual": actual,
        "message": message,
    }


def _target_sources(target: TargetRecord) -> list[dict[str, Any]]:
    fingerprint = target.fingerprint_json or {}
    inventory = target.inventory_json or {}
    sources = [fingerprint, inventory]
    display = fingerprint.get("cloud_display")
    if isinstance(display, dict):
        display_fingerprint = display.get("fingerprint")
        display_inventory = display.get("inventory")
        if isinstance(display_fingerprint, dict):
            sources.append(display_fingerprint)
        if isinstance(display_inventory, dict):
            sources.append(display_inventory)
    return sources


def _first_value(target: TargetRecord, *keys: str) -> Any:
    for source in _target_sources(target):
        for key in keys:
            value = source.get(key)
            if value is not None and value != "":
                return value
    return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _normalize_architecture(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "armv8": "aarch64",
        "aarch64": "aarch64",
    }
    return aliases.get(normalized, normalized or None)


def _normalize_os(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold()
    if any(token in normalized for token in ("linux", "ubuntu", "debian", "centos", "rhel")):
        return "linux"
    if "windows" in normalized:
        return "windows"
    if any(token in normalized for token in ("macos", "darwin", "os x")):
        return "macos"
    if "aix" in normalized:
        return "aix"
    return normalized or None


def _target_architectures(target: TargetRecord) -> set[str]:
    values = {
        _normalize_architecture(_first_value(target, "architecture", "machine", "cpu_architecture"))
    }
    values.update(_normalize_architecture(item) for item in target.capabilities_json)
    return {value for value in values if value in {"x86_64", "aarch64", "ppc64le", "riscv64"}}


def _target_os_families(target: TargetRecord) -> set[str]:
    values = {
        _normalize_os(_first_value(target, "system", "os_name", "platform", "framework"))
    }
    values.update(_normalize_os(item) for item in target.capabilities_json)
    return {value for value in values if value in {"linux", "windows", "macos", "aix"}}


def _memory_gib(target: TargetRecord) -> float | None:
    value = _number(_first_value(target, "memory_gib"))
    if value is not None:
        return value
    memory_bytes = _number(_first_value(target, "memory_bytes"))
    return memory_bytes / (1024**3) if memory_bytes is not None else None


def _capability_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def target_compatibility(
    manifest: dict[str, Any], target: TargetRecord
) -> list[dict[str, Any]]:
    node_group = require_single_node_contract(manifest)
    requirements = node_group.get("requirements") or {}
    constraints: list[dict[str, Any]] = []

    if target.lifecycle_status != "active":
        constraints.append(
            _constraint(
                "target_inactive",
                "target.lifecycleStatus",
                "active",
                target.lifecycle_status,
                "资源不是活动状态",
            )
        )
    if not target.runnable:
        constraints.append(
            _constraint(
                "target_not_runnable",
                "target.runnable",
                True,
                target.runnable,
                "SSH 或 Worker 尚未就绪",
            )
        )

    required_os = {
        value
        for item in requirements.get("osFamilies") or []
        if (value := _normalize_os(item))
    }
    actual_os = _target_os_families(target)
    if required_os and not required_os.intersection(actual_os):
        constraints.append(
            _constraint(
                "os_incompatible",
                "requirements.osFamilies",
                sorted(required_os),
                sorted(actual_os) or None,
                "操作系统不符合 Benchmark 合同",
            )
        )

    required_architectures = {
        value
        for item in requirements.get("architectures") or []
        if (value := _normalize_architecture(item))
    }
    actual_architectures = _target_architectures(target)
    if required_architectures and not required_architectures.intersection(actual_architectures):
        constraints.append(
            _constraint(
                "architecture_incompatible",
                "requirements.architectures",
                sorted(required_architectures),
                sorted(actual_architectures) or None,
                "CPU 架构不符合 Benchmark 合同",
            )
        )

    actual_capabilities = set(target.capabilities_json or [])
    required_capabilities = set(requirements.get("capabilities") or [])
    required_capabilities.update(deployment_capabilities(manifest))
    missing_capabilities = sorted(required_capabilities - actual_capabilities)
    if missing_capabilities:
        constraints.append(
            _constraint(
                "capability_missing",
                "requirements.capabilities",
                sorted(required_capabilities),
                sorted(actual_capabilities),
                f"缺少基础能力：{'、'.join(missing_capabilities)}",
            )
        )

    cpu = requirements.get("cpu") or {}
    logical_cpus = _number(_first_value(target, "logical_cpu_count", "cpu", "vcpu", "vcpus"))
    minimum_logical = _number(cpu.get("minimumLogicalCpus"))
    if minimum_logical is not None and (logical_cpus is None or logical_cpus < minimum_logical):
        constraints.append(
            _constraint(
                "cpu_below_minimum",
                "requirements.cpu.minimumLogicalCpus",
                minimum_logical,
                logical_cpus,
                "逻辑 CPU 数量不足或无法验证",
            )
        )
    unsupported_cpu = {
        key: value
        for key, value in cpu.items()
        if key
        in {"minimumPhysicalCores", "minimumSockets", "minimumNumaNodes", "requiredFlags"}
    }
    if unsupported_cpu:
        constraints.append(
            _constraint(
                "cpu_requirement_unverifiable",
                "requirements.cpu",
                unsupported_cpu,
                None,
                "当前资源指纹无法可靠验证该 CPU 要求",
            )
        )

    memory = requirements.get("memory") or {}
    actual_memory = _memory_gib(target)
    minimum_memory = _number(memory.get("minimumGiB"))
    if minimum_memory is not None and (actual_memory is None or actual_memory < minimum_memory):
        constraints.append(
            _constraint(
                "memory_below_minimum",
                "requirements.memory.minimumGiB",
                minimum_memory,
                actual_memory,
                "内存容量不足或无法验证",
            )
        )
    if memory.get("minimumHugepageGiB") is not None:
        constraints.append(
            _constraint(
                "memory_requirement_unverifiable",
                "requirements.memory.minimumHugepageGiB",
                memory["minimumHugepageGiB"],
                None,
                "当前资源指纹无法可靠验证大页内存要求",
            )
        )

    for key, label in (
        ("accelerators", "加速器"),
        ("storage", "存储"),
        ("network", "网络"),
        ("privileges", "权限"),
    ):
        if requirements.get(key):
            constraints.append(
                _constraint(
                    f"{key}_requirement_unverifiable",
                    f"requirements.{key}",
                    requirements[key],
                    None,
                    f"当前资源指纹无法可靠验证{label}要求",
                )
            )

    provided = {_capability_slug(item) for item in provisioned_capabilities(manifest)}
    actual_software = {_capability_slug(item) for item in actual_capabilities}
    for software in requirements.get("software") or []:
        name = str(software.get("name") or "")
        slug = _capability_slug(name)
        if slug and slug not in provided and slug not in actual_software:
            constraints.append(
                _constraint(
                    "software_requirement_unverifiable",
                    "requirements.software",
                    software,
                    None,
                    f"目标未声明且 Looper 不会自动提供软件：{name}",
                )
            )
    return constraints


def assert_target_compatible(manifest: dict[str, Any], target: TargetRecord) -> None:
    constraints = target_compatibility(manifest, target)
    if constraints:
        raise BenchmarkTargetCompatibilityError(
            f"资源 {target.name!r} 不符合 Benchmark 单机合同",
            constraints,
        )


def requirement_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    node_group = require_single_node_contract(manifest)
    requirements = node_group.get("requirements") or {}
    cpu = requirements.get("cpu") or {}
    memory = requirements.get("memory") or {}
    capabilities = set(requirements.get("capabilities") or [])
    capabilities.update(deployment_capabilities(manifest))
    return {
        "osFamilies": requirements.get("osFamilies") or [],
        "architectures": requirements.get("architectures") or [],
        "minimumLogicalCpus": cpu.get("minimumLogicalCpus"),
        "minimumMemoryGiB": memory.get("minimumGiB"),
        "capabilities": sorted(capabilities),
    }


def incompatibility_summary(
    rejected: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    counts = Counter(
        (item["code"], item["message"])
        for constraints in rejected
        for item in constraints
    )
    return [
        {"code": code, "message": message, "count": count}
        for (code, message), count in counts.most_common()
    ]


def target_environment(target: TargetRecord) -> tuple[str, str]:
    provider = target.provider.casefold()
    if provider in {"alibaba", "alibaba-ecs"} or target.id.startswith("cloud:alibaba:"):
        return "alibaba-ecs", "阿里云 ECS"
    if provider in {"tencent", "tencent-cvm"} or target.id.startswith("cloud:tencent:"):
        return "tencent-cvm", "腾讯云 CVM"
    if provider == "external":
        return "external-ssh", "外部 SSH"
    return provider or "other", target.provider or "其他"
