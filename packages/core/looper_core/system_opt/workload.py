"""M3 workload 合同：业务负载替身的显式声明（workload-tuning.md D0/D6 第一项）。

边界（SO-D020）：动态相位的负载由测试/操作侧**外部提供**——本合同只声明
负载身份与观察口径，**绝不包含可由引擎执行的负载命令**。引擎持有
``load_command`` 身份仅为观察窗核对；实际 argv 留在测试侧台账。

合同职责（只声明，不执行）：
1. O0 业务指标口径：读外部负载自身产物（如 stress-ng 的 bogo-ops），
   引擎只解析产物，不启动进程；
2. 业务目标（primary + scale + MDE，任务注入，无默认）；
3. SLO 与正确性门禁声明（复验窗先过正确性/安全/SLO 再谈收益）；
4. 显式阶段声明（自动阶段识别是 open decision，不默认）；
5. 负载身份：工具 + argv 摘要 + 声明时长（摘要由测试侧用
   :func:`load_argv_digest` 从真实 argv 计算，双方可复现比对）。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import Aggregation, StrictModel

WORKLOAD_CONTRACT_SCHEMA = "looper.workload-contract/v1alpha1"


class WorkloadContractError(ValueError):
    """Raised when a workload contract cannot be parsed or validated."""


class O0MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class BoundComparator(StrEnum):
    AT_LEAST = "at-least"
    AT_MOST = "at-most"
    EXACTLY = "exactly"


class O0MetricSpec(StrictModel):
    """One business metric read from the external load's own output."""

    metric_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9._-]*$")
    unit: str = Field(min_length=1, max_length=80)
    direction: O0MetricDirection
    aggregation: Aggregation
    source: str = Field(min_length=1, max_length=300)


class LoadCommandIdentity(StrictModel):
    """Identity of the externally provided load; never executable by the engine.

    The test harness computes ``argv_digest`` from the argv it actually runs
    via :func:`load_argv_digest`; the raw argv stays in the test-side ledger.
    """

    tool: str = Field(min_length=1, max_length=80)
    argv_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    declared_duration_seconds: float = Field(gt=0)
    description: str = Field(min_length=1, max_length=500)

    @property
    def identity_digest(self) -> str:
        """Identity over tool + argv digest + declared duration only.

        ``description`` is prose, not identity: two windows may word the same
        load differently without becoming different loads.
        """

        return canonical_digest(
            {
                "tool": self.tool,
                "argv_digest": self.argv_digest,
                "declared_duration_seconds": self.declared_duration_seconds,
            }
        )


class WorkloadObjective(StrictModel):
    """The business objective for the dynamic phase (task-injected, no defaults)."""

    primary_metric_id: str = Field(min_length=1, max_length=160)
    scale: float = Field(gt=0)
    mde: float = Field(ge=0)


class SLOStatement(StrictModel):
    metric_id: str = Field(min_length=1, max_length=160)
    comparator: BoundComparator
    bound: float
    unit: str = Field(min_length=1, max_length=80)


class CorrectnessGate(StrictModel):
    """Non-compensable correctness bound checked before any benefit judgment."""

    metric_id: str = Field(min_length=1, max_length=160)
    comparator: BoundComparator
    bound: float
    unit: str = Field(min_length=1, max_length=80)


class WorkloadPhaseSpec(StrictModel):
    """One explicitly declared workload phase (auto phase detection is open)."""

    phase_id: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=300)
    declared_duration_seconds: float | None = Field(default=None, gt=0)
    o0_metric_ids: list[str] = Field(min_length=1)


class WorkloadContract(StrictModel):
    schema_version: Literal[WORKLOAD_CONTRACT_SCHEMA] = WORKLOAD_CONTRACT_SCHEMA
    workload_id: str = Field(min_length=1, max_length=160)
    # SO-D020: external-test is the only provider; the engine never starts loads.
    load_provider: Literal["external-test"] = "external-test"
    load_command: LoadCommandIdentity
    o0_metrics: list[O0MetricSpec] = Field(min_length=1)
    objective: WorkloadObjective
    slos: list[SLOStatement] = Field(default_factory=list)
    correctness_gates: list[CorrectnessGate] = Field(min_length=1)
    phases: list[WorkloadPhaseSpec] = Field(min_length=1)
    limitations: str = Field(min_length=1, max_length=1000)

    @field_validator("o0_metrics", "phases")
    @classmethod
    def reject_empty(cls, values: list[Any]) -> list[Any]:
        if not values:
            raise ValueError("workload contract lists must not be empty")
        return values

    @model_validator(mode="after")
    def validate_references(self) -> WorkloadContract:
        metric_ids = [metric.metric_id for metric in self.o0_metrics]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("o0_metrics metric_id values must be unique")
        known = set(metric_ids)
        if self.objective.primary_metric_id not in known:
            raise ValueError(
                f"objective primary '{self.objective.primary_metric_id}' "
                "is not a declared o0 metric"
            )
        for statement in self.slos:
            if statement.metric_id not in known:
                raise ValueError(f"SLO metric '{statement.metric_id}' is not a declared o0 metric")
        for gate in self.correctness_gates:
            if gate.metric_id not in known:
                raise ValueError(
                    f"correctness gate metric '{gate.metric_id}' is not a declared o0 metric"
                )
        phase_ids = [phase.phase_id for phase in self.phases]
        if len(phase_ids) != len(set(phase_ids)):
            raise ValueError("phases phase_id values must be unique")
        for phase in self.phases:
            undeclared = sorted(set(phase.o0_metric_ids) - known)
            if undeclared:
                raise ValueError(
                    f"phase '{phase.phase_id}' references undeclared o0 metrics: {undeclared}"
                )
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def load_argv_digest(argv: Sequence[str]) -> str:
    """Deterministic digest of the load argv, reproducible on the test side."""

    if not argv:
        raise WorkloadContractError("load argv must not be empty")
    return canonical_digest({"argv": [str(item) for item in argv]})


def same_load(left: LoadCommandIdentity, right: LoadCommandIdentity) -> bool:
    """Exact load-identity comparison for observation windows (v1: no tolerance).

    Feature-level drift tolerance (reactivation case A) is a future contract;
    v1 compares digests exactly and treats any mismatch as an identity event.
    """

    return left.identity_digest == right.identity_digest


def parse_workload_contract_yaml(content: str) -> WorkloadContract:
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise WorkloadContractError("workload contract YAML is invalid") from error
    if not isinstance(payload, dict):
        raise WorkloadContractError("workload contract YAML must contain one object")
    try:
        return WorkloadContract.model_validate(payload)
    except ValueError as error:
        raise WorkloadContractError(str(error)) from error


__all__ = [
    "BoundComparator",
    "CorrectnessGate",
    "LoadCommandIdentity",
    "O0MetricDirection",
    "O0MetricSpec",
    "SLOStatement",
    "WORKLOAD_CONTRACT_SCHEMA",
    "WorkloadContract",
    "WorkloadContractError",
    "WorkloadObjective",
    "WorkloadPhaseSpec",
    "load_argv_digest",
    "parse_workload_contract_yaml",
    "same_load",
]
