from __future__ import annotations

from datetime import UTC, datetime

import pytest

from looper_core.system_opt.collector import (
    CollectedMetric,
    ComponentMetricSnapshot,
    MetricAvailability,
    collect_component_snapshot,
)

ENV = "sha256:" + "a" * 64
FIXED_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestAvailabilityContract:
    def test_readable_metric_requires_finite_value(self):
        with pytest.raises(ValueError, match="finite value"):
            CollectedMetric(
                name="m.ratio",
                unit="ratio",
                value=None,
                availability=MetricAvailability.READABLE,
                source="/proc/x",
            )
        with pytest.raises(ValueError, match="finite value"):
            CollectedMetric(
                name="m.ratio",
                unit="ratio",
                value=float("nan"),
                availability=MetricAvailability.READABLE,
                source="/proc/x",
            )

    def test_readable_metric_rejects_unavailable_reason(self):
        with pytest.raises(ValueError, match="unavailable reason"):
            CollectedMetric(
                name="m.ratio",
                unit="ratio",
                value=1.0,
                availability=MetricAvailability.READABLE,
                unavailable_reason="stale",
                source="/proc/x",
            )

    def test_unavailable_metric_requires_reason_and_null_value(self):
        with pytest.raises(ValueError, match="null"):
            CollectedMetric(
                name="m.ratio",
                unit="ratio",
                value=0.0,
                availability=MetricAvailability.UNAVAILABLE,
                unavailable_reason="hidden in guest",
                source="/sys/x",
            )
        with pytest.raises(ValueError, match="explicit reason"):
            CollectedMetric(
                name="m.ratio",
                unit="ratio",
                value=None,
                availability=MetricAvailability.UNAVAILABLE,
                source="/sys/x",
            )


class TestCpuCollection:
    def test_pmu_absent_marks_counters_unavailable(self, tmp_path):
        snapshot = collect_component_snapshot(
            "cpu",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            interval_seconds=0.01,
            collected_at=FIXED_AT,
        )
        pmu = snapshot.metrics["cpu.pmu-event-sources"]
        assert pmu.availability is MetricAvailability.UNAVAILABLE
        assert pmu.value is None
        assert "PMU not passed through" in pmu.unavailable_reason

    def test_pmu_present_is_readable(self, tmp_path):
        _write(tmp_path / "proc" / "stat", "cpu  100 0 100 700 0 0 0 0\n")
        devices = tmp_path / "sys" / "bus" / "event_source" / "devices"
        devices.mkdir(parents=True)
        (devices / "cpu").mkdir()
        snapshot = collect_component_snapshot(
            "cpu",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            interval_seconds=0.01,
            collected_at=FIXED_AT,
        )
        assert snapshot.metrics["cpu.pmu-event-sources"].value == 1.0

    def test_static_stat_marks_busy_ratio_unavailable_fail_closed(self, tmp_path):
        _write(tmp_path / "proc" / "stat", "cpu  100 0 100 700 0 0 0 0\n")
        snapshot = collect_component_snapshot(
            "cpu",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            interval_seconds=0.01,
            collected_at=FIXED_AT,
        )
        busy = snapshot.metrics["cpu.busy-ratio"]
        assert busy.availability is MetricAvailability.UNAVAILABLE
        assert "tick delta" in busy.unavailable_reason


class TestOtherComponents:
    def test_memory_available_ratio(self, tmp_path):
        _write(
            tmp_path / "proc" / "meminfo",
            "MemTotal:       1000000 kB\nMemAvailable:    250000 kB\n",
        )
        snapshot = collect_component_snapshot(
            "memory",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            collected_at=FIXED_AT,
        )
        assert snapshot.metrics["memory.available-ratio"].value == pytest.approx(0.25)

    def test_memory_missing_meminfo_is_unavailable_not_crash(self, tmp_path):
        snapshot = collect_component_snapshot(
            "memory",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            collected_at=FIXED_AT,
        )
        metric = snapshot.metrics["memory.available-ratio"]
        assert metric.availability is MetricAvailability.UNAVAILABLE
        assert "unreadable" in metric.unavailable_reason

    def test_network_counters_exclude_loopback(self, tmp_path):
        _write(
            tmp_path / "proc" / "net" / "dev",
            "Inter-|   Receive                                                |  Transmit\n"
            " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets\n"
            "  lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\n"
            "  eth0: 1000 0 0 0 0 0 0 0 40 0 0 0 0 0 0 0\n",
        )
        snapshot = collect_component_snapshot(
            "network",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            collected_at=FIXED_AT,
        )
        assert snapshot.metrics["network.rx-bytes-total"].value == 1000
        assert snapshot.metrics["network.tx-bytes-total"].value == 40

    def test_storage_counters_exclude_loop_and_ram_devices(self, tmp_path):
        _write(
            tmp_path / "proc" / "diskstats",
            " 259       0 nvme0n1 100 0 800 10 50 0 400 20 0 30 0 0 0\n"
            "   7       0 loop0 1 0 1 1 1 0 1 1 0 1 0 0 0\n",
        )
        snapshot = collect_component_snapshot(
            "storage",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            collected_at=FIXED_AT,
        )
        assert snapshot.metrics["storage.reads-completed-total"].value == 100
        assert snapshot.metrics["storage.writes-completed-total"].value == 50

    def test_numa_node_count_and_single_node_binding_unavailable(self, tmp_path):
        node = tmp_path / "sys" / "devices" / "system" / "node" / "node0"
        node.mkdir(parents=True)
        snapshot = collect_component_snapshot(
            "numa",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            collected_at=FIXED_AT,
        )
        assert snapshot.metrics["numa.node-count"].value == 1
        binding = snapshot.metrics["numa.binding"]
        assert binding.availability is MetricAvailability.UNAVAILABLE
        assert "single-node" in binding.unavailable_reason


class TestSnapshotContract:
    def test_digest_is_stable_for_identical_input(self, tmp_path):
        _write(
            tmp_path / "proc" / "meminfo",
            "MemTotal: 100 kB\nMemAvailable: 50 kB\n",
        )
        kwargs = dict(
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            collected_at=FIXED_AT,
        )
        first = collect_component_snapshot("memory", **kwargs)
        second = collect_component_snapshot("memory", **kwargs)
        assert first.digest == second.digest
        assert ComponentMetricSnapshot.model_validate_json(
            first.model_dump_json()
        ).digest == first.digest

    def test_unsupported_component_rejected(self):
        with pytest.raises(ValueError, match="unsupported component"):
            collect_component_snapshot(
                "gpu", target_id="t", environment_digest=ENV
            )
