"""动态相位会话适配器：run_dynamic_phase 注入回调的真实接线（SO-D020 对侧）。

会话目录约定（引擎与外部负载会话之间的唯一界面；测试侧 runner 写 ``windows/``，
引擎只读 ``windows/``、只写 ``control/``）::

    session-dir/
      workload-contract.yaml        # WorkloadContract（YAML）
      gate-contract.json            # DynamicPhaseGateContract
      promotion-contract.json       # PromotionContract
      hypothesis-proposals.yaml     # 症状 → 竞争假设的声明式提案（rank 显式注入）
      business-policy.json          # BusinessRetestPolicy（全部数值任务显式）
      baseline-batch.json           # 冻结的业务基线 MeasurementBatch
      windows/<window_id>/
        identity.json               # LoadCommandIdentity（runner 按其实际执行计算）
        o0.txt                      # 外部负载自身输出原文（O0 解析输入）
      control/                      # 引擎 → 外部的邮箱（复测请求、干预证据）

设计边界（诚实声明）：

- 假设源 v1 是**声明式提案文件**——rank 与 change 由会话资产显式给出，
  不是从 O1 数据在线推导的 S4 优先级；在线推导依赖 O1 活体采集源（PKG-B
  泳道）与 S4 映射接线，是后续层。本适配器就是那个可替换的注入缝。
- 干预 = SafetyController ``keep`` 路径施加配置 → 请求外部复测 → S6/S7 业务
  裁决；被拒绝的假设通过同一安全路径把 pre-apply 快照值写回（候选级回退）。
  恢复失败属于安全事件：抛 :class:`DynamicInterventionError` 让整个相位
  停下来（fail-closed，宁停不溜）。
- 复测身份：每个窗口的 ``identity.json`` 必须与 workload 合同声明的
  ``load_command.identity_digest`` 一致，否则视为负载漂移，复测不可比。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import Field, TypeAdapter, model_validator

from looper_core.analysis import aggregate
from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import ComponentCollectionPlan
from looper_core.system_opt.config_manifest import ConfigComponent, ConfigManifest, RiskLevel
from looper_core.system_opt.executor import ExecutorBackend
from looper_core.system_opt.hypothesis import (
    ComponentHypothesis,
    InterventionExperiment,
    SymptomRecord,
)
from looper_core.system_opt.intervention import (
    INTERVENTION_RISK_SOURCE_SCHEMA,
    InterventionOutcome,
    InterventionPlan,
    ReceiptOperation,
    ReceiptStageV2,
    RiskSource,
    RiskSourceItem,
    RiskSourceKind,
    receipt_stage_for,
)
from looper_core.system_opt.intervention_receipt import AttentionSink, DurableReceiptStore
from looper_core.system_opt.observation import parse_o0_metrics
from looper_core.system_opt.policy import (
    MetricContract,
    MetricDirection,
    MetricRole,
    PressureMethod,
    StatisticsPolicy,
)
from looper_core.system_opt.safety import (
    ObservedSafetyResult,
    ProgressRecordError,
    SafetyController,
    SafetyProgressEvent,
    SafetyProgressStage,
    SafetyState,
)
from looper_core.system_opt.scoring import (
    ImprovementEvidence,
    MeasurementBatch,
    MetricEvidence,
    bootstrap_improvement,
)
from looper_core.system_opt.verification import RetestOutcome
from looper_core.system_opt.workload import (
    LoadCommandIdentity,
    O0MetricDirection,
    WorkloadContract,
    parse_workload_contract_yaml,
)

HYPOTHESIS_PROPOSALS_SCHEMA = "looper.hypothesis-proposals/v1alpha1"
HYPOTHESIS_PROPOSALS_V2_SCHEMA = "looper.hypothesis-proposals/v1alpha2"
BUSINESS_POLICY_SCHEMA = "looper.business-retest-policy/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Publish one control JSON only after its complete payload is written to a temp file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".control-{os.getpid()}-{uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class SessionFileMissing(TimeoutError):
    """A session file the engine must read never appeared within the wait budget."""


class RetestIdentityDrift(ValueError):
    """A retest window ran a different load than the workload contract declares."""


class DynamicInterventionError(RuntimeError):
    """Safety-relevant intervention failure; the phase must stop (fail-closed)."""

    def __init__(
        self,
        message: str,
        *,
        plan_digest: str | None = None,
        outcome: InterventionOutcome | None = None,
        candidate_receipt_digest: str | None = None,
        recovery_receipt_digest: str | None = None,
        triggered_field: str = "intervention.execution",
    ) -> None:
        super().__init__(message)
        self.plan_digest = plan_digest
        self.outcome = outcome
        self.candidate_receipt_digest = candidate_receipt_digest
        self.recovery_receipt_digest = recovery_receipt_digest
        self.triggered_field = triggered_field


# ---------------------------------------------------------------------------
# 会话布局与声明式资产
# ---------------------------------------------------------------------------


class SessionLayout:
    """Pure path conventions for one dynamic-phase session directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def workload_contract(self) -> Path:
        return self.root / "workload-contract.yaml"

    @property
    def gate_contract(self) -> Path:
        return self.root / "gate-contract.json"

    @property
    def promotion_contract(self) -> Path:
        return self.root / "promotion-contract.json"

    @property
    def hypothesis_proposals(self) -> Path:
        return self.root / "hypothesis-proposals.yaml"

    @property
    def o1_collection_plans(self) -> Path:
        return self.root / "o1-collection-plans.json"

    @property
    def business_policy(self) -> Path:
        return self.root / "business-policy.json"

    @property
    def baseline_batch(self) -> Path:
        return self.root / "baseline-batch.json"

    @property
    def online_routing_policy(self) -> Path:
        return self.root / "online-routing-policy.yaml"

    @property
    def diagnostic_reference(self) -> Path:
        return self.root / "diagnostic-reference.json"

    @property
    def online_o1_snapshots(self) -> Path:
        return self.root / "online-o1-snapshots.json"

    @property
    def online_routing_contract(self) -> Path:
        return self.root / "online-routing-contract.json"

    @property
    def hypothesis_cache_retention(self) -> Path:
        return self.root / "hypothesis-cache-retention.json"

    @property
    def general_profile_baseline(self) -> Path:
        return self.root / "general-profile-baseline.json"

    @property
    def windows(self) -> Path:
        return self.root / "windows"

    @property
    def control(self) -> Path:
        return self.root / "control"

    def window(self, window_id: str) -> Path:
        return self.windows / window_id


class HypothesisProposal(StrictModel):
    """One declarative competing hypothesis: component + rank + concrete change."""

    hypothesis_id: str = Field(min_length=1, max_length=160)
    component: ConfigComponent
    rank: int = Field(ge=1)
    rationale: str = Field(min_length=1, max_length=500)
    change: dict[str, Any] = Field(min_length=1)
    supporting_digests: list[str] = Field(default_factory=list)


class HypothesisProposalsFile(StrictModel):
    schema_version: Literal[HYPOTHESIS_PROPOSALS_SCHEMA] = HYPOTHESIS_PROPOSALS_SCHEMA
    proposals: list[HypothesisProposal] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> HypothesisProposalsFile:
        ids = [p.hypothesis_id for p in self.proposals]
        if len(ids) != len(set(ids)):
            raise ValueError("hypothesis proposal ids must be unique")
        return self

    def by_id(self) -> dict[str, HypothesisProposal]:
        return {p.hypothesis_id: p for p in self.proposals}


class HypothesisProposalV2(StrictModel):
    """A v2 proposal with explicit task risk; no implicit-low migration exists."""

    hypothesis_id: str = Field(min_length=1, max_length=160)
    component: ConfigComponent
    rank: int = Field(ge=1)
    rationale: str = Field(min_length=1, max_length=500)
    change: dict[str, Any] = Field(min_length=1)
    supporting_digests: list[str] = Field(default_factory=list)
    risk: RiskLevel
    risk_kind: RiskSourceKind
    risk_rationale: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_risk_claim(self) -> HypothesisProposalV2:
        if self.risk_kind is RiskSourceKind.TASK_OVERRIDE:
            if self.risk_rationale is None:
                raise ValueError("task-override proposals require risk_rationale")
        elif self.risk_rationale is not None:
            raise ValueError("manifest-derived proposals cannot carry risk_rationale")
        return self


class HypothesisProposalsFileV2(StrictModel):
    schema_version: Literal[HYPOTHESIS_PROPOSALS_V2_SCHEMA] = HYPOTHESIS_PROPOSALS_V2_SCHEMA
    proposals: list[HypothesisProposalV2] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> HypothesisProposalsFileV2:
        ids = [proposal.hypothesis_id for proposal in self.proposals]
        if len(ids) != len(set(ids)):
            raise ValueError("hypothesis proposal ids must be unique")
        return self

    def by_id(self) -> dict[str, HypothesisProposalV2]:
        return {proposal.hypothesis_id: proposal for proposal in self.proposals}


class DynamicInterventionExecution(StrictModel):
    """A completed adapter execution with durable receipt associations."""

    plan_digest: str = Field(pattern=_DIGEST)
    outcome: InterventionOutcome
    candidate_receipt_digest: str = Field(pattern=_DIGEST)
    recovery_receipt_digest: str | None = Field(default=None, pattern=_DIGEST)

    @model_validator(mode="after")
    def bind_outcome(self) -> DynamicInterventionExecution:
        if self.outcome.plan_digest != self.plan_digest:
            raise ValueError("dynamic intervention outcome is not bound to its plan")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class BusinessRetestPolicy(StrictModel):
    """Every number behind S6/S7 on the business metric is an explicit task input."""

    schema_version: Literal[BUSINESS_POLICY_SCHEMA] = BUSINESS_POLICY_SCHEMA
    business_metric_id: str = Field(min_length=1, max_length=160)
    phase_id: str = Field(min_length=1, max_length=120)
    scale: float = Field(gt=0)
    minimum_effect: float = Field(ge=0)
    minimum_samples: int = Field(ge=2)
    confidence_level: float = Field(gt=0.5, lt=1)
    bootstrap_resamples: int = Field(ge=100, le=100000)
    random_seed: int = Field(ge=0)
    retest_window_count: int = Field(ge=2)
    window_wait_timeout_seconds: float = Field(gt=0)
    window_poll_seconds: float = Field(gt=0, le=60)


# ---------------------------------------------------------------------------
# 窗口文件源（load_identity / o0_source）
# ---------------------------------------------------------------------------


def _wait_for_file(path: Path, poll_seconds: float, timeout_seconds: float) -> Path:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if path.is_file():
            return path
        if time.monotonic() >= deadline:
            raise SessionFileMissing(f"session file never appeared: {path}")
        time.sleep(poll_seconds)


class FileLoadIdentity:
    """``load_identity`` adapter: read the runner-recorded LoadCommandIdentity."""

    def __init__(self, layout: SessionLayout, policy: BusinessRetestPolicy) -> None:
        self._layout = layout
        self._policy = policy

    def __call__(self, window_id: str) -> LoadCommandIdentity:
        path = _wait_for_file(
            self._layout.window(window_id) / "identity.json",
            self._policy.window_poll_seconds,
            self._policy.window_wait_timeout_seconds,
        )
        return LoadCommandIdentity.model_validate(json.loads(path.read_text(encoding="utf-8")))


class FileO0Source:
    """``o0_source`` adapter: raw external-load output text per window."""

    def __init__(self, layout: SessionLayout, policy: BusinessRetestPolicy) -> None:
        self._layout = layout
        self._policy = policy

    def __call__(self, window_id: str) -> str:
        path = _wait_for_file(
            self._layout.window(window_id) / "o0.txt",
            self._policy.window_poll_seconds,
            self._policy.window_wait_timeout_seconds,
        )
        return path.read_text(encoding="utf-8")


class FileHypothesisProposals:
    """``hypothesis_source`` adapter: declarative competing hypotheses.

    The same proposal set serves any symptom raised in this session; the ledger
    still enforces the D2 hard rules (>= 2 registered hypotheses before any
    intervention request is admissible).
    """

    def __init__(self, proposals: HypothesisProposalsFile) -> None:
        self._proposals = proposals.proposals

    def __call__(self, symptom: SymptomRecord) -> list[ComponentHypothesis]:
        return [
            ComponentHypothesis(
                hypothesis_id=proposal.hypothesis_id,
                symptom_id=symptom.symptom_id,
                component=proposal.component,
                rank=proposal.rank,
                supporting_digests=list(proposal.supporting_digests),
            )
            for proposal in self._proposals
        ]


class FileHypothesisProposalsV2:
    """Expose v2 declarative proposals to the unchanged hypothesis ledger."""

    def __init__(self, proposals: HypothesisProposalsFileV2) -> None:
        self._proposals = proposals.proposals

    def __call__(self, symptom: SymptomRecord) -> list[ComponentHypothesis]:
        return [
            ComponentHypothesis(
                hypothesis_id=proposal.hypothesis_id,
                symptom_id=symptom.symptom_id,
                component=proposal.component,
                rank=proposal.rank,
                supporting_digests=list(proposal.supporting_digests),
            )
            for proposal in self._proposals
        ]


def load_hypothesis_proposals(path: Path) -> HypothesisProposalsFile:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"hypothesis proposals file is not a mapping: {path}")
    return HypothesisProposalsFile.model_validate(document)


def load_hypothesis_proposals_versioned(
    path: Path,
) -> HypothesisProposalsFile | HypothesisProposalsFileV2:
    """Dispatch by schema without filling v2 risk fields into legacy input."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"hypothesis proposals file is not a mapping: {path}")
    version = document.get("schema_version")
    if version == HYPOTHESIS_PROPOSALS_SCHEMA:
        return HypothesisProposalsFile.model_validate(document)
    if version == HYPOTHESIS_PROPOSALS_V2_SCHEMA:
        return HypothesisProposalsFileV2.model_validate(document)
    raise ValueError(f"unsupported hypothesis proposals schema: {version!r}")


def load_business_policy(path: Path) -> BusinessRetestPolicy:
    return BusinessRetestPolicy.model_validate_json(path.read_text(encoding="utf-8"))


def load_o1_collection_plans(
    path: Path, *, environment_digest: str
) -> list[ComponentCollectionPlan]:
    """Load live O1/O2 collection plans, bound to the current environment.

    A plan declared on another environment is an identity mismatch, not a
    portable capability: refuse instead of silently collecting on the wrong
    machine (S0 discipline applied to the collection side).
    """

    plans = TypeAdapter(list[ComponentCollectionPlan]).validate_json(
        path.read_text(encoding="utf-8")
    )
    mismatched = sorted(
        plan.component for plan in plans if plan.environment_digest != environment_digest
    )
    if mismatched:
        raise ValueError(
            f"collection plans bound to a different environment for components "
            f"{mismatched}; redeclare plans on this machine"
        )
    return plans


def load_workload_contract(layout: SessionLayout) -> WorkloadContract:
    return parse_workload_contract_yaml(layout.workload_contract.read_text("utf-8"))


# ---------------------------------------------------------------------------
# 业务复测核心（干预与复验共用）
# ---------------------------------------------------------------------------


def build_business_batch_identity(contract: WorkloadContract, phase_id: str) -> dict[str, str]:
    """Identity fields every business batch shares (S0 comparability scope)."""

    return {
        "load.identity_digest": contract.load_command.identity_digest,
        "workload_contract_digest": contract.digest,
        "phase_id": phase_id,
    }


def build_business_metric_contract(
    contract: WorkloadContract, policy: BusinessRetestPolicy
) -> MetricContract:
    spec = next((m for m in contract.o0_metrics if m.metric_id == policy.business_metric_id), None)
    if spec is None:
        raise ValueError(
            f"business metric '{policy.business_metric_id}' is not declared by the "
            "workload contract"
        )
    direction = (
        MetricDirection.MAXIMIZE
        if spec.direction is O0MetricDirection.MAXIMIZE
        else MetricDirection.MINIMIZE
    )
    return MetricContract(
        id=spec.metric_id,
        role=MetricRole.BUSINESS_PRIMARY,
        component="workload",
        direction=direction,
        unit=spec.unit,
        scope=spec.source,
        phase=policy.phase_id,
        aggregation=spec.aggregation,
        minimum_samples=policy.minimum_samples,
        scale=policy.scale,
        minimum_effect=policy.minimum_effect,
        pressure_method=PressureMethod.NONE,
        source=f"external-load-o0:{spec.source}",
    )


class BusinessRetestPlanner:
    """Read retest windows into a batch and judge them against the frozen baseline.

    A retest is a *group* of ``retest_window_count`` window directories; each
    window contributes one aggregated business value, so a group reaches
    ``minimum_samples`` without pretending a single window has a distribution.
    """

    def __init__(
        self,
        *,
        contract: WorkloadContract,
        policy: BusinessRetestPolicy,
        baseline_batch: MeasurementBatch,
        layout: SessionLayout,
    ) -> None:
        self._contract = contract
        self._policy = policy
        self._baseline = baseline_batch
        self._layout = layout
        self._metric_contract = build_business_metric_contract(contract, policy)
        self._statistics = StatisticsPolicy(
            confidence_level=policy.confidence_level,
            bootstrap_resamples=policy.bootstrap_resamples,
            random_seed=policy.random_seed,
            baseline_repeats=policy.minimum_samples,
            candidate_repeats=policy.retest_window_count,
            baseline_every_n=1,
        )

    @property
    def business_metric_id(self) -> str:
        return self._policy.business_metric_id

    def retest_window_ids(self, group_id: str) -> list[str]:
        return [
            f"{group_id}-run{index}" for index in range(1, self._policy.retest_window_count + 1)
        ]

    def read_window_batch(self, window_ids: Sequence[str]) -> MeasurementBatch:
        values: list[float] = []
        for window_id in window_ids:
            window = self._layout.window(window_id)
            identity_path = _wait_for_file(
                window / "identity.json",
                self._policy.window_poll_seconds,
                self._policy.window_wait_timeout_seconds,
            )
            identity = LoadCommandIdentity.model_validate(
                json.loads(identity_path.read_text(encoding="utf-8"))
            )
            if identity.identity_digest != self._contract.load_command.identity_digest:
                raise RetestIdentityDrift(
                    f"retest window '{window_id}' ran a different load than the "
                    "workload contract declares"
                )
            raw = _wait_for_file(
                window / "o0.txt",
                self._policy.window_poll_seconds,
                self._policy.window_wait_timeout_seconds,
            ).read_text(encoding="utf-8")
            parsed = parse_o0_metrics(
                self._contract.load_command.tool,
                [self._policy.business_metric_id],
                raw,
            )
            window_values = parsed.get(self._policy.business_metric_id)
            if not window_values:
                raise ValueError(f"window '{window_id}' output carries no business metric values")
            values.append(aggregate(window_values, self._metric_contract.aggregation))
        batch = MeasurementBatch(
            identity=build_business_batch_identity(self._contract, self._policy.phase_id),
            metrics={
                self._policy.business_metric_id: MetricEvidence(
                    metric_id=self._policy.business_metric_id,
                    values=values,
                )
            },
            gate_values={},
        )
        return batch

    def judge(self, batch: MeasurementBatch) -> ImprovementEvidence:
        candidate = batch.metrics[self._policy.business_metric_id]
        baseline = self._baseline.metrics[self._policy.business_metric_id]
        comparable_fields = sorted(set(batch.identity) & set(self._baseline.identity))
        if batch.identity != self._baseline.identity:
            differing = [
                field
                for field in comparable_fields
                if batch.identity[field] != self._baseline.identity[field]
            ]
            raise RetestIdentityDrift(
                f"retest batch identity differs from the frozen baseline on {differing}"
            )
        return bootstrap_improvement(candidate, baseline, self._metric_contract, self._statistics)


# ---------------------------------------------------------------------------
# L1 安全干预与复验源
# ---------------------------------------------------------------------------


class SafetyBackedIntervention:
    """``intervention`` adapter: apply via the L1 stack, judge on the business retest.

    Accepted experiments keep the configuration applied (verification windows
    must measure under it); rejected experiments restore the pre-apply snapshot
    values through the same safety path. Apply/verify/keep failures decline the
    window (``None``) with the safety result preserved under ``control/``; a
    failed *restoration* raises instead — leaving a machine modified after a
    rejected hypothesis is a stop-the-phase safety event.
    """

    def __init__(
        self,
        *,
        controller: SafetyController,
        manifest: ConfigManifest,
        backend: ExecutorBackend,
        fencing_token: int,
        proposals: Mapping[str, HypothesisProposal],
        planner: BusinessRetestPlanner,
        layout: SessionLayout,
    ) -> None:
        self._controller = controller
        self._manifest = manifest
        self._backend = backend
        self._fencing_token = fencing_token
        self._proposals = dict(proposals)
        self._planner = planner
        self._layout = layout

    def _write_control(self, name: str, payload: Mapping[str, Any]) -> Path:
        path = self._layout.control / name
        _atomic_write_json(path, payload)
        return path

    def __call__(self, hypothesis: ComponentHypothesis) -> InterventionExperiment | None:
        proposal = self._proposals.get(hypothesis.hypothesis_id)
        if proposal is None:
            self._write_control(
                f"intervention-failure-{hypothesis.hypothesis_id}.json",
                {"hypothesis_id": hypothesis.hypothesis_id, "reason": "unknown-hypothesis"},
            )
            return None

        applied = self._controller.execute(
            self._manifest,
            proposal.change,
            self._backend,
            fencing_token=self._fencing_token,
            keep=True,
            keep_authorized=True,
        )
        if applied.state is not SafetyState.KEPT or applied.snapshot is None:
            self._write_control(
                f"intervention-failure-{hypothesis.hypothesis_id}.json",
                {
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "reason": "apply-or-keep-failed",
                    "safety_result": applied.model_dump(mode="json"),
                },
            )
            return None

        retest_ids = self._planner.retest_window_ids(f"retest-{hypothesis.hypothesis_id}")
        self._write_control(
            f"retest-request-{hypothesis.hypothesis_id}.json",
            {
                "hypothesis_id": hypothesis.hypothesis_id,
                "change": proposal.change,
                "window_ids": retest_ids,
            },
        )
        batch = self._planner.read_window_batch(retest_ids)
        evidence = self._planner.judge(batch)

        if not evidence.accepted:
            restoration_values: dict[str, Any] = {}
            for parameter_id in proposal.change:
                item = self._manifest.item_for_parameter(parameter_id)
                entry = applied.snapshot.entries.get(item.id)
                if entry is None or entry.value is None:
                    raise DynamicInterventionError(
                        f"pre-apply snapshot lacks a value for '{parameter_id}'; "
                        "cannot restore after rejection"
                    )
                restoration_values[parameter_id] = entry.value
            restored = self._controller.execute(
                self._manifest,
                restoration_values,
                self._backend,
                fencing_token=self._fencing_token,
                keep=True,
                keep_authorized=True,
            )
            if restored.state is not SafetyState.KEPT:
                raise DynamicInterventionError(
                    f"candidate-level restoration failed for hypothesis "
                    f"'{hypothesis.hypothesis_id}'; machine may be left modified "
                    f"(safety reason: {restored.reason})"
                )

        return InterventionExperiment(
            measurement_batch_digest=batch.digest,
            business_metric_id=self._planner.business_metric_id,
            accepted=evidence.accepted,
            business_lcb=evidence.lower,
        )


class TwoStageSafetyBackedIntervention:
    """Prepare without writes, then execute with durable candidate/recovery logs."""

    def __init__(
        self,
        *,
        controller: SafetyController,
        manifest: ConfigManifest,
        backend: ExecutorBackend,
        fencing_token: int,
        proposals: Mapping[str, HypothesisProposalV2],
        planner: BusinessRetestPlanner,
        layout: SessionLayout,
        receipt_store: DurableReceiptStore,
        attention_sink: AttentionSink,
    ) -> None:
        self._controller = controller
        self._manifest = manifest
        self._backend = backend
        self._fencing_token = fencing_token
        self._proposals = dict(proposals)
        self._planner = planner
        self._layout = layout
        self._receipt_store = receipt_store
        self._attention_sink = attention_sink

    def prepare_intervention(self, hypothesis: ComponentHypothesis) -> InterventionPlan:
        proposal = self._proposals.get(hypothesis.hypothesis_id)
        if proposal is None:
            raise DynamicInterventionError(f"unknown hypothesis '{hypothesis.hypothesis_id}'")
        items = {
            self._manifest.item_for_parameter(parameter_id).id: self._manifest.item_for_parameter(
                parameter_id
            )
            for parameter_id in proposal.change
        }
        return InterventionPlan(
            hypothesis=hypothesis,
            change=dict(proposal.change),
            risk=proposal.risk,
            risk_source=RiskSource(
                schema_version=INTERVENTION_RISK_SOURCE_SCHEMA,
                kind=proposal.risk_kind,
                manifest_digest=self._manifest.digest,
                items=[
                    RiskSourceItem(item_id=item_id, risk=items[item_id].risk)
                    for item_id in sorted(items)
                ],
                rationale=proposal.risk_rationale,
            ),
        )

    @staticmethod
    def _progress_flags(stages: set[SafetyProgressStage]) -> dict[str, bool]:
        apply_started = SafetyProgressStage.APPLY_STARTED in stages
        return {
            "write_attempted": apply_started,
            "apply_started": apply_started,
            "rollback_attempted": SafetyProgressStage.ROLLBACK_STARTED in stages,
            "rollback_verified": SafetyProgressStage.ROLLBACK_VERIFIED in stages,
        }

    def _observer(self, current: list[Any], stages: set[SafetyProgressStage]):
        def observe(event: SafetyProgressEvent) -> None:
            stages.add(event.stage)
            fields: dict[str, Any] = {"safety_state": event.safety_state}
            if event.stage is SafetyProgressStage.SAFETY_TERMINAL:
                fields["evidence_digest"] = event.evidence_digest
            current[0] = self._receipt_store.advance(
                current[0], receipt_stage_for(event.stage), **fields
            )

        return observe

    def _attention(self, reason: str, evidence_digest: str) -> None:
        self._attention_sink(reason[:2000], evidence_digest)

    def _run_observed(
        self,
        *,
        plan: InterventionPlan,
        values: Mapping[str, Any],
        execution_id: str,
        operation: ReceiptOperation,
        parent_receipt_digest: str | None = None,
    ) -> tuple[ObservedSafetyResult, Any, set[SafetyProgressStage]]:
        current = [
            self._receipt_store.start(
                plan=plan,
                execution_id=execution_id,
                operation=operation,
                parent_receipt_digest=parent_receipt_digest,
            )
        ]
        stages: set[SafetyProgressStage] = set()
        try:
            observed = self._controller.execute_observed(
                self._manifest,
                values,
                self._backend,
                fencing_token=self._fencing_token,
                progress_observer=self._observer(current, stages),
                keep=True,
                keep_authorized=True,
            )
        except ProgressRecordError as error:
            raise DynamicInterventionError(
                f"receipt persistence failed before backend apply: {error}",
                plan_digest=plan.digest,
                candidate_receipt_digest=(
                    current[0].digest
                    if operation is ReceiptOperation.CANDIDATE
                    else parent_receipt_digest
                ),
                recovery_receipt_digest=(
                    current[0].digest if operation is ReceiptOperation.RECOVERY else None
                ),
                triggered_field="intervention.receipt",
            ) from error
        return observed, current[0], stages

    def _restoration_values(
        self, plan: InterventionPlan, observed: ObservedSafetyResult
    ) -> dict[str, Any]:
        snapshot = observed.result.snapshot
        if snapshot is None:
            raise DynamicInterventionError(
                "candidate safety result has no baseline snapshot for recovery",
                plan_digest=plan.digest,
                triggered_field="intervention.recovery",
            )
        values: dict[str, Any] = {}
        for parameter_id in plan.change:
            item = self._manifest.item_for_parameter(parameter_id)
            entry = snapshot.entries.get(item.id)
            if entry is None or entry.value is None:
                raise DynamicInterventionError(
                    f"pre-apply snapshot lacks a value for '{parameter_id}'",
                    plan_digest=plan.digest,
                    triggered_field="intervention.recovery",
                )
            values[parameter_id] = entry.value
        return values

    def _operation_terminal(
        self,
        current: Any,
        outcome: InterventionOutcome | None,
        observed: ObservedSafetyResult,
    ) -> Any:
        fields: dict[str, Any] = {
            "safety_state": outcome.safety_state if outcome else observed.result.state,
            "evidence_digest": (outcome.evidence_digest if outcome else observed.result.digest),
        }
        if outcome is not None:
            fields["outcome"] = outcome
        return self._receipt_store.advance(current, ReceiptStageV2.OPERATION_TERMINAL, **fields)

    def execute_intervention(
        self, plan: InterventionPlan, execution_id: str
    ) -> DynamicInterventionExecution:
        candidate, candidate_head, candidate_stages = self._run_observed(
            plan=plan,
            values=plan.change,
            execution_id=execution_id,
            operation=ReceiptOperation.CANDIDATE,
        )
        flags = self._progress_flags(candidate_stages)
        base_outcome = {
            "plan_digest": plan.digest,
            **flags,
            "experiment": None,
            "safety_state": candidate.result.state,
            "evidence_digest": candidate.result.digest,
        }
        if candidate.progress_failures:
            outcome = InterventionOutcome(**base_outcome)
            self._attention(
                "candidate receipt progress failed after apply; reconciliation required",
                candidate.digest,
            )
            raise DynamicInterventionError(
                "candidate receipt progress failed after backend apply",
                plan_digest=plan.digest,
                outcome=outcome,
                candidate_receipt_digest=candidate_head.digest,
                triggered_field="intervention.receipt",
            )

        if candidate.result.state is not SafetyState.KEPT:
            outcome = InterventionOutcome(**base_outcome)
            try:
                terminal = self._operation_terminal(candidate_head, outcome, candidate)
            except Exception as error:
                if outcome.apply_started:
                    self._attention(
                        "candidate operation-terminal receipt failed", candidate_head.digest
                    )
                raise DynamicInterventionError(
                    f"candidate operation-terminal receipt failed: {error}",
                    plan_digest=plan.digest,
                    outcome=outcome,
                    candidate_receipt_digest=candidate_head.digest,
                    triggered_field="intervention.receipt",
                ) from error
            if candidate.result.state is SafetyState.NEEDS_ATTENTION:
                self._attention(
                    candidate.result.reason or "candidate execution needs attention",
                    terminal.digest,
                )
            return DynamicInterventionExecution(
                plan_digest=plan.digest,
                outcome=outcome,
                candidate_receipt_digest=terminal.digest,
            )

        retest_ids = self._planner.retest_window_ids(f"retest-{plan.hypothesis.hypothesis_id}")
        _atomic_write_json(
            self._layout.control / f"retest-request-{plan.hypothesis.hypothesis_id}.json",
            {
                "hypothesis_id": plan.hypothesis.hypothesis_id,
                "change": plan.change,
                "window_ids": retest_ids,
            },
        )
        retest_error: Exception | None = None
        try:
            batch = self._planner.read_window_batch(retest_ids)
            evidence = self._planner.judge(batch)
        except Exception as error:
            retest_error = error
            batch = None
            evidence = None
        experiment = (
            InterventionExperiment(
                measurement_batch_digest=batch.digest,
                business_metric_id=self._planner.business_metric_id,
                accepted=evidence.accepted,
                business_lcb=evidence.lower,
            )
            if batch is not None and evidence is not None
            else None
        )
        if experiment is not None and experiment.accepted:
            outcome = InterventionOutcome(
                plan_digest=plan.digest,
                **flags,
                experiment=experiment,
                safety_state=candidate.result.state,
                evidence_digest=batch.digest,
            )
            try:
                terminal = self._operation_terminal(candidate_head, outcome, candidate)
            except Exception as error:
                self._attention(
                    "candidate operation-terminal receipt failed after apply",
                    candidate_head.digest,
                )
                raise DynamicInterventionError(
                    f"candidate operation-terminal receipt failed: {error}",
                    plan_digest=plan.digest,
                    outcome=outcome,
                    candidate_receipt_digest=candidate_head.digest,
                    triggered_field="intervention.receipt",
                ) from error
            return DynamicInterventionExecution(
                plan_digest=plan.digest,
                outcome=outcome,
                candidate_receipt_digest=terminal.digest,
            )

        restoration_values = self._restoration_values(plan, candidate)
        try:
            recovery, recovery_head, _ = self._run_observed(
                plan=plan,
                values=restoration_values,
                execution_id=execution_id,
                operation=ReceiptOperation.RECOVERY,
                parent_receipt_digest=candidate_head.digest,
            )
        except DynamicInterventionError as error:
            self._attention(
                "recovery receipt failed after candidate apply",
                error.recovery_receipt_digest or candidate_head.digest,
            )
            raise
        except Exception as error:
            self._attention(
                "recovery receipt could not start after candidate apply",
                candidate_head.digest,
            )
            raise DynamicInterventionError(
                f"recovery receipt could not start: {error}",
                plan_digest=plan.digest,
                outcome=InterventionOutcome(**base_outcome),
                candidate_receipt_digest=candidate_head.digest,
                triggered_field="intervention.receipt",
            ) from error
        if recovery.progress_failures:
            self._attention(
                "recovery receipt progress failed; reconciliation required",
                recovery.digest,
            )
            raise DynamicInterventionError(
                "recovery receipt progress failed",
                plan_digest=plan.digest,
                outcome=InterventionOutcome(
                    plan_digest=plan.digest,
                    **flags,
                    experiment=experiment,
                    safety_state=recovery.result.state,
                    evidence_digest=recovery.result.digest,
                ),
                candidate_receipt_digest=candidate_head.digest,
                recovery_receipt_digest=recovery_head.digest,
                triggered_field="intervention.receipt",
            )
        outcome = InterventionOutcome(
            plan_digest=plan.digest,
            **flags,
            experiment=experiment,
            safety_state=recovery.result.state,
            evidence_digest=recovery.result.digest,
        )
        try:
            recovery_terminal = self._operation_terminal(recovery_head, None, recovery)
        except Exception as error:
            self._attention("recovery operation-terminal receipt failed", recovery_head.digest)
            raise DynamicInterventionError(
                f"recovery operation-terminal receipt failed: {error}",
                plan_digest=plan.digest,
                outcome=outcome,
                candidate_receipt_digest=candidate_head.digest,
                recovery_receipt_digest=recovery_head.digest,
                triggered_field="intervention.receipt",
            ) from error
        try:
            candidate_terminal = self._operation_terminal(candidate_head, outcome, candidate)
        except Exception as error:
            self._attention(
                "candidate operation-terminal receipt failed after recovery",
                recovery_terminal.digest,
            )
            raise DynamicInterventionError(
                f"candidate operation-terminal receipt failed after recovery: {error}",
                plan_digest=plan.digest,
                outcome=outcome,
                candidate_receipt_digest=candidate_head.digest,
                recovery_receipt_digest=recovery_terminal.digest,
                triggered_field="intervention.receipt",
            ) from error
        if retest_error is not None:
            self._attention(
                "business retest failed after candidate apply; candidate was restored",
                recovery_terminal.digest,
            )
            raise DynamicInterventionError(
                f"business retest failed after candidate apply: {retest_error}",
                plan_digest=plan.digest,
                outcome=outcome,
                candidate_receipt_digest=candidate_terminal.digest,
                recovery_receipt_digest=recovery_terminal.digest,
                triggered_field="intervention.execution",
            ) from retest_error
        if recovery.result.state is not SafetyState.KEPT:
            self._attention(
                recovery.result.reason or "candidate recovery failed",
                recovery_terminal.digest,
            )
        return DynamicInterventionExecution(
            plan_digest=plan.digest,
            outcome=outcome,
            candidate_receipt_digest=candidate_terminal.digest,
            recovery_receipt_digest=recovery_terminal.digest,
        )


class FileRetestSource:
    """``retest`` adapter: judge one verification window group against the baseline."""

    def __init__(self, planner: BusinessRetestPlanner, layout: SessionLayout) -> None:
        self._planner = planner
        self._layout = layout

    def __call__(self, verify_id: str) -> RetestOutcome:
        window_ids = self._planner.retest_window_ids(verify_id)
        control = self._layout.control / f"retest-request-{verify_id}.json"
        _atomic_write_json(
            control,
            {"verify_id": verify_id, "window_ids": window_ids},
        )
        batch = self._planner.read_window_batch(window_ids)
        evidence = self._planner.judge(batch)
        return RetestOutcome(
            improvement=evidence,
            measurement_batch_digest=batch.digest,
        )


__all__ = [
    "BUSINESS_POLICY_SCHEMA",
    "BusinessRetestPolicy",
    "BusinessRetestPlanner",
    "DynamicInterventionError",
    "DynamicInterventionExecution",
    "FileHypothesisProposals",
    "FileHypothesisProposalsV2",
    "FileLoadIdentity",
    "FileO0Source",
    "FileRetestSource",
    "HYPOTHESIS_PROPOSALS_SCHEMA",
    "HYPOTHESIS_PROPOSALS_V2_SCHEMA",
    "HypothesisProposal",
    "HypothesisProposalV2",
    "HypothesisProposalsFile",
    "HypothesisProposalsFileV2",
    "RetestIdentityDrift",
    "SafetyBackedIntervention",
    "TwoStageSafetyBackedIntervention",
    "SessionFileMissing",
    "SessionLayout",
    "build_business_batch_identity",
    "build_business_metric_contract",
    "load_business_policy",
    "load_hypothesis_proposals",
    "load_hypothesis_proposals_versioned",
    "load_o1_collection_plans",
    "load_workload_contract",
]
