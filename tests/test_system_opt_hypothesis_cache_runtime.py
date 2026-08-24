from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from looper_core.system_opt.dynamic_adapters import (
    BusinessRetestPolicy,
    build_business_metric_contract,
)
from looper_core.system_opt.dynamic_demo import (
    BUSINESS_METRIC,
    build_demo_business_policy,
    build_demo_workload_contract,
)
from looper_core.system_opt.hypothesis import (
    ComponentHypothesis,
    InterventionExperiment,
    SymptomRecord,
)
from looper_core.system_opt.hypothesis_cache import (
    HypothesisCacheBinding,
    HypothesisCacheRuntime,
)
from looper_core.system_opt.negative_cache import (
    HypothesisCacheRetentionPolicy,
    NegativeCache,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
NOW = datetime(2026, 8, 24, tzinfo=UTC)


def _runtime(path: Path) -> tuple[HypothesisCacheRuntime, HypothesisCacheBinding]:
    contract = build_demo_workload_contract()
    policy = build_demo_business_policy()
    binding = HypothesisCacheBinding(
        environment_digest=DIGEST_A,
        workload_contract_digest=contract.digest,
        symptom_class_digest=DIGEST_B,
        metric_contract=build_business_metric_contract(contract, policy),
        refutation_policy_digest=policy_digest(policy),
        formula_versions={"F-PROJECT-S6-S7": "v1alpha1"},
    )
    return (
        HypothesisCacheRuntime(
            cache=NegativeCache(),
            path=path,
            binding=binding,
            retention_policy=HypothesisCacheRetentionPolicy(
                policy_id="test-policy",
                mode="identity-change-only",
                expires_at=None,
            ),
        ),
        binding,
    )


def policy_digest(policy: BusinessRetestPolicy) -> str:
    from looper_core.canonical import canonical_digest

    return canonical_digest(policy.model_dump(mode="json"))


def _records(binding: HypothesisCacheBinding):
    hypothesis = ComponentHypothesis(
        hypothesis_id="hyp-cpu",
        symptom_id="symptom-1",
        component="cpu",
        rank=1,
    )
    symptom = SymptomRecord(
        symptom_id="symptom-1",
        window_id="window-1",
        workload_contract_digest=binding.workload_contract_digest,
        evidence_digest=DIGEST_A,
        description="business SLO violation",
    )
    experiment = InterventionExperiment(
        measurement_batch_digest=DIGEST_A,
        business_metric_id=BUSINESS_METRIC,
        accepted=False,
        business_lcb=-0.1,
    )
    return hypothesis, symptom, experiment


def test_runtime_persists_rejected_business_retest_and_excludes_component(
    tmp_path: Path,
) -> None:
    runtime, binding = _runtime(tmp_path / "negative-cache.jsonl")
    hypothesis, symptom, experiment = _records(binding)

    entry = runtime.record_refutation(
        hypothesis, symptom, experiment, recorded_at=NOW
    )

    assert runtime.entries == [entry]
    assert runtime.excluded_components({hypothesis.hypothesis_id: hypothesis}, at=NOW) == {
        "cpu"
    }
    assert NegativeCache.load(tmp_path / "negative-cache.jsonl").hypothesis_entries == [entry]


def test_runtime_rejects_accepted_or_wrong_metric_experiments(tmp_path: Path) -> None:
    runtime, binding = _runtime(tmp_path / "negative-cache.jsonl")
    hypothesis, symptom, experiment = _records(binding)

    with pytest.raises(ValueError, match="accepted"):
        runtime.record_refutation(
            hypothesis,
            symptom,
            experiment.model_copy(update={"accepted": True}),
            recorded_at=NOW,
        )
    with pytest.raises(ValueError, match="business metric"):
        runtime.record_refutation(
            hypothesis,
            symptom,
            experiment.model_copy(update={"business_metric_id": "other.metric"}),
            recorded_at=NOW,
        )
    assert not (tmp_path / "negative-cache.jsonl").exists()
