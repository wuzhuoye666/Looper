"""Scenario Profile publication binds both comparison baselines exactly."""

from pathlib import Path

import pytest
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.dynamic_adapters import HypothesisProposalV2
from looper_core.system_opt.dynamic_loop import DynamicPhaseRunV2
from looper_core.system_opt.intervention import RiskSourceKind
from looper_core.system_opt.result_vector import PromotionEvidence
from looper_core.system_opt.scenario_profile import (
    build_scenario_profile_report,
    persist_scenario_profile,
)
from looper_core.system_opt.scoring import (
    ImprovementEvidence,
    MeasurementBatch,
    MetricEvidence,
)

DIGEST = "sha256:" + "a" * 64
METRIC = "stress-ng.bogo-ops-per-second-usr-sys-time"


def _batch(values: list[float]) -> MeasurementBatch:
    return MeasurementBatch(
        identity={"target": "demo", "phase": "steady"},
        metrics={METRIC: MetricEvidence(metric_id=METRIC, values=values)},
        gate_values={},
    )


def _improvement(
    baseline: MeasurementBatch, candidate: MeasurementBatch
) -> ImprovementEvidence:
    return ImprovementEvidence(
        metric_id=METRIC,
        formula_id="F-PROJECT-S6-S7/v1alpha1",
        baseline_digest=baseline.metrics[METRIC].digest,
        candidate_digest=candidate.metrics[METRIC].digest,
        baseline_estimate=400.0,
        candidate_estimate=440.0,
        estimate=0.1,
        lower=0.08,
        upper=0.12,
        minimum_effect=0.02,
        accepted=True,
    )


def _inputs():
    candidate = _batch([438.0, 440.0, 442.0])
    original = _batch([398.0, 400.0, 402.0])
    general = _batch([408.0, 410.0, 412.0])
    proposal = HypothesisProposalV2(
        hypothesis_id="hyp-governor-performance",
        component="cpu",
        rank=1,
        rationale="synthetic scenario",
        change={"system.cpu-governor": "performance"},
        risk="low",
        risk_kind=RiskSourceKind.MANIFEST_DERIVED,
    )
    run = DynamicPhaseRunV2(
        workload_contract_digest=DIGEST,
        gate_contract_digest=DIGEST,
        risky_interventions=0,
        promotion=PromotionEvidence(
            candidate_id=proposal.hypothesis_id,
            promoted=True,
            reason="two accepted verification observations",
            observation_count=2,
            distinct_time_blocks=2,
            distinct_environments=1,
        ),
    )
    return run, proposal, candidate, original, general


def test_scenario_profile_persists_content_addressed_dual_baseline_report(
    tmp_path: Path,
) -> None:
    run, proposal, candidate, original, general = _inputs()
    report = build_scenario_profile_report(
        run=run,
        manifest=build_demo_manifest(),
        proposal=proposal,
        environment_digest=DIGEST,
        workload_contract_digest=DIGEST,
        formula_versions_digest=DIGEST,
        candidate_batch=candidate,
        original_baseline=original,
        general_profile_baseline=general,
        original_improvement=_improvement(original, candidate),
        general_profile_improvement=_improvement(general, candidate),
    )

    index = persist_scenario_profile(tmp_path, report)

    assert [item.baseline_kind for item in report.comparisons] == [
        "original",
        "general-profile",
    ]
    assert (
        tmp_path / f"scenario-profile-{index.profile_digest.removeprefix('sha256:')}.json"
    ).is_file()
    assert (
        tmp_path
        / f"scenario-profile-report-{index.report_digest.removeprefix('sha256:')}.json"
    ).is_file()
    assert (tmp_path / "scenario-profile-index.json").is_file()


def test_scenario_profile_rejects_a_forged_baseline_association() -> None:
    run, proposal, candidate, original, general = _inputs()
    forged = _improvement(original, candidate).model_copy(
        update={"baseline_digest": DIGEST}
    )

    with pytest.raises(ValueError, match="different baseline metric"):
        build_scenario_profile_report(
            run=run,
            manifest=build_demo_manifest(),
            proposal=proposal,
            environment_digest=DIGEST,
            workload_contract_digest=DIGEST,
            formula_versions_digest=DIGEST,
            candidate_batch=candidate,
            original_baseline=original,
            general_profile_baseline=general,
            original_improvement=forged,
            general_profile_improvement=_improvement(general, candidate),
        )
