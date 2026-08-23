from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from looper_core.canonical import canonical_json
from looper_core.contracts import SearchParameter, StrictModel
from looper_core.system_opt.config_manifest import ConfigItem, ConfigValueType, ValueDomain


class DomainResolutionError(ValueError):
    pass


class DomainEvidence(StrictModel):
    item_id: str = Field(min_length=1, max_length=120)
    domain: ValueDomain
    verified: bool
    source: str = Field(min_length=1, max_length=1000)
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AuthorizedDomain(StrictModel):
    item_id: str = Field(min_length=1, max_length=120)
    domain: ValueDomain
    reason: str = Field(min_length=1, max_length=1000)


class ResolvedDomain(StrictModel):
    item_id: str
    parameter_id: str
    value_type: ConfigValueType
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: list[Any] | None = None
    log: bool
    sources: list[str]

    @model_validator(mode="after")
    def validate_nonempty(self) -> ResolvedDomain:
        if self.value_type in {ConfigValueType.INTEGER, ConfigValueType.NUMBER}:
            if self.minimum is None or self.maximum is None or self.minimum > self.maximum:
                raise ValueError("resolved numeric domain is empty")
        elif not self.choices:
            raise ValueError("resolved discrete domain is empty")
        return self

    def to_search_parameter(self, default: Any | None = None) -> SearchParameter:
        if self.value_type in {ConfigValueType.INTEGER, ConfigValueType.NUMBER}:
            return SearchParameter(
                type=self.value_type.value,
                minimum=self.minimum,
                maximum=self.maximum,
                step=self.step,
                log=self.log,
                default=default,
            )
        choices = list(self.choices or [])
        if self.value_type == ConfigValueType.BOOLEAN and set(choices) == {False, True}:
            return SearchParameter(type="boolean", default=default)
        return SearchParameter(type="categorical", choices=choices, default=default)


def _choice_intersection(*domains: ValueDomain) -> list[Any]:
    first = list(domains[0].choices or [False, True])
    remaining = [
        {canonical_json(value) for value in (domain.choices or [False, True])}
        for domain in domains[1:]
    ]
    return [
        value for value in first if all(canonical_json(value) in allowed for allowed in remaining)
    ]


def resolve_domain(
    item: ConfigItem,
    capability: DomainEvidence,
    authorization: AuthorizedDomain,
) -> ResolvedDomain:
    if capability.item_id != item.id or authorization.item_id != item.id:
        raise DomainResolutionError("domain evidence identity does not match the config item")
    if not capability.verified:
        raise DomainResolutionError(f"target domain for {item.id!r} is unverified")
    declared = item.domain
    target = capability.domain
    allowed = authorization.domain
    sources = [item.source, capability.source, authorization.reason]
    if item.value_type in {ConfigValueType.INTEGER, ConfigValueType.NUMBER}:
        minima = [domain.minimum for domain in (declared, target, allowed)]
        maxima = [domain.maximum for domain in (declared, target, allowed)]
        if any(value is None for value in minima + maxima):
            raise DomainResolutionError("numeric domain intersection requires all bounds")
        minimum = max(float(value) for value in minima if value is not None)
        maximum = min(float(value) for value in maxima if value is not None)
        if minimum > maximum:
            raise DomainResolutionError(f"dynamic domain for {item.id!r} is empty")
        step = declared.step
        if target.step not in {None, step} or allowed.step not in {None, step}:
            raise DomainResolutionError(
                "numeric domain steps differ; an explicit discrete intersection is required"
            )
        return ResolvedDomain(
            item_id=item.id,
            parameter_id=item.parameter_id,
            value_type=item.value_type,
            minimum=minimum,
            maximum=maximum,
            step=step,
            choices=None,
            log=declared.log,
            sources=sources,
        )
    choices = _choice_intersection(declared, target, allowed)
    if not choices:
        raise DomainResolutionError(f"dynamic domain for {item.id!r} is empty")
    return ResolvedDomain(
        item_id=item.id,
        parameter_id=item.parameter_id,
        value_type=item.value_type,
        choices=choices,
        log=False,
        sources=sources,
    )


__all__ = [
    "AuthorizedDomain",
    "DomainEvidence",
    "DomainResolutionError",
    "ResolvedDomain",
    "resolve_domain",
]
