from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from looper_core.analysis import InsufficientEvidence
from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import StrictModel
from looper_core.evidence import DIGEST_PATTERN
from looper_core.system_opt.config_manifest import ConfigComponent, ConfigManifest, ConfigValueType
from looper_core.system_opt.domain import ResolvedDomain
from looper_core.system_opt.scoring import DiagnosticPriority

HYPOTHESIS_SCHEMA = "looper.optimization-hypothesis/v1alpha1"
CAPACITY_DECISION_SCHEMA = "looper.capacity-frontier-decision/v1alpha1"
CAPACITY_FORMULA_ID = "F-CAPACITY-FRONTIER-001/v1alpha1"

# These fields prevent a capacity result from being reused after code, workload,
# SLO, host, route, or measurement-contract drift.
CAPACITY_IDENTITY_FIELDS = (
    "source_digest",
    "workload_digest",
    "slo_digest",
    "environment_digest",
    "network",
    "target_id",
    "capacity_unit",
    "confidence_level",
    "measurement_contract_digest",
)

_PARAMETER_ID = re.compile(r"^[a-z][a-z0-9._-]*$")


class HypothesisState(StrEnum):
    OBSERVED_ASSOCIATION = "observed-association"
    SUPPORTED_HYPOTHESIS = "supported-hypothesis"
    INTERVENTION_SUPPORTED = "intervention-supported"
    UNRESOLVED = "unresolved"


class CapacityDecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    INCOMPARABLE = "incomparable"
    SAFETY_FAILED = "safety-failed"


class HypothesisEvidence(StrictModel):
    kind: Literal[
        "runtime-profile",
        "source-code",
        "configuration-contract",
        "capacity-outcome",
    ]
    digest: str = Field(pattern=DIGEST_PATTERN)
    locator: str = Field(min_length=1, max_length=1000)
    claim: str = Field(min_length=1, max_length=2000)
    symbol: str | None = Field(default=None, min_length=1, max_length=500)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_source_location(self) -> HypothesisEvidence:
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("line_start and line_end must be declared together")
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError("line_end cannot precede line_start")
        if self.kind == "source-code" and self.symbol is None and self.line_start is None:
            raise ValueError("source-code evidence requires a symbol or exact line range")
        return self


class OptimizationHypothesis(StrictModel):
    schema_version: Literal[HYPOTHESIS_SCHEMA]
    hypothesis_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z][a-z0-9._-]*$",
    )
    statement: str = Field(min_length=1, max_length=4000)
    state: HypothesisState
    context_digest: str = Field(pattern=DIGEST_PATTERN)
    affected_components: list[str] = Field(min_length=1)
    candidate_parameters: dict[str, Any] = Field(min_length=1)
    evidence: list[HypothesisEvidence] = Field(min_length=2)
    competing_hypothesis_digests: list[str] = Field(default_factory=list)
    predecessor_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def validate_evidence_contract(self) -> OptimizationHypothesis:
        if len(self.affected_components) != len(set(self.affected_components)):
            raise ValueError("affected_components must be unique")
        if len(self.competing_hypothesis_digests) != len(
            set(self.competing_hypothesis_digests)
        ):
            raise ValueError("competing_hypothesis_digests must be unique")
        evidence_digests = [item.digest for item in self.evidence]
        if len(evidence_digests) != len(set(evidence_digests)):
            raise ValueError("evidence digests must be unique and cannot inflate support")

        kinds = {item.kind for item in self.evidence}
        if "runtime-profile" not in kinds:
            raise ValueError("a hypothesis requires runtime-profile evidence")
        if not kinds.intersection({"source-code", "configuration-contract"}):
            raise ValueError("a hypothesis requires source-code or configuration-contract evidence")
        if self.state == HypothesisState.INTERVENTION_SUPPORTED:
            if "capacity-outcome" not in kinds:
                raise ValueError("intervention-supported requires capacity-outcome evidence")
            if self.predecessor_digest is None:
                raise ValueError("intervention-supported requires predecessor_digest")
        elif "capacity-outcome" in kinds and self.state != HypothesisState.UNRESOLVED:
            raise ValueError(
                "capacity-outcome evidence requires intervention-supported or unresolved state"
            )

        for parameter_id in self.candidate_parameters:
            if not _PARAMETER_ID.fullmatch(parameter_id):
                raise ValueError(f"invalid candidate parameter id: {parameter_id!r}")
        try:
            canonical_digest(self.candidate_parameters)
        except (TypeError, ValueError) as error:
            raise ValueError("candidate_parameters must be canonical JSON values") from error
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class CapacityFrontierDecision(StrictModel):
    schema_version: Literal[CAPACITY_DECISION_SCHEMA]
    hypothesis_digest: str = Field(pattern=DIGEST_PATTERN)
    baseline_report_digest: str = Field(pattern=DIGEST_PATTERN)
    candidate_report_digest: str = Field(pattern=DIGEST_PATTERN)
    baseline_context_digest: str = Field(pattern=DIGEST_PATTERN)
    candidate_context_digest: str = Field(pattern=DIGEST_PATTERN)
    formula_id: Literal[CAPACITY_FORMULA_ID]
    metric_id: Literal["committed_tps"]
    minimum_effect: float = Field(ge=0)
    baseline_confirmed_pass: float | None = Field(default=None, gt=0)
    baseline_confirmed_fail: float | None = Field(default=None, gt=0)
    candidate_confirmed_pass: float | None = Field(default=None, gt=0)
    candidate_confirmed_fail: float | None = Field(default=None, gt=0)
    estimate: float | None = None
    lower: float | None = None
    upper: float | None = None
    status: CapacityDecisionStatus
    rollback_verified: bool
    identity_mismatches: list[str]
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_decision(self) -> CapacityFrontierDecision:
        numeric = [
            self.minimum_effect,
            self.baseline_confirmed_pass,
            self.baseline_confirmed_fail,
            self.candidate_confirmed_pass,
            self.candidate_confirmed_fail,
            self.estimate,
            self.lower,
            self.upper,
        ]
        if any(value is not None and not math.isfinite(value) for value in numeric):
            raise ValueError("capacity decision values must be finite")
        if self.status == CapacityDecisionStatus.ACCEPTED:
            if not self.rollback_verified:
                raise ValueError("an accepted capacity decision requires verified rollback")
            if self.lower is None or self.lower <= self.minimum_effect:
                raise ValueError("an accepted capacity decision must clear the minimum effect")
        if self.status == CapacityDecisionStatus.INCOMPARABLE and not self.identity_mismatches:
            raise ValueError("an incomparable decision requires identity mismatches")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def hypothesis_context_digest(identity: Mapping[str, str]) -> str:
    missing = [field for field in CAPACITY_IDENTITY_FIELDS if not identity.get(field)]
    if missing:
        raise InsufficientEvidence(f"capacity identity is missing: {missing}")
    return canonical_digest({field: identity[field] for field in CAPACITY_IDENTITY_FIELDS})


def _value_in_resolved_domain(value: Any, domain: ResolvedDomain) -> bool:
    if domain.value_type == ConfigValueType.BOOLEAN:
        return type(value) is bool and canonical_json(value) in {
            canonical_json(choice) for choice in (domain.choices or [False, True])
        }
    if domain.value_type in {ConfigValueType.INTEGER, ConfigValueType.NUMBER}:
        if domain.value_type == ConfigValueType.INTEGER and type(value) is not int:
            return False
        if domain.value_type == ConfigValueType.NUMBER and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            return False
        numeric = float(value)
        if not math.isfinite(numeric):
            return False
        assert domain.minimum is not None and domain.maximum is not None
        if numeric < domain.minimum or numeric > domain.maximum:
            return False
        if domain.step is not None:
            steps = (numeric - domain.minimum) / domain.step
            return math.isclose(steps, round(steps), abs_tol=1e-9)
        return True
    return canonical_json(value) in {
        canonical_json(choice) for choice in (domain.choices or [])
    }


def rank_authorized_hypotheses(
    hypotheses: Sequence[OptimizationHypothesis],
    priorities: Sequence[DiagnosticPriority],
    *,
    expected_context_digest: str,
    manifest: ConfigManifest,
    resolved_domains: Mapping[str, ResolvedDomain],
) -> tuple[list[OptimizationHypothesis], dict[str, str]]:
    """Filter and order hypotheses without inventing a cross-component score.

    The mapping in ``resolved_domains`` is the already intersected target-capability
    and task-authorization domain. A hypothesis outside that mapping, outside its
    values, or outside a runtime-routed component is rejected before it can become
    an executable candidate.
    """

    priority_keys: dict[str, tuple[Any, ...]] = {}
    for priority in priorities:
        key = (
            priority.pareto_rank if priority.pareto_rank is not None else math.inf,
            -priority.pressure,
            -priority.adverse_change,
            -priority.persistence,
            -priority.confidence,
            priority.metric_id,
        )
        previous = priority_keys.get(priority.component)
        if previous is None or key < previous:
            priority_keys[priority.component] = key

    accepted: list[tuple[tuple[Any, ...], OptimizationHypothesis]] = []
    rejected: dict[str, str] = {}
    for hypothesis in hypotheses:
        digest = hypothesis.digest
        if hypothesis.context_digest != expected_context_digest:
            rejected[digest] = "context-digest-mismatch"
            continue
        if hypothesis.state not in {
            HypothesisState.OBSERVED_ASSOCIATION,
            HypothesisState.SUPPORTED_HYPOTHESIS,
        }:
            rejected[digest] = "hypothesis-state-is-not-actionable"
            continue

        components: set[str] = set()
        invalid_reason: str | None = None
        for parameter_id, value in hypothesis.candidate_parameters.items():
            domain = resolved_domains.get(parameter_id)
            if domain is None:
                invalid_reason = f"parameter-not-authorized:{parameter_id}"
                break
            if domain.parameter_id != parameter_id:
                invalid_reason = f"resolved-domain-identity-mismatch:{parameter_id}"
                break
            if not _value_in_resolved_domain(value, domain):
                invalid_reason = f"value-outside-resolved-domain:{parameter_id}"
                break
            item = manifest.item_for_parameter(parameter_id)
            components.add(item.primary_component.value)
        if invalid_reason is not None:
            rejected[digest] = invalid_reason
            continue
        if not components.issubset(set(hypothesis.affected_components)):
            rejected[digest] = "candidate-component-is-not-explained-by-hypothesis"
            continue
        routed = components.intersection(priority_keys)
        if not routed:
            rejected[digest] = "no-runtime-priority-for-candidate-component"
            continue

        component_key = min(priority_keys[component] for component in routed)
        maturity_key = (
            0 if hypothesis.state == HypothesisState.SUPPORTED_HYPOTHESIS else 1
        )
        accepted.append(
            ((*component_key, maturity_key, hypothesis.hypothesis_id, digest), hypothesis)
        )

    accepted.sort(key=lambda item: item[0])
    return [hypothesis for _, hypothesis in accepted], rejected


def _resolved_frontier(
    frontier: Mapping[str, Any],
    *,
    label: str,
) -> tuple[float, float] | None:
    if frontier.get("status") != "resolved":
        return None
    confirmed_pass = frontier.get("confirmed_pass")
    confirmed_fail = frontier.get("confirmed_fail")
    if isinstance(confirmed_pass, bool) or not isinstance(confirmed_pass, (int, float)):
        raise InsufficientEvidence(f"{label} confirmed_pass is missing or invalid")
    if isinstance(confirmed_fail, bool) or not isinstance(confirmed_fail, (int, float)):
        raise InsufficientEvidence(f"{label} confirmed_fail is missing or invalid")
    lower = float(confirmed_pass)
    upper = float(confirmed_fail)
    if not math.isfinite(lower) or not math.isfinite(upper) or lower <= 0 or upper <= 0:
        raise InsufficientEvidence(f"{label} capacity frontier must be finite and positive")
    if lower > upper:
        raise InsufficientEvidence(f"{label} confirmed_pass exceeds confirmed_fail")
    return lower, upper


def evaluate_capacity_frontiers(
    *,
    hypothesis_digest: str,
    baseline_frontier: Mapping[str, Any],
    candidate_frontier: Mapping[str, Any],
    baseline_report_digest: str,
    candidate_report_digest: str,
    baseline_identity: Mapping[str, str],
    candidate_identity: Mapping[str, str],
    minimum_effect: float,
    rollback_verified: bool,
) -> CapacityFrontierDecision:
    """Compare two SLO-constrained capacity intervals and fail closed on overlap."""

    if not math.isfinite(minimum_effect) or minimum_effect < 0:
        raise ValueError("minimum_effect must be finite and non-negative")
    baseline_context = hypothesis_context_digest(baseline_identity)
    candidate_context = hypothesis_context_digest(candidate_identity)
    mismatches = [
        field
        for field in CAPACITY_IDENTITY_FIELDS
        if baseline_identity[field] != candidate_identity[field]
    ]
    common = {
        "schema_version": CAPACITY_DECISION_SCHEMA,
        "hypothesis_digest": hypothesis_digest,
        "baseline_report_digest": baseline_report_digest,
        "candidate_report_digest": candidate_report_digest,
        "baseline_context_digest": baseline_context,
        "candidate_context_digest": candidate_context,
        "formula_id": CAPACITY_FORMULA_ID,
        "metric_id": "committed_tps",
        "minimum_effect": minimum_effect,
        "rollback_verified": rollback_verified,
        "identity_mismatches": mismatches,
    }
    if mismatches:
        return CapacityFrontierDecision(
            **common,
            status=CapacityDecisionStatus.INCOMPARABLE,
            reason=f"capacity identities differ: {mismatches}",
        )

    baseline = _resolved_frontier(baseline_frontier, label="baseline")
    candidate = _resolved_frontier(candidate_frontier, label="candidate")
    if baseline is None or candidate is None:
        return CapacityFrontierDecision(
            **common,
            status=CapacityDecisionStatus.INCONCLUSIVE,
            reason="both baseline and candidate capacity frontiers must be resolved",
        )

    baseline_pass, baseline_fail = baseline
    candidate_pass, candidate_fail = candidate
    baseline_midpoint = (baseline_pass + baseline_fail) / 2
    candidate_midpoint = (candidate_pass + candidate_fail) / 2
    estimate = candidate_midpoint / baseline_midpoint - 1
    lower = candidate_pass / baseline_fail - 1
    upper = candidate_fail / baseline_pass - 1

    if not rollback_verified:
        status = CapacityDecisionStatus.SAFETY_FAILED
        reason = "candidate result cannot advance because rollback was not verified"
    elif lower > minimum_effect:
        status = CapacityDecisionStatus.ACCEPTED
        reason = "the worst-case capacity gain clears the explicit minimum effect"
    elif upper <= minimum_effect:
        status = CapacityDecisionStatus.REJECTED
        reason = "the best-case capacity gain does not clear the explicit minimum effect"
    else:
        status = CapacityDecisionStatus.INCONCLUSIVE
        reason = "capacity intervals overlap the explicit minimum-effect boundary"

    return CapacityFrontierDecision(
        **common,
        baseline_confirmed_pass=baseline_pass,
        baseline_confirmed_fail=baseline_fail,
        candidate_confirmed_pass=candidate_pass,
        candidate_confirmed_fail=candidate_fail,
        estimate=estimate,
        lower=lower,
        upper=upper,
        status=status,
        reason=reason,
    )


__all__ = [
    "CAPACITY_DECISION_SCHEMA",
    "CAPACITY_FORMULA_ID",
    "CAPACITY_IDENTITY_FIELDS",
    "HYPOTHESIS_SCHEMA",
    "CapacityDecisionStatus",
    "CapacityFrontierDecision",
    "HypothesisEvidence",
    "HypothesisState",
    "OptimizationHypothesis",
    "evaluate_capacity_frontiers",
    "hypothesis_context_digest",
    "rank_authorized_hypotheses",
    "ComponentHypothesis",
    "HypothesisLedger",
    "HypothesisRoutingError",
    "HypothesisStatus",
    "InterventionExperiment",
    "SymptomRecord",
]


# ======================================================================
# 作用域声明：以下为 System Optimizer M3 动态相位假设路由（组件假设线）。
# 上方容量线（OptimizationHypothesis/CapacityFrontierDecision）与下方组件线
# 属不同业务域，除共享 pydantic/canonical 基建外互不依赖。
# ======================================================================

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



COMPONENT_HYPOTHESIS_SCHEMA = "looper.component-hypothesis/v1alpha1"
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

    schema_version: str = COMPONENT_HYPOTHESIS_SCHEMA
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
    schema_version: str = COMPONENT_HYPOTHESIS_SCHEMA
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
