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


# ======================================================================
# 作用域分隔：以下为 System Optimizer M3 假设路由测试（组件假设线）。
# ======================================================================

"""M3 S3 symptom-to-hypothesis routing tests (workload-tuning.md D2, SO-D019)."""

from looper_core.system_opt.hypothesis import (
    ComponentHypothesis,
    HypothesisLedger,
    HypothesisRoutingError,
    HypothesisStatus,
    InterventionExperiment,
    SymptomRecord,
)

DIGEST = "sha256:" + "9" * 64


def _symptom() -> SymptomRecord:
    return SymptomRecord(
        symptom_id="sym-tail-latency",
        window_id="win-1",
        workload_contract_digest=DIGEST,
        evidence_digest=DIGEST,
        description="p99 exceeded SLO for three consecutive windows",
    )


def _hypothesis_m3(hypothesis_id: str, component: str, rank: int) -> ComponentHypothesis:
    return ComponentHypothesis(
        hypothesis_id=hypothesis_id,
        symptom_id="sym-tail-latency",
        component=component,
        rank=rank,
    )


def _ledger_with_two() -> HypothesisLedger:
    ledger = HypothesisLedger()
    ledger.register_symptom(_symptom())
    ledger.register_hypothesis(_hypothesis_m3("hyp-cpu", "cpu", rank=1))
    ledger.register_hypothesis(_hypothesis_m3("hyp-memory", "memory", rank=2))
    return ledger


def _experiment(accepted: bool = True) -> InterventionExperiment:
    return InterventionExperiment(
        measurement_batch_digest=DIGEST,
        business_metric_id="stress-ng.bogo-ops-per-second-usr-sys-time",
        accepted=accepted,
    )


class TestRegistration:
    def test_ledger_digest_is_deterministic_and_replayable(self):
        one = _ledger_with_two()
        two = _ledger_with_two()

        assert one.digest == two.digest
        assert one.hypothesis("hyp-cpu").digest == two.hypothesis("hyp-cpu").digest

    def test_hypothesis_requires_a_registered_symptom_and_starts_proposed(self):
        ledger = HypothesisLedger()
        with pytest.raises(HypothesisRoutingError, match="unregistered symptom"):
            ledger.register_hypothesis(_hypothesis_m3("hyp-orphan", "cpu", rank=1))
        ledger.register_symptom(_symptom())
        with pytest.raises(HypothesisRoutingError, match="already registered"):
            ledger.register_symptom(_symptom())
        ledger.register_hypothesis(_hypothesis_m3("hyp-cpu", "cpu", rank=1))
        assert ledger.hypothesis("hyp-cpu").status is HypothesisStatus.PROPOSED


class TestInterventionGate:
    def test_single_hypothesis_cannot_request_intervention(self):
        ledger = HypothesisLedger()
        ledger.register_symptom(_symptom())
        ledger.register_hypothesis(_hypothesis_m3("hyp-cpu", "cpu", rank=1))

        with pytest.raises(HypothesisRoutingError, match="competing hypothesis"):
            ledger.request_intervention("hyp-cpu")

    def test_two_competing_hypotheses_unlock_intervention(self):
        ledger = _ledger_with_two()

        ledger.request_intervention("hyp-cpu")

    def test_refuting_the_competitor_relocks_the_gate(self):
        ledger = _ledger_with_two()
        ledger.refute("hyp-memory", DIGEST)

        with pytest.raises(HypothesisRoutingError, match="competing hypothesis"):
            ledger.request_intervention("hyp-cpu")


class TestConfirmations:
    def test_confirmation_requires_probing_then_accepted_business_retest(self):
        ledger = _ledger_with_two()

        with pytest.raises(HypothesisRoutingError, match="only a probing"):
            ledger.confirm("hyp-cpu", _experiment())

        ledger.begin_probing("hyp-cpu", DIGEST)
        confirmed = ledger.confirm("hyp-cpu", _experiment(accepted=True))

        assert confirmed.status is HypothesisStatus.CONFIRMED
        assert confirmed.confirm_evidence is not None
        assert ledger.hypothesis("hyp-memory").status is HypothesisStatus.SUPERSEDED

    def test_rejected_business_retest_must_refute_instead(self):
        ledger = _ledger_with_two()
        ledger.begin_probing("hyp-cpu", DIGEST)

        with pytest.raises(HypothesisRoutingError, match="refutes"):
            ledger.confirm("hyp-cpu", _experiment(accepted=False))

        refuted = ledger.refute("hyp-cpu", DIGEST)
        assert refuted.status is HypothesisStatus.REFUTED
        assert refuted.refute_evidence_digest == DIGEST

    def test_o2_evidence_never_confirms_directly(self):
        ledger = _ledger_with_two()
        for _ in range(3):
            ledger.begin_probing("hyp-cpu", DIGEST)

        assert ledger.hypothesis("hyp-cpu").status is HypothesisStatus.PROBING

    def test_terminal_hypotheses_are_immutable(self):
        ledger = _ledger_with_two()
        ledger.begin_probing("hyp-cpu", DIGEST)
        ledger.confirm("hyp-cpu", _experiment())

        with pytest.raises(HypothesisRoutingError, match="immutable|terminal"):
            ledger.refute("hyp-cpu", DIGEST)
        with pytest.raises(HypothesisRoutingError, match="terminal"):
            ledger.begin_probing("hyp-cpu", DIGEST)


class TestRouting:
    def test_probe_queue_orders_by_rank_then_id_and_caps_top_k(self):
        ledger = _ledger_with_two()
        ledger.register_hypothesis(_hypothesis_m3("hyp-net-a", "network", rank=2))
        ledger.register_hypothesis(_hypothesis_m3("hyp-net-b", "network", rank=2))

        full = ledger.probe_queue(top_k=10)
        assert [h.hypothesis_id for h in full] == [
            "hyp-cpu",
            "hyp-memory",
            "hyp-net-a",
            "hyp-net-b",
        ]
        capped = ledger.probe_queue(top_k=2)
        assert [h.hypothesis_id for h in capped] == ["hyp-cpu", "hyp-memory"]

    def test_terminal_hypotheses_leave_the_queue(self):
        ledger = _ledger_with_two()
        ledger.refute("hyp-cpu", DIGEST)

        assert [h.hypothesis_id for h in ledger.probe_queue(top_k=10)] == ["hyp-memory"]

    def test_top_k_is_explicit_no_default_cap(self):
        with pytest.raises(HypothesisRoutingError, match="top_k"):
            _ledger_with_two().probe_queue(top_k=0)

    def test_every_transition_moves_the_ledger_digest(self):
        ledger = _ledger_with_two()
        before = ledger.digest
        ledger.begin_probing("hyp-cpu", DIGEST)
        after_probe = ledger.digest
        ledger.confirm("hyp-cpu", _experiment())

        assert before != after_probe != ledger.digest
