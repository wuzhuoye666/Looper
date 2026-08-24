"""外部负载会话脚本（#11）测试：用假 runner 验证窗口落盘与身份契约。

脚本本身是测试侧 runner（会真实起压），单测只覆盖纯逻辑：身份计算、
窗口落盘往返、观察窗生产、复测请求发现与服务。用 importlib 加载
examples/system-optimizer/external_load_session.py（与 memory_pressure 同模式）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from looper_core.system_opt.dynamic_adapters import SessionLayout
from looper_core.system_opt.workload import (
    LoadCommandIdentity,
    load_argv_digest,
    parse_workload_contract_yaml,
)

EXAMPLE_CONTRACT = (
    Path(__file__).parents[1]
    / "examples"
    / "system-optimizer"
    / "stress-ng-workload-contract.yaml"
)

ARGV = ["stress-ng", "--cpu", "2", "--timeout", "120s", "--yaml", "--metrics-brief"]
RAW_OUTPUT = "metrics:\n- stressor: cpu\n  bogo-ops: 47151\n"


def _load_module():
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


def _layout(tmp_path: Path) -> SessionLayout:
    root = tmp_path / "session"
    root.mkdir()
    (root / "workload-contract.yaml").write_text(
        EXAMPLE_CONTRACT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return SessionLayout(root)


class TestComputeIdentity:
    def test_argv_digest_matches_when_argv_equal(self):
        module = _load_module()
        contract = parse_workload_contract_yaml(
            EXAMPLE_CONTRACT.read_text(encoding="utf-8")
        )

        identity = module.compute_identity(contract, ARGV)

        assert identity.tool == contract.load_command.tool
        assert identity.argv_digest == load_argv_digest(ARGV)
        assert (
            identity.declared_duration_seconds
            == contract.load_command.declared_duration_seconds
        )

    def test_identity_digest_differs_when_argv_differs(self):
        module = _load_module()
        contract = parse_workload_contract_yaml(
            EXAMPLE_CONTRACT.read_text(encoding="utf-8")
        )

        identity = module.compute_identity(contract, ARGV)
        drifted = module.compute_identity(contract, [*ARGV, "--extra"])

        assert identity.identity_digest != drifted.identity_digest


class TestWriteWindow:
    def test_window_files_round_trip(self, tmp_path):
        module = _load_module()
        layout = _layout(tmp_path)
        contract = parse_workload_contract_yaml(
            EXAMPLE_CONTRACT.read_text(encoding="utf-8")
        )
        identity = module.compute_identity(contract, ARGV)

        module.write_window(layout, "window-1", identity, RAW_OUTPUT)

        assert (
            layout.window("window-1") / "o0.txt"
        ).read_text(encoding="utf-8") == RAW_OUTPUT
        round_tripped = LoadCommandIdentity.model_validate_json(
            (layout.window("window-1") / "identity.json").read_text(encoding="utf-8")
        )
        assert round_tripped == identity


class TestObservationWindows:
    def test_produces_numbered_windows_with_fake_runner(self, tmp_path):
        module = _load_module()
        layout = _layout(tmp_path)

        calls: list[list[str]] = []

        def fake_run(argv):
            calls.append(argv)
            return RAW_OUTPUT

        produced = module.run_observation_windows(
            layout=layout, argv=ARGV, window_count=3, run=fake_run
        )

        assert produced == ["window-1", "window-2", "window-3"]
        assert calls == [ARGV, ARGV, ARGV]
        for window_id in produced:
            assert (layout.window(window_id) / "o0.txt").is_file()
            assert (layout.window(window_id) / "identity.json").is_file()


class TestRetestRequests:
    def test_discovers_and_serves_requested_windows(self, tmp_path):
        module = _load_module()
        layout = _layout(tmp_path)
        control = layout.control
        control.mkdir(parents=True, exist_ok=True)
        (control / "retest-request-hyp-governor.json").write_text(
            json.dumps(
                {
                    "window_ids": [
                        "retest-hyp-governor-run1",
                        "retest-hyp-governor-run2",
                    ]
                }
            ),
            encoding="utf-8",
        )

        requests = module.discover_retest_requests(layout)
        assert list(requests) == ["retest-request-hyp-governor.json"]
        assert requests["retest-request-hyp-governor.json"] == [
            "retest-hyp-governor-run1",
            "retest-hyp-governor-run2",
        ]

        def fake_run(argv):
            return RAW_OUTPUT

        produced = module.serve_retest_requests(
            layout=layout,
            argv=ARGV,
            run=fake_run,
            poll_seconds=0.01,
            timeout_seconds=1.0,
        )
        assert produced == [
            "retest-request-hyp-governor.json:retest-hyp-governor-run1",
            "retest-request-hyp-governor.json:retest-hyp-governor-run2",
        ]
        for window_id in ["retest-hyp-governor-run1", "retest-hyp-governor-run2"]:
            assert (layout.window(window_id) / "o0.txt").is_file()

    def test_malformed_request_fails_closed(self, tmp_path):
        module = _load_module()
        layout = _layout(tmp_path)
        control = layout.control
        control.mkdir(parents=True, exist_ok=True)
        (control / "retest-request-bad.json").write_text(
            json.dumps({"no_window_ids": True}), encoding="utf-8"
        )

        with pytest.raises(ValueError, match="no window_ids"):
            module.discover_retest_requests(layout)

    def test_serves_sequential_request_batches_until_idle(self, tmp_path):
        # ZCode amendment regression: one dynamic phase issues several request
        # batches over time (intervention retest group, then verification
        # groups). The runner must keep serving instead of exiting after the
        # first batch; the injected run callback materializes a second request
        # the way the engine would while the first batch is being produced.
        module = _load_module()
        layout = _layout(tmp_path)
        control = layout.control
        control.mkdir(parents=True, exist_ok=True)
        (control / "retest-request-hyp-thp.json").write_text(
            json.dumps({"window_ids": ["retest-hyp-thp-run1"]}),
            encoding="utf-8",
        )
        calls = {"count": 0}

        def fake_run(argv):
            calls["count"] += 1
            if calls["count"] == 1:
                (control / "retest-request-verify-window-3-1.json").write_text(
                    json.dumps({"window_ids": ["verify-window-3-1-run1"]}),
                    encoding="utf-8",
                )
            return RAW_OUTPUT

        produced = module.serve_retest_requests(
            layout=layout,
            argv=ARGV,
            run=fake_run,
            poll_seconds=0.01,
            timeout_seconds=5.0,
            idle_seconds=0.2,
        )

        assert (
            "retest-request-hyp-thp.json:retest-hyp-thp-run1" in produced
        )
        assert (
            "retest-request-verify-window-3-1.json:verify-window-3-1-run1" in produced
        )
        assert (layout.window("verify-window-3-1-run1") / "o0.txt").is_file()
