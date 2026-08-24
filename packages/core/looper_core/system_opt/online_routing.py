"""S3 online hypothesis routing from digest-bound O1 component evidence.

The producer keeps the frozen S4 v1 formula intact.  It converts readable O1
snapshots into a ``MeasurementBatch``, reuses the existing diagnostic and L8
component scorers, and injects only the resulting rank into the unchanged
hypothesis ledger shape.  Declarative proposal change/risk data remains keyed
by proposal id in the intervention adapter.

No proposal falls back to its file rank when O1 evidence is incomplete: a
partially ranked set would silently mix declared and observed semantics.
Instead the complete online route fails closed.  The declaration file is never
rewritten; every successful route exposes a content-addressed evidence record.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from looper_core.analysis import InsufficientEvidence
from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import (
    ComponentMetricSnapshot,
    MetricAvailability,
)
from looper_core.system_opt.dynamic_adapters import HypothesisProposalsFileV2
from looper_core.system_opt.engine.scorer import rank_components, score_components
from looper_core.system_opt.hypothesis import ComponentHypothesis, SymptomRecord
from looper_core.system_opt.negative_cache import (
    HYPOTHESIS_SEMANTICS_VERSION,
    formula_versions_digest,
)
from looper_core.system_opt.policy import MetricContract, MetricRole, SystemOptimizationPolicy
from looper_core.system_opt.scoring import (
    DiagnosticPriority,
    MeasurementBatch,
    MetricEvidence,
    diagnostic_priorities,
)

ONLINE_ROUTING_CONTRACT_SCHEMA = "looper.online-hypothesis-routing-contract/v1alpha1"
ONLINE_ROUTING_EVIDENCE_SCHEMA = "looper.online-hypothesis-routing-evidence/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class OnlineRoutingContract(StrictModel):
    """Task-explicit identities needed to interpret O1 values as S4 inputs."""

    schema_version: Literal[ONLINE_ROUTING_CONTRACT_SCHEMA] = ONLINE_ROUTING_CONTRACT_SCHEMA
    target_id: str = Field(min_length=1, max_length=160)
    environment_digest: str = Field(pattern=_DIGEST)
    measurement_identity: dict[str, str] = Field(min_length=1)
    pressure_protocol_digest: str = Field(pattern=_DIGEST)
    formula_versions: dict[str, str] = Field(min_length=1)
    symptom_class_digest: str = Field(pattern=_DIGEST)
    hypothesis_semantics_version: Literal[HYPOTHESIS_SEMANTICS_VERSION]

    @field_validator("formula_versions")
    @classmethod
    def require_explicit_formula_versions(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key or not value for key, value in values.items()):
            raise ValueError("formula version keys and values must be non-empty")
        return values

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class RankedHypothesis(StrictModel):
    hypothesis_id: str = Field(min_length=1, max_length=160)
    component: str = Field(min_length=1, max_length=80)
    declared_rank: int = Field(ge=1)
    online_rank: int = Field(ge=1)


class OnlineRoutingEvidence(StrictModel):
    schema_version: Literal[ONLINE_ROUTING_EVIDENCE_SCHEMA] = ONLINE_ROUTING_EVIDENCE_SCHEMA
    symptom_digest: str = Field(pattern=_DIGEST)
    routing_contract_digest: str = Field(pattern=_DIGEST)
    policy_digest: str = Field(pattern=_DIGEST)
    current_batch_digest: str = Field(pattern=_DIGEST)
    reference_batch_digest: str = Field(pattern=_DIGEST)
    source_snapshot_digests: list[str] = Field(min_length=1)
    formula_versions_digest: str = Field(pattern=_DIGEST)
    priorities: list[DiagnosticPriority] = Field(min_length=1)
    component_order: list[str] = Field(min_length=1)
    ranked_hypotheses: list[RankedHypothesis] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_deterministic_bindings(self) -> OnlineRoutingEvidence:
        if self.source_snapshot_digests != sorted(set(self.source_snapshot_digests)):
            raise ValueError("source snapshot digests must be unique and sorted")
        if len(self.component_order) != len(set(self.component_order)):
            raise ValueError("component order must be unique")
        online_ranks = [item.online_rank for item in self.ranked_hypotheses]
        if online_ranks != list(range(1, len(online_ranks) + 1)):
            raise ValueError("online hypothesis ranks must be contiguous and ordered")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class OnlineRoutingEvidenceIndex(StrictModel):
    schema_version: Literal["looper.online-hypothesis-routing-evidence-index/v1alpha1"] = (
        "looper.online-hypothesis-routing-evidence-index/v1alpha1"
    )
    evidence_digests_by_symptom: dict[str, str]


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor_open = False
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor_open:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def persist_online_routing_evidence(
    control_dir: Path, source: OnlineHypothesisSource
) -> OnlineRoutingEvidenceIndex:
    """Publish content-addressed records, then the fixed index, atomically per file."""

    evidence_by_symptom = source.evidence_by_symptom
    index = OnlineRoutingEvidenceIndex(
        evidence_digests_by_symptom={
            symptom_id: evidence_by_symptom[symptom_id].digest
            for symptom_id in sorted(evidence_by_symptom)
        }
    )
    # Validate every association before creating any output file.
    for symptom_id, digest in index.evidence_digests_by_symptom.items():
        if evidence_by_symptom[symptom_id].digest != digest:
            raise ValueError("online routing evidence index association changed")
    for evidence in evidence_by_symptom.values():
        _atomic_write_json(
            control_dir / f"online-routing-evidence-{evidence.digest.removeprefix('sha256:')}.json",
            evidence.model_dump(mode="json"),
        )
    _atomic_write_json(
        control_dir / "online-routing-evidence-index.json",
        index.model_dump(mode="json"),
    )
    return index


def _diagnostic_contracts(policy: SystemOptimizationPolicy) -> list[MetricContract]:
    contracts = [
        metric for metric in policy.metrics if metric.role is MetricRole.COMPONENT_DIAGNOSTIC
    ]
    if not contracts:
        raise InsufficientEvidence("online routing policy has no component diagnostic metrics")
    return contracts


def measurement_batch_from_o1(
    snapshots: Sequence[ComponentMetricSnapshot],
    *,
    contracts: Sequence[MetricContract],
    routing_contract: OnlineRoutingContract,
) -> MeasurementBatch:
    """Build one diagnostic batch without inventing missing/unavailable values."""

    if not snapshots:
        raise InsufficientEvidence("online routing requires at least one O1 snapshot")
    snapshot_digests = [snapshot.digest for snapshot in snapshots]
    if len(snapshot_digests) != len(set(snapshot_digests)):
        raise InsufficientEvidence("online routing refuses duplicate O1 snapshot evidence")
    target_ids = {snapshot.target_id for snapshot in snapshots}
    environments = {snapshot.environment_digest for snapshot in snapshots}
    if len(target_ids) != 1 or len(environments) != 1:
        raise InsufficientEvidence("O1 snapshots mix target or environment identities")
    if target_ids != {routing_contract.target_id}:
        raise InsufficientEvidence("O1 snapshots belong to a different target")
    if environments != {routing_contract.environment_digest}:
        raise InsufficientEvidence("O1 snapshots belong to a different environment")

    values_by_metric: dict[str, list[float]] = {}
    for contract in contracts:
        values: list[float] = []
        for snapshot in snapshots:
            metric = snapshot.metrics.get(contract.id)
            if metric is None:
                continue
            if metric.availability is not MetricAvailability.READABLE:
                raise InsufficientEvidence(
                    f"O1 metric {contract.id} is unavailable: {metric.unavailable_reason}"
                )
            raw_values = metric.value if isinstance(metric.value, list) else [metric.value]
            assert all(value is not None for value in raw_values)
            values.extend(float(value) for value in raw_values if value is not None)
        if not values:
            raise InsufficientEvidence(f"O1 metric {contract.id} is missing")
        if any(not math.isfinite(value) for value in values):
            raise InsufficientEvidence(f"O1 metric {contract.id} contains a non-finite value")
        values_by_metric[contract.id] = values

    return MeasurementBatch(
        identity=dict(routing_contract.measurement_identity),
        metrics={
            metric_id: MetricEvidence(metric_id=metric_id, values=values)
            for metric_id, values in values_by_metric.items()
        },
        gate_values={},
        pressure_protocol_digest=routing_contract.pressure_protocol_digest,
    )


class OnlineHypothesisSource:
    """Drop-in v2 ``hypothesis_source`` whose rank is derived from O1 evidence."""

    def __init__(
        self,
        *,
        proposals: HypothesisProposalsFileV2,
        snapshots: Sequence[ComponentMetricSnapshot],
        reference: MeasurementBatch,
        policy: SystemOptimizationPolicy,
        routing_contract: OnlineRoutingContract,
        excluded_components: Sequence[str] = (),
    ) -> None:
        self._proposals = proposals
        self._snapshots = list(snapshots)
        self._reference = reference
        self._policy = policy
        self._routing_contract = routing_contract
        self._excluded_components = set(excluded_components)
        self._evidence_by_symptom: dict[str, OnlineRoutingEvidence] = {}

    @property
    def evidence_by_symptom(self) -> Mapping[str, OnlineRoutingEvidence]:
        return dict(self._evidence_by_symptom)

    def __call__(self, symptom: SymptomRecord) -> list[ComponentHypothesis]:
        contracts = _diagnostic_contracts(self._policy)
        current = measurement_batch_from_o1(
            self._snapshots,
            contracts=contracts,
            routing_contract=self._routing_contract,
        )
        priorities = diagnostic_priorities(current, self._reference, contracts)
        component_order = rank_components(score_components(priorities))
        component_position = {component: index for index, component in enumerate(component_order)}

        active = [
            proposal
            for proposal in self._proposals.proposals
            if proposal.component.value not in self._excluded_components
        ]
        missing = sorted(
            {
                proposal.component.value
                for proposal in active
                if proposal.component.value not in component_position
            }
        )
        if missing:
            raise InsufficientEvidence(
                f"online proposals have no eligible O1 diagnostic evidence: {missing}"
            )
        if not active:
            raise InsufficientEvidence("all online proposals were excluded by prior evidence")

        ordered = sorted(
            active,
            key=lambda proposal: (
                component_position[proposal.component.value],
                proposal.rank,
                proposal.hypothesis_id,
            ),
        )
        ranked = [
            RankedHypothesis(
                hypothesis_id=proposal.hypothesis_id,
                component=proposal.component.value,
                declared_rank=proposal.rank,
                online_rank=index,
            )
            for index, proposal in enumerate(ordered, start=1)
        ]
        evidence = OnlineRoutingEvidence(
            symptom_digest=symptom.digest,
            routing_contract_digest=self._routing_contract.digest,
            policy_digest=canonical_digest(self._policy.model_dump(mode="json")),
            current_batch_digest=current.digest,
            reference_batch_digest=self._reference.digest,
            source_snapshot_digests=sorted(snapshot.digest for snapshot in self._snapshots),
            formula_versions_digest=formula_versions_digest(
                self._routing_contract.formula_versions
            ),
            priorities=priorities,
            component_order=component_order,
            ranked_hypotheses=ranked,
        )
        self._evidence_by_symptom[symptom.symptom_id] = evidence
        return [
            ComponentHypothesis(
                hypothesis_id=proposal.hypothesis_id,
                symptom_id=symptom.symptom_id,
                component=proposal.component,
                rank=index,
                supporting_digests=[*proposal.supporting_digests, evidence.digest],
            )
            for index, proposal in enumerate(ordered, start=1)
        ]


__all__ = [
    "ONLINE_ROUTING_CONTRACT_SCHEMA",
    "ONLINE_ROUTING_EVIDENCE_SCHEMA",
    "OnlineHypothesisSource",
    "OnlineRoutingContract",
    "OnlineRoutingEvidence",
    "OnlineRoutingEvidenceIndex",
    "RankedHypothesis",
    "measurement_batch_from_o1",
    "persist_online_routing_evidence",
]
