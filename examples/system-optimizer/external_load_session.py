"""外部负载会话脚本（测试侧 runner）：读 workload 合同 → 循环起压 → 逐窗落盘。

SO-D020 对侧：本脚本是**唯一会启动负载进程的一方**；引擎（dynamic-run）只读
``windows/``、只写 ``control/``。落盘约定见
docs/system-optimizer/contracts/dynamic-session-files.md：

- ``windows/<id>/o0.txt`` —— 负载工具 stdout 原文（O0 解析输入，引擎侧
  ``parse_o0_metrics`` 按合同 tool 解析）；
- ``windows/<id>/identity.json`` —— LoadCommandIdentity（用**实际执行的 argv**
  计算 argv_digest；与合同不一致即身份漂移，引擎会停相位）。

argv 由外部侧持有：本脚本通过 ``--`` 之后的完整命令提供真实 argv，引擎侧合同
只存 argv_digest，双方用 ``looper_core.system_opt.workload.load_argv_digest``
复现比对。本脚本工具无关（stress-ng/fio/iperf3/sysbench 一律把 stdout 原文落盘），
输出格式由引擎侧 O0 解析器负责，不在本脚本预设。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from looper_core.system_opt.dynamic_adapters import SessionLayout
from looper_core.system_opt.workload import (
    LoadCommandIdentity,
    load_argv_digest,
    parse_workload_contract_yaml,
)

# 注入回调：argv -> 负载 stdout 原文。测试侧用假 runner，生产用 run_load。
RunCommand = Callable[[list[str]], str]


def run_load(argv: list[str], *, timeout_seconds: float) -> str:
    """Run the load once and return its raw stdout (test-side only)."""

    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or f"load exited {completed.returncode}")
    return completed.stdout


def compute_identity(contract, argv: list[str]) -> LoadCommandIdentity:
    """Bind the actually-executed argv to a LoadCommandIdentity.

    身份 digest 只覆盖 tool + argv_digest + declared_duration_seconds；
    description 是 prose 不进入身份（见 workload.LoadCommandIdentity）。
    """

    return LoadCommandIdentity(
        tool=contract.load_command.tool,
        argv_digest=load_argv_digest(argv),
        declared_duration_seconds=contract.load_command.declared_duration_seconds,
        description=contract.load_command.description,
    )


def write_window(
    layout: SessionLayout,
    window_id: str,
    identity: LoadCommandIdentity,
    raw_output: str,
) -> Path:
    """Write one window's identity.json + o0.txt (session-files 约定)."""

    window = layout.window(window_id)
    window.mkdir(parents=True, exist_ok=True)
    (window / "identity.json").write_text(
        identity.model_dump_json(indent=2), encoding="utf-8"
    )
    (window / "o0.txt").write_text(raw_output, encoding="utf-8")
    return window


def discover_retest_requests(layout: SessionLayout) -> dict[str, list[str]]:
    """Read control/retest-request-*.json -> {request_name: window_ids}."""

    control = layout.control
    if not control.is_dir():
        return {}
    requests: dict[str, list[str]] = {}
    for path in sorted(control.glob("retest-request-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        window_ids = payload.get("window_ids")
        if not isinstance(window_ids, list) or not window_ids:
            raise ValueError(f"retest request {path.name} carries no window_ids")
        requests[path.name] = [str(item) for item in window_ids]
    return requests


def run_observation_windows(
    *,
    layout: SessionLayout,
    argv: list[str],
    window_count: int,
    run: RunCommand,
) -> list[str]:
    """Produce the initial observation windows ``window-1``..``window-N``."""

    contract = parse_workload_contract_yaml(
        layout.workload_contract.read_text(encoding="utf-8")
    )
    identity = compute_identity(contract, argv)
    produced: list[str] = []
    for index in range(1, window_count + 1):
        window_id = f"window-{index}"
        write_window(layout, window_id, identity, run(argv))
        produced.append(window_id)
    return produced


def serve_retest_requests(
    *,
    layout: SessionLayout,
    argv: list[str],
    run: RunCommand,
    poll_seconds: float,
    timeout_seconds: float,
) -> list[str]:
    """Poll control/ for retest requests and supply the requested windows.

    只补尚未落盘的窗口；窗口身份由合同 + 实际 argv 计算，供引擎逐窗核对。
    """

    contract = parse_workload_contract_yaml(
        layout.workload_contract.read_text(encoding="utf-8")
    )
    identity = compute_identity(contract, argv)
    deadline = time.monotonic() + timeout_seconds
    produced: list[str] = []
    while True:
        requests = discover_retest_requests(layout)
        for request_name, window_ids in requests.items():
            for window_id in window_ids:
                if (layout.window(window_id) / "o0.txt").is_file():
                    continue
                write_window(layout, window_id, identity, run(argv))
                produced.append(f"{request_name}:{window_id}")
        if produced:
            return produced
        if time.monotonic() >= deadline:
            return produced
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="外部负载会话 runner：读 workload 合同，循环起压并逐窗落盘"
    )
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--window-count", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--retest-poll-seconds", type=float, default=1.0)
    parser.add_argument("--retest-wait-seconds", type=float, default=0.0)
    parser.add_argument(
        "load_argv",
        nargs=argparse.REMAINDER,
        help="-- 之后的完整负载命令（如 -- stress-ng --cpu 4 --timeout 120s --yaml）",
    )
    args = parser.parse_args()

    if not args.load_argv:
        raise SystemExit(
            "provide the load command after '--' (e.g. "
            "-- stress-ng --cpu 4 --timeout 120s --yaml)"
        )
    layout = SessionLayout(args.session_dir)
    if not layout.workload_contract.is_file():
        raise SystemExit(f"workload contract is missing: {layout.workload_contract}")
    if args.window_count < 1:
        raise SystemExit("--window-count must be positive")

    def run(argv: list[str]) -> str:
        return run_load(argv, timeout_seconds=args.timeout_seconds)

    produced = run_observation_windows(
        layout=layout,
        argv=args.load_argv,
        window_count=args.window_count,
        run=run,
    )
    print(f"observation windows written: {', '.join(produced)}")

    if args.retest_wait_seconds > 0:
        retests = serve_retest_requests(
            layout=layout,
            argv=args.load_argv,
            run=run,
            poll_seconds=args.retest_poll_seconds,
            timeout_seconds=args.retest_wait_seconds,
        )
        for item in retests:
            print(f"retest window written: {item}")


if __name__ == "__main__":
    main()
