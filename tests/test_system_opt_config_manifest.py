from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from looper_core.canonical import canonical_digest
from looper_core.contracts import (
    Direction,
    ExperimentSpec,
    ObjectiveSpec,
    SearchParameter,
    SystemTuningSpec,
)
from looper_core.system_opt.config_manifest import (
    ActivationMode,
    ConfigItem,
    ConfigManifest,
    RiskLevel,
    SystemTuningBinding,
    parse_config_manifest_yaml,
)
from pydantic import ValidationError
from system_opt_support import boolean_item, integer_item, manifest


def test_manifest_digest_is_canonical() -> None:
    first = manifest(integer_item(), boolean_item(), metadata={"b": 2, "a": 1})
    second = manifest(integer_item(), boolean_item(), metadata={"a": 1, "b": 2})

    assert first.digest == second.digest
    assert first.digest.startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("read", None, "read"),
        ("default", 101, "outside the numeric domain"),
    ],
)
def test_rejects_missing_read_and_out_of_domain_default(
    field: str, value: object, message: str
) -> None:
    payload = integer_item().model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError, match=message):
        ConfigItem.model_validate(payload)


def test_observation_only_item_can_preserve_unknown_default_and_domain() -> None:
    payload = integer_item().model_dump(mode="python")
    payload.update(
        {
            "default": None,
            "domain": {
                "minimum": None,
                "maximum": None,
                "step": None,
                "choices": None,
                "log": False,
            },
            "apply": None,
            "searchable": False,
        }
    )

    item = ConfigItem.model_validate(payload)
    assert item.default is None
    assert item.validate_readback(2400000) == 2400000

    payload["searchable"] = True
    with pytest.raises(ValidationError, match="explicit default"):
        ConfigItem.model_validate(payload)


def test_m1_documented_manifest_preserves_exact_20_item_counting_basis() -> None:
    path = (
        Path(__file__).parents[1]
        / "examples"
        / "system-optimizer"
        / "wsl2-m1-20-observation-manifest.yaml"
    )
    documented = parse_config_manifest_yaml(path.read_text(encoding="utf-8"))

    assert len(documented.items) == 20
    assert Counter(item.category.value for item in documented.items) == {
        "sysctl": 3,
        "cpufreq": 3,
        "thp": 3,
        "io": 3,
        "numa": 3,
        "net": 3,
        "irq": 1,
        "other": 1,
    }
    assert all(item.default is None for item in documented.items)
    assert all(not item.searchable and item.apply is None for item in documented.items)
    assert all(item.source.startswith("https://docs.kernel.org/") for item in documented.items)


def test_rejects_high_risk_item_without_reason() -> None:
    payload = integer_item().model_dump(mode="python")
    payload.update({"risk": RiskLevel.HIGH, "risk_reason": None, "searchable": False})

    with pytest.raises(ValidationError, match="risk reason"):
        ConfigItem.model_validate(payload)


def test_rejects_reboot_item_in_search_space() -> None:
    payload = integer_item().model_dump(mode="python")
    payload["activation"] = ActivationMode.REBOOT

    with pytest.raises(ValidationError, match="observation-only"):
        ConfigItem.model_validate(payload)


def test_blacklisted_item_is_observation_only() -> None:
    payload = integer_item().model_dump(mode="python")
    payload.update(
        {
            "target": "kernel.panic_on_oops",
            "apply": None,
            "searchable": False,
            "risk": RiskLevel.HIGH,
            "risk_reason": "Changing panic behavior can make the target unreachable.",
        }
    )
    item = ConfigItem.model_validate(payload)
    assert item.permanently_blacklisted

    payload["apply"] = integer_item().apply.model_dump(mode="python")
    with pytest.raises(ValidationError, match="permanently blacklisted"):
        ConfigItem.model_validate(payload)


def test_manifest_rejects_unknown_dependencies_and_cycles() -> None:
    unknown = integer_item(dependencies=["not-registered"])
    with pytest.raises(ValidationError, match="unknown dependencies"):
        manifest(unknown)

    first = integer_item("first", target="vm.first", dependencies=["second"])
    second = integer_item("second", target="vm.second", dependencies=["first"])
    with pytest.raises(ValidationError, match="cycle"):
        manifest(first, second)


def test_search_parameter_mapping_is_stable_and_bidirectional() -> None:
    config = manifest(boolean_item(), integer_item())
    search = config.search_parameters()

    assert list(search) == ["system.kernel-numa-balancing", "system.vm-swappiness"]
    assert search["system.vm-swappiness"].minimum == 0
    assert config.item_for_parameter("system.vm-swappiness").id == "vm-swappiness"
    with pytest.raises(KeyError):
        config.item_for_parameter("benchmark.vm-swappiness")


def test_system_tuning_binding_requires_complete_profile_identity_and_change_reason() -> None:
    digest = "sha256:" + "a" * 64
    with pytest.raises(ValidationError, match="declared together"):
        SystemTuningBinding(
            config_manifest_id="linux",
            config_manifest_digest=digest,
            profile_id="baseline",
        )
    with pytest.raises(ValidationError, match="explicit reason"):
        SystemTuningBinding(
            config_manifest_id="linux",
            config_manifest_digest=digest,
            max_changes=6,
        )


def test_manifest_orders_dependencies_before_dependents() -> None:
    base = integer_item("base", target="vm.base")
    child = integer_item("child", target="vm.child", dependencies=["base"])
    config = ConfigManifest(
        id="ordering",
        version="1",
        description="Dependency ordering test.",
        items=[child, base],
    )

    assert [item.id for item in config.ordered_items()] == ["base", "child"]


def test_optional_system_tuning_preserves_legacy_experiment_digest() -> None:
    spec = ExperimentSpec(
        benchmark_id="legacy",
        benchmark_version="1",
        objectives=[ObjectiveSpec(metric="score", unit="score", direction=Direction.MAXIMIZE)],
    )
    payload = spec.model_dump(mode="json")

    assert "system_tuning" not in payload
    assert canonical_digest(payload) == (
        "sha256:b9687fc2fc42093745876c42be7401f2f6a822deae65788da7cef1a97ff1122f"
    )


def test_system_tuning_requires_namespaces_and_serializes_identity() -> None:
    digest = "sha256:" + "b" * 64
    binding = SystemTuningSpec(
        config_manifest_id="linux",
        config_manifest_digest=digest,
    )
    common = {
        "benchmark_id": "demo",
        "benchmark_version": "1",
        "objectives": [ObjectiveSpec(metric="score", unit="score", direction=Direction.MAXIMIZE)],
        "system_tuning": binding,
    }
    with pytest.raises(ValidationError, match="namespaces"):
        ExperimentSpec(
            **common,
            search_space={"threads": SearchParameter(type="integer", minimum=1, maximum=2)},
            baseline_parameters={"threads": 1},
        )

    spec = ExperimentSpec(
        **common,
        search_space={
            "system.vm-swappiness": SearchParameter(type="integer", minimum=0, maximum=100),
            "benchmark.threads": SearchParameter(type="integer", minimum=1, maximum=2),
        },
        baseline_parameters={"system.vm-swappiness": 60, "benchmark.threads": 1},
    )
    assert spec.model_dump(mode="json")["system_tuning"]["config_manifest_digest"] == digest
