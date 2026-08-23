from __future__ import annotations

import pytest

from looper_core.system_opt.result_vector import (
    GeneralResultVector,
    PromotionContract,
    VerificationObservation,
    evaluate_promotion,
    pareto_layers,
    rank_vectors,
    regression_triggered,
)

NORM = "sha256:" + "a" * 64
ENV_A = "sha256:" + "b" * 64
ENV_B = "sha256:" + "c" * 64


def _vector(candidate: str, **dimensions) -> GeneralResultVector:
    base = {name: 0.5 for name in
            ("u_cpu", "u_memory", "u_storage", "u_network", "u_stability", "u_regression")}
    base.update(dimensions)
    return GeneralResultVector(candidate_id=candidate, normalization_digest=NORM, **base)


class TestVectorContract:
    def test_rejects_non_finite_dimensions(self):
        with pytest.raises(ValueError, match="finite"):
            _vector("x", u_cpu=float("inf"))

    def test_digest_round_trip_is_stable(self):
        vector = _vector("x", u_cpu=0.7)
        again = GeneralResultVector.model_validate_json(vector.model_dump_json())
        assert again.digest == vector.digest


class TestParetoLayers:
    def test_dominating_vector_takes_front_layer(self):
        dominant = _vector("dominant", u_cpu=0.9, u_memory=0.9)
        weak = _vector("weak", u_cpu=0.1, u_memory=0.1)
        trade = _vector("trade", u_cpu=0.95, u_memory=0.1)
        vectors = [weak, dominant, trade]
        layers = pareto_layers(vectors)
        assert layers[1] == 0  # dominant non-dominated
        # trade beats dominant on u_cpu and loses on u_memory -> genuine trade-off,
        # neither dominates the other -> same front
        assert layers[2] == 0
        assert layers[0] > 0  # weak dominated by both -> deeper layer

    def test_rank_requires_exact_dimension_cover(self):
        vectors = [_vector("a"), _vector("b")]
        with pytest.raises(ValueError, match="six dimensions"):
            rank_vectors(vectors, ("u_cpu", "u_memory"))

    def test_rank_orders_by_layer_then_task_tie_break(self):
        low = _vector("low", u_cpu=0.1)
        high_a = _vector("high-a", u_memory=0.8)
        high_b = _vector("high-b", u_memory=0.9)
        order = rank_vectors(
            [low, high_a, high_b],
            ("u_cpu", "u_memory", "u_storage", "u_network", "u_stability", "u_regression"),
        )
        assert order[0] == 2  # high-b best on the tie-break dimension
        assert order[2] == 0  # low is dominated, ranked last


class TestPromotion:
    def _observation(self, **overrides) -> VerificationObservation:
        payload = dict(
            candidate_id="cand",
            passed=True,
            time_block_id="tb-1",
            environment_digest=ENV_A,
            evidence_digest="sha256:" + "d" * 64,
        )
        payload.update(overrides)
        return VerificationObservation(**payload)

    contract = PromotionContract(
        min_observations=3, min_distinct_time_blocks=2, min_environments=2
    )

    def test_success_when_all_thresholds_met(self):
        evidence = evaluate_promotion(
            [
                self._observation(time_block_id="tb-1", environment_digest=ENV_A),
                self._observation(time_block_id="tb-2", environment_digest=ENV_B),
                self._observation(time_block_id="tb-2", environment_digest=ENV_A),
            ],
            self.contract,
        )
        assert evidence.promoted
        assert evidence.distinct_time_blocks == 2
        assert evidence.distinct_environments == 2

    def test_any_failed_observation_blocks_promotion_fail_closed(self):
        evidence = evaluate_promotion(
            [
                self._observation(),
                self._observation(passed=False, time_block_id="tb-2",
                                  environment_digest=ENV_B),
                self._observation(time_block_id="tb-2", environment_digest=ENV_B),
            ],
            self.contract,
        )
        assert not evidence.promoted
        assert len(evidence.failed_observations) == 1
        assert "fail-closed" in evidence.reason

    def test_insufficient_time_blocks_blocks(self):
        evidence = evaluate_promotion(
            [
                self._observation(time_block_id="tb-1", environment_digest=ENV_A),
                self._observation(time_block_id="tb-1", environment_digest=ENV_B),
                self._observation(time_block_id="tb-1", environment_digest=ENV_B),
            ],
            self.contract,
        )
        assert not evidence.promoted
        assert "time blocks" in evidence.reason

    def test_mixed_candidates_rejected(self):
        with pytest.raises(ValueError, match="mix candidates"):
            evaluate_promotion(
                [
                    self._observation(),
                    self._observation(candidate_id="other"),
                ],
                self.contract,
            )


class TestRegressionTrigger:
    def test_triggers_below_task_threshold(self):
        vector = _vector("x", u_regression=0.2)
        assert regression_triggered(vector, threshold=0.3)
        assert not regression_triggered(vector, threshold=0.1)

    def test_threshold_must_be_finite(self):
        with pytest.raises(ValueError, match="finite"):
            regression_triggered(_vector("x"), threshold=float("nan"))
