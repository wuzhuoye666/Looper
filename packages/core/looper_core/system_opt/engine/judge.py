"""L8 判断器：S0 可比 → S2 硬门禁 → S7 接受 的固定顺序裁决。

架构层：总体架构 v2 的 L8。每个否定结论必须带理由（哪条公式、哪个输入不满足）；
门禁不可被收益补偿（S2）；接受要求主目标 LCB 严格大于 MDE（S7）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.tuning import CandidateEvaluation

JUDGE_SCHEMA = "looper.candidate-verdict/v1alpha1"


class CandidateVerdict(StrictModel):
    schema_version: Literal[JUDGE_SCHEMA] = JUDGE_SCHEMA
    candidate_id: str = Field(min_length=1)
    comparable: bool
    feasible: bool
    accepted: bool
    reasons: list[str] = Field(min_length=1)
    primary_metric: str = Field(min_length=1)
    minimum_effect: float

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def evaluate_candidate(
    candidate: CandidateEvaluation,
    *,
    primary_metric: str,
    minimum_effect: float,
) -> CandidateVerdict:
    """Judge one candidate evaluation in fixed S0 -> S2 -> S7 order."""

    reasons: list[str] = []

    # S0: identity comparability.
    if not candidate.comparable:
        reasons.append(
            f"S0: identity mismatch {sorted(candidate.identity_mismatches)}; "
            "candidate is not comparable to its baseline"
        )
        return CandidateVerdict(
            candidate_id=candidate.candidate_id,
            comparable=False,
            feasible=False,
            accepted=False,
            reasons=reasons,
            primary_metric=primary_metric,
            minimum_effect=minimum_effect,
        )

    # S2: non-compensable hard gates.
    failed_gates = sorted(
        gate.gate_id for gate in candidate.gates if not gate.passed
    )
    if failed_gates or not candidate.feasible:
        reasons.append(
            f"S2: hard gates failed {failed_gates or ['feasible=false']}; "
            "no improvement can compensate a gate failure"
        )
        return CandidateVerdict(
            candidate_id=candidate.candidate_id,
            comparable=True,
            feasible=False,
            accepted=False,
            reasons=reasons,
            primary_metric=primary_metric,
            minimum_effect=minimum_effect,
        )

    # S7: robust acceptance on the primary metric.
    improvement = candidate.improvements.get(primary_metric)
    if improvement is None:
        reasons.append(
            f"S7: primary metric '{primary_metric}' has no improvement evidence "
            "for this candidate"
        )
        return CandidateVerdict(
            candidate_id=candidate.candidate_id,
            comparable=True,
            feasible=True,
            accepted=False,
            reasons=reasons,
            primary_metric=primary_metric,
            minimum_effect=minimum_effect,
        )
    accepted = improvement.lower > minimum_effect
    reasons.append(
        f"S7: primary LCB={improvement.lower:.6f} vs MDE={minimum_effect:.6f}; "
        f"accepted={accepted} (formula {improvement.formula_id})"
    )
    return CandidateVerdict(
        candidate_id=candidate.candidate_id,
        comparable=True,
        feasible=True,
        accepted=accepted,
        reasons=reasons,
        primary_metric=primary_metric,
        minimum_effect=minimum_effect,
    )


__all__ = ["CandidateVerdict", "evaluate_candidate"]
