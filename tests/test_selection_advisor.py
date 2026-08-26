from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from looper_api.app import cloud_selection_advisor
from looper_api.cloud_contracts import CatalogResponse, InstanceTypeInfo, ProviderId
from looper_api.selection_advisor import SelectionAdvisorRequest, advise_instance_types
from looper_api.selection_pricing import PriceInfo
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
    provider: ProviderId = ProviderId.ALIBABA,
    attributes: dict[str, object] | None = None,
) -> InstanceTypeInfo:
    return InstanceTypeInfo(
        provider=provider,
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
        attributes=attributes or {},
    )


def request(**values: object) -> SelectionAdvisorRequest:
    payload: dict[str, object] = {
        "region": "cn-test",
        "zone": "cn-test-a",
        "primaryScenario": "web-api",
    }
    payload.update(values)
    return SelectionAdvisorRequest.model_validate(payload)


def advise(
    query: SelectionAdvisorRequest,
    items: list[InstanceTypeInfo],
    price_reader=None,
):
    return advise_instance_types(
        query,
        items,
        source="live",
        fetched_at="2026-08-22T00:00:00+00:00",
        expires_at="2026-08-22T00:05:00+00:00",
        price_reader=price_reader,
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
    assert result.eligible_total == 5
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


def test_search_filters_all_ranked_candidates_before_pagination() -> None:
    items = [
        instance(
            "ecs.g9i.needle" if index == 619 else f"ecs.g9i.test-{index}",
            "ecs.g9i",
        )
        for index in range(620)
    ]

    result = advise(
        request(query="G9I.NEEDLE", limit=20, codeAvailability="available"),
        items,
    )

    assert result.eligible_total == 620
    assert result.total == 1
    assert [item.id for item in result.items] == ["ecs.g9i.needle"]
    assert result.next_offset is None


def test_facets_filter_ranked_candidates_without_changing_recommendation_order() -> None:
    items = [
        instance("ecs.g9i.large", "ecs.g9i"),
        instance("ecs.g8i.large", "ecs.g8i"),
        instance("ecs.c9i.large", "ecs.c9i"),
    ]
    unfiltered = advise(request(codeAvailability="available"), items)
    filtered = advise(
        request(
            codeAvailability="available",
            architectureClass="x86",
            typeKind="general",
        ),
        items,
    )

    assert filtered.eligible_total == 3
    assert filtered.total == 2
    assert [item.id for item in filtered.items] == [
        item.id for item in unfiltered.items if item.id.startswith("ecs.g")
    ]
    assert filtered.instance_type_facets is not None
    assert filtered.instance_type_facets.architectures[0].count == 3


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
    items = [instance(f"ecs.g9i.{index:02d}", "ecs.g9i", available=None) for index in range(25)]
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
    assert result.top_picks == []
    assert result.most_restrictive_stage is not None
    assert result.most_restrictive_stage.code == "exact-spec"
    assert result.most_restrictive_stage.after == 0


def test_top_picks_returns_three_distinct_categories_with_reasons() -> None:
    items = [
        instance("ecs.g9i.xlarge", "ecs.g9i"),
        instance("ecs.c9i.xlarge", "ecs.c9i"),
        instance("ecs.r9i.xlarge", "ecs.r9i"),
    ]

    def price_reader(_item: InstanceTypeInfo) -> PriceInfo:
        return PriceInfo(
            hourly_amount="1.000",
            monthly_amount="30.000",
            source="price-table",
        )

    result = advise(request(codeAvailability="available"), items, price_reader)

    assert [pick.category for pick in result.top_picks] == [
        "balanced",
        "value",
        "performance",
    ]
    assert len({pick.item.id for pick in result.top_picks}) == 3
    assert all(pick.reason for pick in result.top_picks)
    assert all(pick.scores.performance > 0 for pick in result.top_picks)
    assert all(0 <= pick.scores.category_score <= 100 for pick in result.top_picks)
    assert all(0 <= pick.scores.scenario_fit <= 100 for pick in result.top_picks)
    assert all(
        pick.price is not None and pick.price.source == "price-table" for pick in result.top_picks
    )


def test_top_picks_ignore_search_and_facets() -> None:
    items = [
        instance("ecs.g9i.xlarge", "ecs.g9i"),
        instance("ecs.c9i.xlarge", "ecs.c9i"),
        instance("ecs.r9i.xlarge", "ecs.r9i"),
    ]
    base = advise(request(codeAvailability="available"), items)
    filtered = advise(
        request(
            codeAvailability="available",
            query="g9i",
            architectureClass="x86",
        ),
        items,
    )

    assert [pick.item.id for pick in base.top_picks] == [
        pick.item.id for pick in filtered.top_picks
    ]


def test_top_picks_fall_back_to_fewer_candidates_when_pool_is_small() -> None:
    items = [
        instance("ecs.g9i.xlarge", "ecs.g9i"),
        instance("ecs.c9i.xlarge", "ecs.c9i"),
    ]
    result = advise(request(codeAvailability="available"), items)

    assert [pick.category for pick in result.top_picks] == ["balanced", "performance"]
    assert len({pick.item.id for pick in result.top_picks}) == 2


def test_top_picks_avoid_choosing_the_cheapest_and_largest_by_default() -> None:
    items = [
        instance("ecs.g9i.tiny", "ecs.g9i", cpu=2, memory=2, bandwidth=1, pps=300_000),
        instance("ecs.g9i.small", "ecs.g9i", cpu=4, memory=8, bandwidth=3, pps=1_000_000),
        instance("ecs.g9i.mid", "ecs.g9i", cpu=8, memory=16, bandwidth=8, pps=2_000_000),
        instance("ecs.g9i.fast", "ecs.g9i", cpu=32, memory=64, bandwidth=25, pps=6_000_000),
        instance("ecs.g9i.huge", "ecs.g9i", cpu=192, memory=576, bandwidth=75, pps=16_800_000),
    ]
    hourly_prices = {
        "ecs.g9i.tiny": 0.05,
        "ecs.g9i.small": 0.35,
        "ecs.g9i.mid": 0.90,
        "ecs.g9i.fast": 4.80,
        "ecs.g9i.huge": 31.93,
    }

    def price_reader(item: InstanceTypeInfo) -> PriceInfo:
        hourly = hourly_prices[item.id]
        return PriceInfo(
            hourly_amount=f"{hourly:.3f}",
            monthly_amount=f"{hourly * 730:.3f}",
            source="price-table",
        )

    result = advise(
        request(primaryScenario="game", codeAvailability="available"),
        items,
        price_reader,
    )
    by_category = {pick.category: pick for pick in result.top_picks}

    assert set(by_category) == {"balanced", "value", "performance"}
    assert by_category["value"].item.id != "ecs.g9i.tiny"
    assert by_category["performance"].item.id != "ecs.g9i.huge"
    assert len({pick.item.id for pick in result.top_picks}) == 3
    assert "避免只因价格最低" in by_category["value"].reason
    assert "不直接选择最大或最贵规格" in by_category["performance"].reason


def test_budget_filters_candidates_by_monthly_price() -> None:
    items = [
        instance("ecs.g9i.xlarge", "ecs.g9i"),
        instance("ecs.c9i.xlarge", "ecs.c9i"),
        instance("ecs.r9i.xlarge", "ecs.r9i"),
    ]

    def price_reader(item: InstanceTypeInfo):
        if item.id == "ecs.r9i.xlarge":
            return PriceInfo(
                hourly_amount="3.000",
                monthly_amount="900.000",
                source="price-table",
            )
        return PriceInfo(
            hourly_amount="1.000",
            monthly_amount="300.000",
            source="price-table",
        )

    result = advise(
        request(codeAvailability="available", budgetMonthlyCny=500),
        items,
        price_reader,
    )

    assert result.eligible_total == 2
    assert all(
        item.price is not None and float(item.price.monthly_amount) <= 500 for item in result.items
    )
    assert any(stage.code == "budget" for stage in result.exclusion_stages)


def test_selection_quote_falls_back_to_estimate_when_image_is_missing() -> None:
    from looper_api.providers.registry import CloudProviderRegistry
    from looper_api.selection_pricing import (
        SelectionPriceQuoteRequest,
        selection_instance_quote,
    )

    class FakeProvider:
        id = ProviderId.ALIBABA

        def search_images(self, _filters: object):
            return []

    registry = CloudProviderRegistry(factories={ProviderId.ALIBABA: lambda: FakeProvider()})
    item = instance("ecs.g9i.xlarge", "ecs.g9i")
    result = selection_instance_quote(
        SelectionPriceQuoteRequest(item=item, zone="cn-test-a"),
        registry,
    )

    assert result.source == "estimated"
    assert "未找到兼容镜像" in (result.warning or "")


def test_exact_sizing_requires_both_cpu_and_memory() -> None:
    with pytest.raises(ValidationError, match="exact sizing requires"):
        request(sizingMode="exact", exactCpu=8)


def test_tencent_scenario_ranking_uses_cvm_families_without_excluding() -> None:
    items = [
        instance("M8.LARGE32", "M8", provider=ProviderId.TENCENT),
        instance("S8.LARGE32", "S8", provider=ProviderId.TENCENT),
        instance("C7.LARGE32", "C7", provider=ProviderId.TENCENT),
    ]

    result = advise(
        request(provider="tencent", primaryScenario="database", codeAvailability="available"),
        items,
    )

    assert result.provider == ProviderId.TENCENT
    assert result.total == 3
    assert [item.id for item in result.items] == ["M8.LARGE32", "S8.LARGE32", "C7.LARGE32"]


@pytest.mark.parametrize("provider", ["tencent", "alibaba"])
def test_region_aggregation_requires_one_zone_to_satisfy_all_constraints(
    provider: str,
) -> None:
    split_capabilities = [
        {
            "zone": "ap-test-1",
            "available": True,
            "gpu": 1,
            "networkBandwidthGbps": 20,
            "networkPps": 2_000_000,
        },
        {
            "zone": "ap-test-2",
            "available": True,
            "gpu": 0,
            "localStorageCategory": "LOCAL_SSD",
            "localStorageCapacityGib": 1000,
            "networkBandwidthGbps": 20,
            "networkPps": 2_000_000,
        },
    ]
    matching_capabilities = [
        {
            "zone": "ap-test-3",
            "available": True,
            "gpu": 1,
            "localStorageCategory": "LOCAL_SSD",
            "localStorageCapacityGib": 1000,
            "networkBandwidthGbps": 20,
            "networkPps": 2_000_000,
            "statusCategory": "ClosedWithStock",
        }
    ]
    provider_id = ProviderId(provider)
    split_id = "GN7.SPLIT" if provider == "tencent" else "ecs.gn7.split"
    match_id = "GN7.MATCH" if provider == "tencent" else "ecs.gn7.match"
    items = [
        instance(
            split_id,
            "GN7" if provider == "tencent" else "ecs.gn7",
            provider=provider_id,
            gpu=1,
            local_storage=1,
            bandwidth=20,
            pps=2_000_000,
            attributes={"zoneCapabilities": split_capabilities},
        ),
        instance(
            match_id,
            "GN7" if provider == "tencent" else "ecs.gn7",
            provider=provider_id,
            gpu=1,
            local_storage=1,
            bandwidth=20,
            pps=2_000_000,
            attributes={"zoneCapabilities": matching_capabilities},
        ),
    ]

    result = advise(
        request(
            provider=provider,
            zone=None,
            primaryScenario="ai",
            minimumGpuCount=1,
            localStorage="required",
            minimumNetworkBandwidthGbps=10,
            minimumNetworkPps=1_000_000,
        ),
        items,
    )

    assert [item.id for item in result.items] == [match_id]
    assert "当前匹配：ap-test-3" in result.items[0].warnings[0]
    assert "不会继续补充" in result.items[0].warnings[1]
    assert {stage.code: stage.removed for stage in result.exclusion_stages}["local-storage"] == 1


def test_selection_advisor_route_uses_requested_provider(monkeypatch, db_session) -> None:
    requested: list[ProviderId] = []
    now = datetime.now(UTC)

    def catalog_inventory_stub(
        _session: object,
        _settings: object,
        _registry: object,
        provider: ProviderId,
        _resource_type: str,
        _filters: object,
    ) -> CatalogResponse:
        requested.append(provider)
        item = instance("S8.LARGE32", "S8", provider=ProviderId.TENCENT)
        return CatalogResponse(
            provider=provider,
            resourceType="instance-type",
            items=[item.model_dump(mode="json", by_alias=True)],
            total=1,
            source="live",
            fetchedAt=now,
            expiresAt=now + timedelta(minutes=5),
        )

    monkeypatch.setattr("looper_api.app.catalog_inventory", catalog_inventory_stub)
    response = cloud_selection_advisor(
        request(provider="tencent", codeAvailability="available"),
        db_session,
        object(),
        object(),
    )

    assert requested == [ProviderId.TENCENT]
    assert response["provider"] == "tencent"
