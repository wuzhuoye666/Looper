from __future__ import annotations

import pytest
from looper_core.contracts import SearchParameter
from looper_core.system_opt.profiles import (
    ProfileCondition,
    ProfileExpansionError,
    ProfileRepository,
    TuningProfile,
    dry_run_diff,
    parse_profile_yaml,
    validate_search_parameter_mapping,
)
from system_opt_support import boolean_item, integer_item, manifest


def profile(
    profile_id: str,
    digest: str,
    *,
    includes: list[str] | None = None,
    settings: dict[str, object] | None = None,
    variables: dict[str, object] | None = None,
    conditions: list[ProfileCondition] | None = None,
) -> TuningProfile:
    return TuningProfile(
        id=profile_id,
        config_manifest_digest=digest,
        includes=includes or [],
        settings=settings or {},
        variables=variables or {},
        conditions=conditions or [],
        description=f"Profile {profile_id} used by a unit test.",
    )


def test_include_override_is_deterministic() -> None:
    config = manifest(integer_item())
    base = profile("base", config.digest, settings={"system.vm-swappiness": 60})
    child = profile(
        "child",
        config.digest,
        includes=["base"],
        settings={"system.vm-swappiness": 10},
    )

    first = ProfileRepository([base, child]).expand("child", config, target_facts={})
    second = ProfileRepository([child, base]).expand("child", config, target_facts={})

    assert first.settings == {"system.vm-swappiness": 10}
    assert first.sources["system.vm-swappiness"] == ["base", "child"]
    assert first.digest == second.digest


def test_include_cycle_and_unresolved_condition_fail_closed() -> None:
    config = manifest(integer_item())
    first = profile("first", config.digest, includes=["second"])
    second = profile("second", config.digest, includes=["first"])
    with pytest.raises(ProfileExpansionError, match="include cycle"):
        ProfileRepository([first, second]).expand("first", config, target_facts={})

    conditional = profile(
        "conditional",
        config.digest,
        settings={"system.vm-swappiness": 10},
        conditions=[ProfileCondition(fact="numa.nodeCount", operator="gt", value=1)],
    )
    with pytest.raises(ProfileExpansionError, match="unavailable"):
        ProfileRepository([conditional]).expand("conditional", config, target_facts={})


def test_false_child_condition_retains_included_profile_only() -> None:
    config = manifest(integer_item())
    base = profile("base", config.digest, settings={"system.vm-swappiness": 60})
    child = profile(
        "child",
        config.digest,
        includes=["base"],
        settings={"system.vm-swappiness": 10},
        conditions=[ProfileCondition(fact="numa.nodeCount", operator="gt", value=1)],
    )

    expanded = ProfileRepository([base, child]).expand(
        "child", config, target_facts={"numa.nodeCount": 1}
    )

    assert expanded.settings == {"system.vm-swappiness": 60}
    assert expanded.conditions[0].matched is False


def test_variables_are_exact_scalars_and_unresolved_values_fail() -> None:
    config = manifest(integer_item())
    valid = profile(
        "valid",
        config.digest,
        variables={"swappiness": 12},
        settings={"system.vm-swappiness": "${swappiness}"},
    )
    expanded = ProfileRepository([valid]).expand("valid", config, target_facts={})
    assert expanded.settings["system.vm-swappiness"] == 12

    invalid = profile(
        "invalid",
        config.digest,
        settings={"system.vm-swappiness": "prefix-${missing}"},
    )
    with pytest.raises(ProfileExpansionError, match="entire scalar"):
        ProfileRepository([invalid]).expand("invalid", config, target_facts={})


def test_dry_run_outputs_diff_without_executor_side_effects() -> None:
    config = manifest(integer_item(), boolean_item())
    desired = profile(
        "desired",
        config.digest,
        settings={
            "system.kernel-numa-balancing": False,
            "system.vm-swappiness": 10,
        },
    )
    expanded = ProfileRepository([desired]).expand("desired", config, target_facts={})

    diff = dry_run_diff(
        expanded,
        config,
        {"system.kernel-numa-balancing": True, "system.vm-swappiness": 60},
        pinned={"kernel-numa-balancing"},
    )

    assert [(item.item_id, item.status) for item in diff] == [
        ("kernel-numa-balancing", "pinned"),
        ("vm-swappiness", "change"),
    ]


def test_search_parameter_mapping_is_bidirectional_and_cannot_expand_domain() -> None:
    config = manifest(integer_item())
    mapping = validate_search_parameter_mapping(
        {
            "system.vm-swappiness": SearchParameter(
                type="integer", minimum=10, maximum=50, step=1, default=20
            )
        },
        config,
    )
    assert mapping == {"vm-swappiness": "system.vm-swappiness"}

    with pytest.raises(ProfileExpansionError, match="expands"):
        validate_search_parameter_mapping(
            {
                "system.vm-swappiness": SearchParameter(
                    type="integer", minimum=-1, maximum=101, step=1, default=20
                )
            },
            config,
        )


def test_yaml_parser_rejects_non_object_and_wrong_namespace() -> None:
    with pytest.raises(ProfileExpansionError, match="one object"):
        parse_profile_yaml("- one\n- two\n")

    content = """
schema_version: looper.system-tuning-profile/v1alpha1
id: invalid
config_manifest_digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
description: invalid namespace
settings:
  benchmark.threads: 2
"""
    with pytest.raises(ProfileExpansionError, match="system namespace"):
        parse_profile_yaml(content)
