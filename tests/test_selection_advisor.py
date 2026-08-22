from __future__ import annotations

import pytest
from looper_api.cloud_contracts import InstanceTypeInfo, ProviderId
from looper_api.selection_advisor import SelectionAdvisorRequest, advise_instance_types
from pydantic import ValidationError


def instance(
    instance_id: str,
    family: str,
    *,
    cpu: int = 8,
    memory: float = 32,
    architecture: str = "X86",
    available: bool | None = True,
    gpu: int = 0,
    local_storage: int = 0,
    bandwidth: float = 10,
    pps: int = 1_000_000,
) -> InstanceTypeInfo:
    return InstanceTypeInfo(
        provider=ProviderId.ALIBABA,
        region="cn-test",
        id=instance_id,
        family=family,
        cpu=cpu,
        memoryGib=memory,
        architecture=architecture,
        available=available,
        gpu=gpu,
        localStorageCount=local_storage,
        networkBandwidthRxGbps=bandwidth,
        networkBandwidthTxGbps=bandwidth,
        networkPpsRx=pps,
        networkPpsTx=pps,
        zones=["cn-test-a"],
    )


def request(**values: object) -> SelectionAdvisorRequest:
    payload: dict[str, object] = {
        "region": "cn-test",
        "zone": "cn-test-a",
        "primaryScenario": "web-api",
    }
    payload.update(values)
    return SelectionAdvisorRequest.model_validate(payload)


def advise(query: SelectionAdvisorRequest, items: list[InstanceTypeInfo]):
    return advise_instance_types(
        query,
        items,
        source="live",
        fetched_at="2026-08-22T00:00:00+00:00",
        expires_at="2026-08-22T00:05:00+00:00",
    )


def test_scenario_ranks_without_excluding_and_prefers_x86_when_architecture_unknown() -> None:
    items = [
        instance("ecs.r9i.xlarge", "ecs.r9i"),
        instance("ecs.c9i.xlarge", "ecs.c9i"),
        instance("ecs.g8y.xlarge", "ecs.g8y", architecture="ARM"),
        instance("ecs.g8i.xlarge", "ecs.g8i"),
        instance("ecs.g9i.xlarge", "ecs.g9i"),
    ]
    result = advise(request(codeAvailability="available"), items)

    assert result.total == 5
    assert [item.id for item in result.items] == [
        "ecs.g9i.xlarge",
        "ecs.g8i.xlarge",
        "ecs.g8y.xlarge",
        "ecs.c9i.xlarge",
        "ecs.r9i.xlarge",
    ]
    arm = result.items[2]
    assert arm.match_tier == "preferred"
    assert "ARM 兼容性未经代码分析验证" in arm.warnings


def test_exact_configuration_and_architecture_are_hard_filters() -> None:
    items = [
        instance("ecs.g9i.xlarge", "ecs.g9i"),
        instance("ecs.g9i.2xlarge", "ecs.g9i", cpu=16, memory=64),
        instance("ecs.g8y.xlarge", "ecs.g8y", architecture="ARM"),
    ]
    result = advise(
        request(
            sizingMode="exact",
            exactCpu=8,
            exactMemoryGib=32,
            architecture="x86",
        ),
        items,
    )

    assert [item.id for item in result.items] == ["ecs.g9i.xlarge"]
    assert [(stage.code, stage.removed) for stage in result.exclusion_stages] == [
        ("availability", 0),
        ("exact-spec", 1),
        ("architecture", 1),
    ]


def test_gpu_local_storage_network_and_inventory_filters_are_explained() -> None:
    items = [
        instance(
            "ecs.gn9i.2xlarge",
            "ecs.gn9i",
            gpu=2,
            local_storage=1,
            bandwidth=25,
            pps=3_000_000,
        ),
        instance("ecs.gn9i.soldout", "ecs.gn9i", available=False, gpu=2, local_storage=1),
        instance("ecs.gn9i.no-disk", "ecs.gn9i", gpu=2, bandwidth=25, pps=3_000_000),
        instance("ecs.gn9i.slow", "ecs.gn9i", gpu=2, local_storage=1, bandwidth=5),
    ]
    result = advise(
        request(
            primaryScenario="ai",
            minimumGpuCount=2,
            localStorage="required",
            minimumNetworkBandwidthGbps=20,
            minimumNetworkPps=2_000_000,
        ),
        items,
    )

    assert [item.id for item in result.items] == ["ecs.gn9i.2xlarge"]
    assert {stage.code: stage.removed for stage in result.exclusion_stages} == {
        "availability": 1,
        "gpu": 0,
        "local-storage": 1,
        "network-bandwidth": 1,
        "network-pps": 0,
    }
    assert result.most_restrictive_stage is not None


def test_unknown_inventory_is_retained_and_pagination_is_stable() -> None:
    items = [
        instance(f"ecs.g9i.{index:02d}", "ecs.g9i", available=None)
        for index in range(25)
    ]
    first = advise(request(limit=20), items)
    second = advise(request(offset=20, limit=20), items)

    assert len(first.items) == 20
    assert first.next_offset == 20
    assert len(second.items) == 5
    assert second.next_offset is None
    assert not ({item.id for item in first.items} & {item.id for item in second.items})
    assert "库存状态未知，选择后仍需实时确认" in first.items[0].warnings


def test_zero_result_reports_the_most_restrictive_stage_without_relaxing() -> None:
    result = advise(
        request(sizingMode="exact", exactCpu=64, exactMemoryGib=512),
        [instance("ecs.g9i.xlarge", "ecs.g9i")],
    )
    assert result.total == 0
    assert result.most_restrictive_stage is not None
    assert result.most_restrictive_stage.code == "exact-spec"
    assert result.most_restrictive_stage.after == 0


def test_exact_sizing_requires_both_cpu_and_memory() -> None:
    with pytest.raises(ValidationError, match="exact sizing requires"):
        request(sizingMode="exact", exactCpu=8)
