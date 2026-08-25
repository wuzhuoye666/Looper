"""D4-R4 动态证据回放验证器测试：persist → replay 往返 + 负向 fail-closed。

关键语义：
- O1 overhead 是集合级墙钟证据：相同 collector_id 的多组件共享同一 overhead digest，
  replay 必须成功；组件交换负测仅在两组件 collector_id 不同时成立。
- O2 probe↔overhead 精确一对一：两个 probe 共享同一 overhead digest 必须拒绝。
- 非法 digest（路径穿越/反斜杠/非 64 hex/大写 hex）与非法 kind 在读取证据文件之前
  即被拒绝，不能让路径逃出 control_dir。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from looper_core.system_opt.collector import (
    BuiltinLinuxGuestCollector,
    CollectedMetric,
    CollectionOverheadABEvidence,
    ComponentCollectionPlan,
    ComponentCollectionScope,
    ComponentMetricSnapshot,
    MetricAvailability,
)
from looper_core.system_opt.dynamic_collection import (
    O2ComponentProbeEvidence,
    collection_evidence_filename,
    o1_live_source,
    o2_component_probe,
    persist_dynamic_collection_evidence,
)
from looper_core.system_opt.dynamic_replay import (
    DynamicEvidenceVerificationError,
    verify_dynamic_collection_evidence,
)
from looper_core.system_opt.hypothesis import ComponentHypothesis
from looper_core.system_opt.observation import O0Observation, ObservationWindow
from looper_core.system_opt.workload import LoadCommandIdentity

ENVIRONMENT = "sha256:" + "e" * 64
WORKLOAD = "sha256:" + "c" * 64
AT = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)
INDEX_SCHEMA = "looper.dynamic-collection-evidence-index/v1alpha1"


def _plan(component: str, collector_id: str | None = None) -> ComponentCollectionPlan:
    metrics = {
        "cpu": ["cpu.utilization"],
        "memory": ["memory.used-bytes"],
    }
    return ComponentCollectionPlan(
        component=component,
        target_id="fixture-target",
        environment_digest=ENVIRONMENT,
        workload_phase_id="steady",
        workload_source="external fixture load",
        collector_id=collector_id or f"fixture.{component}",
        requested_metrics=metrics[component],
        interval_seconds=0.1,
        scope=ComponentCollectionScope(),
    )


class _Session:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.closed = False

    def finish(self, request):
        self.closed = True
        metric_name = self.plan.requested_metrics[0]
        return ComponentMetricSnapshot(
            component=self.plan.component,
            target_id=self.plan.target_id,
            environment_digest=self.plan.environment_digest,
            collected_at=AT,
            metrics={
                metric_name: CollectedMetric(
                    name=metric_name,
                    unit="ratio",
                    value=0.5,
                    availability=MetricAvailability.READABLE,
                    source="fixture live counter",
                )
            },
            counting_basis="fixture exact live window",
        )

    def cancel(self) -> None:
        self.closed = True


class _Collector:
    collector_version = "1.0.0"

    def __init__(self, component: str, collector_id: str | None = None) -> None:
        self.collector_id = collector_id or f"fixture.{component}"

    def begin_collection(self, plan):
        return _Session(plan)


def _observation_window() -> ObservationWindow:
    return ObservationWindow(
        window_id="window-1",
        phase_id="steady",
        workload_contract_digest=WORKLOAD,
        load_command=LoadCommandIdentity(
            tool="fixture-load",
            argv_digest="sha256:" + "a" * 64,
            declared_duration_seconds=10,
            description="external fixture load",
        ),
        o0=[
            O0Observation(
                metric_id="workload.rate",
                values=[1.0],
                raw_output_digest="sha256:" + "b" * 64,
            )
        ],
        o1=[],
        started_at=AT,
        finished_at=AT,
    )


def _hypothesis(component: str = "memory") -> ComponentHypothesis:
    return ComponentHypothesis(
        hypothesis_id=f"hyp-{component}",
        symptom_id="symptom-1",
        component=component,
        rank=1,
    )


def _build_persisted(control: Path):
    """Build O1 (cpu+memory, distinct collector ids) + O2 (memory) and persist."""

    o1 = o1_live_source(
        plans=[_plan("cpu"), _plan("memory")],
        collectors={"cpu": _Collector("cpu"), "memory": _Collector("memory")},
        window_seconds=0.5,
        sleep_fn=lambda _: None,
        wall_clock=lambda: AT,
        monotonic=iter([1.0, 1.5, 2.0, 2.75]).__next__,
    )
    o2 = o2_component_probe(
        plans=[_plan("memory")],
        collectors={"memory": _Collector("memory")},
        window_seconds=0.25,
        sleep_fn=lambda _: None,
        wall_clock=lambda: AT,
        monotonic=iter([5.0, 5.25, 6.0, 6.5]).__next__,
    )
    assert o1 is not None and o2 is not None
    o1("window-1")
    o2(_hypothesis("memory"), _observation_window())
    return persist_dynamic_collection_evidence(control, o1_source=o1, o2_probe=o2)


def _write_index(control: Path, index: dict) -> None:
    (control / "dynamic-collection-evidence-index.json").write_text(
        json.dumps(index), encoding="utf-8"
    )


def _load_index(control: Path) -> dict:
    return json.loads(
        (control / "dynamic-collection-evidence-index.json").read_text(encoding="utf-8")
    )


def test_replay_verifies_persisted_evidence(tmp_path: Path) -> None:
    control = tmp_path / "control"
    index = _build_persisted(control)

    verification = verify_dynamic_collection_evidence(control)

    assert verification.index == index
    assert set(verification.o1_runs_by_window) == {"window-1"}
    assert {run.request.component for run in verification.o1_runs_by_window["window-1"]} == {
        "cpu",
        "memory",
    }
    probe = verification.o2_probe_by_digest[index.o2_probe_evidence_digests[0]]
    assert probe.hypothesis.component.value == "memory"
    assert probe.collection_overhead_evidence_digest in verification.o2_overhead_by_digest


def test_shared_collector_id_overhead_digest_replays(tmp_path: Path) -> None:
    """O1 集合级：同 collector_id 的多组件共享同一 O1 overhead digest，replay 成功。"""

    control = tmp_path / "control"
    shared = BuiltinLinuxGuestCollector.collector_id  # "looper.builtin-linux-guest"
    o1 = o1_live_source(
        plans=[_plan("cpu", shared), _plan("memory", shared)],
        collectors={"cpu": _Collector("cpu", shared), "memory": _Collector("memory", shared)},
        window_seconds=0.5,
        sleep_fn=lambda _: None,
        wall_clock=lambda: AT,
        monotonic=iter([1.0, 1.5, 2.0, 2.75]).__next__,
    )
    assert o1 is not None
    o1("window-1")

    index = persist_dynamic_collection_evidence(control, o1_source=o1)

    bindings = index.o1_overhead_digests_by_window["window-1"]
    assert bindings["cpu"] == bindings["memory"]  # 集合级：共享同一 overhead digest
    assert len(index.o1_runs_by_window["window-1"]) == 2

    verification = verify_dynamic_collection_evidence(control)
    assert verification.index == index
    assert len(verification.o1_overhead_by_digest) == 1


# ---------------------------------------------------------------------------
# 非法 digest / 非法 kind：在读取证据文件之前拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_digest",
    [
        "sha256:../../../outside",  # 路径穿越
        "sha256:..\\..\\outside",  # 反斜杠
        "sha256:abc",  # 非 64 hex
        "sha256:" + "g" * 64,  # 非法 hex 字符
        "sha256:" + "A" * 64,  # 大写 hex
    ],
)
def test_illegal_index_digest_rejected_before_file_read(tmp_path: Path, bad_digest: str) -> None:
    control = tmp_path / "control"
    control.mkdir()
    # 只写索引，不写任何证据文件：非法 digest 必须在加载阶段被拒，而非读到文件才报错。
    (control / "dynamic-collection-evidence-index.json").write_text(
        json.dumps(
            {
                "schema_version": INDEX_SCHEMA,
                "o1_runs_by_window": {},
                "o1_overhead_digests_by_window": {},
                "o2_probe_evidence_digests": [bad_digest],
                "o2_overhead_evidence_digests": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DynamicEvidenceVerificationError, match="invalid index"):
        verify_dynamic_collection_evidence(control)
    # 目录里除索引外没有任何证据文件被创建/读取。
    assert {p.name for p in control.iterdir()} == {"dynamic-collection-evidence-index.json"}


@pytest.mark.parametrize(
    "bad_kind",
    ["../o1-collection-run", "o1-collection-run/../x", "foo", "o1-collection-run.json"],
)
def test_collection_evidence_filename_rejects_illegal_kind(bad_kind: str) -> None:
    with pytest.raises(ValueError, match="unknown collection evidence kind"):
        collection_evidence_filename(bad_kind, "sha256:" + "a" * 64)


@pytest.mark.parametrize(
    "bad_digest",
    [
        "sha256:../../../outside",
        "sha256:..\\..\\outside",
        "sha256:abc",
        "sha256:" + "g" * 64,
        "sha256:" + "A" * 64,
    ],
)
def test_collection_evidence_filename_rejects_illegal_digest(bad_digest: str) -> None:
    with pytest.raises(ValueError, match="sha256-shaped"):
        collection_evidence_filename("o1-collection-run", bad_digest)


# ---------------------------------------------------------------------------
# 缺文件 / 孤儿 / 畸形 / 篡改
# ---------------------------------------------------------------------------


def test_missing_index_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _build_persisted(control)
    (control / "dynamic-collection-evidence-index.json").unlink()
    with pytest.raises(DynamicEvidenceVerificationError, match="missing index file"):
        verify_dynamic_collection_evidence(control)


def test_missing_evidence_file_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control"
    index = _build_persisted(control)
    run_digest = index.o1_runs_by_window["window-1"][0]
    (control / collection_evidence_filename("o1-collection-run", run_digest)).unlink()
    with pytest.raises(DynamicEvidenceVerificationError, match="missing evidence file"):
        verify_dynamic_collection_evidence(control)


def test_orphan_evidence_file_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _build_persisted(control)
    (control / f"o2-probe-evidence-{'f' * 64}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DynamicEvidenceVerificationError, match="orphan evidence file"):
        verify_dynamic_collection_evidence(control)


def test_malformed_evidence_file_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _build_persisted(control)
    (control / "o1-collection-run-not-a-digest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DynamicEvidenceVerificationError, match="malformed evidence file"):
        verify_dynamic_collection_evidence(control)


def test_tampered_content_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control"
    index = _build_persisted(control)
    run_digest = index.o1_runs_by_window["window-1"][0]
    run_path = control / collection_evidence_filename("o1-collection-run", run_digest)
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["snapshot"]["metrics"]["cpu.utilization"]["value"] = 0.9
    run_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DynamicEvidenceVerificationError, match="digest mismatch"):
        verify_dynamic_collection_evidence(control)


# ---------------------------------------------------------------------------
# 负向：forged window / 交换组件 overhead / 重复 / 悬空 / 身份 / 共享 O2 overhead
# ---------------------------------------------------------------------------


def test_forged_window_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _build_persisted(control)
    index = _load_index(control)
    index["o1_runs_by_window"] = {"window-9": index["o1_runs_by_window"]["window-1"]}
    index["o1_overhead_digests_by_window"] = {
        "window-9": index["o1_overhead_digests_by_window"]["window-1"]
    }
    _write_index(control, index)
    with pytest.raises(DynamicEvidenceVerificationError, match="window_id does not match"):
        verify_dynamic_collection_evidence(control)


def test_swapped_component_overhead_fails_closed(tmp_path: Path) -> None:
    # 仅在两组件 collector_id 不同（fixture.cpu / fixture.memory）时交换可被检出。
    control = tmp_path / "control"
    _build_persisted(control)
    index = _load_index(control)
    bindings = index["o1_overhead_digests_by_window"]["window-1"]
    bindings["cpu"], bindings["memory"] = bindings["memory"], bindings["cpu"]
    _write_index(control, index)
    with pytest.raises(DynamicEvidenceVerificationError, match="collector_id"):
        verify_dynamic_collection_evidence(control)


def test_duplicate_digest_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _build_persisted(control)
    index = _load_index(control)
    index["o2_probe_evidence_digests"].append(index["o2_probe_evidence_digests"][0])
    _write_index(control, index)
    with pytest.raises(DynamicEvidenceVerificationError, match="duplicate O2 probe"):
        verify_dynamic_collection_evidence(control)


def test_dangling_reference_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _build_persisted(control)
    index = _load_index(control)
    index["o2_overhead_evidence_digests"] = []
    _write_index(control, index)
    with pytest.raises(DynamicEvidenceVerificationError, match="dangling overhead digest"):
        verify_dynamic_collection_evidence(control)


def test_run_overhead_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control"
    index = _build_persisted(control)
    cpu_overhead_digest = index.o1_overhead_digests_by_window["window-1"]["cpu"]
    old_path = control / collection_evidence_filename("o1-overhead-evidence", cpu_overhead_digest)
    payload = json.loads(old_path.read_text(encoding="utf-8"))
    payload["target_id"] = "forged-target"
    retampered = CollectionOverheadABEvidence.model_validate(payload)
    new_digest = retampered.digest
    old_path.unlink()
    (control / collection_evidence_filename("o1-overhead-evidence", new_digest)).write_text(
        json.dumps(retampered.model_dump(mode="json", exclude_none=False)), encoding="utf-8"
    )
    index = _load_index(control)
    index["o1_overhead_digests_by_window"]["window-1"]["cpu"] = new_digest
    _write_index(control, index)

    with pytest.raises(DynamicEvidenceVerificationError, match="target_id does not match"):
        verify_dynamic_collection_evidence(control)


def test_shared_o2_overhead_reference_fails_closed(tmp_path: Path) -> None:
    """两个不同 O2 probe 共同引用同一 O2 overhead digest → 必须拒绝。"""

    control = tmp_path / "control"
    index = _build_persisted(control)
    probe_digest = index.o2_probe_evidence_digests[0]
    probe_path = control / collection_evidence_filename("o2-probe-evidence", probe_digest)
    payload = json.loads(probe_path.read_text(encoding="utf-8"))
    payload["collection_run"]["snapshot"]["metrics"]["memory.used-bytes"]["value"] = 0.7
    copied = O2ComponentProbeEvidence.model_validate(payload)
    copied_digest = copied.digest
    assert copied_digest != probe_digest
    assert (
        copied.collection_overhead_evidence_digest
        == payload["collection_overhead_evidence_digest"]
    )
    (control / collection_evidence_filename("o2-probe-evidence", copied_digest)).write_text(
        json.dumps(copied.model_dump(mode="json", exclude_none=False)), encoding="utf-8"
    )
    index = _load_index(control)
    index["o2_probe_evidence_digests"].append(copied_digest)
    _write_index(control, index)

    with pytest.raises(DynamicEvidenceVerificationError, match="shares overhead digest"):
        verify_dynamic_collection_evidence(control)
