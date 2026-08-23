from __future__ import annotations

from datetime import UTC, datetime

import pytest
from looper_core.system_opt.collector import (
    CollectedMetric,
    CollectionInputArtifact,
    CollectionMeasurementEnvelope,
    CollectionOverheadABEvidence,
    ComponentCollectionRequest,
    ComponentCollectionRun,
    ComponentCollectionScope,
    ComponentMetricSnapshot,
    MetricAvailability,
    bind_collection_to_measurement_batch,
    collect_component_snapshot,
    run_component_collection,
)
from looper_core.system_opt.scoring import MeasurementBatch, MetricEvidence

ENV = "sha256:" + "a" * 64
FIXED_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestAvailabilityContract:
    def test_readable_metric_requires_finite_value(self):
        with pytest.raises(ValueError, match="finite"):
            CollectedMetric(
                name="m.ratio",
                unit="ratio",
                value=None,
                availability=MetricAvailability.READABLE,
                source="/proc/x",
            )
        with pytest.raises(ValueError, match="finite"):
            CollectedMetric(
                name="m.ratio",
                unit="ratio",
                value=float("nan"),
                availability=MetricAvailability.READABLE,
                source="/proc/x",
            )

    def test_readable_metric_accepts_finite_series_and_rejects_empty_or_nonfinite(self):
        metric = CollectedMetric(
            name="m.samples",
            unit="ops/s",
            value=[1.0, 2.0, 3.0],
            availability=MetricAvailability.READABLE,
            source="artifact:tool-output",
        )
        assert metric.value == [1.0, 2.0, 3.0]
        for invalid in ([], [1.0, float("nan")]):
            with pytest.raises(ValueError, match="finite scalar or non-empty series"):
                CollectedMetric(
                    name="m.samples",
                    unit="ops/s",
                    value=invalid,
                    availability=MetricAvailability.READABLE,
                    source="artifact:tool-output",
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
        assert "canonical CPU PMU" in pmu.unavailable_reason

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

    def test_network_scope_is_explicit_and_can_include_loopback(self, tmp_path):
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
            network_interfaces=["lo"],
            sleep_fn=lambda _: None,
        )
        assert snapshot.metrics["network.rx-bytes-total"].value == 999
        assert snapshot.metrics["network.tx-bytes-total"].value == 999
        assert snapshot.metrics["network.rx-bytes-delta"].value == 0

    def test_storage_scope_uses_exact_device_only(self, tmp_path):
        _write(
            tmp_path / "proc" / "diskstats",
            " 259       0 nvme0n1 100 0 800 10 50 0 400 20 0 30 0 0 0\n"
            " 259       1 nvme0n1p1 90 0 700 9 40 0 300 19 0 20 0 0 0\n"
            "   7       0 loop0 1 0 1 1 1 0 1 1 0 1 0 0 0\n",
        )
        snapshot = collect_component_snapshot(
            "storage",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            collected_at=FIXED_AT,
            storage_devices=["nvme0n1"],
            sleep_fn=lambda _: None,
        )
        assert snapshot.metrics["storage.reads-completed-total"].value == 100
        assert snapshot.metrics["storage.writes-completed-total"].value == 50
        assert snapshot.metrics["storage.reads-completed-delta"].value == 0

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
        assert "binding measurement" in binding.unavailable_reason


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
        assert (
            ComponentMetricSnapshot.model_validate_json(first.model_dump_json()).digest
            == first.digest
        )

    def test_unsupported_component_rejected(self):
        with pytest.raises(ValueError, match="unsupported component"):
            collect_component_snapshot("gpu", target_id="t", environment_digest=ENV)


def _request(component="memory", *, scope=None):
    requested = {
        "memory": ["memory.available-ratio", "memory.hidden-counter"],
        "cpu": ["cpu.busy-ratio"],
        "network": ["network.rx-bytes-total"],
        "storage": ["storage.reads-completed-total"],
        "numa": ["numa.node-count"],
    }[component]
    return ComponentCollectionRequest(
        component=component,
        target_id="t",
        environment_digest=ENV,
        workload_phase_id="measure",
        workload_source="test protocol",
        collector_id="test.fake",
        requested_metrics=requested,
        input_artifacts=[],
        interval_seconds=1.0,
        scope=scope or ComponentCollectionScope(),
        measurement_identity={"run_id": "r1"},
    )


def _memory_snapshot(*, target_id="t"):
    return ComponentMetricSnapshot(
        component="memory",
        target_id=target_id,
        environment_digest=ENV,
        collected_at=FIXED_AT,
        metrics={
            "memory.available-ratio": CollectedMetric(
                name="memory.available-ratio",
                unit="ratio",
                value=0.5,
                availability=MetricAvailability.READABLE,
                source="/proc/meminfo",
            ),
            "memory.hidden-counter": CollectedMetric(
                name="memory.hidden-counter",
                unit="count",
                availability=MetricAvailability.UNAVAILABLE,
                unavailable_reason="not exposed to guest",
                source="/sys/hidden",
            ),
        },
        counting_basis="fixture",
    )


class TestStrengthenedSnapshotContract:
    def test_rejects_unsupported_component_at_model_boundary(self):
        payload = _memory_snapshot().model_dump()
        payload["component"] = "gpu"
        with pytest.raises(ValueError):
            ComponentMetricSnapshot.model_validate(payload)

    def test_rejects_metric_key_name_mismatch(self):
        payload = _memory_snapshot().model_dump()
        payload["metrics"] = {"memory.wrong-key": payload["metrics"]["memory.available-ratio"]}
        with pytest.raises(ValueError, match="must equal"):
            ComponentMetricSnapshot.model_validate(payload)

    def test_rejects_cross_component_metric(self):
        metric = CollectedMetric(
            name="cpu.busy-ratio",
            unit="ratio",
            value=0.5,
            availability=MetricAvailability.READABLE,
            source="/proc/stat",
        )
        with pytest.raises(ValueError, match="does not belong"):
            ComponentMetricSnapshot(
                component="memory",
                target_id="t",
                environment_digest=ENV,
                collected_at=FIXED_AT,
                metrics={metric.name: metric},
                counting_basis="fixture",
            )

    def test_rejects_naive_datetime(self):
        payload = _memory_snapshot().model_dump()
        payload["collected_at"] = datetime(2026, 8, 23, 12, 0, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            ComponentMetricSnapshot.model_validate(payload)

    def test_legacy_snapshot_digest_shape_is_unchanged(self):
        snapshot = ComponentMetricSnapshot(
            component="memory",
            target_id="t",
            environment_digest=ENV,
            collected_at=datetime(2026, 8, 23, tzinfo=UTC),
            metrics={
                "memory.available-ratio": CollectedMetric(
                    name="memory.available-ratio",
                    unit="ratio",
                    value=0.5,
                    availability=MetricAvailability.READABLE,
                    source="/proc/meminfo",
                )
            },
            counting_basis="legacy basis",
        )
        assert (
            snapshot.digest
            == "sha256:d3fe2fa214d8dc310aa5bccdbfc1bddcc099f01e16f759fc9c144a8ec6b8abf6"
        )


class TestCorrectedBuiltins:
    def test_cpu_uses_fields_through_steal_and_excludes_guest_double_count(self, tmp_path):
        stat = tmp_path / "proc" / "stat"
        _write(stat, "cpu 100 0 100 700 0 10 10 10 900 900\ncpu0 1 1 1 1\n")

        def advance(_):
            _write(stat, "cpu 110 0 110 710 0 20 20 20 1900 1900\ncpu0 1 1 1 1\n")

        snapshot = collect_component_snapshot(
            "cpu",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            interval_seconds=1,
            collected_at=FIXED_AT,
            sleep_fn=advance,
        )
        assert snapshot.metrics["cpu.busy-ratio"].value == pytest.approx(5 / 6)

    def test_malformed_cpu_stat_is_unavailable_not_exception(self, tmp_path):
        _write(tmp_path / "proc" / "stat", "cpu one two three four\n")
        snapshot = collect_component_snapshot(
            "cpu",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            collected_at=FIXED_AT,
            sleep_fn=lambda _: None,
        )
        assert snapshot.metrics["cpu.busy-ratio"].availability is MetricAvailability.UNAVAILABLE

    def test_non_cpu_event_source_does_not_claim_cpu_pmu(self, tmp_path):
        _write(tmp_path / "proc" / "stat", "cpu 1 0 1 8 0 0 0 0\n")
        (tmp_path / "sys" / "bus" / "event_source" / "devices" / "software").mkdir(parents=True)
        snapshot = collect_component_snapshot(
            "cpu",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            sys_root=tmp_path / "sys",
            collected_at=FIXED_AT,
            sleep_fn=lambda _: None,
        )
        assert (
            snapshot.metrics["cpu.pmu-event-sources"].availability is MetricAvailability.UNAVAILABLE
        )

    @pytest.mark.parametrize(
        "meminfo",
        [
            "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
            "MemTotal: 100 kB\nMemAvailable: 101 kB\n",
            "MemTotal: 100 kB\nMemAvailable: -1 kB\n",
        ],
    )
    def test_memory_invalid_bounds_are_unavailable(self, tmp_path, meminfo):
        _write(tmp_path / "proc" / "meminfo", meminfo)
        snapshot = collect_component_snapshot(
            "memory",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            collected_at=FIXED_AT,
        )
        assert (
            snapshot.metrics["memory.available-ratio"].availability
            is MetricAvailability.UNAVAILABLE
        )

    def test_unscoped_network_and_storage_fail_closed(self, tmp_path):
        for component in ("network", "storage"):
            snapshot = collect_component_snapshot(
                component,
                target_id="t",
                environment_digest=ENV,
                proc_root=tmp_path / "proc",
                collected_at=FIXED_AT,
            )
            assert all(
                metric.availability is MetricAvailability.UNAVAILABLE
                for metric in snapshot.metrics.values()
            )
            assert "explicitly supplied" in next(iter(snapshot.metrics.values())).unavailable_reason

    def test_network_window_delta_rate_and_missing_interface(self, tmp_path):
        dev = tmp_path / "proc" / "net" / "dev"
        header = "Inter-| Receive | Transmit\n face |bytes |bytes\n"
        _write(dev, header + " eth0: 100 0 0 0 0 0 0 0 40 0 0 0 0 0 0 0\n")

        def advance(_):
            _write(dev, header + " eth0: 160 0 0 0 0 0 0 0 60 0 0 0 0 0 0 0\n")

        snapshot = collect_component_snapshot(
            "network",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            collected_at=FIXED_AT,
            interval_seconds=2,
            network_interfaces=["eth0"],
            sleep_fn=advance,
        )
        assert snapshot.metrics["network.rx-bytes-total"].value == 160
        assert snapshot.metrics["network.rx-bytes-delta"].value == 60
        assert snapshot.metrics["network.rx-bytes-per-second"].value == 30

        missing = collect_component_snapshot(
            "network",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            collected_at=FIXED_AT,
            network_interfaces=["ens9"],
            sleep_fn=lambda _: None,
        )
        assert (
            missing.metrics["network.rx-bytes-total"].availability is MetricAvailability.UNAVAILABLE
        )

    def test_counter_reset_keeps_raw_total_but_invalidates_derived(self, tmp_path):
        dev = tmp_path / "proc" / "net" / "dev"
        header = "Inter-| Receive | Transmit\n face |bytes |bytes\n"
        _write(dev, header + " eth0: 100 0 0 0 0 0 0 0 40 0 0 0 0 0 0 0\n")

        def reset(_):
            _write(dev, header + " eth0: 10 0 0 0 0 0 0 0 50 0 0 0 0 0 0 0\n")

        snapshot = collect_component_snapshot(
            "network",
            target_id="t",
            environment_digest=ENV,
            proc_root=tmp_path / "proc",
            collected_at=FIXED_AT,
            network_interfaces=["eth0"],
            sleep_fn=reset,
        )
        assert snapshot.metrics["network.rx-bytes-total"].value == 10
        assert (
            snapshot.metrics["network.rx-bytes-delta"].availability
            is MetricAvailability.UNAVAILABLE
        )
        assert snapshot.metrics["network.tx-bytes-delta"].value == 10

    def test_multi_node_topology_never_infers_workload_binding(self, tmp_path):
        root = tmp_path / "sys" / "devices" / "system" / "node"
        (root / "node0").mkdir(parents=True)
        (root / "node1").mkdir()
        snapshot = collect_component_snapshot(
            "numa",
            target_id="t",
            environment_digest=ENV,
            sys_root=tmp_path / "sys",
            collected_at=FIXED_AT,
        )
        assert snapshot.metrics["numa.node-count"].value == 2
        assert snapshot.metrics["numa.binding"].availability is MetricAvailability.UNAVAILABLE


class _FakeCollector:
    collector_id = "test.fake"
    collector_version = "1"

    def __init__(self, snapshot=None):
        self.snapshot = snapshot or _memory_snapshot()
        self.calls = 0

    def collect(self, request):
        self.calls += 1
        return self.snapshot


class TestCollectionBoundary:
    def test_scope_contract_requires_exact_network_and_storage_targets(self):
        with pytest.raises(ValueError, match="network_interfaces"):
            _request("network", scope=ComponentCollectionScope())
        with pytest.raises(ValueError, match="storage_devices"):
            _request("storage", scope=ComponentCollectionScope())
        with pytest.raises(ValueError, match="does not accept"):
            _request(
                "cpu",
                scope=ComponentCollectionScope(network_interfaces=["lo"]),
            )

    def test_request_binds_raw_artifact_digest_and_requested_metrics(self):
        artifact = CollectionInputArtifact(
            artifact_id="iperf-json",
            source="/tmp/iperf.json",
            media_type="application/json",
            digest="sha256:" + "c" * 64,
        )
        request = _request().model_copy(update={"input_artifacts": [artifact]}, deep=True)
        assert request.input_artifacts[0].digest == "sha256:" + "c" * 64
        with pytest.raises(ValueError, match="requested component"):
            ComponentCollectionRequest(
                **{
                    **_request().model_dump(),
                    "requested_metrics": ["cpu.busy-ratio"],
                }
            )

    def test_mismatched_collector_is_rejected_before_collection(self):
        fake = _FakeCollector()
        request = ComponentCollectionRequest(
            **{**_request().model_dump(), "collector_id": "different.collector"}
        )
        with pytest.raises(ValueError, match="injected collector_id"):
            run_component_collection(request, collector=fake, enabled=True)
        assert fake.calls == 0

    def test_injected_collector_is_replaceable(self):
        fake = _FakeCollector()
        times = iter([FIXED_AT, FIXED_AT])
        run = run_component_collection(
            _request(), collector=fake, enabled=True, wall_clock=lambda: next(times)
        )
        assert fake.calls == 1
        assert run.snapshot == fake.snapshot
        assert run.collector_id == "test.fake"

    def test_disabled_run_never_invokes_collector(self):
        fake = _FakeCollector()
        times = iter([FIXED_AT, FIXED_AT])
        run = run_component_collection(
            _request(), collector=fake, enabled=False, wall_clock=lambda: next(times)
        )
        assert fake.calls == 0
        assert run.snapshot is None

    def test_run_rejects_snapshot_identity_mismatch(self):
        with pytest.raises(ValueError, match="target_id"):
            ComponentCollectionRun(
                request=_request(),
                collector_id="test.fake",
                collector_version="1",
                enabled=True,
                started_at=FIXED_AT,
                finished_at=FIXED_AT,
                snapshot=_memory_snapshot(target_id="different"),
            )

    def test_l2_envelope_binds_digest_and_preserves_unavailable(self):
        fake = _FakeCollector()
        times = iter([FIXED_AT, FIXED_AT])
        run = run_component_collection(
            _request(), collector=fake, enabled=True, wall_clock=lambda: next(times)
        )
        envelope = bind_collection_to_measurement_batch(run, gate_values={"safe": True})
        assert envelope.measurement_batch_digest == envelope.measurement_batch.digest
        assert set(envelope.measurement_batch.metrics) == {"memory.available-ratio"}
        assert set(envelope.unavailable_metrics) == {"memory.hidden-counter"}
        assert envelope.measurement_batch.identity["collection_run_digest"] == run.digest
        assert (
            CollectionMeasurementEnvelope.model_validate_json(envelope.model_dump_json()).digest
            == envelope.digest
        )

    def test_collected_distribution_maps_to_l2_metric_evidence_values(self):
        series = CollectedMetric(
            name="memory.throughput-samples",
            unit="ops/s",
            value=[100.0, 110.0, 105.0],
            availability=MetricAvailability.READABLE,
            source="artifact:sysbench-json",
        )
        snapshot = ComponentMetricSnapshot(
            component="memory",
            target_id="t",
            environment_digest=ENV,
            collected_at=FIXED_AT,
            metrics={series.name: series},
            counting_basis="all raw trial values from digest-bound artifact",
        )
        request = ComponentCollectionRequest(
            **{**_request().model_dump(), "requested_metrics": [series.name]}
        )
        fake = _FakeCollector(snapshot)
        times = iter([FIXED_AT, FIXED_AT])
        run = run_component_collection(
            request, collector=fake, enabled=True, wall_clock=lambda: next(times)
        )
        envelope = bind_collection_to_measurement_batch(run, gate_values={})
        assert envelope.measurement_batch.metrics[series.name].values == series.value

    def test_existing_l2_primary_metrics_and_phase_fields_are_preserved(self):
        fake = _FakeCollector()
        times = iter([FIXED_AT, FIXED_AT])
        request = _request()
        run = run_component_collection(
            request, collector=fake, enabled=True, wall_clock=lambda: next(times)
        )
        original = MeasurementBatch(
            identity=request.measurement_identity,
            metrics={
                "workload.throughput": MetricEvidence(
                    metric_id="workload.throughput", values=[123.0]
                )
            },
            gate_values={"safe": True},
            pressure_protocol_digest="sha256:" + "b" * 64,
        )
        envelope = bind_collection_to_measurement_batch(run, measurement_batch=original)
        assert envelope.measurement_batch.metrics["workload.throughput"].values == [123.0]
        assert (
            envelope.measurement_batch.pressure_protocol_digest == original.pressure_protocol_digest
        )
        assert envelope.collection_metric_names == ["memory.available-ratio"]

    def test_all_unavailable_snapshot_is_preserved_with_empty_l2_metrics(self):
        unavailable = CollectedMetric(
            name="memory.available-ratio",
            unit="ratio",
            availability=MetricAvailability.UNAVAILABLE,
            unavailable_reason="guest source unreadable",
            source="/proc/meminfo",
        )
        snapshot = ComponentMetricSnapshot(
            component="memory",
            target_id="t",
            environment_digest=ENV,
            collected_at=FIXED_AT,
            metrics={unavailable.name: unavailable},
            counting_basis="fixture",
        )
        request = ComponentCollectionRequest(
            **{**_request().model_dump(), "requested_metrics": [unavailable.name]}
        )
        fake = _FakeCollector(snapshot)
        times = iter([FIXED_AT, FIXED_AT])
        run = run_component_collection(
            request, collector=fake, enabled=True, wall_clock=lambda: next(times)
        )
        envelope = bind_collection_to_measurement_batch(run, gate_values={})
        assert envelope.measurement_batch.metrics == {}
        assert envelope.collection_metric_names == []
        assert envelope.unavailable_metrics == {unavailable.name: unavailable}

    def test_l2_envelope_rejects_tampered_original_identity(self):
        fake = _FakeCollector()
        times = iter([FIXED_AT, FIXED_AT])
        run = run_component_collection(
            _request(), collector=fake, enabled=True, wall_clock=lambda: next(times)
        )
        payload = bind_collection_to_measurement_batch(run, gate_values={}).model_dump()
        payload["measurement_batch"]["identity"].pop("run_id")
        payload["measurement_batch_digest"] = MeasurementBatch.model_validate(
            payload["measurement_batch"]
        ).digest
        with pytest.raises(ValueError, match="exactly bound"):
            CollectionMeasurementEnvelope.model_validate(payload)

    def test_l2_envelope_rejects_tampered_digest(self):
        fake = _FakeCollector()
        times = iter([FIXED_AT, FIXED_AT])
        run = run_component_collection(
            _request(), collector=fake, enabled=True, wall_clock=lambda: next(times)
        )
        payload = bind_collection_to_measurement_batch(run, gate_values={}).model_dump()
        payload["measurement_batch_digest"] = "sha256:" + "0" * 64
        with pytest.raises(ValueError, match="does not match"):
            CollectionMeasurementEnvelope.model_validate(payload)

    def test_overhead_ab_requires_paired_finite_raw_observations(self):
        evidence = CollectionOverheadABEvidence(
            target_id="t",
            environment_digest=ENV,
            workload_identity={"protocol": "p1"},
            collector_id="test.fake",
            collection_disabled_seconds=[1.0, 1.1],
            collection_enabled_seconds=[1.01, 1.12],
            collected_at=FIXED_AT,
        )
        assert evidence.collection_enabled_seconds == [1.01, 1.12]
        with pytest.raises(ValueError, match="paired"):
            CollectionOverheadABEvidence(
                target_id="t",
                environment_digest=ENV,
                workload_identity={"protocol": "p1"},
                collector_id="test.fake",
                collection_disabled_seconds=[1.0],
                collection_enabled_seconds=[1.0, 1.1],
                collected_at=FIXED_AT,
            )
        with pytest.raises(ValueError, match="finite"):
            CollectionOverheadABEvidence(
                target_id="t",
                environment_digest=ENV,
                workload_identity={"protocol": "p1"},
                collector_id="test.fake",
                collection_disabled_seconds=[1.0],
                collection_enabled_seconds=[float("nan")],
                collected_at=FIXED_AT,
            )
