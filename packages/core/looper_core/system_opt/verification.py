"""M3 S9 复验窗口：VerificationObservation 的真实生产者（workload-tuning.md D3）。

闭合审查 M11 的设计缺口：此前唯一观测源是引擎轮内终裁（``passed`` 复用同轮
verdict、恒真）。本模块让 ``passed`` 由**重测批次**的 S7 裁决产生——对同一
workload 身份重测业务主指标（S6/S7 由调用方按既有公式计算，本层不重复实现），
``LCB > MDE`` 即 passed，可为 false；失败观测进入 ``evaluate_promotion`` 的
fail-closed 路径（不晋升 + 触发 L6 候选级回退）。

静态相位的 engine-round 观测保留"采纳记录"性质；晋升合同
（``PromotionContract`` 的 min_observations / distinct time blocks /
environments）意味着仅靠轮内观测天然不够，必须由本模块的复验窗口补足。
"""

from __future__ import annotations

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.result_vector import VerificationObservation
from looper_core.system_opt.scoring import ImprovementEvidence

VERIFICATION_WINDOW_SCHEMA = "looper.verification-window/v1alpha1"
RETEST_OUTCOME_SCHEMA = "looper.retest-outcome/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class RetestOutcome(StrictModel):
    """What a re-verification window produces: the S6 improvement recomputed on
    the retest batch plus the digest of that raw MeasurementBatch."""

    improvement: ImprovementEvidence
    measurement_batch_digest: str = Field(pattern=_DIGEST)


class VerificationWindow(StrictModel):
    """One re-verification window over a promoted-candidate under study.

    ``observation_window_digest`` binds the ObservationWindow (D1) that
    re-measured the business metric under the same workload identity;
    ``evidence_digest`` binds the retest MeasurementBatch.
    """

    schema_version: str = VERIFICATION_WINDOW_SCHEMA
    window_id: str = Field(min_length=1, max_length=160)
    promoted_candidate_id: str = Field(min_length=1, max_length=200)
    workload_contract_digest: str = Field(pattern=_DIGEST)
    observation_window_digest: str = Field(pattern=_DIGEST)
    business_metric_id: str = Field(min_length=1, max_length=160)
    passed: bool
    evidence_digest: str = Field(pattern=_DIGEST)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def verification_observation(
    *,
    window_id: str,
    promoted_candidate_id: str,
    environment_digest: str,
    outcome: ImprovementEvidence,
    evidence_digest: str,
) -> VerificationObservation:
    """Produce one S9 observation with ``passed`` from the S7 rule on retest data.

    ``passed`` is ``outcome.lower > outcome.minimum_effect`` — the same rule
    the engine judge applies, computed on the **retest** improvement, so it
    can legitimately be false (the M11 closure).
    """

    return VerificationObservation(
        candidate_id=promoted_candidate_id,
        passed=outcome.lower > outcome.minimum_effect,
        time_block_id=window_id,
        environment_digest=environment_digest,
        evidence_digest=evidence_digest,
    )


__all__ = [
    "RETEST_OUTCOME_SCHEMA",
    "RetestOutcome",
    "VERIFICATION_WINDOW_SCHEMA",
    "VerificationWindow",
    "verification_observation",
]
