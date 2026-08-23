"""L8 引擎 incumbent 跟踪与 SO-D017 轮级预筛。

决策依据：governance/decision-log.md SO-D017（用户设计输入 2026-08-23）。
置信度判定需要多轮重复、成本高；在置信证据不足时，以"候选轮整体性能
指标不得低于当前最佳轮（incumbent）超过任务容差"做廉价预筛。

红线（SO-D017）：
- 预筛淘汰不是置信性无改善证明，不得写成 L7 负缓存的 no-improvement-lcb；
- 容差是任务输入，不内置默认；
- 没有证据（无 incumbent 或无效用值）时不得淘汰——fail-closed 方向是
  "放行继续测"，因为预筛的目的是省预算，不是制造假阴性。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel

INCUMBENT_SCHEMA = "looper.incumbent-screen/v1alpha1"


class ScreenVerdict(StrEnum):
    PROCEED = "proceed"
    FIRST_OBSERVATION = "first-observation"
    EARLY_SCREENED_OUT = "early-screened-out"
    UNDECIDED = "undecided"


class IncumbentBest(StrictModel):
    round_index: int = Field(ge=1)
    candidate_id: str = Field(min_length=1)
    utility: float
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ScreenDecision(StrictModel):
    schema_version: Literal[INCUMBENT_SCHEMA] = INCUMBENT_SCHEMA
    verdict: ScreenVerdict
    reason: str = Field(min_length=1, max_length=500)
    utility: float | None = None
    incumbent: IncumbentBest | None = None
    tolerance: float | None = None

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class IncumbentTracker:
    """Track the best observed round utility and screen cheap early candidates."""

    def __init__(self, tolerance: float) -> None:
        if tolerance < 0:
            raise ValueError("pre-screen tolerance must be non-negative")
        self.tolerance = tolerance
        self.best: IncumbentBest | None = None

    def screen(self, utility: float | None, *, candidate_id: str) -> ScreenDecision:
        if utility is None:
            return ScreenDecision(
                verdict=ScreenVerdict.UNDECIDED,
                reason="no utility evidence for the candidate; "
                "fail-closed direction is to keep measuring",
            )
        if self.best is None:
            return ScreenDecision(
                verdict=ScreenVerdict.FIRST_OBSERVATION,
                reason=f"first observed utility {utility}; it becomes the incumbent",
                utility=utility,
                tolerance=self.tolerance,
            )
        floor = self.best.utility - self.tolerance
        if utility >= floor:
            return ScreenDecision(
                verdict=ScreenVerdict.PROCEED,
                reason=f"utility {utility} within tolerance floor {floor} "
                f"(incumbent {self.best.utility})",
                utility=utility,
                incumbent=self.best,
                tolerance=self.tolerance,
            )
        return ScreenDecision(
            verdict=ScreenVerdict.EARLY_SCREENED_OUT,
            reason=f"utility {utility} below incumbent floor {floor} "
            f"(incumbent {self.best.utility} from round {self.best.round_index}); "
            "SO-D017 pre-screen: not written to the negative cache",
            utility=utility,
            incumbent=self.best,
            tolerance=self.tolerance,
        )

    def observe(
        self,
        *,
        round_index: int,
        candidate_id: str,
        utility: float,
        evidence_digest: str,
    ) -> bool:
        """Update the incumbent when the utility beats the current best."""

        if self.best is None or utility > self.best.utility:
            self.best = IncumbentBest(
                round_index=round_index,
                candidate_id=candidate_id,
                utility=utility,
                evidence_digest=evidence_digest,
            )
            return True
        return False


__all__ = [
    "IncumbentBest",
    "IncumbentTracker",
    "ScreenDecision",
    "ScreenVerdict",
]
