"""M3 动态相位 S3 假设路由：症状 → 多组件假设（workload-tuning.md D2，SO-D019）。

假设是一等记录，不是一次路由调用。三条硬规则（用户确认 2026-08-23）：

1. **一个症状至少两个竞争假设登记后才允许干预**——假设数不足时只允许
   O2 取证开窗，不允许改配置（防单次相关归因）；
2. **confirmed 的唯一路径是干预实验**：单组件小步干预 → 同 workload 协议
   复测 → 业务指标（S7）裁决。O2 证据只能把假设推进到 probing，
   永远不能直接 confirmed；
3. refuted 假设的证据身份（环境×组件×症状类×公式版本）**未来**进 L7 负缓存
   ——该桥接是 open decision（SO-D019：L7 第二条目类型的 schema 并存细节
   待提案），本模块只保留 refute 证据 digest，不写缓存。

rank 由 L8 打分器按 S4 二维优先级算出后**注入**（本模块不计算优先级），
排序用 (rank, hypothesis_id) 词典序保证确定性。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigComponent

HYPOTHESIS_SCHEMA = "looper.component-hypothesis/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class HypothesisRoutingError(ValueError):
    """Raised when a hypothesis transition violates the D2 rules."""


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    PROBING = "probing"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    SUPERSEDED = "superseded"


_TERMINAL = {HypothesisStatus.CONFIRMED, HypothesisStatus.REFUTED, HypothesisStatus.SUPERSEDED}


class SymptomRecord(StrictModel):
    """One O0 business symptom raised by an observation window."""

    schema_version: str = HYPOTHESIS_SCHEMA
    symptom_id: str = Field(min_length=1, max_length=160)
    window_id: str = Field(min_length=1, max_length=160)
    workload_contract_digest: str = Field(pattern=_DIGEST)
    evidence_digest: str = Field(pattern=_DIGEST)
    description: str = Field(min_length=1, max_length=500)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class InterventionExperiment(StrictModel):
    """The only admissible confirmation evidence (D2 rule 2).

    ``accepted`` is the S7 verdict on the **business** metric of the retest
    batch under the same workload identity; component micro-metrics never
    confirm a hypothesis. ``business_lcb`` carries the S6 lower confidence
    bound of that retest so stop class 2 (convergence) can count rounds
    without re-parsing the batch; ``None`` marks adapters that do not judge
    through S6 (convergence then simply never fires on their rounds).
    """

    measurement_batch_digest: str = Field(pattern=_DIGEST)
    business_metric_id: str = Field(min_length=1, max_length=160)
    accepted: bool
    business_lcb: float | None = None


class ComponentHypothesis(StrictModel):
    schema_version: str = HYPOTHESIS_SCHEMA
    hypothesis_id: str = Field(min_length=1, max_length=160)
    symptom_id: str = Field(min_length=1, max_length=160)
    component: ConfigComponent
    rank: int = Field(ge=1)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    supporting_digests: list[str] = Field(default_factory=list)
    confirm_evidence: InterventionExperiment | None = None
    refute_evidence_digest: str | None = Field(default=None, pattern=_DIGEST)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class HypothesisLedger:
    """In-memory hypothesis store enforcing the D2 transition rules."""

    def __init__(self) -> None:
        self._symptoms: dict[str, SymptomRecord] = {}
        self._hypotheses: dict[str, ComponentHypothesis] = {}

    # -- registration -------------------------------------------------

    def register_symptom(self, symptom: SymptomRecord) -> None:
        if symptom.symptom_id in self._symptoms:
            raise HypothesisRoutingError(f"symptom '{symptom.symptom_id}' already registered")
        self._symptoms[symptom.symptom_id] = symptom

    def register_hypothesis(self, hypothesis: ComponentHypothesis) -> None:
        if hypothesis.hypothesis_id in self._hypotheses:
            raise HypothesisRoutingError(
                f"hypothesis '{hypothesis.hypothesis_id}' already registered"
            )
        if hypothesis.symptom_id not in self._symptoms:
            raise HypothesisRoutingError(
                f"hypothesis '{hypothesis.hypothesis_id}' references unregistered "
                f"symptom '{hypothesis.symptom_id}'"
            )
        if hypothesis.status is not HypothesisStatus.PROPOSED:
            raise HypothesisRoutingError("hypotheses must register as proposed")
        self._hypotheses[hypothesis.hypothesis_id] = hypothesis

    # -- queries -------------------------------------------------------

    def symptom(self, symptom_id: str) -> SymptomRecord:
        return self._symptoms[symptom_id]

    def hypothesis(self, hypothesis_id: str) -> ComponentHypothesis:
        return self._hypotheses[hypothesis_id]

    def for_symptom(self, symptom_id: str) -> list[ComponentHypothesis]:
        return [h for h in self._hypotheses.values() if h.symptom_id == symptom_id]

    def probe_queue(self, top_k: int) -> list[ComponentHypothesis]:
        """Routing output: non-terminal hypotheses in (rank, id) order, capped.

        ``top_k`` is a task input (D2 rule 4: budget splits across the top-K
        competing hypotheses); this function never invents a default cap.
        """

        if top_k < 1:
            raise HypothesisRoutingError("top_k must be at least 1")
        active = [h for h in self._hypotheses.values() if h.status not in _TERMINAL]
        active.sort(key=lambda h: (h.rank, h.hypothesis_id))
        return active[:top_k]

    # -- transitions ----------------------------------------------------

    def begin_probing(self, hypothesis_id: str, o2_evidence_digest: str) -> ComponentHypothesis:
        """O2 evidence advances proposed -> probing and never further (rule 2)."""

        record = self._hypotheses[hypothesis_id]
        if record.status is HypothesisStatus.PROBING:
            updated = record.model_copy(
                update={
                    "supporting_digests": [*record.supporting_digests, o2_evidence_digest]
                }
            )
        elif record.status is HypothesisStatus.PROPOSED:
            updated = record.model_copy(
                update={
                    "status": HypothesisStatus.PROBING,
                    "supporting_digests": [*record.supporting_digests, o2_evidence_digest],
                }
            )
        else:
            raise HypothesisRoutingError(
                f"hypothesis '{hypothesis_id}' is {record.status.value}; "
                "terminal hypotheses do not collect evidence"
            )
        self._hypotheses[hypothesis_id] = updated
        return updated

    def request_intervention(self, hypothesis_id: str) -> None:
        """Rule 1 gate: at least two competing non-terminal hypotheses per symptom."""

        record = self._hypotheses[hypothesis_id]
        if record.status in _TERMINAL:
            raise HypothesisRoutingError(
                f"hypothesis '{hypothesis_id}' is {record.status.value}; "
                "terminal hypotheses cannot request interventions"
            )
        competing = [
            h
            for h in self.for_symptom(record.symptom_id)
            if h.hypothesis_id != hypothesis_id and h.status not in _TERMINAL
        ]
        if not competing:
            raise HypothesisRoutingError(
                f"symptom '{record.symptom_id}' has only one hypothesis; "
                "single-correlation root causes are not actionable — register at "
                "least one competing hypothesis or collect more O2 evidence only"
            )

    def confirm(
        self, hypothesis_id: str, experiment: InterventionExperiment
    ) -> ComponentHypothesis:
        """Rule 2: confirmation requires a probing hypothesis plus an accepted
        business-metric retest; confirming supersedes the remaining siblings."""

        record = self._hypotheses[hypothesis_id]
        if record.status is not HypothesisStatus.PROBING:
            raise HypothesisRoutingError(
                f"hypothesis '{hypothesis_id}' is {record.status.value}; only a "
                "probing hypothesis with O2 evidence can enter an intervention "
                "experiment"
            )
        if not experiment.accepted:
            raise HypothesisRoutingError(
                "a rejected business retest refutes the hypothesis instead; "
                "call refute with the experiment batch digest"
            )
        updated = record.model_copy(
            update={"status": HypothesisStatus.CONFIRMED, "confirm_evidence": experiment}
        )
        self._hypotheses[hypothesis_id] = updated
        for sibling in self.for_symptom(record.symptom_id):
            if sibling.hypothesis_id != hypothesis_id and sibling.status not in _TERMINAL:
                self._hypotheses[sibling.hypothesis_id] = sibling.model_copy(
                    update={"status": HypothesisStatus.SUPERSEDED}
                )
        return updated

    def refute(self, hypothesis_id: str, evidence_digest: str) -> ComponentHypothesis:
        """Refutation records its evidence digest (rule 3: L7 bridging stays open)."""

        record = self._hypotheses[hypothesis_id]
        if record.status in _TERMINAL:
            raise HypothesisRoutingError(
                f"hypothesis '{hypothesis_id}' is {record.status.value}; "
                "terminal hypotheses are immutable"
            )
        updated = record.model_copy(
            update={"status": HypothesisStatus.REFUTED, "refute_evidence_digest": evidence_digest}
        )
        self._hypotheses[hypothesis_id] = updated
        return updated

    # -- evidence chain --------------------------------------------------

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "symptoms": [
                    self._symptoms[key].model_dump(mode="json", exclude_none=False)
                    for key in sorted(self._symptoms)
                ],
                "hypotheses": [
                    self._hypotheses[key].model_dump(mode="json", exclude_none=False)
                    for key in sorted(self._hypotheses)
                ],
            }
        )


__all__ = [
    "ComponentHypothesis",
    "HypothesisLedger",
    "HypothesisRoutingError",
    "HypothesisStatus",
    "InterventionExperiment",
    "SymptomRecord",
]
