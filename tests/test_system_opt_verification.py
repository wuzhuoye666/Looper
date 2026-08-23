"""M3 S9 re-verification window tests — the M11 closure (D3).

The decisive property: ``passed`` comes from re-measured data and can be
false; a single failed observation drives ``evaluate_promotion`` to its
fail-closed (non-promoted) branch — previously unreachable from any real
producer.
"""

from __future__ import annotations

import pytest
from looper_core.analysis import InsufficientEvidence
from looper_core.system_opt.policy import (
    Aggregation,
    MetricContract,
    MetricDirection,
    MetricRole,
    PressureMethod,
    StatisticsPolicy,
)
from looper_core.system_opt.result_vector import (
    PromotionContract,
    evaluate_promotion,
)
from looper_core.system_opt.scoring import MetricEvidence, bootstrap_improvement
from looper_core.system_opt.verification import (
    VerificationWindow,
    verification_observation,
)

ENV = "sha256:" + "7" * 64
EVIDENCE = "sha256:" + "8" * 64

_CONTRACT = MetricContract(
    id="stress-ng.bogo-ops-per-second-usr-sys-time",
    role=MetricRole.BUSINESS_PRIMARY,
    component="cpu",
    direction=MetricDirection.MAXIMIZE,
    unit="bogo-ops/s",
    scope="verification fixture",
    phase="measure",
    aggregation=Aggregation.MEAN,
    minimum_samples=2,
    scale=1.0,
    minimum_effect=0.5,
    pressure_method=PressureMethod.NONE,
    source="stress-ng yaml metrics",
)
_STATS = StatisticsPolicy(
    confidence_level=0.95,
    bootstrap_resamples=2000,
    random_seed=7,
    baseline_repeats=2,
    candidate_repeats=2,
    baseline_every_n=1,
)
_BASELINE = MetricEvidence(
    metric_id=_CONTRACT.id, values=[100.0, 100.5, 99.5, 100.2]
)


def _retest(values: list[float]):
    return bootstrap_improvement(
        MetricEvidence(metric_id=_CONTRACT.id, values=values),
        _BASELINE,
        _CONTRACT,
        _STATS,
    )


def test_passing_retest_produces_a_passed_observation():
    outcome = _retest([102.0, 102.4, 101.8, 102.2])

    observation = verification_observation(
        window_id="verify-w1",
        promoted_candidate_id="cand-1",
        environment_digest=ENV,
        outcome=outcome,
        evidence_digest=EVIDENCE,
    )

    assert observation.passed is True
    assert observation.time_block_id == "verify-w1"


def test_worse_retest_produces_a_failed_observation():
    outcome = _retest([100.1, 99.9, 100.0, 100.2])

    observation = verification_observation(
        window_id="verify-w2",
        promoted_candidate_id="cand-1",
        environment_digest=ENV,
        outcome=outcome,
        evidence_digest=EVIDENCE,
    )

    assert observation.passed is False, "M11: passed reflects retest data"


def test_a_single_failed_retest_blocks_promotion_fail_closed():
    passed_observation = verification_observation(
        window_id="verify-w1",
        promoted_candidate_id="cand-1",
        environment_digest=ENV,
        outcome=_retest([102.0, 102.4, 101.8, 102.2]),
        evidence_digest=EVIDENCE,
    )
    failed_observation = verification_observation(
        window_id="verify-w3",
        promoted_candidate_id="cand-1",
        environment_digest=ENV,
        outcome=_retest([99.0, 98.8, 99.4, 99.2]),
        evidence_digest=EVIDENCE,
    )

    evidence = evaluate_promotion(
        [passed_observation, failed_observation],
        PromotionContract(min_observations=2, min_distinct_time_blocks=2, min_environments=1),
    )

    assert evidence.promoted is False
    assert "failed" in evidence.reason
    assert evidence.failed_observations


def test_promotion_requires_real_reverification_spread():
    """One engine-round acceptance record alone cannot promote (min blocks)."""

    acceptance_only = verification_observation(
        window_id="engine-round-1",
        promoted_candidate_id="cand-1",
        environment_digest=ENV,
        outcome=_retest([102.0, 102.4, 101.8, 102.2]),
        evidence_digest=EVIDENCE,
    )

    evidence = evaluate_promotion(
        [acceptance_only],
        PromotionContract(min_observations=3, min_distinct_time_blocks=3, min_environments=1),
    )

    assert evidence.promoted is False


def test_full_chain_from_retests_promotes():
    observations = [
        verification_observation(
            window_id=f"verify-w{i}",
            promoted_candidate_id="cand-1",
            environment_digest=ENV,
            outcome=_retest([102.0 + i * 0.1, 102.4, 101.8, 102.2]),
            evidence_digest=EVIDENCE,
        )
        for i in (1, 2, 3)
    ]

    evidence = evaluate_promotion(
        observations,
        PromotionContract(min_observations=3, min_distinct_time_blocks=3, min_environments=1),
    )

    assert evidence.promoted is True


def test_window_model_binds_identity_and_is_deterministic():
    payload = dict(
        window_id="verify-w1",
        promoted_candidate_id="cand-1",
        workload_contract_digest=ENV,
        observation_window_digest=EVIDENCE,
        business_metric_id=_CONTRACT.id,
        passed=True,
        evidence_digest=EVIDENCE,
    )
    window = VerificationWindow(**payload)
    dump = VerificationWindow(**payload).model_dump(mode="python")
    rebuilt = VerificationWindow.model_validate(dump)

    assert window.digest == rebuilt.digest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VerificationWindow(**{**payload, "evidence_digest": "not-a-digest"})
    with pytest.raises(InsufficientEvidence):
        _retest([100.0])  # sample floor still applies to retests
