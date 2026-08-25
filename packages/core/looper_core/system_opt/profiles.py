"""L5 配置档：Profile 展开、条件、变量解析与参数映射。

架构层：L5（docs/system-optimizer/architecture/overall.md）。
include/条件/变量展开必须确定且不越域；循环或未决变量 fail-closed。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import SearchParameter, StrictModel
from looper_core.system_opt.config_manifest import (
    CONFIG_MANIFEST_SCHEMA,
    SYSTEM_PARAMETER_PREFIX,
    ConfigManifest,
)

PROFILE_SCHEMA = "looper.system-tuning-profile/v1alpha1"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_VARIABLE_PATTERN = re.compile(r"^\$\{([a-zA-Z][a-zA-Z0-9_.-]*)\}$")
Scalar = str | int | float | bool


class ProfileExpansionError(ValueError):
    pass


class ProfileCondition(StrictModel):
    fact: str = Field(min_length=1, max_length=200)
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not-in"]
    value: Scalar | list[Scalar]


class TuningProfile(StrictModel):
    schema_version: Literal[PROFILE_SCHEMA] = PROFILE_SCHEMA
    id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9./-]*$")
    config_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    includes: list[str] = Field(default_factory=list)
    variables: dict[str, Scalar] = Field(default_factory=dict)
    conditions: list[ProfileCondition] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    description: str = Field(min_length=1, max_length=2000)

    @field_validator("includes")
    @classmethod
    def unique_includes(cls, includes: list[str]) -> list[str]:
        if len(includes) != len(set(includes)):
            raise ValueError("profile includes must be unique")
        return includes

    @model_validator(mode="after")
    def validate_keys(self) -> TuningProfile:
        invalid = sorted(
            key for key in self.settings if not key.startswith(SYSTEM_PARAMETER_PREFIX)
        )
        if invalid:
            raise ValueError(f"profile settings must use the system namespace: {invalid}")
        return self


class EvaluatedCondition(StrictModel):
    profile_id: str
    fact: str
    operator: str
    expected: Any
    actual: Any
    matched: bool


class ExpandedProfile(StrictModel):
    schema_version: Literal[PROFILE_SCHEMA] = PROFILE_SCHEMA
    profile_id: str
    config_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    settings: dict[str, Any]
    sources: dict[str, list[str]]
    variables: dict[str, Scalar]
    conditions: list[EvaluatedCondition]

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=True))


class ProfileDiff(StrictModel):
    parameter_id: str
    item_id: str
    current: Any | None = None
    requested: Any
    status: Literal["change", "unchanged", "pinned", "ownership-unknown", "unavailable"]
    source_profiles: list[str]


def parse_profile_yaml(content: str) -> TuningProfile:
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ProfileExpansionError("profile YAML is invalid") from error
    if not isinstance(payload, dict):
        raise ProfileExpansionError("profile YAML must contain one object")
    try:
        return TuningProfile.model_validate(payload)
    except ValueError as error:
        raise ProfileExpansionError(str(error)) from error


def _resolve_scalar(value: Any, variables: Mapping[str, Scalar]) -> Any:
    if not isinstance(value, str):
        return value
    match = _VARIABLE_PATTERN.fullmatch(value)
    if match is None:
        if "${" in value:
            raise ProfileExpansionError(
                "variables must occupy the entire scalar; interpolation is not supported"
            )
        return value
    name = match.group(1)
    if name not in variables:
        raise ProfileExpansionError(f"profile variable {name!r} is unresolved")
    return variables[name]


def _condition_matches(condition: ProfileCondition, actual: Any, expected: Any) -> bool:
    operator = condition.operator
    if operator == "eq":
        return canonical_json(actual) == canonical_json(expected)
    if operator == "ne":
        return canonical_json(actual) != canonical_json(expected)
    if operator in {"in", "not-in"}:
        if not isinstance(expected, list):
            raise ProfileExpansionError(f"condition {operator} requires a list value")
        contains = canonical_json(actual) in {canonical_json(value) for value in expected}
        return contains if operator == "in" else not contains
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        raise ProfileExpansionError(f"condition {operator} requires a numeric target fact")
    if isinstance(expected, bool) or not isinstance(expected, (int, float)):
        raise ProfileExpansionError(f"condition {operator} requires a numeric expected value")
    return {
        "gt": actual > expected,
        "gte": actual >= expected,
        "lt": actual < expected,
        "lte": actual <= expected,
    }[operator]


class ProfileRepository:
    def __init__(self, profiles: list[TuningProfile]) -> None:
        self._profiles = {profile.id: profile for profile in profiles}
        if len(self._profiles) != len(profiles):
            raise ProfileExpansionError("profile ids must be unique")

    def profile(self, profile_id: str) -> TuningProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as error:
            raise ProfileExpansionError(f"profile {profile_id!r} is not registered") from error

    def expand(
        self,
        profile_id: str,
        manifest: ConfigManifest,
        *,
        target_facts: Mapping[str, Any],
    ) -> ExpandedProfile:
        ordered: list[TuningProfile] = []
        visiting: list[str] = []
        visited: set[str] = set()

        def visit(current_id: str) -> None:
            if current_id in visiting:
                cycle = " -> ".join([*visiting, current_id])
                raise ProfileExpansionError(f"profile include cycle: {cycle}")
            if current_id in visited:
                return
            current = self.profile(current_id)
            if current.config_manifest_digest != manifest.digest:
                raise ProfileExpansionError(
                    f"profile {current.id!r} references a different Config Manifest digest"
                )
            visiting.append(current_id)
            for included in current.includes:
                visit(included)
            visiting.pop()
            visited.add(current_id)
            ordered.append(current)

        visit(profile_id)

        active_profiles: list[TuningProfile] = []
        variables: dict[str, Scalar] = {}
        evaluated: list[EvaluatedCondition] = []
        for profile in ordered:
            candidate_variables = {**variables, **profile.variables}
            matched = True
            for condition in profile.conditions:
                if condition.fact not in target_facts or target_facts[condition.fact] is None:
                    raise ProfileExpansionError(
                        f"target fact {condition.fact!r} is unavailable for profile {profile.id!r}"
                    )
                actual = target_facts[condition.fact]
                expected = _resolve_scalar(condition.value, candidate_variables)
                condition_match = _condition_matches(condition, actual, expected)
                evaluated.append(
                    EvaluatedCondition(
                        profile_id=profile.id,
                        fact=condition.fact,
                        operator=condition.operator,
                        expected=expected,
                        actual=actual,
                        matched=condition_match,
                    )
                )
                matched = matched and condition_match
            if matched:
                variables = candidate_variables
                active_profiles.append(profile)

        settings: dict[str, Any] = {}
        sources: dict[str, list[str]] = {}
        for profile in active_profiles:
            for parameter_id, raw_value in profile.settings.items():
                value = _resolve_scalar(raw_value, variables)
                try:
                    item = manifest.item_for_parameter(parameter_id)
                except KeyError as error:
                    raise ProfileExpansionError(
                        f"profile {profile.id!r} references unknown item {parameter_id!r}"
                    ) from error
                try:
                    item.validate_value(value)
                except ValueError as error:
                    raise ProfileExpansionError(str(error)) from error
                settings[parameter_id] = value
                sources.setdefault(parameter_id, []).append(profile.id)

        return ExpandedProfile(
            profile_id=profile_id,
            config_manifest_digest=manifest.digest,
            settings={key: settings[key] for key in sorted(settings)},
            sources={key: sources[key] for key in sorted(sources)},
            variables={key: variables[key] for key in sorted(variables)},
            conditions=evaluated,
        )


def dry_run_diff(
    expanded: ExpandedProfile,
    manifest: ConfigManifest,
    current_values: Mapping[str, Any],
    *,
    pinned: set[str] | None = None,
    ownership_unknown: set[str] | None = None,
) -> list[ProfileDiff]:
    pinned_ids = pinned or set()
    unknown_ids = ownership_unknown or set()
    result: list[ProfileDiff] = []
    for parameter_id, requested in expanded.settings.items():
        item = manifest.item_for_parameter(parameter_id)
        if item.id in pinned_ids:
            status = "pinned"
        elif item.id in unknown_ids:
            status = "ownership-unknown"
        elif parameter_id not in current_values:
            status = "unavailable"
        elif canonical_json(current_values[parameter_id]) == canonical_json(requested):
            status = "unchanged"
        else:
            status = "change"
        result.append(
            ProfileDiff(
                parameter_id=parameter_id,
                item_id=item.id,
                current=current_values.get(parameter_id),
                requested=requested,
                status=status,
                source_profiles=expanded.sources[parameter_id],
            )
        )
    return result


def validate_search_parameter_mapping(
    search_space: Mapping[str, SearchParameter], manifest: ConfigManifest
) -> dict[str, str]:
    reverse: dict[str, str] = {}
    for parameter_id, parameter in search_space.items():
        try:
            item = manifest.item_for_parameter(parameter_id)
        except KeyError as error:
            raise ProfileExpansionError(
                f"system search parameter {parameter_id!r} has no Config Manifest item"
            ) from error
        if not item.searchable:
            raise ProfileExpansionError(f"Config Manifest item {item.id!r} is not searchable")
        expected = item.to_search_parameter()
        if parameter.type != expected.type:
            raise ProfileExpansionError(f"search parameter {parameter_id!r} has a type mismatch")
        if parameter.type in {"integer", "number"}:
            assert expected.minimum is not None and expected.maximum is not None
            assert parameter.minimum is not None and parameter.maximum is not None
            if parameter.minimum < expected.minimum or parameter.maximum > expected.maximum:
                raise ProfileExpansionError(
                    f"search parameter {parameter_id!r} expands the manifest numeric domain"
                )
        elif parameter.type == "categorical":
            expected_choices = {canonical_json(value) for value in expected.choices or []}
            actual_choices = {canonical_json(value) for value in parameter.choices or []}
            if not actual_choices <= expected_choices:
                raise ProfileExpansionError(
                    f"search parameter {parameter_id!r} expands the manifest choices"
                )
        if parameter.default is not None:
            try:
                item.validate_value(parameter.default)
            except ValueError as error:
                raise ProfileExpansionError(str(error)) from error
        reverse[item.id] = parameter_id
    return reverse


def validate_profile_schema_identity(profile: TuningProfile) -> None:
    if profile.schema_version != PROFILE_SCHEMA:
        raise ProfileExpansionError("unsupported profile schema")
    if not profile.config_manifest_digest.startswith("sha256:"):
        raise ProfileExpansionError("profile Config Manifest identity is invalid")


__all__ = [
    "CONFIG_MANIFEST_SCHEMA",
    "ExpandedProfile",
    "PROFILE_SCHEMA",
    "ProfileDiff",
    "ProfileExpansionError",
    "ProfileRepository",
    "TuningProfile",
    "dry_run_diff",
    "parse_profile_yaml",
    "validate_search_parameter_mapping",
]
