from __future__ import annotations

import hashlib
import random
from typing import Any

from looper_core.canonical import canonical_digest
from looper_core.contracts import Aggregation, Operator, StrictModel
from looper_core.system_opt.config_manifest import (
    ActivationMode,
    CommandTemplate,
    CompatibilitySpec,
    ConfigCategory,
    ConfigComponent,
    ConfigItem,
    ConfigManifest,
    ConfigValueType,
    ReadSpec,
    RiskLevel,
    RollbackMode,
    RollbackSpec,
    ValueDomain,
    ValueParser,
)
from looper_core.system_opt.domain import (
    AuthorizedDomain,
    DomainEvidence,
    ResolvedDomain,
    resolve_domain,
)
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.policy import (
    HardGateContract,
    IdentityPolicy,
    MetricContract,
    MetricDirection,
    MetricRole,
    OptimizationMode,
    PressureMethod,
    SafetyExecutionContract,
    SearchPolicy,
    StatisticsPolicy,
    SystemOptimizationPolicy,
)
from looper_core.system_opt.scoring import MeasurementBatch, MetricEvidence
from looper_core.system_opt.tuning import OptimizationRun, SystemOptimizationEngine


def _command(*argv: str) -> CommandTemplate:
    return CommandTemplate(argv=list(argv), timeout_seconds=5)


def _item(
    item_id: str,
    *,
    category: ConfigCategory,
    component: ConfigComponent,
    target: str,
    value_type: ConfigValueType,
    domain: ValueDomain,
    default: Any,
    choices_reader: bool = False,
) -> ConfigItem:
    parser = {
        ConfigValueType.INTEGER: ValueParser.INTEGER,
        ConfigValueType.NUMBER: ValueParser.NUMBER,
        ConfigValueType.CATEGORICAL: (
            ValueParser.BRACKET_SELECTED if choices_reader else ValueParser.RAW
        ),
        ConfigValueType.BOOLEAN: ValueParser.BOOLEAN,
    }[value_type]
    is_file = target.startswith("/")
    return ConfigItem(
        id=item_id,
        category=category,
        primary_component=component,
        related_components=[],
        target=target,
        value_type=value_type,
        domain=domain,
        default=default,
        read=ReadSpec(
            command=(
                _command("read-file", "{target}")
                if is_file
                else _command("sysctl", "-n", "{target}")
            ),
            parser=parser,
            true_values=["1"],
            false_values=["0"],
        ),
        apply=(
            _command("write-file", "{target}", "{value}")
            if is_file
            else _command("sysctl", "-w", "{target}={value}")
        ),
        rollback=RollbackSpec(mode=RollbackMode.RESTORE_SNAPSHOT),
        activation=ActivationMode.IMMEDIATE,
        risk=RiskLevel.LOW,
        risk_reason=None,
        dependencies=[],
        preconditions=[],
        compatibility=(
            CompatibilitySpec(required_paths=[target])
            if is_file
            else CompatibilitySpec(required_commands=["sysctl"])
        ),
        searchable=True,
        value_aliases={},
        description="Synthetic Linux-shaped setting for the System Optimizer E2E demo.",
        source="synthetic fixture; not target capability evidence",
    )


def build_demo_manifest() -> ConfigManifest:
    return ConfigManifest(
        id="linux-guest-synthetic-demo",
        version="1",
        description=(
            "Synthetic Linux-shaped Config Manifest used only to prove the closed-loop "
            "implementation; it is not a Linux performance recommendation."
        ),
        items=[
            _item(
                "cpu-governor",
                category=ConfigCategory.CPUFREQ,
                component=ConfigComponent.CPU,
                target="/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
                value_type=ConfigValueType.CATEGORICAL,
                domain=ValueDomain(
                    minimum=None,
                    maximum=None,
                    step=None,
                    choices=["performance", "powersave"],
                    log=False,
                ),
                default="powersave",
            ),
            _item(
                "vm-swappiness",
                category=ConfigCategory.SYSCTL,
                component=ConfigComponent.MEMORY,
                target="vm.swappiness",
                value_type=ConfigValueType.INTEGER,
                domain=ValueDomain(minimum=10, maximum=60, step=50, choices=None, log=False),
                default=60,
            ),
            _item(
                "net-somaxconn",
                category=ConfigCategory.NET,
                component=ConfigComponent.NETWORK,
                target="net.core.somaxconn",
                value_type=ConfigValueType.INTEGER,
                domain=ValueDomain(minimum=128, maximum=1152, step=1024, choices=None, log=False),
                default=128,
            ),
            _item(
                "storage-scheduler",
                category=ConfigCategory.IO,
                component=ConfigComponent.STORAGE,
                target="/sys/block/sda/queue/scheduler",
                value_type=ConfigValueType.CATEGORICAL,
                domain=ValueDomain(
                    minimum=None,
                    maximum=None,
                    step=None,
                    choices=["none", "mq-deadline"],
                    log=False,
                ),
                default="mq-deadline",
                choices_reader=True,
            ),
        ],
        metadata={"evidence_kind": "synthetic", "target_os_shape": "linux"},
    )


def _metric(
    metric_id: str,
    *,
    role: MetricRole,
    component: str,
    direction: MetricDirection,
    unit: str,
    scale: float | None,
    minimum_effect: float | None,
    pressure_method: PressureMethod = PressureMethod.NONE,
    pressure_reference: float | None = None,
) -> MetricContract:
    return MetricContract(
        id=metric_id,
        role=role,
        component=component,
        direction=direction,
        unit=unit,
        scope="synthetic single Linux guest",
        phase="steady-state",
        aggregation=Aggregation.MEDIAN,
        minimum_samples=5,
        scale=scale,
        minimum_effect=minimum_effect,
        target=None,
        lower_bound=None,
        upper_bound=None,
        pressure_method=pressure_method,
        pressure_reference=pressure_reference,
        source="synthetic E2E fixture; not empirical Linux data",
    )


def build_demo_policy(mode: OptimizationMode) -> SystemOptimizationPolicy:
    metrics = [
        _metric(
            "workload.score",
            role=MetricRole.BUSINESS_PRIMARY,
            component="workload",
            direction=MetricDirection.MAXIMIZE,
            unit="synthetic-score",
            scale=100,
            minimum_effect=0.02,
        ),
        _metric(
            "workload.latency-p99",
            role=MetricRole.BUSINESS_SECONDARY,
            component="workload",
            direction=MetricDirection.MINIMIZE,
            unit="synthetic-ms",
            scale=100,
            minimum_effect=0,
        ),
        _metric(
            "gate.correctness",
            role=MetricRole.HARD_GATE,
            component="workload",
            direction=MetricDirection.DIAGNOSTIC_ONLY,
            unit="boolean",
            scale=None,
            minimum_effect=None,
        ),
    ]
    if mode == OptimizationMode.WORKLOAD:
        metrics.extend(
            [
                _metric(
                    "cpu.utilization",
                    role=MetricRole.COMPONENT_DIAGNOSTIC,
                    component="cpu",
                    direction=MetricDirection.DIAGNOSTIC_ONLY,
                    unit="ratio",
                    scale=1,
                    minimum_effect=None,
                    pressure_method=PressureMethod.UTILIZATION,
                    pressure_reference=1,
                ),
                _metric(
                    "memory.psi-some",
                    role=MetricRole.COMPONENT_DIAGNOSTIC,
                    component="memory",
                    direction=MetricDirection.MINIMIZE,
                    unit="ratio",
                    scale=0.1,
                    minimum_effect=None,
                    pressure_method=PressureMethod.UPPER_LIMIT_EXCESS,
                    pressure_reference=0.1,
                ),
                _metric(
                    "storage.io-latency",
                    role=MetricRole.COMPONENT_DIAGNOSTIC,
                    component="storage",
                    direction=MetricDirection.MINIMIZE,
                    unit="synthetic-ms",
                    scale=10,
                    minimum_effect=None,
                    pressure_method=PressureMethod.UPPER_LIMIT_EXCESS,
                    pressure_reference=10,
                ),
                _metric(
                    "network.retransmits",
                    role=MetricRole.COMPONENT_DIAGNOSTIC,
                    component="network",
                    direction=MetricDirection.MINIMIZE,
                    unit="synthetic-count",
                    scale=10,
                    minimum_effect=None,
                    pressure_method=PressureMethod.UPPER_LIMIT_EXCESS,
                    pressure_reference=10,
                ),
            ]
        )
    return SystemOptimizationPolicy(
        schema_version="looper.system-optimization-policy/v1alpha1",
        id=f"synthetic-{mode.value}-closed-loop",
        mode=mode,
        identity=IdentityPolicy(
            required_fields=["target", "workload", "phase", "tool", "statistics"]
        ),
        statistics=StatisticsPolicy(
            confidence_level=0.95,
            bootstrap_resamples=400,
            random_seed=20260822,
            baseline_repeats=7,
            candidate_repeats=7,
        ),
        search=SearchPolicy(
            generator="grid",
            random_seed=20260822,
            max_candidates=16,
            max_attempts=32,
            wall_time_seconds=30,
            no_improvement_limit=8,
            target_improvement=0.08,
            routed_component_limit=1 if mode == OptimizationMode.WORKLOAD else None,
            tie_break_order=[
                "primary-lower",
                "primary-estimate",
                "fewer-changes",
                "candidate-id",
            ],
        ),
        safety=SafetyExecutionContract(
            max_changes=4,
            max_changes_reason=None,
            require_privileged=True,
            pinned_items=[],
            ownership_unknown_items=[],
            high_risk_waivers=[],
        ),
        metrics=metrics,
        hard_gates=[
            HardGateContract(
                id="correctness",
                metric="gate.correctness",
                operator=Operator.TRUE,
                threshold=None,
                reason="synthetic workload correctness must pass",
            )
        ],
        authorized_components=["cpu", "memory", "storage", "network"],
        metadata={
            "evidence_kind": "synthetic",
            "warning": "not a Linux performance result or recommended configuration",
        },
    )


def resolve_demo_domains(manifest: ConfigManifest) -> dict[str, ResolvedDomain]:
    result: dict[str, ResolvedDomain] = {}
    for item in manifest.items:
        digest = canonical_digest(
            {
                "kind": "synthetic-domain-evidence",
                "item": item.id,
                "domain": item.domain.model_dump(mode="json"),
            }
        )
        result[item.parameter_id] = resolve_domain(
            item,
            DomainEvidence(
                item_id=item.id,
                domain=item.domain,
                verified=True,
                source="synthetic backend capability fixture",
                evidence_digest=digest,
            ),
            AuthorizedDomain(
                item_id=item.id,
                domain=item.domain,
                reason="synthetic demo authorization; not valid for a real target",
            ),
        )
    return result


class SyntheticMeasurementAdapter:
    def __init__(self, backend: SimulatedBackend, *, mode: OptimizationMode) -> None:
        self.backend = backend
        self.mode = mode

    @staticmethod
    def _noise(state: dict[str, Any], metric: str, index: int) -> float:
        digest = hashlib.sha256(
            repr((sorted(state.items()), metric, index)).encode("utf-8")
        ).digest()
        return random.Random(int.from_bytes(digest[:8], "big")).uniform(-0.12, 0.12)

    def __call__(self, repeats: int) -> MeasurementBatch:
        state = self.backend.state()
        cpu = 12 if state["cpu-governor"] == "performance" else 0
        memory = 5 if state["vm-swappiness"] == 10 else 0
        network = 4 if state["net-somaxconn"] == 1152 else 0
        storage = 3 if state["storage-scheduler"] == "none" else 0
        total = 100 + cpu + memory + network + storage
        latency = 100 - cpu * 0.8 - memory * 0.4 - network * 0.5 - storage * 0.3
        metric_values = {
            "workload.score": [
                total + self._noise(state, "score", index) for index in range(repeats)
            ],
            "workload.latency-p99": [
                latency + self._noise(state, "latency", index) for index in range(repeats)
            ],
            "gate.correctness": [1.0 for _ in range(repeats)],
        }
        if self.mode == OptimizationMode.WORKLOAD:
            metric_values.update(
                {
                    "cpu.utilization": [0.92 - cpu * 0.01 for _ in range(repeats)],
                    "memory.psi-some": [0.14 - memory * 0.005 for _ in range(repeats)],
                    "storage.io-latency": [12 - storage * 0.2 for _ in range(repeats)],
                    "network.retransmits": [11 - network * 0.2 for _ in range(repeats)],
                }
            )
        return MeasurementBatch(
            identity={
                "target": "synthetic-linux-guest",
                "workload": f"synthetic-{self.mode.value}",
                "phase": "steady-state",
                "tool": "looper-system-opt-synthetic/v1",
                "statistics": "explicit-policy",
            },
            metrics={
                metric: MetricEvidence(metric_id=metric, values=values)
                for metric, values in metric_values.items()
            },
            gate_values={"gate.correctness": True},
        )


def build_workload_reference(policy: SystemOptimizationPolicy) -> MeasurementBatch:
    repeats = policy.statistics.baseline_repeats
    values = {
        "cpu.utilization": [0.55 for _ in range(repeats)],
        "memory.psi-some": [0.08 for _ in range(repeats)],
        "storage.io-latency": [9.5 for _ in range(repeats)],
        "network.retransmits": [9 for _ in range(repeats)],
    }
    return MeasurementBatch(
        identity={
            "target": "synthetic-linux-guest",
            "workload": "synthetic-workload",
            "phase": "steady-state",
            "tool": "looper-system-opt-synthetic/v1",
            "statistics": "explicit-policy",
        },
        metrics={
            metric: MetricEvidence(metric_id=metric, values=observations)
            for metric, observations in values.items()
        },
        gate_values={},
    )


class FullDemoResult(StrictModel):
    schema_version: str
    evidence_kind: str
    warning: str
    general: OptimizationRun
    workload: OptimizationRun


def run_full_demo() -> FullDemoResult:
    manifest = build_demo_manifest()
    initial = {item.id: item.default for item in manifest.items}
    domains = resolve_demo_domains(manifest)

    general_backend = SimulatedBackend(initial, target_id="synthetic-general")
    general_policy = build_demo_policy(OptimizationMode.GENERAL)
    general = SystemOptimizationEngine(general_policy, manifest, domains, general_backend).run(
        baseline_parameters={item.parameter_id: item.default for item in manifest.items},
        measure=SyntheticMeasurementAdapter(general_backend, mode=OptimizationMode.GENERAL),
        fencing_token=1,
    )

    workload_backend = SimulatedBackend(initial, target_id="synthetic-workload")
    workload_policy = build_demo_policy(OptimizationMode.WORKLOAD)
    workload = SystemOptimizationEngine(workload_policy, manifest, domains, workload_backend).run(
        baseline_parameters={item.parameter_id: item.default for item in manifest.items},
        measure=SyntheticMeasurementAdapter(workload_backend, mode=OptimizationMode.WORKLOAD),
        fencing_token=1,
        diagnostic_reference=build_workload_reference(workload_policy),
    )
    return FullDemoResult(
        schema_version="looper.system-optimizer-full-demo/v1alpha1",
        evidence_kind="synthetic",
        warning="This proves the closed loop only; it is not a Linux performance result.",
        general=general,
        workload=workload,
    )


__all__ = [
    "FullDemoResult",
    "SyntheticMeasurementAdapter",
    "build_demo_manifest",
    "build_demo_policy",
    "build_workload_reference",
    "resolve_demo_domains",
    "run_full_demo",
]
