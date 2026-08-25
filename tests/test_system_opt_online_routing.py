from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from looper_core.analysis import InsufficientEvidence
from looper_core.system_opt.collector import (
    CollectedMetric,
    ComponentMetricSnapshot,
    MetricAvailability,
)
from looper_core.system_opt.demo import build_demo_policy, build_workload_reference
from looper_core.system_opt.dynamic_adapters import (
    HYPOTHESIS_PROPOSALS_V2_SCHEMA,
    HypothesisProposalsFileV2,
    HypothesisProposalV2,
)
from looper_core.system_opt.hypothesis import SymptomRecord
from looper_core.system_opt.negative_cache import HYPOTHESIS_SEMANTICS_VERSION
from looper_core.system_opt.online_routing import (
    OnlineHypothesisSource,
    OnlineRoutingContract,
)
from looper_core.system_opt.policy import OptimizationMode

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _snapshot(
    component: str, values: dict[str, float], *, sample_index: int
) -> ComponentMetricSnapshot:
    return ComponentMetricSnapshot(
        component=component,
        target_id="target-1",
        environment_digest=DIGEST_A,
        collected_at=datetime(2026, 8, 24, tzinfo=UTC) + timedelta(seconds=sample_index),
        metrics={
            name: CollectedMetric(
                name=name,
                unit="synthetic",
                source="unit-test",
                availability=MetricAvailability.READABLE,
                value=value,
            )
            for name, value in values.items()
        },
        counting_basis="one synthetic O1 sample per metric",
    )


def _source(*, missing_network: bool = False) -> OnlineHypothesisSource:
    policy = build_demo_policy(OptimizationMode.WORKLOAD)
    reference = build_workload_reference(policy)
    snapshots = []
    # Each diagnostic contract requires seven samples in the demo policy.
    for sample_index in range(7):
        snapshots.extend(
            [
                _snapshot(
                    "cpu", {"cpu.utilization": 0.95}, sample_index=sample_index
                ),
                _snapshot(
                    "memory", {"memory.psi-some": 0.07}, sample_index=sample_index
                ),
                _snapshot(
                    "storage", {"storage.io-latency": 9.0}, sample_index=sample_index
                ),
            ]
        )
        if not missing_network:
            snapshots.append(
                _snapshot(
                    "network", {"network.retransmits": 8.0}, sample_index=sample_index
                )
            )
    proposals = HypothesisProposalsFileV2(
        schema_version=HYPOTHESIS_PROPOSALS_V2_SCHEMA,
        proposals=[
            HypothesisProposalV2(
                hypothesis_id="memory-hypothesis",
                component="memory",
                rank=1,
                rationale="memory competitor",
                change={"system.vm-swappiness": 10},
                risk="low",
                risk_kind="manifest-derived",
            ),
            HypothesisProposalV2(
                hypothesis_id="cpu-hypothesis",
                component="cpu",
                rank=2,
                rationale="cpu pressure is highest",
                change={"system.cpu-governor": "performance"},
                risk="low",
                risk_kind="manifest-derived",
            ),
        ],
    )
    return OnlineHypothesisSource(
        proposals=proposals,
        snapshots=snapshots,
        reference=reference,
        policy=policy,
        routing_contract=OnlineRoutingContract(
            target_id="target-1",
            environment_digest=DIGEST_A,
            measurement_identity=dict(reference.identity),
            pressure_protocol_digest=reference.pressure_protocol_digest,
            formula_versions={"F-PROJECT-S4-PIECEWISE-LINEAR": "v1alpha1"},
            symptom_class_digest=DIGEST_B,
            hypothesis_semantics_version=HYPOTHESIS_SEMANTICS_VERSION,
        ),
    )


def _symptom() -> SymptomRecord:
    return SymptomRecord(
        symptom_id="symptom-1",
        window_id="window-1",
        workload_contract_digest=DIGEST_A,
        evidence_digest=DIGEST_B,
        description="business SLO violation",
    )


def test_online_source_replaces_declared_rank_and_records_replay_evidence() -> None:
    source = _source()

    first = source(_symptom())
    second = source(_symptom())

    assert [item.hypothesis_id for item in first] == ["cpu-hypothesis", "memory-hypothesis"]
    assert [item.rank for item in first] == [1, 2]
    assert [item.model_dump(mode="json") for item in first] == [
        item.model_dump(mode="json") for item in second
    ]
    evidence = source.evidence_by_symptom["symptom-1"]
    assert evidence.ranked_hypotheses[0].declared_rank == 2
    assert first[0].supporting_digests[-1] == evidence.digest


def test_online_source_fails_closed_when_a_proposal_component_has_no_o1_metric() -> None:
    source = _source(missing_network=True)
    # Replace one proposal with a component whose policy metric has no snapshot.
    source._proposals = source._proposals.model_copy(
        update={
            "proposals": [
                *source._proposals.proposals,
                HypothesisProposalV2(
                    hypothesis_id="network-hypothesis",
                    component="network",
                    rank=3,
                    rationale="missing network evidence",
                    change={"system.net-somaxconn": 1024},
                    risk="low",
                    risk_kind="manifest-derived",
                ),
            ]
        }
    )

    with pytest.raises(InsufficientEvidence, match="network.retransmits is missing"):
        source(_symptom())


def test_online_source_never_falls_back_when_all_components_are_excluded() -> None:
    source = _source()
    source._excluded_components = {"cpu", "memory"}

    with pytest.raises(InsufficientEvidence, match="all online proposals"):
        source(_symptom())
