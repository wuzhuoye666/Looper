from __future__ import annotations

from looper_api.cloud_contracts import CatalogFilters, InstanceTypeInfo
from looper_api.providers.utils import (
    build_instance_type_facets,
    enrich_instance_type_labels,
    filter_instance_types,
    instance_type_labels,
)
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


def test_selection_classes_honor_hpc_bare_metal_gpu_arm_priority() -> None:
    cases = [
        (instance("alibaba", "ecs.sccg7.large", "ecs.sccg7"), "hpc"),
        (instance("alibaba", "ecs.ebmgn7.large", "ecs.ebmgn7"), "bare-metal"),
        (instance("alibaba", "ecs.gn7.large", "ecs.gn7"), "heterogeneous"),
        (
            instance("alibaba", "ecs.c8y.large", "ecs.c8y").model_copy(
                update={"architecture": "ARM"}
            ),
            "arm",
        ),
        (instance("tencent", "HCC.4XLARGE", "HCC"), "hpc"),
        (instance("tencent", "BM.GN7", "BM"), "bare-metal"),
        (instance("tencent", "GN10.XLARGE", "GN10"), "heterogeneous"),
    ]

    assert [enrich_instance_type_labels(item).selection_class for item, _ in cases] == [
        expected for _, expected in cases
    ]


def test_facets_include_full_hierarchy_and_sort_newer_generations_first() -> None:
    items = [
        instance("alibaba", "ecs.g7.large", "ecs.g7"),
        instance("alibaba", "ecs.g9i.large", "ecs.g9i"),
        instance("alibaba", "ecs.g8i.large", "ecs.g8i"),
        instance("alibaba", "ecs.c8a.large", "ecs.c8a"),
    ]

    facets = build_instance_type_facets(items)
    x86 = next(item for item in facets.architectures if item.value == "x86")
    general = next(item for item in x86.types if item.value == "general")

    assert x86.count == 4
    assert general.count == 3
    assert [item.value for item in general.families] == ["g9i", "g8i", "g7"]


def test_text_and_three_level_facets_are_combined_with_and() -> None:
    items = [
        instance("alibaba", "ecs.g9i.large", "ecs.g9i"),
        instance("alibaba", "ecs.g8i.large", "ecs.g8i"),
        instance("alibaba", "ecs.c9i.large", "ecs.c9i"),
    ]

    result = filter_instance_types(
        items,
        CatalogFilters(
            query="通用型",
            architectureClass="x86",
            typeKind="general",
            familyToken="g9i",
        ),
    )

    assert [item.id for item in result] == ["ecs.g9i.large"]
