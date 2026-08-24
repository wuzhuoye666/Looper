"""M3 scenario Profile and explicit original/general dual-baseline report."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigManifest
from looper_core.system_opt.dynamic_adapters import HypothesisProposalV2
from looper_core.system_opt.dynamic_loop import DynamicPhaseRunV2
from looper_core.system_opt.profiles import TuningProfile
from looper_core.system_opt.scoring import ImprovementEvidence, MeasurementBatch

SCENARIO_PROFILE_REPORT_SCHEMA = "looper.scenario-profile-report/v1alpha1"
SCENARIO_PROFILE_INDEX_SCHEMA = "looper.scenario-profile-index/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class ScenarioBaselineComparison(StrictModel):
    baseline_kind: Literal["original", "general-profile"]
    baseline_batch_digest: str = Field(pattern=_DIGEST)
    candidate_batch_digest: str = Field(pattern=_DIGEST)
    improvement: ImprovementEvidence


class ScenarioProfileReport(StrictModel):
    schema_version: Literal[SCENARIO_PROFILE_REPORT_SCHEMA] = SCENARIO_PROFILE_REPORT_SCHEMA
    environment_digest: str = Field(pattern=_DIGEST)
    workload_contract_digest: str = Field(pattern=_DIGEST)
    formula_versions_digest: str = Field(pattern=_DIGEST)
    dynamic_run_digest: str = Field(pattern=_DIGEST)
    promotion_digest: str = Field(pattern=_DIGEST)
    candidate_batch_digest: str = Field(pattern=_DIGEST)
    profile: TuningProfile
    comparisons: list[ScenarioBaselineComparison] = Field(min_length=2, max_length=2)
    boundaries: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dual_baselines(self) -> ScenarioProfileReport:
        if [item.baseline_kind for item in self.comparisons] != [
            "original",
            "general-profile",
        ]:
            raise ValueError("scenario report requires original then general-profile baselines")
        if any(
            item.candidate_batch_digest != self.candidate_batch_digest
            for item in self.comparisons
        ):
            raise ValueError("dual-baseline comparisons must share the candidate batch")
        return self

    @property
    def profile_digest(self) -> str:
        return canonical_digest(self.profile.model_dump(mode="json"))

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class ScenarioProfileIndex(StrictModel):
    schema_version: Literal[SCENARIO_PROFILE_INDEX_SCHEMA] = SCENARIO_PROFILE_INDEX_SCHEMA
    profile_digest: str = Field(pattern=_DIGEST)
    report_digest: str = Field(pattern=_DIGEST)


def build_scenario_profile_report(
    *,
    run: DynamicPhaseRunV2,
    manifest: ConfigManifest,
    proposal: HypothesisProposalV2,
    environment_digest: str,
    workload_contract_digest: str,
    formula_versions_digest: str,
    candidate_batch: MeasurementBatch,
    original_baseline: MeasurementBatch,
    general_profile_baseline: MeasurementBatch,
    original_improvement: ImprovementEvidence,
    general_profile_improvement: ImprovementEvidence,
) -> ScenarioProfileReport:
    if run.promotion is None or not run.promotion.promoted:
        raise ValueError("scenario Profile requires a promoted dynamic candidate")
    if run.promotion.candidate_id != proposal.hypothesis_id:
        raise ValueError("promotion candidate does not match the scenario proposal")
    if not (
        candidate_batch.identity
        == original_baseline.identity
        == general_profile_baseline.identity
    ):
        raise ValueError("scenario Profile batches must have identical measurement identity")
    for comparison_name, baseline, improvement in (
        ("original", original_baseline, original_improvement),
        ("general-profile", general_profile_baseline, general_profile_improvement),
    ):
        metric_id = improvement.metric_id
        if metric_id not in candidate_batch.metrics or metric_id not in baseline.metrics:
            raise ValueError(
                f"{comparison_name} comparison metric is absent from a bound batch"
            )
        if improvement.candidate_digest != candidate_batch.metrics[metric_id].digest:
            raise ValueError(
                f"{comparison_name} improvement references a different candidate metric"
            )
        if improvement.baseline_digest != baseline.metrics[metric_id].digest:
            raise ValueError(
                f"{comparison_name} improvement references a different baseline metric"
            )
    for parameter_id, value in proposal.change.items():
        try:
            item = manifest.item_for_parameter(parameter_id)
        except KeyError as error:
            raise ValueError(
                f"scenario proposal references unknown manifest parameter {parameter_id!r}"
            ) from error
        item.validate_value(value)
    profile = TuningProfile(
        id=f"scenario/{proposal.hypothesis_id}",
        config_manifest_digest=manifest.digest,
        settings=dict(proposal.change),
        description=(
            "Scenario-only Profile produced by a promoted dynamic hypothesis; "
            "valid only for the bound environment and workload evidence."
        ),
    )
    return ScenarioProfileReport(
        environment_digest=environment_digest,
        workload_contract_digest=workload_contract_digest,
        formula_versions_digest=formula_versions_digest,
        dynamic_run_digest=run.digest,
        promotion_digest=run.promotion.digest,
        candidate_batch_digest=candidate_batch.digest,
        profile=profile,
        comparisons=[
            ScenarioBaselineComparison(
                baseline_kind="original",
                baseline_batch_digest=original_baseline.digest,
                candidate_batch_digest=candidate_batch.digest,
                improvement=original_improvement,
            ),
            ScenarioBaselineComparison(
                baseline_kind="general-profile",
                baseline_batch_digest=general_profile_baseline.digest,
                candidate_batch_digest=candidate_batch.digest,
                improvement=general_profile_improvement,
            ),
        ],
        boundaries=[
            "synthetic or task-local evidence is not a cross-environment performance claim",
            "the Profile is not enabled automatically and the phase is restored to its start state",
            "reuse requires an exact environment, workload, formula, and manifest identity match",
        ],
    )


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".scenario-profile.", suffix=".tmp", dir=path.parent
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


def persist_scenario_profile(
    control_dir: Path, report: ScenarioProfileReport
) -> ScenarioProfileIndex:
    index = ScenarioProfileIndex(
        profile_digest=report.profile_digest,
        report_digest=report.digest,
    )
    _atomic_write(
        control_dir / f"scenario-profile-{report.profile_digest.removeprefix('sha256:')}.json",
        report.profile.model_dump(mode="json"),
    )
    _atomic_write(
        control_dir / f"scenario-profile-report-{report.digest.removeprefix('sha256:')}.json",
        report.model_dump(mode="json"),
    )
    _atomic_write(
        control_dir / "scenario-profile-index.json",
        index.model_dump(mode="json"),
    )
    return index


__all__ = [
    "SCENARIO_PROFILE_INDEX_SCHEMA",
    "SCENARIO_PROFILE_REPORT_SCHEMA",
    "ScenarioBaselineComparison",
    "ScenarioProfileIndex",
    "ScenarioProfileReport",
    "build_scenario_profile_report",
    "persist_scenario_profile",
]
