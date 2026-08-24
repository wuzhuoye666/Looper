"""M3 observation window + O0 load-output parsers (workload-tuning.md D1/D6).

Parsers are pinned against REAL captured outputs from the 2026-08-23 Aliyun
sessions: stress-ng --yaml (CPU calibration), fio JSON (5-candidate storage
session) and iperf3 JSON (VPC intranet CC calibration).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from looper_core.system_opt.observation import (
    O0ParseError,
    WorkloadIdentityDrift,
    parse_o0_metrics,
    record_window,
)
from looper_core.system_opt.workload import (
    parse_workload_contract_yaml,
)

ARTIFACTS = Path(__file__).parents[1] / ".artifacts" / "system-opt"
EXAMPLE_CONTRACT = (
    Path(__file__).parents[1]
    / "examples"
    / "system-optimizer"
    / "stress-ng-workload-contract.yaml"
)

STRESS_NG_YAML = (
    ARTIFACTS
    / "m2-component-calibration-20260823"
    / "looper-m2-cpu-calibration-20260823-b"
    / "cpu-20260823T052438.003303Z-1.yaml"
)
FIO_JSON = (
    ARTIFACTS
    / "aliyun-ecs-fio-20260823"
    / "raw"
    / "fio-mq-deadline-20260823T023450.946047Z-44279-1.json"
)
IPERF3_JSON = (
    ARTIFACTS
    / "m2-network-cc-calibration-20260823"
    / "network-20260823T105619.260182Z-1.json"
)
SYSBENCH_CPU = (
    ARTIFACTS
    / "sysbench-o0-capture-20260823"
    / "sysbench-cpu-threads2-time5.txt"
)
SYSBENCH_MEMORY = (
    ARTIFACTS
    / "sysbench-o0-capture-20260823"
    / "sysbench-memory-threads2-1M-2G.txt"
)

START = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
END = datetime(2026, 8, 23, 12, 2, tzinfo=UTC)


class TestParsersAgainstRealOutputs:
    def test_stress_ng_yaml_real_fixture(self):
        raw = STRESS_NG_YAML.read_text(encoding="utf-8")
        parsed = parse_o0_metrics(
            "stress-ng",
            ["stress-ng.bogo-ops", "stress-ng.bogo-ops-per-second-usr-sys-time"],
            raw,
        )

        assert parsed["stress-ng.bogo-ops"] == [47151.0]
        assert parsed["stress-ng.bogo-ops-per-second-usr-sys-time"] == [
            pytest.approx(1182.489703)
        ]

    def test_fio_json_real_fixture(self):
        raw = FIO_JSON.read_text(encoding="utf-8")
        parsed = parse_o0_metrics(
            "fio",
            ["fio.read-iops", "fio.read-bw-bytes", "fio.read-clat-p99-ns"],
            raw,
        )

        # The captured run has two jobs; values stay per-job raw facts.
        assert parsed["fio.read-iops"] == [
            pytest.approx(2089.921344),
            pytest.approx(2105.256145),
        ]
        assert len(parsed["fio.read-clat-p99-ns"]) == 2
        assert parsed["fio.read-clat-p99-ns"][0] == 30801920.0
        assert all(value > 0 for value in parsed["fio.read-bw-bytes"])

    def test_iperf3_json_real_fixture(self):
        raw = IPERF3_JSON.read_text(encoding="utf-8")
        parsed = parse_o0_metrics(
            "iperf3",
            ["iperf3.sum-received-bps", "iperf3.seconds"],
            raw,
        )

        assert parsed["iperf3.sum-received-bps"] == [
            pytest.approx(2074142875.2558315)
        ]
        assert parsed["iperf3.seconds"] == [pytest.approx(5.000362)]

    def test_sysbench_cpu_real_fixture(self):
        raw = SYSBENCH_CPU.read_text(encoding="utf-8")
        parsed = parse_o0_metrics(
            "sysbench",
            [
                "sysbench.events-per-second",
                "sysbench.total-events",
                "sysbench.total-time-seconds",
                "sysbench.latency-avg-ms",
                "sysbench.latency-p95-ms",
                "sysbench.latency-max-ms",
            ],
            raw,
        )

        assert parsed["sysbench.events-per-second"] == [pytest.approx(2148.46)]
        assert parsed["sysbench.total-events"] == [10747.0]
        assert parsed["sysbench.total-time-seconds"] == [pytest.approx(5.0005)]
        assert parsed["sysbench.latency-avg-ms"] == [pytest.approx(0.93)]
        assert parsed["sysbench.latency-p95-ms"] == [pytest.approx(0.97)]
        assert parsed["sysbench.latency-max-ms"] == [pytest.approx(27.62)]

    def test_sysbench_memory_real_fixture(self):
        raw = SYSBENCH_MEMORY.read_text(encoding="utf-8")
        parsed = parse_o0_metrics(
            "sysbench",
            [
                "sysbench.throughput-mib-per-sec",
                "sysbench.total-events",
                "sysbench.latency-p95-ms",
            ],
            raw,
        )

        assert parsed["sysbench.throughput-mib-per-sec"] == [pytest.approx(20049.42)]
        assert parsed["sysbench.total-events"] == [2048.0]
        assert parsed["sysbench.latency-p95-ms"] == [pytest.approx(0.13)]

    def test_unregistered_tool_and_metric_fail_closed(self):
        with pytest.raises(O0ParseError, match="no O0 parser registered"):
            parse_o0_metrics("numactl", ["numactl.node-count"], "{}")
        with pytest.raises(O0ParseError, match="unregistered O0 metric"):
            parse_o0_metrics("stress-ng", ["stress-ng.not-a-field"], "metrics:\n- stressor: cpu\n")
        with pytest.raises(O0ParseError, match="unregistered O0 metric"):
            parse_o0_metrics("sysbench", ["sysbench.not-a-field"], "sysbench 1.0.20\n")

    def test_malformed_outputs_fail_closed(self):
        with pytest.raises(O0ParseError, match="invalid"):
            parse_o0_metrics("fio", ["fio.read-iops"], "{not json")
        with pytest.raises(O0ParseError, match="no jobs"):
            parse_o0_metrics("fio", ["fio.read-iops"], '{"jobs": []}')
        with pytest.raises(O0ParseError, match="no end section"):
            parse_o0_metrics("iperf3", ["iperf3.seconds"], '{"start": {}}')


class TestRecordWindow:
    def _contract(self):
        return parse_workload_contract_yaml(EXAMPLE_CONTRACT.read_text(encoding="utf-8"))

    def _window(self, contract, **overrides):
        payload = dict(
            contract=contract,
            window_id="win-1",
            phase_id="steady",
            load_command=contract.load_command,
            o0_raw=STRESS_NG_YAML.read_text(encoding="utf-8"),
            started_at=START,
            finished_at=END,
        )
        payload.update(overrides)
        return record_window(**payload)

    def test_window_assembles_with_phase_metrics_and_deterministic_digest(self):
        contract = self._contract()
        window = self._window(contract)
        again = self._window(contract)

        assert window.workload_contract_digest == contract.digest
        assert [item.metric_id for item in window.o0] == [
            "stress-ng.bogo-ops-per-second-usr-sys-time",
            "stress-ng.bogo-ops",
        ]
        assert window.o0[0].values == [pytest.approx(1182.489703)]
        assert {item.raw_output_digest for item in window.o0} == {
            item.raw_output_digest for item in again.o0
        }
        assert window.digest == again.digest

    def test_identity_drift_fails_closed_with_both_digests(self):
        contract = self._contract()
        drifted_load = contract.load_command.model_copy(
            update={
                "declared_duration_seconds": (
                    contract.load_command.declared_duration_seconds + 1
                )
            }
        )

        with pytest.raises(WorkloadIdentityDrift) as captured:
            self._window(contract, load_command=drifted_load)

        assert captured.value.expected == contract.load_command.identity_digest
        assert captured.value.actual == drifted_load.identity_digest

    def test_undeclared_phase_is_rejected(self):
        contract = self._contract()
        with pytest.raises(ValueError, match="not declared by workload contract"):
            self._window(contract, phase_id="ramp-up")

    def test_naive_or_inverted_times_are_rejected(self):
        contract = self._contract()
        naive = datetime(2026, 8, 23, 12, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            self._window(contract, started_at=naive)
        with pytest.raises(ValueError, match="must not precede"):
            self._window(contract, finished_at=START, started_at=END)
