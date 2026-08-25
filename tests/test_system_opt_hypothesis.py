from __future__ import annotations

import pytest
from looper_core.analysis import InsufficientEvidence
from looper_core.canonical import canonical_digest
from looper_core.system_opt.demo import build_demo_manifest, resolve_demo_domains
from looper_core.system_opt.hypothesis import (
    HYPOTHESIS_SCHEMA,
    CapacityDecisionStatus,
    HypothesisEvidence,
    HypothesisState,
    OptimizationHypothesis,
    evaluate_capacity_frontiers,
    hypothesis_context_digest,
    rank_authorized_hypotheses,
)
from looper_core.system_opt.scoring import DiagnosticPriority
from pydantic import ValidationError


def _digest(seed: str) -> str:
    return canonical_digest({"seed": seed})


def _identity(**updates: str) -> dict[str, str]:
    identity = {
        "source_digest": _digest("source"),
        "workload_digest": _digest("workload"),
        "slo_digest": _digest("slo"),
        "environment_digest": _digest("environment"),
        "network": "internal",
        "target_id": "target-a",
        "capacity_unit": "successful business iterations/second",
        "confidence_level": "0.95",
        "measurement_contract_digest": _digest("measurement-contract"),
    }
    identity.update(updates)
    return identity


def _evidence(seed: str) -> list[HypothesisEvidence]:
    return [
        HypothesisEvidence(
            kind="runtime-profile",
            digest=_digest(f"{seed}-runtime"),
            locator="evidence://attempt/runtime-profile",
            claim="The storage queue remains pressured at the capacity boundary.",
        ),
        HypothesisEvidence(
            kind="configuration-contract",
            digest=_digest(f"{seed}-config-contract"),
            locator="https://docs.kernel.org/block/queue-sysfs.html#scheduler-rw",
            claim="The target exposes an authorized scheduler control for this device.",
        ),
    ]


def _hypothesis(
    *,
    hypothesis_id: str = "storage.scheduler.none",
    context_digest: str | None = None,
    state: HypothesisState = HypothesisState.SUPPORTED_HYPOTHESIS,
    parameters: dict[str, object] | None = None,
    components: list[str] | None = None,
) -> OptimizationHypothesis:
    return OptimizationHypothesis(
        schema_version=HYPOTHESIS_SCHEMA,
        hypothesis_id=hypothesis_id,
        statement="Reducing scheduler work may move the SLO-constrained capacity boundary.",
        state=state,
        context_digest=context_digest or hypothesis_context_digest(_identity()),
        affected_components=components or ["storage"],
        candidate_parameters=parameters
        or {"system.storage-scheduler": "none"},
        evidence=_evidence(hypothesis_id),
    )


def _priority(
    component: str,
    *,
    rank: int,
    pressure: float,
) -> DiagnosticPriority:
    return DiagnosticPriority(
        metric_id=f"{component}.pressure",
        component=component,
        pressure=pressure,
        adverse_change=0.25,
        persistence=0.8,
        confidence=1.0,
        pareto_rank=rank,
    )


def _frontier(confirmed_pass: float, confirmed_fail: float) -> dict[str, object]:
    return {
        "status": "resolved",
        "confirmed_pass": confirmed_pass,
        "confirmed_fail": confirmed_fail,
    }


def test_hypothesis_requires_runtime_and_source_or_configuration_evidence() -> None:
    payload = _hypothesis().model_dump(mode="python")
    payload["evidence"] = [
        payload["evidence"][0],
        HypothesisEvidence(
            kind="runtime-profile",
            digest=_digest("second-runtime"),
            locator="evidence://attempt/second-runtime-profile",
            claim="A second runtime artifact cannot substitute for source provenance.",
        ).model_dump(mode="python"),
    ]

    with pytest.raises(ValidationError, match="source-code or configuration-contract"):
        OptimizationHypothesis.model_validate(payload)


def test_source_code_evidence_requires_an_exact_location() -> None:
    with pytest.raises(ValidationError, match="symbol or exact line range"):
        HypothesisEvidence(
            kind="source-code",
            digest=_digest("source"),
            locator="src/storage.py",
            claim="A configuration branch controls the storage path.",
        )


def test_intervention_supported_hypothesis_requires_bound_outcome_revision() -> None:
    payload = _hypothesis().model_dump(mode="python")
    payload["state"] = HypothesisState.INTERVENTION_SUPPORTED

    with pytest.raises(ValidationError, match="capacity-outcome"):
        OptimizationHypothesis.model_validate(payload)

    payload["evidence"].append(
        HypothesisEvidence(
            kind="capacity-outcome",
            digest=_digest("outcome"),
            locator="evidence://capacity/candidate-a",
            claim="The candidate capacity interval cleared the minimum effect.",
        ).model_dump(mode="python")
    )
    payload["predecessor_digest"] = _hypothesis().digest
    revised = OptimizationHypothesis.model_validate(payload)

    assert revised.state == HypothesisState.INTERVENTION_SUPPORTED
    assert revised.digest != revised.predecessor_digest


def test_ranker_uses_runtime_priority_and_rejects_unauthorized_or_stale_candidates() -> None:
    manifest = build_demo_manifest()
    domains = resolve_demo_domains(manifest)
    context = hypothesis_context_digest(_identity())
    storage = _hypothesis(context_digest=context)
    cpu = _hypothesis(
        hypothesis_id="cpu.governor.performance",
        context_digest=context,
        state=HypothesisState.OBSERVED_ASSOCIATION,
        parameters={"system.cpu-governor": "performance"},
        components=["cpu"],
    )
    stale = _hypothesis(
        hypothesis_id="storage.stale",
        context_digest=_digest("stale-context"),
    )
    unauthorized = _hypothesis(
        hypothesis_id="storage.unauthorized",
        context_digest=context,
        parameters={"system.storage-read-ahead": 4096},
    )
    outside_domain = _hypothesis(
        hypothesis_id="storage.outside-domain",
        context_digest=context,
        parameters={"system.storage-scheduler": "kyber"},
    )
    inconsistent_domain = _hypothesis(
        hypothesis_id="storage.inconsistent-domain",
        context_digest=context,
        parameters={"system.ghost-scheduler": "none"},
    )
    domains_with_inconsistent_identity = {
        **domains,
        "system.ghost-scheduler": domains["system.storage-scheduler"],
    }

    ranked, rejected = rank_authorized_hypotheses(
        [cpu, stale, outside_domain, inconsistent_domain, storage, unauthorized],
        [
            _priority("storage", rank=1, pressure=0.95),
            _priority("cpu", rank=2, pressure=0.80),
        ],
        expected_context_digest=context,
        manifest=manifest,
        resolved_domains=domains_with_inconsistent_identity,
    )

    assert [item.hypothesis_id for item in ranked] == [
        "storage.scheduler.none",
        "cpu.governor.performance",
    ]
    assert rejected[stale.digest] == "context-digest-mismatch"
    assert rejected[unauthorized.digest].startswith("parameter-not-authorized")
    assert rejected[outside_domain.digest].startswith("value-outside-resolved-domain")
    assert rejected[inconsistent_domain.digest].startswith("resolved-domain-identity-mismatch")


def test_capacity_frontier_accepts_only_non_overlapping_minimum_effect() -> None:
    decision = evaluate_capacity_frontiers(
        hypothesis_digest=_hypothesis().digest,
        baseline_frontier=_frontier(90, 100),
        candidate_frontier=_frontier(120, 130),
        baseline_report_digest=_digest("baseline-report"),
        candidate_report_digest=_digest("candidate-report"),
        baseline_identity=_identity(),
        candidate_identity=_identity(),
        minimum_effect=0.05,
        rollback_verified=True,
    )

    assert decision.status == CapacityDecisionStatus.ACCEPTED
    assert decision.lower == pytest.approx(0.20)
    assert decision.lower > decision.minimum_effect
    assert decision.rollback_verified is True


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (_frontier(80, 90), CapacityDecisionStatus.REJECTED),
        (_frontier(100, 110), CapacityDecisionStatus.INCONCLUSIVE),
    ],
)
def test_capacity_frontier_distinguishes_rejection_from_overlap(
    candidate: dict[str, object], expected: CapacityDecisionStatus
) -> None:
    decision = evaluate_capacity_frontiers(
        hypothesis_digest=_hypothesis().digest,
        baseline_frontier=_frontier(90, 100),
        candidate_frontier=candidate,
        baseline_report_digest=_digest("baseline-report"),
        candidate_report_digest=_digest("candidate-report"),
        baseline_identity=_identity(),
        candidate_identity=_identity(),
        minimum_effect=0.05,
        rollback_verified=True,
    )

    assert decision.status == expected


def test_capacity_frontier_fails_closed_on_identity_drift_or_unverified_rollback() -> None:
    incomparable = evaluate_capacity_frontiers(
        hypothesis_digest=_hypothesis().digest,
        baseline_frontier=_frontier(90, 100),
        candidate_frontier=_frontier(120, 130),
        baseline_report_digest=_digest("baseline-report"),
        candidate_report_digest=_digest("candidate-report"),
        baseline_identity=_identity(),
        candidate_identity=_identity(network="external"),
        minimum_effect=0.05,
        rollback_verified=True,
    )
    safety_failed = evaluate_capacity_frontiers(
        hypothesis_digest=_hypothesis().digest,
        baseline_frontier=_frontier(90, 100),
        candidate_frontier=_frontier(120, 130),
        baseline_report_digest=_digest("baseline-report"),
        candidate_report_digest=_digest("candidate-report"),
        baseline_identity=_identity(),
        candidate_identity=_identity(),
        minimum_effect=0.05,
        rollback_verified=False,
    )

    assert incomparable.status == CapacityDecisionStatus.INCOMPARABLE
    assert incomparable.identity_mismatches == ["network"]
    assert safety_failed.status == CapacityDecisionStatus.SAFETY_FAILED


def test_capacity_frontier_requires_complete_identity_and_valid_resolved_bounds() -> None:
    incomplete = _identity()
    incomplete.pop("slo_digest")

    with pytest.raises(InsufficientEvidence, match="slo_digest"):
        evaluate_capacity_frontiers(
            hypothesis_digest=_hypothesis().digest,
            baseline_frontier=_frontier(90, 100),
            candidate_frontier=_frontier(120, 130),
            baseline_report_digest=_digest("baseline-report"),
            candidate_report_digest=_digest("candidate-report"),
            baseline_identity=incomplete,
            candidate_identity=_identity(),
            minimum_effect=0.05,
            rollback_verified=True,
        )

    with pytest.raises(InsufficientEvidence, match="exceeds"):
        evaluate_capacity_frontiers(
            hypothesis_digest=_hypothesis().digest,
            baseline_frontier=_frontier(100, 90),
            candidate_frontier=_frontier(120, 130),
            baseline_report_digest=_digest("baseline-report"),
            candidate_report_digest=_digest("candidate-report"),
            baseline_identity=_identity(),
            candidate_identity=_identity(),
            minimum_effect=0.05,
            rollback_verified=True,
        )
