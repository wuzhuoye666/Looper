"""Runtime bridge for business-retest hypothesis refutations and L7 storage."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigComponent
from looper_core.system_opt.hypothesis import (
    ComponentHypothesis,
    InterventionExperiment,
    SymptomRecord,
)
from looper_core.system_opt.negative_cache import (
    HYPOTHESIS_SEMANTICS_VERSION,
    HypothesisCacheRetentionPolicy,
    HypothesisNegativeCacheEntry,
    HypothesisNegativeCacheIdentity,
    NegativeCache,
    formula_versions_digest,
)
from looper_core.system_opt.policy import MetricContract

_DIGEST = r"^sha256:[0-9a-f]{64}$"


class HypothesisCacheBinding(StrictModel):
    """Task-provided identities that cannot be inferred from a refute digest."""

    environment_digest: str = Field(pattern=_DIGEST)
    workload_contract_digest: str = Field(pattern=_DIGEST)
    symptom_class_digest: str = Field(pattern=_DIGEST)
    metric_contract: MetricContract
    refutation_policy_digest: str = Field(pattern=_DIGEST)
    formula_versions: dict[str, str] = Field(min_length=1)
    hypothesis_semantics_version: str = HYPOTHESIS_SEMANTICS_VERSION

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

    def identity_for_component(
        self, component: ConfigComponent
    ) -> HypothesisNegativeCacheIdentity:
        return HypothesisNegativeCacheIdentity(
            environment_digest=self.environment_digest,
            workload_identity_digest=self.workload_contract_digest,
            component=component,
            symptom_class_digest=self.symptom_class_digest,
            metric_contract_digest=canonical_digest(
                self.metric_contract.model_dump(mode="json")
            ),
            refutation_policy_digest=self.refutation_policy_digest,
            formula_versions_digest=formula_versions_digest(self.formula_versions),
            hypothesis_semantics_version=self.hypothesis_semantics_version,
        )

    def identity_for(self, hypothesis: ComponentHypothesis) -> HypothesisNegativeCacheIdentity:
        return self.identity_for_component(hypothesis.component)


class HypothesisCacheRuntime:
    """Consult and append hypothesis refutations under one explicit lifecycle policy."""

    def __init__(
        self,
        *,
        cache: NegativeCache,
        path: Path,
        binding: HypothesisCacheBinding,
        retention_policy: HypothesisCacheRetentionPolicy,
    ) -> None:
        self._cache = cache
        self._path = path
        self._binding = binding
        self._retention_policy = retention_policy

    @property
    def entries(self) -> list[HypothesisNegativeCacheEntry]:
        return self._cache.hypothesis_entries

    def excluded_components(
        self,
        hypotheses: Mapping[str, ComponentHypothesis],
        *,
        at: datetime,
    ) -> set[str]:
        excluded: set[str] = set()
        for hypothesis in hypotheses.values():
            identity = self._binding.identity_for(hypothesis)
            if self._cache.lookup_hypothesis(
                identity,
                retention_policy=self._retention_policy,
                at=at,
            ):
                excluded.add(hypothesis.component.value)
        return excluded

    def excluded_proposal_components(
        self,
        components: Mapping[str, ConfigComponent],
        *,
        at: datetime,
    ) -> set[str]:
        excluded: set[str] = set()
        for component in components.values():
            identity = self._binding.identity_for_component(component)
            if self._cache.lookup_hypothesis(
                identity,
                retention_policy=self._retention_policy,
                at=at,
            ):
                excluded.add(component.value)
        return excluded

    def record_refutation(
        self,
        hypothesis: ComponentHypothesis,
        symptom: SymptomRecord,
        experiment: InterventionExperiment,
        *,
        recorded_at: datetime,
    ) -> HypothesisNegativeCacheEntry:
        """Persist only the loop's stable, comparable business-retest rejection."""

        if experiment.accepted:
            raise ValueError("accepted experiments cannot enter the hypothesis negative cache")
        if experiment.business_metric_id != self._binding.metric_contract.id:
            raise ValueError("refutation business metric does not match the cache binding")
        if symptom.workload_contract_digest != self._binding.workload_contract_digest:
            raise ValueError("refutation symptom belongs to a different workload contract")
        if hypothesis.symptom_id != symptom.symptom_id:
            raise ValueError("refutation hypothesis belongs to a different symptom")
        entry = HypothesisNegativeCacheEntry(
            identity=self._binding.identity_for(hypothesis),
            evidence_digests=[experiment.measurement_batch_digest],
            detail=(
                "stable comparable business retest rejected hypothesis "
                f"{hypothesis.hypothesis_id} for symptom class "
                f"{self._binding.symptom_class_digest}"
            ),
            recorded_at=recorded_at,
        )
        self._cache.append_to(self._path, entry)
        return entry


__all__ = ["HypothesisCacheBinding", "HypothesisCacheRuntime"]
