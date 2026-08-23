"""M3 S3 symptom-to-hypothesis routing tests (workload-tuning.md D2, SO-D019)."""

from __future__ import annotations

import pytest
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


def _hypothesis(hypothesis_id: str, component: str, rank: int) -> ComponentHypothesis:
    return ComponentHypothesis(
        hypothesis_id=hypothesis_id,
        symptom_id="sym-tail-latency",
        component=component,
        rank=rank,
    )


def _ledger_with_two() -> HypothesisLedger:
    ledger = HypothesisLedger()
    ledger.register_symptom(_symptom())
    ledger.register_hypothesis(_hypothesis("hyp-cpu", "cpu", rank=1))
    ledger.register_hypothesis(_hypothesis("hyp-memory", "memory", rank=2))
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
            ledger.register_hypothesis(_hypothesis("hyp-orphan", "cpu", rank=1))
        ledger.register_symptom(_symptom())
        with pytest.raises(HypothesisRoutingError, match="already registered"):
            ledger.register_symptom(_symptom())
        ledger.register_hypothesis(_hypothesis("hyp-cpu", "cpu", rank=1))
        assert ledger.hypothesis("hyp-cpu").status is HypothesisStatus.PROPOSED


class TestInterventionGate:
    def test_single_hypothesis_cannot_request_intervention(self):
        ledger = HypothesisLedger()
        ledger.register_symptom(_symptom())
        ledger.register_hypothesis(_hypothesis("hyp-cpu", "cpu", rank=1))

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
        ledger.register_hypothesis(_hypothesis("hyp-net-a", "network", rank=2))
        ledger.register_hypothesis(_hypothesis("hyp-net-b", "network", rank=2))

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
