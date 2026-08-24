from __future__ import annotations

from looper_api.cloud_contracts import CatalogFilters, InstanceTypeInfo
from looper_api.providers.utils import filter_instance_types, instance_type_labels
from looper_api.selection_advisor import SelectionAdvisorRequest, advise_instance_types


def instance(provider: str, item_id: str, family: str, **attributes: object) -> InstanceTypeInfo:
    return InstanceTypeInfo(
        provider=provider,
        region="region-test",
        id=item_id,
        family=family,
        cpu=2,
        memoryGib=4,
        architecture="x86_64",
        available=True,
        zones=["zone-test"],
        attributes={
            "zoneCapabilities": [{"zone": "zone-test", "available": True}],
            **attributes,
        },
    )


def test_instance_type_labels_cover_known_and_unknown_families() -> None:
    shared = instance("alibaba", "ecs.s6-c1m1.small", "ecs.s6")
    standard = instance("tencent", "S5.SMALL2", "S5", typeName="标准型 S5")
    unknown = instance("alibaba", "ecs.xyz.large", "ecs.xyz")

    assert instance_type_labels(shared) == ("共享型", "共享标准型 s6")
    assert instance_type_labels(standard) == ("标准型", "标准型 S5")
    assert instance_type_labels(unknown) == ("其他类型", "规格族 xyz")


def test_manual_catalog_search_matches_chinese_labels() -> None:
    items = [
        instance("alibaba", "ecs.s6-c1m1.small", "ecs.s6"),
        instance("alibaba", "ecs.c7.large", "ecs.c7"),
    ]

    result = filter_instance_types(
        items,
        CatalogFilters(region="region-test", query="共享标准型"),
    )

    assert [item.id for item in result] == ["ecs.s6-c1m1.small"]
    assert result[0].type_label == "共享型"


def test_selection_advisor_search_matches_chinese_labels_without_changing_ranking() -> None:
    items = [
        instance("alibaba", "ecs.s6-c1m1.small", "ecs.s6"),
        instance("alibaba", "ecs.c7.large", "ecs.c7"),
    ]
    request = SelectionAdvisorRequest(
        provider="alibaba",
        region="region-test",
        zone="zone-test",
        primaryScenario="development-test",
        coLocatedComponents=[],
        sizingMode="unknown",
        minimumGpuCount=0,
        localStorage="unknown",
        codeAvailability="unknown",
        architecture="unknown",
        query="共享型",
        offset=0,
        limit=20,
    )

    result = advise_instance_types(
        request,
        items,
        source="live",
        fetched_at="2026-08-24T00:00:00Z",
        expires_at="2026-08-24T00:05:00Z",
    )

    assert [item.id for item in result.items] == ["ecs.s6-c1m1.small"]
    assert result.items[0].family_label == "共享标准型 s6"
