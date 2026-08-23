from __future__ import annotations

from typing import Any

from looper_core.system_opt.config_manifest import (
    ActivationMode,
    CommandTemplate,
    CompatibilitySpec,
    ConfigCategory,
    ConfigComponent,
    ConfigItem,
    ConfigManifest,
    ConfigValueType,
    Precondition,
    ReadSpec,
    RiskLevel,
    RollbackMode,
    RollbackSpec,
    ValueDomain,
    ValueParser,
)


def command(*argv: str) -> CommandTemplate:
    return CommandTemplate(argv=list(argv), timeout_seconds=5)


def integer_item(
    item_id: str = "vm-swappiness",
    *,
    target: str = "vm.swappiness",
    default: int = 60,
    dependencies: list[str] | None = None,
    category: ConfigCategory = ConfigCategory.SYSCTL,
    preconditions: list[Precondition] | None = None,
) -> ConfigItem:
    return ConfigItem(
        id=item_id,
        category=category,
        primary_component=ConfigComponent.MEMORY,
        related_components=[ConfigComponent.NUMA],
        target=target,
        value_type=ConfigValueType.INTEGER,
        domain=ValueDomain(minimum=0, maximum=100, step=1, choices=None, log=False),
        default=default,
        read=ReadSpec(command=command("sysctl", "-n", "{target}"), parser=ValueParser.INTEGER),
        apply=command("sysctl", "-w", "{target}={value}"),
        rollback=RollbackSpec(mode=RollbackMode.RESTORE_SNAPSHOT),
        activation=ActivationMode.IMMEDIATE,
        risk=RiskLevel.LOW,
        dependencies=dependencies or [],
        preconditions=preconditions or [],
        compatibility=CompatibilitySpec(required_commands=["sysctl"]),
        searchable=True,
        description="Synthetic integer item used by System Optimizer unit tests.",
        source="test fixture",
    )


def boolean_item(
    item_id: str = "kernel-numa-balancing",
    *,
    target: str = "kernel.numa_balancing",
    default: bool = True,
    category: ConfigCategory = ConfigCategory.NUMA,
) -> ConfigItem:
    return ConfigItem(
        id=item_id,
        category=category,
        primary_component=ConfigComponent.NUMA,
        related_components=[ConfigComponent.MEMORY],
        target=target,
        value_type=ConfigValueType.BOOLEAN,
        domain=ValueDomain(minimum=None, maximum=None, step=None, choices=None, log=False),
        default=default,
        read=ReadSpec(
            command=command("sysctl", "-n", "{target}"),
            parser=ValueParser.BOOLEAN,
            true_values=["1"],
            false_values=["0"],
        ),
        apply=command("sysctl", "-w", "{target}={value}"),
        rollback=RollbackSpec(mode=RollbackMode.RESTORE_SNAPSHOT),
        activation=ActivationMode.IMMEDIATE,
        risk=RiskLevel.LOW,
        compatibility=CompatibilitySpec(required_commands=["sysctl"]),
        searchable=True,
        value_aliases={"false": "0", "true": "1"},
        description="Synthetic boolean item used by System Optimizer unit tests.",
        source="test fixture",
    )


def categorical_item(
    item_id: str = "thp-enabled",
    *,
    default: str = "madvise",
) -> ConfigItem:
    return ConfigItem(
        id=item_id,
        category=ConfigCategory.THP,
        primary_component=ConfigComponent.MEMORY,
        related_components=[ConfigComponent.NUMA],
        target="/sys/kernel/mm/transparent_hugepage/enabled",
        value_type=ConfigValueType.CATEGORICAL,
        domain=ValueDomain(
            minimum=None,
            maximum=None,
            step=None,
            choices=["always", "madvise", "never"],
            log=False,
        ),
        default=default,
        read=ReadSpec(
            command=command("read-file", "{target}"), parser=ValueParser.BRACKET_SELECTED
        ),
        apply=command("write-file", "{target}", "{value}"),
        rollback=RollbackSpec(mode=RollbackMode.RESTORE_SNAPSHOT),
        activation=ActivationMode.IMMEDIATE,
        risk=RiskLevel.MEDIUM,
        compatibility=CompatibilitySpec(
            required_paths=["/sys/kernel/mm/transparent_hugepage/enabled"]
        ),
        searchable=True,
        description="Synthetic categorical item used by System Optimizer unit tests.",
        source="test fixture",
    )


def manifest(*items: ConfigItem, metadata: dict[str, Any] | None = None) -> ConfigManifest:
    return ConfigManifest(
        id="test-linux-guest",
        version="1",
        description="Synthetic Config Manifest used only by unit tests.",
        items=list(items) or [integer_item()],
        metadata=metadata or {},
    )
