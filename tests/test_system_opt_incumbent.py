from __future__ import annotations

import pytest

from looper_core.system_opt.engine.incumbent import (
    IncumbentTracker,
    ScreenVerdict,
)

EVIDENCE = "sha256:" + "1" * 64


def _primed(tolerance: float, utility: float = 1.0) -> IncumbentTracker:
    tracker = IncumbentTracker(tolerance=tolerance)
    tracker.observe(round_index=1, candidate_id="c0", utility=utility, evidence_digest=EVIDENCE)
    return tracker


class TestIncumbentTracker:
    def test_first_observation_becomes_incumbent(self):
        tracker = IncumbentTracker(tolerance=0.1)
        decision = tracker.screen(0.5, candidate_id="c0")
        assert decision.verdict is ScreenVerdict.FIRST_OBSERVATION

    def test_within_tolerance_proceeds(self):
        tracker = _primed(0.1)
        assert tracker.screen(0.95, candidate_id="c1").verdict is ScreenVerdict.PROCEED

    def test_below_floor_is_early_screened_out(self):
        tracker = _primed(0.1)
        decision = tracker.screen(0.5, candidate_id="c1")
        assert decision.verdict is ScreenVerdict.EARLY_SCREENED_OUT
        assert "SO-D017" in decision.reason
        assert "not written to the negative cache" in decision.reason

    def test_no_utility_is_undecided_not_screened_out(self):
        tracker = _primed(0.1)
        decision = tracker.screen(None, candidate_id="c1")
        assert decision.verdict is ScreenVerdict.UNDECIDED

    def test_observe_updates_only_on_new_best(self):
        tracker = _primed(1.0)
        assert not tracker.observe(round_index=2, candidate_id="c1", utility=0.5,
                                   evidence_digest=EVIDENCE)
        assert tracker.observe(round_index=2, candidate_id="c2", utility=1.5,
                               evidence_digest=EVIDENCE)
        assert tracker.best.candidate_id == "c2"

    def test_negative_tolerance_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            IncumbentTracker(tolerance=-0.1)
