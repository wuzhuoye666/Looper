from __future__ import annotations

import math
import string
from enum import StrEnum
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import SearchParameter, StrictModel, SystemTuningSpec

SYSTEM_PARAMETER_PREFIX = "system."
CONFIG_MANIFEST_SCHEMA = "looper.system-config-manifest/v1alpha1"
_ALLOWED_COMMAND_PLACEHOLDERS = frozenset({"snapshot", "target", "value"})


class ConfigManifestError(ValueError):
    pass


class ConfigCategory(StrEnum):
    SYSCTL = "sysctl"
    CPUFREQ = "cpufreq"
    THP = "thp"
    IO = "io"
    NET = "net"
    NUMA = "numa"
    IRQ = "irq"
    OTHER = "other"


class ConfigComponent(StrEnum):
    CPU = "cpu"
    MEMORY = "memory"
    NUMA = "numa"
    STORAGE = "storage"
    NETWORK = "network"
    SCHEDULER = "scheduler"
    STABILITY = "stability"
    OTHER = "other"


class ConfigValueType(StrEnum):
    INTEGER = "integer"
    NUMBER = "number"
    CATEGORICAL = "categorical"
    BOOLEAN = "boolean"


class ActivationMode(StrEnum):
    IMMEDIATE = "immediate"
    REBOOT = "reboot"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ValueParser(StrEnum):
    RAW = "raw"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    BRACKET_SELECTED = "bracket-selected"


class RollbackMode(StrEnum):
    RESTORE_SNAPSHOT = "restore-snapshot"
    COMMAND = "command"


class ValueDomain(StrictModel):
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(default=None, gt=0)
    choices: list[Any] | None = None
    log: bool

    @model_validator(mode="after")
    def validate_range(self) -> ValueDomain:
        if self.minimum is not None and not math.isfinite(self.minimum):
            raise ValueError("domain minimum must be finite")
        if self.maximum is not None and not math.isfinite(self.maximum):
            raise ValueError("domain maximum must be finite")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("domain minimum cannot exceed maximum")
        if self.choices is not None:
            encoded = [canonical_json(value) for value in self.choices]
            if len(encoded) != len(set(encoded)):
                raise ValueError("domain choices must be unique")
        return self


class CommandTemplate(StrictModel):
    argv: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0, le=300)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, argv: list[str]) -> list[str]:
        formatter = string.Formatter()
        for argument in argv:
            if not argument or "\x00" in argument or "\n" in argument or "\r" in argument:
                raise ValueError("command arguments must be non-empty single-line strings")
            try:
                fields = {
                    field_name
                    for _, field_name, _, _ in formatter.parse(argument)
                    if field_name is not None
                }
            except ValueError as error:
                raise ValueError("command contains an invalid placeholder") from error
            unsupported = fields - _ALLOWED_COMMAND_PLACEHOLDERS
            if unsupported:
                raise ValueError(f"unsupported command placeholders: {sorted(unsupported)}")
        return argv

    def render(self, **values: str) -> list[str]:
        missing = set(values) - _ALLOWED_COMMAND_PLACEHOLDERS
        if missing:
            raise ValueError(f"unsupported render values: {sorted(missing)}")
        try:
            return [argument.format_map(values) for argument in self.argv]
        except KeyError as error:
            raise ValueError(f"missing command placeholder: {error.args[0]}") from error

    def placeholders(self) -> set[str]:
        formatter = string.Formatter()
        return {
            field_name
            for argument in self.argv
            for _, field_name, _, _ in formatter.parse(argument)
            if field_name is not None
        }


class ReadSpec(StrictModel):
    command: CommandTemplate
    parser: ValueParser
    true_values: list[str] = Field(default_factory=list)
    false_values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_boolean_tokens(self) -> ReadSpec:
        if self.parser == ValueParser.BOOLEAN:
            true_values = {value.strip().lower() for value in self.true_values}
            false_values = {value.strip().lower() for value in self.false_values}
            if not true_values or not false_values or true_values & false_values:
                raise ValueError("boolean parser tokens must be non-empty and disjoint")
        return self


class RollbackSpec(StrictModel):
    mode: RollbackMode
    command: CommandTemplate | None = None

    @model_validator(mode="after")
    def validate_command(self) -> RollbackSpec:
        if self.mode == RollbackMode.COMMAND and self.command is None:
            raise ValueError("command rollback requires a command")
        if self.mode == RollbackMode.RESTORE_SNAPSHOT and self.command is not None:
            raise ValueError("restore-snapshot rollback cannot declare a command")
        return self


class Precondition(StrictModel):
    kind: Literal["capability", "command", "fact", "path"]
    key: str = Field(min_length=1, max_length=500)
    operator: Literal["eq", "exists", "gte", "gt", "in"]
    value: Any | None = None

    @model_validator(mode="after")
    def validate_value(self) -> Precondition:
        if self.operator != "exists" and self.value is None:
            raise ValueError("non-existence preconditions require a value")
        return self


class CompatibilitySpec(StrictModel):
    kernel_min: str | None = Field(default=None, max_length=80)
    kernel_max: str | None = Field(default=None, max_length=80)
    distributions: list[str] = Field(default_factory=list)
    architectures: list[str] = Field(default_factory=list)
    required_paths: list[str] = Field(default_factory=list)
    required_commands: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_explicit_constraint(self) -> CompatibilitySpec:
        if not any(
            (
                self.kernel_min,
                self.kernel_max,
                self.distributions,
                self.architectures,
                self.required_paths,
                self.required_commands,
            )
        ):
            raise ValueError("compatibility must declare at least one explicit constraint")
        return self

    @field_validator("distributions", "architectures", "required_paths", "required_commands")
    @classmethod
    def reject_duplicates(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("compatibility constraints must be unique")
        return values


def is_permanently_blacklisted(target: str) -> bool:
    normalized = target.strip().lower()
    return (
        normalized == "kernel.panic"
        or normalized.startswith("kernel.panic_")
        or normalized == "vm.panic_on_oom"
        or normalized
        in {
            "net.ipv4.ip_forward",
            "net.ipv4.conf.all.forwarding",
            "net.ipv6.conf.all.forwarding",
        }
        or normalized.startswith("ssh.")
        or normalized.startswith("sshd.")
        or normalized.startswith("network.route.")
        or normalized.startswith("/etc/ssh/")
    )


class ConfigItem(StrictModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9.-]*$")
    category: ConfigCategory
    primary_component: ConfigComponent
    related_components: list[ConfigComponent]
    target: str = Field(min_length=1, max_length=500)
    value_type: ConfigValueType
    domain: ValueDomain
    default: Any | None = None
    read: ReadSpec
    apply: CommandTemplate | None = None
    rollback: RollbackSpec
    activation: ActivationMode
    risk: RiskLevel
    risk_reason: str | None = Field(default=None, min_length=1, max_length=1000)
    dependencies: list[str] = Field(default_factory=list)
    preconditions: list[Precondition] = Field(default_factory=list)
    compatibility: CompatibilitySpec
    searchable: bool
    value_aliases: dict[str, str] = Field(default_factory=dict)
    description: str = Field(min_length=1, max_length=1000)
    source: str = Field(min_length=1, max_length=1000)

    @field_validator("dependencies")
    @classmethod
    def unique_dependencies(cls, dependencies: list[str]) -> list[str]:
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("config item dependencies must be unique")
        return dependencies

    @field_validator("related_components")
    @classmethod
    def unique_related_components(cls, components: list[ConfigComponent]) -> list[ConfigComponent]:
        if len(components) != len(set(components)):
            raise ValueError("related components must be unique")
        return components

    @model_validator(mode="after")
    def validate_item(self) -> ConfigItem:
        if self.default is None:
            if self.searchable:
                raise ValueError("searchable config items require an explicit default")
        else:
            self.validate_value(self.default)
        if self.id in self.dependencies:
            raise ValueError("config item cannot depend on itself")
        if self.risk == RiskLevel.HIGH and not self.risk_reason:
            raise ValueError("high-risk config items require a risk reason")
        if self.risk == RiskLevel.HIGH and self.searchable:
            raise ValueError("high-risk config items cannot be searchable by default")
        if self.activation == ActivationMode.REBOOT and self.searchable:
            raise ValueError("reboot config items are observation-only")
        blacklisted = is_permanently_blacklisted(self.target)
        if blacklisted and (self.searchable or self.apply is not None):
            raise ValueError("permanently blacklisted targets must be observation-only")
        if self.searchable and self.apply is None:
            raise ValueError("searchable config items require an apply command")
        if self.apply is not None and "value" not in self.apply.placeholders():
            raise ValueError("apply commands must include the {value} placeholder")
        if (
            self.value_type == ConfigValueType.BOOLEAN
            and self.searchable
            and set(self.value_aliases) != {"false", "true"}
        ):
            raise ValueError("searchable boolean items require explicit false/true value aliases")
        return self

    @property
    def parameter_id(self) -> str:
        return f"{SYSTEM_PARAMETER_PREFIX}{self.id}"

    @property
    def permanently_blacklisted(self) -> bool:
        return is_permanently_blacklisted(self.target)

    def validate_value(self, value: Any) -> Any:
        self.validate_readback(value)
        if self.value_type == ConfigValueType.BOOLEAN:
            return value
        if self.value_type in {ConfigValueType.INTEGER, ConfigValueType.NUMBER}:
            numeric = float(value)
        else:
            if not self.domain.choices:
                raise ValueError(f"{self.id} categorical domain requires choices")
            encoded = canonical_json(value)
            if encoded not in {canonical_json(choice) for choice in self.domain.choices}:
                raise ValueError(f"{self.id} value is outside the categorical domain")
            return value

        if self.domain.minimum is None or self.domain.maximum is None:
            raise ValueError(f"{self.id} numeric domain requires minimum and maximum")
        if numeric < self.domain.minimum or numeric > self.domain.maximum:
            raise ValueError(f"{self.id} value is outside the numeric domain")
        if self.domain.log and self.domain.minimum <= 0:
            raise ValueError(f"{self.id} logarithmic domain requires a positive minimum")
        if self.domain.step is not None:
            steps = (numeric - self.domain.minimum) / self.domain.step
            if not math.isclose(steps, round(steps), abs_tol=1e-9):
                raise ValueError(f"{self.id} value is not aligned to the configured step")
        return value

    def validate_readback(self, value: Any) -> Any:
        if self.value_type == ConfigValueType.BOOLEAN:
            if type(value) is not bool:
                raise ValueError(f"{self.id} requires a boolean value")
        elif self.value_type == ConfigValueType.INTEGER:
            if type(value) is not int:
                raise ValueError(f"{self.id} requires an integer value")
        elif self.value_type == ConfigValueType.NUMBER:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{self.id} requires a numeric value")
            if not math.isfinite(float(value)):
                raise ValueError(f"{self.id} requires a finite numeric value")
        elif not isinstance(value, str):
            raise ValueError(f"{self.id} requires a categorical string value")
        return value

    def encode_value(self, value: Any) -> str:
        self.validate_value(value)
        alias_key = canonical_json(value)
        if alias_key in self.value_aliases:
            return self.value_aliases[alias_key]
        if isinstance(value, bool):
            raise ValueError(f"{self.id} has no explicit alias for {alias_key}")
        return str(value)

    def to_search_parameter(self) -> SearchParameter:
        if not self.searchable:
            raise ValueError(f"{self.id} is not searchable")
        if self.value_type in {ConfigValueType.INTEGER, ConfigValueType.NUMBER}:
            return SearchParameter(
                type=self.value_type.value,
                minimum=self.domain.minimum,
                maximum=self.domain.maximum,
                step=self.domain.step,
                log=self.domain.log,
                default=self.default,
            )
        if self.value_type == ConfigValueType.CATEGORICAL:
            return SearchParameter(
                type="categorical", choices=self.domain.choices, default=self.default
            )
        return SearchParameter(type="boolean", default=self.default)


class ConfigManifest(StrictModel):
    schema_version: Literal[CONFIG_MANIFEST_SCHEMA] = CONFIG_MANIFEST_SCHEMA
    id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9.-]*$")
    version: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=2000)
    items: list[ConfigItem] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self) -> ConfigManifest:
        by_id = {item.id: item for item in self.items}
        if len(by_id) != len(self.items):
            raise ValueError("config item ids must be unique")
        for item in self.items:
            missing = set(item.dependencies) - set(by_id)
            if missing:
                raise ValueError(f"{item.id} has unknown dependencies: {sorted(missing)}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(item_id: str) -> None:
            if item_id in visiting:
                raise ValueError("config item dependency graph contains a cycle")
            if item_id in visited:
                return
            visiting.add(item_id)
            for dependency in by_id[item_id].dependencies:
                visit(dependency)
            visiting.remove(item_id)
            visited.add(item_id)

        for item_id in sorted(by_id):
            visit(item_id)
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=True))

    def item(self, item_id: str) -> ConfigItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def item_for_parameter(self, parameter_id: str) -> ConfigItem:
        if not parameter_id.startswith(SYSTEM_PARAMETER_PREFIX):
            raise KeyError(parameter_id)
        return self.item(parameter_id.removeprefix(SYSTEM_PARAMETER_PREFIX))

    def search_parameters(self) -> dict[str, SearchParameter]:
        return {
            item.parameter_id: item.to_search_parameter()
            for item in sorted(self.items, key=lambda candidate: candidate.id)
            if item.searchable
        }

    def ordered_items(self, item_ids: set[str] | None = None) -> list[ConfigItem]:
        selected = item_ids if item_ids is not None else {item.id for item in self.items}
        by_id = {item.id: item for item in self.items}
        ordered: list[ConfigItem] = []
        visited: set[str] = set()

        def visit(item_id: str) -> None:
            if item_id in visited:
                return
            for dependency in sorted(by_id[item_id].dependencies):
                if dependency in selected:
                    visit(dependency)
            visited.add(item_id)
            ordered.append(by_id[item_id])

        for item_id in sorted(selected):
            if item_id not in by_id:
                raise KeyError(item_id)
            visit(item_id)
        return ordered


SystemTuningBinding = SystemTuningSpec


def parse_config_manifest_yaml(content: str) -> ConfigManifest:
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ConfigManifestError("Config Manifest YAML is invalid") from error
    if not isinstance(payload, dict):
        raise ConfigManifestError("Config Manifest YAML must contain one object")
    try:
        return ConfigManifest.model_validate(payload)
    except ValueError as error:
        raise ConfigManifestError(str(error)) from error
