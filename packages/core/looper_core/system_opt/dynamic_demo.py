"""动态相位模拟演示会话构建器（Windows 可跑，无任何真实系统写入）。

一次性把 ``dynamic-run --backend simulated`` 需要的整套会话资产写进一个目录：
workload/gate/promotion 合同、业务复测策略、冻结基线、竞争假设提案、
预产出的外部负载窗口（模拟 SO-D020 的测试侧 runner 已完成的行为）。

演示剧本（数值全部是合成的，仅用于证明动态闭环机制）：

    基线 bogo-ops/s ≈ 391（SLO 下界 420 持续违反）
    → window-1 症状登记（两个竞争假设：cpu governor / vm.swappiness）
    → window-2 假设 A（governor→performance）O2 取证推进 probing
    → window-3 干预 A：L1 施加 → 业务复测 LCB≈0.49 > MDE=0.08 → 接受
       → 两个复验窗组 → S9 晋升；兄弟假设 B superseded
    → window-4/5 SLO 连续 2 窗达标 → 目标类停止
    → 相位收尾：L1 把 governor 写回 powersave（机器回到起点）

演示用数值与 M2/M3 任何真实会话无关，不可作为性能结论引用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from looper_core.canonical import canonical_digest
from looper_core.contracts import Aggregation
from looper_core.system_opt.collector import (
    CollectedMetric,
    ComponentMetricSnapshot,
    MetricAvailability,
)
from looper_core.system_opt.config_manifest import ConfigComponent, ConfigManifest
from looper_core.system_opt.demo import (
    build_demo_manifest,
    build_demo_policy,
    build_workload_reference,
)
from looper_core.system_opt.dynamic_adapters import (
    BusinessRetestPolicy,
    HypothesisProposal,
    HypothesisProposalsFile,
    HypothesisProposalsFileV2,
    HypothesisProposalV2,
    SessionLayout,
    build_business_batch_identity,
)
from looper_core.system_opt.negative_cache import (
    HYPOTHESIS_SEMANTICS_VERSION,
    HypothesisCacheRetentionPolicy,
)
from looper_core.system_opt.online_routing import OnlineRoutingContract
from looper_core.system_opt.phase_gate import (
    BoundComparator,
    ConvergencePolicy,
    DegradationGate,
    DynamicPhaseGateContract,
    DynamicPhaseGateContractV2,
    PhaseBudget,
    SloTarget,
)
from looper_core.system_opt.policy import OptimizationMode
from looper_core.system_opt.result_vector import PromotionContract
from looper_core.system_opt.scoring import MeasurementBatch, MetricEvidence
from looper_core.system_opt.state_evidence import (
    STATE_EVIDENCE_SCHEMA,
    ConfigStateRecord,
    ConfigurationStateEvidence,
    OwnershipDisposition,
    PersistenceDisposition,
    StateSource,
)
from looper_core.system_opt.workload import (
    BoundComparator as WorkloadBoundComparator,
)
from looper_core.system_opt.workload import (
    CorrectnessGate,
    LoadCommandIdentity,
    O0MetricDirection,
    O0MetricSpec,
    SLOStatement,
    WorkloadContract,
    WorkloadObjective,
    WorkloadPhaseSpec,
    load_argv_digest,
)

DEMO_ARGV = [
    "stress-ng",
    "--cpu",
    "2",
    "--timeout",
    "120s",
    "--yaml",
    "--metrics-brief",
]
BUSINESS_METRIC = "stress-ng.bogo-ops-per-second-usr-sys-time"
TOTAL_OPS_METRIC = "stress-ng.bogo-ops"
PHASE_ID = "demo-steady"

# 合成时间线（bogo-ops/s）：外部负载在干预前 ~391，干预后 ~441。
BASELINE_VALUES = [390.5, 392.1, 391.3, 389.8, 391.7]
WINDOW_VALUES = {
    "window-1": 390.7,
    "window-2": 391.5,
    "window-3": 390.2,
    "window-4": 440.8,
    "window-5": 441.2,
    "window-6": 441.0,
}
RETEST_HYP_A = [441.2, 439.8, 440.5, 442.0, 440.9]
VERIFY_1 = [439.6, 441.0, 440.2, 441.8, 440.5]
VERIFY_2 = [440.9, 442.1, 441.3, 439.9, 441.6]

SLO_BOUND = 420.0


def _load_identity() -> LoadCommandIdentity:
    return LoadCommandIdentity(
        tool="stress-ng",
        argv_digest=load_argv_digest(DEMO_ARGV),
        declared_duration_seconds=120.0,
        description=(
            "simulated demo external CPU load; argv is held by the simulated "
            "test side and never executed by the engine"
        ),
    )


def build_demo_workload_contract() -> WorkloadContract:
    return WorkloadContract(
        workload_id="demo-dynamic-cpu-stress-v1",
        load_command=_load_identity(),
        o0_metrics=[
            O0MetricSpec(
                metric_id=BUSINESS_METRIC,
                unit="bogo-ops/s",
                direction=O0MetricDirection.MAXIMIZE,
                aggregation=Aggregation.MEAN,
                source="stress-ng --yaml metrics[].bogo-ops-per-second-usr-sys-time",
            ),
            O0MetricSpec(
                metric_id=TOTAL_OPS_METRIC,
                unit="ops",
                direction=O0MetricDirection.MAXIMIZE,
                aggregation=Aggregation.MEAN,
                source="stress-ng --yaml metrics[].bogo-ops",
            ),
        ],
        objective=WorkloadObjective(primary_metric_id=BUSINESS_METRIC, scale=100.0, mde=0.08),
        slos=[
            SLOStatement(
                metric_id=BUSINESS_METRIC,
                comparator=WorkloadBoundComparator.AT_LEAST,
                bound=SLO_BOUND,
                unit="bogo-ops/s",
            )
        ],
        correctness_gates=[
            CorrectnessGate(
                metric_id=TOTAL_OPS_METRIC,
                comparator=WorkloadBoundComparator.AT_LEAST,
                bound=1,
                unit="ops",
            )
        ],
        phases=[
            WorkloadPhaseSpec(
                phase_id=PHASE_ID,
                purpose="simulated steady CPU load; full O0 observation per window",
                declared_duration_seconds=120.0,
                o0_metric_ids=[BUSINESS_METRIC, TOTAL_OPS_METRIC],
            )
        ],
        limitations=(
            "fully synthetic demo session (values invented to exercise the loop "
            "machinery); not a Linux performance statement; conclusions hold "
            "only for the declared tool identity and this simulated environment"
        ),
    )


def build_demo_gate_contract(workload_digest: str) -> DynamicPhaseGateContract:
    return DynamicPhaseGateContract(
        workload_contract_digest=workload_digest,
        slo=SloTarget(
            metric_id=BUSINESS_METRIC,
            comparator=BoundComparator.AT_LEAST,
            bound=SLO_BOUND,
            hold_windows=2,
        ),
        convergence=ConvergencePolicy(rounds=2, lcb_threshold=0.05),
        budget=PhaseBudget(
            max_interventions=4, wall_clock_seconds=3600.0, risk_quota=0
        ),
        degradation=DegradationGate(
            metric_id=BUSINESS_METRIC, relative_limit=0.10
        ),
        reactivation_holdout_windows=3,
    )


def build_demo_business_policy() -> BusinessRetestPolicy:
    return BusinessRetestPolicy(
        business_metric_id=BUSINESS_METRIC,
        phase_id=PHASE_ID,
        scale=100.0,
        minimum_effect=0.08,
        minimum_samples=5,
        confidence_level=0.95,
        bootstrap_resamples=2000,
        random_seed=7,
        retest_window_count=5,
        window_wait_timeout_seconds=5.0,
        window_poll_seconds=0.05,
    )


def build_demo_proposals() -> HypothesisProposalsFile:
    return HypothesisProposalsFile(
        proposals=[
            HypothesisProposal(
                hypothesis_id="hyp-governor-performance",
                component=ConfigComponent.CPU,
                rank=1,
                rationale=(
                    "simulated: SLO miss under sustained CPU load with the "
                    "powersave governor; performance raises sustained throughput"
                ),
                change={"system.cpu-governor": "performance"},
            ),
            HypothesisProposal(
                hypothesis_id="hyp-swappiness-10",
                component=ConfigComponent.MEMORY,
                rank=2,
                rationale=(
                    "simulated competing attribution: lower swap aggression "
                    "might reduce reclaim stalls under the same load"
                ),
                change={"system.vm-swappiness": 10},
            ),
        ]
    )


def _o0_yaml(bogo_ops_per_second: float) -> str:
    return (
        "metrics:\n"
        "- stressor: cpu\n"
        f"  bogo-ops: {int(bogo_ops_per_second * 120)}\n"
        f"  bogo-ops-per-second-usr-sys-time: {bogo_ops_per_second}\n"
    )


def _write_window(
    layout: SessionLayout, window_id: str, identity: LoadCommandIdentity, value: float
) -> None:
    window = layout.window(window_id)
    window.mkdir(parents=True, exist_ok=True)
    (window / "identity.json").write_text(
        identity.model_dump_json(indent=2), encoding="utf-8"
    )
    (window / "o0.txt").write_text(_o0_yaml(value), encoding="utf-8")


def build_dynamic_demo_session(root: Path) -> SessionLayout:
    """Materialize the full simulated demo session under ``root``."""

    layout = SessionLayout(root)
    root.mkdir(parents=True, exist_ok=True)
    contract = build_demo_workload_contract()
    identity = contract.load_command

    layout.workload_contract.write_text(
        yaml.safe_dump(contract.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    layout.gate_contract.write_text(
        build_demo_gate_contract(contract.digest).model_dump_json(indent=2),
        encoding="utf-8",
    )
    layout.promotion_contract.write_text(
        PromotionContract(
            min_observations=2, min_distinct_time_blocks=2, min_environments=1
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    layout.business_policy.write_text(
        build_demo_business_policy().model_dump_json(indent=2), encoding="utf-8"
    )
    baseline = MeasurementBatch(
        identity=build_business_batch_identity(contract, PHASE_ID),
        metrics={
            BUSINESS_METRIC: MetricEvidence(
                metric_id=BUSINESS_METRIC, values=list(BASELINE_VALUES)
            )
        },
        gate_values={},
    )
    layout.baseline_batch.write_text(
        baseline.model_dump_json(indent=2), encoding="utf-8"
    )
    layout.hypothesis_proposals.write_text(
        yaml.safe_dump(
            build_demo_proposals().model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    for window_id, value in WINDOW_VALUES.items():
        _write_window(layout, window_id, identity, value)
    for index, value in enumerate(RETEST_HYP_A, start=1):
        _write_window(layout, f"retest-hyp-governor-performance-run{index}", identity, value)
    for index, value in enumerate(VERIFY_1, start=1):
        _write_window(layout, f"verify-window-3-1-run{index}", identity, value)
    for index, value in enumerate(VERIFY_2, start=1):
        _write_window(layout, f"verify-window-3-2-run{index}", identity, value)

    return layout


def build_m3_demo_session(root: Path, *, environment_digest: str) -> SessionLayout:
    """Build the durable v2 demo plus online O1 routing/cache/profile assets."""

    layout = build_dynamic_demo_session(root)
    contract = build_demo_workload_contract()
    gate_v1 = build_demo_gate_contract(contract.digest)
    gate_v2 = DynamicPhaseGateContractV2.model_validate(
        {
            **gate_v1.model_dump(mode="json"),
            "schema_version": "looper.dynamic-phase-gate/v1alpha2",
        }
    )
    layout.gate_contract.write_text(gate_v2.model_dump_json(indent=2), encoding="utf-8")

    proposals_v2 = HypothesisProposalsFileV2(
        proposals=[
            HypothesisProposalV2(
                hypothesis_id=proposal.hypothesis_id,
                component=proposal.component,
                rank=proposal.rank,
                rationale=proposal.rationale,
                change=dict(proposal.change),
                supporting_digests=list(proposal.supporting_digests),
                risk="low",
                risk_kind="manifest-derived",
            )
            for proposal in build_demo_proposals().proposals
        ]
    )
    layout.hypothesis_proposals.write_text(
        yaml.safe_dump(
            proposals_v2.model_dump(mode="json"), allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )

    policy = build_demo_policy(OptimizationMode.WORKLOAD)
    layout.online_routing_policy.write_text(
        yaml.safe_dump(policy.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    reference = build_workload_reference(policy).model_copy(
        update={
            "identity": {
                "target": "demo-dynamic-target",
                "workload": contract.digest,
                "phase": "steady-state",
                "load_state": "loaded",
                "tool": "looper-m3-simulated-o1/v1",
                "statistics": "explicit-policy",
            }
        }
    )
    layout.diagnostic_reference.write_text(
        reference.model_dump_json(indent=2), encoding="utf-8"
    )
    routing_contract = OnlineRoutingContract(
        target_id="demo-dynamic-target",
        environment_digest=environment_digest,
        measurement_identity=dict(reference.identity),
        pressure_protocol_digest=reference.pressure_protocol_digest,
        formula_versions={
            "F-PROJECT-S4-PIECEWISE-LINEAR": "v1alpha1",
            "F-PROJECT-S6-S7": "v1alpha1",
        },
        symptom_class_digest=canonical_digest(
            {
                "kind": "business-slo-below-bound",
                "metric_id": BUSINESS_METRIC,
                "phase_id": PHASE_ID,
            }
        ),
        hypothesis_semantics_version=HYPOTHESIS_SEMANTICS_VERSION,
    )
    layout.online_routing_contract.write_text(
        routing_contract.model_dump_json(indent=2), encoding="utf-8"
    )
    retention = HypothesisCacheRetentionPolicy(
        policy_id="m3-demo-explicit-identity-retention",
        mode="identity-change-only",
        expires_at=None,
    )
    layout.hypothesis_cache_retention.write_text(
        retention.model_dump_json(indent=2), encoding="utf-8"
    )

    component_values = {
        "cpu": {"cpu.utilization": 0.95},
        "memory": {"memory.psi-some": 0.07},
        "storage": {"storage.io-latency": 9.0},
        "network": {"network.retransmits": 8.0},
    }
    snapshots: list[ComponentMetricSnapshot] = []
    for sample_index in range(policy.statistics.baseline_repeats):
        for component, metrics in component_values.items():
            snapshots.append(
                ComponentMetricSnapshot(
                    component=component,
                    target_id="demo-dynamic-target",
                    environment_digest=environment_digest,
                    collected_at=datetime(2026, 8, 24, tzinfo=UTC)
                    + timedelta(seconds=sample_index),
                    metrics={
                        name: CollectedMetric(
                            name=name,
                            unit="synthetic",
                            source="m3-demo-session",
                            availability=MetricAvailability.READABLE,
                            value=value,
                        )
                        for name, value in metrics.items()
                    },
                    counting_basis="one synthetic O1 sample for online-routing demo only",
                )
            )
    layout.online_o1_snapshots.write_text(
        "[\n"
        + ",\n".join(snapshot.model_dump_json(indent=2) for snapshot in snapshots)
        + "\n]\n",
        encoding="utf-8",
    )

    general_baseline = MeasurementBatch(
        identity=build_business_batch_identity(contract, PHASE_ID),
        metrics={
            BUSINESS_METRIC: MetricEvidence(
                metric_id=BUSINESS_METRIC,
                values=[399.5, 400.2, 399.8, 400.5, 400.0],
            )
        },
        gate_values={},
    )
    layout.general_profile_baseline.write_text(
        general_baseline.model_dump_json(indent=2), encoding="utf-8"
    )
    return layout


def build_demo_initial_state() -> dict[str, object]:
    """SimulatedBackend initial state: item id -> starting value."""

    return {
        "cpu-governor": "powersave",
        "vm-swappiness": 60,
        "net-somaxconn": 128,
    }


def build_demo_state_evidence(
    manifest: ConfigManifest, *, environment_digest: str
) -> ConfigurationStateEvidence:
    """Build explicit synthetic ownership evidence for the simulated target only."""

    locator = "demo://m3-simulated-target"
    source = StateSource(
        kind="user-declaration",
        locator=locator,
        content_sha256=hashlib.sha256(locator.encode()).hexdigest(),
        line=1,
        raw_value=None,
    )
    return ConfigurationStateEvidence(
        schema_version=STATE_EVIDENCE_SCHEMA,
        target_id="demo-dynamic-target",
        manifest_digest=manifest.digest,
        environment_digest=environment_digest,
        collected_at=datetime(2026, 8, 24, tzinfo=UTC),
        source_scope=[locator],
        assignments=[],
        records=[
            ConfigStateRecord(
                item_id=item.id,
                parameter_id=item.parameter_id,
                persistence=PersistenceDisposition.UNKNOWN,
                persistent_value=None,
                ownership=OwnershipDisposition.UNOWNED,
                owner_id=None,
                pinned=False,
                sources=[source],
                reason="synthetic demo: no external writer exists in SimulatedBackend",
            )
            for item in manifest.items
        ],
        counting_basis="one synthetic UNOWNED record per demo manifest item",
    )


def build_m3_demo_workspace(
    root: Path, *, environment_digest: str
) -> dict[str, Path]:
    """Materialize every input needed by the one-command simulated M3 demo.

    Existing non-empty directories are refused so a demo cannot overwrite or
    be mistaken for previously collected evidence.
    """

    if root.exists() and any(root.iterdir()):
        raise ValueError("M3 demo workspace must be absent or empty")
    root.mkdir(parents=True, exist_ok=True)
    session = root / "session"
    build_m3_demo_session(session, environment_digest=environment_digest)
    manifest = build_demo_manifest()
    manifest_path = root / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            manifest.model_dump(mode="json"), allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )
    state_evidence_path = root / "state-evidence.json"
    state_evidence_path.write_text(
        build_demo_state_evidence(
            manifest, environment_digest=environment_digest
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    initial_state_path = root / "initial-state.json"
    initial_state_path.write_text(
        json.dumps(build_demo_initial_state(), indent=2), encoding="utf-8"
    )
    return {
        "session": session,
        "manifest": manifest_path,
        "state_evidence": state_evidence_path,
        "initial_state": initial_state_path,
        "output": root / "dynamic-run.json",
        "lease_root": root / "leases",
        "hypothesis_cache": session / "control" / "hypothesis-negative-cache.jsonl",
        "retention_policy": session / "hypothesis-cache-retention.json",
    }


__all__ = [
    "build_demo_business_policy",
    "build_demo_gate_contract",
    "build_demo_initial_state",
    "build_demo_proposals",
    "build_demo_state_evidence",
    "build_demo_workload_contract",
    "build_dynamic_demo_session",
    "build_m3_demo_session",
    "build_m3_demo_workspace",
]
