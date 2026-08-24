"""D3：外部负载会话 runner ↔ 引擎文件适配器的端到端对齐验证。

证明两侧对 ``contracts/dynamic-session-files.md`` 的理解完全一致，走完整握手：

1. runner（``external_load_session.py``，注入 fake load 输出 stress-ng yaml）在
   临时会话目录生产观察窗（``windows/<id>/o0.txt`` + ``identity.json``）；
2. 引擎侧 ``FileLoadIdentity``/``FileO0Source`` 逐窗读回并核对身份；
3. 症状登记后，``SafetyBackedIntervention`` 写 ``control/retest-request-*.json``，
   runner 轮询补窗，``BusinessRetestPlanner`` 读复测组并 S6/S7 裁决；
4. ``FileRetestSource`` 复验窗组 → S9 晋升。

窗口文件由 runner 产出（不是 ``dynamic_demo`` 的 builder），否则就不是两侧对齐验证。
"""

from __future__ import annotations

import importlib.util
import threading
from datetime import UTC, datetime
from pathlib import Path

import yaml
from looper_core.system_opt.demo import build_demo_manifest
from looper_core.system_opt.dynamic_adapters import (
    BusinessRetestPlanner,
    FileHypothesisProposals,
    FileLoadIdentity,
    FileO0Source,
    FileRetestSource,
    SafetyBackedIntervention,
    SessionLayout,
    build_business_batch_identity,
    load_business_policy,
    load_hypothesis_proposals,
)
from looper_core.system_opt.dynamic_demo import (
    BASELINE_VALUES,
    BUSINESS_METRIC,
    DEMO_ARGV,
    PHASE_ID,
    build_demo_business_policy,
    build_demo_gate_contract,
    build_demo_initial_state,
    build_demo_proposals,
    build_demo_workload_contract,
)
from looper_core.system_opt.dynamic_loop import run_dynamic_phase
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.result_vector import PromotionContract
from looper_core.system_opt.safety import SafetyController, SafetyPolicy
from looper_core.system_opt.scoring import MeasurementBatch, MetricEvidence

ENV = "sha256:" + "a" * 64
OBSERVATION_WINDOWS = 3
RETEST_RATE = 441.0  # 高于 SLO 420 且相对基线 ~391 的改善 LCB > MDE 0.08
OBSERVE_RATE = 390.0  # 低于 SLO 420 -> 触发症状


def _load_runner():
    path = (
        Path(__file__).parents[1]
        / "examples"
        / "system-optimizer"
        / "external_load_session.py"
    )
    spec = importlib.util.spec_from_file_location("external_load_session", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _o0_yaml(rate: float) -> str:
    return (
        "metrics:\n"
        "- stressor: cpu\n"
        f"  bogo-ops: {int(rate * 120)}\n"
        f"  bogo-ops-per-second-usr-sys-time: {rate}\n"
    )


def _write_session_assets(root: Path) -> SessionLayout:
    """Write the engine-side session assets (no windows)."""

    layout = SessionLayout(root)
    root.mkdir(parents=True, exist_ok=True)
    contract = build_demo_workload_contract()
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
    layout.baseline_batch.write_text(baseline.model_dump_json(indent=2), encoding="utf-8")
    layout.hypothesis_proposals.write_text(
        yaml.safe_dump(
            build_demo_proposals().model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return layout


def test_runner_engine_end_to_end_handshake(tmp_path: Path) -> None:
    runner_module = _load_runner()
    layout = _write_session_assets(tmp_path / "session")

    contract = build_demo_workload_contract()
    policy = load_business_policy(layout.business_policy)
    baseline = MeasurementBatch.model_validate_json(
        layout.baseline_batch.read_text(encoding="utf-8")
    )
    proposals = load_hypothesis_proposals(layout.hypothesis_proposals)
    manifest = build_demo_manifest()
    backend = SimulatedBackend(build_demo_initial_state(), target_id="demo-dynamic-target")
    controller = SafetyController(
        SafetyPolicy(allow_keep=True, pinned_items=set(), ownership_unknown_items=set())
    )
    planner = BusinessRetestPlanner(
        contract=contract, policy=policy, baseline_batch=baseline, layout=layout
    )
    intervention = SafetyBackedIntervention(
        controller=controller,
        manifest=manifest,
        backend=backend,
        fencing_token=1,
        proposals=proposals.by_id(),
        planner=planner,
        layout=layout,
    )

    # 注入的 fake load：前 3 次调用（观察窗）低于 SLO，其后（复测/复验窗）高于 SLO。
    state = {"calls": 0}

    def fake_run(argv: list[str]) -> str:
        state["calls"] += 1
        rate = OBSERVE_RATE if state["calls"] <= OBSERVATION_WINDOWS else RETEST_RATE
        return _o0_yaml(rate)

    result: dict = {}

    def engine() -> None:
        try:
            result["run"] = run_dynamic_phase(
                contract=contract,
                gate_contract=build_demo_gate_contract(contract.digest),
                promotion_contract=PromotionContract(
                    min_observations=2, min_distinct_time_blocks=2, min_environments=1
                ),
                environment_digest=ENV,
                max_windows=OBSERVATION_WINDOWS,
                probe_top_k=2,
                load_identity=FileLoadIdentity(layout, policy),
                o0_source=FileO0Source(layout, policy),
                hypothesis_source=FileHypothesisProposals(proposals),
                clock=lambda: datetime.now(UTC),
                component_probe=lambda hypothesis, window: window.digest,
                intervention=intervention,
                retest=FileRetestSource(planner, layout),
                verification_window_count=2,
            )
        except Exception as error:  # 线程吞异常，落盘后由主线程重抛
            result["error"] = error

    thread = threading.Thread(target=engine)
    thread.start()

    runner_module.run_observation_windows(
        layout=layout, argv=DEMO_ARGV, window_count=OBSERVATION_WINDOWS, run=fake_run
    )
    served = runner_module.serve_retest_requests(
        layout=layout,
        argv=DEMO_ARGV,
        run=fake_run,
        poll_seconds=0.01,
        timeout_seconds=30.0,
        idle_seconds=2.0,
    )

    thread.join(timeout=60)
    if thread.is_alive():
        raise AssertionError("engine thread did not finish within the wait budget")
    if "error" in result:
        raise result["error"]
    run = result["run"]

    # 两侧合龙：症状登记 -> 干预 -> 复验 -> 晋升。
    actions = [window.action.value for window in run.windows]
    assert actions[0] == "symptom-registered"
    assert "verified" in actions

    promotion = run.promotion
    assert promotion is not None
    assert promotion.promoted is True
    assert promotion.candidate_id == "hyp-governor-performance"

    # runner 侧确实收到复测/复验请求并逐批补窗（完整握手）。
    assert any("retest-hyp-governor-performance" in item for item in served)
    assert any("verify-window-3-1" in item for item in served)
    assert any("verify-window-3-2" in item for item in served)

    # 引擎侧读到了 runner 产出的窗口文件，且写了复测/复验请求邮箱。
    assert (layout.window("retest-hyp-governor-performance-run1") / "o0.txt").is_file()
    assert (layout.window("verify-window-3-2-run1") / "o0.txt").is_file()
    assert (layout.control / "retest-request-hyp-governor-performance.json").is_file()
    assert (layout.control / "retest-request-verify-window-3-1.json").is_file()
