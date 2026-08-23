from __future__ import annotations

import pytest
from looper_core.system_opt.executor import CommandResult, OperationStatus
from looper_core.system_opt.executor.local_linux import LocalLinuxBackend, parse_readback
from looper_core.system_opt.executor.simulated import (
    SimulatedBackend,
    SimulatedFailurePlan,
)
from looper_core.system_opt.executor.ssh_remote import SSHRemoteBackend
from system_opt_support import boolean_item, categorical_item, integer_item


class FakeRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], float]] = []

    def run(self, argv: list[str], *, timeout_seconds: float) -> CommandResult:
        self.calls.append((argv, timeout_seconds))
        return self.result


def exercise_simulated() -> tuple[dict[str, object], list[dict[str, object]]]:
    item = integer_item()
    backend = SimulatedBackend({item.id: 60}, seed=7)
    snapshot = backend.snapshot([item], fencing_token=1)
    backend.apply(item, 10, fencing_token=1)
    backend.verify(item, 10, fencing_token=1)
    backend.rollback(item, snapshot.entries[item.id].value, fencing_token=1)
    return backend.state(), [result.model_dump(mode="json") for result in backend.operations]


def test_simulated_same_seed_same_trace() -> None:
    assert exercise_simulated() == exercise_simulated()


def test_simulated_injects_drift_delay_and_failure() -> None:
    item = integer_item()
    backend = SimulatedBackend(
        {item.id: 60},
        failure_plan=SimulatedFailurePlan(
            drift_on_verify={item.id: 30},
            virtual_delays={f"apply:{item.id}": 1.25},
        ),
    )

    applied = backend.apply(item, 10, fencing_token=2)
    verified = backend.verify(item, 10, fencing_token=2)

    assert applied.status == OperationStatus.SUCCEEDED
    assert applied.virtual_elapsed_seconds == 1.25
    assert verified.status == OperationStatus.FAILED
    assert verified.readback_value == 30


def test_simulated_rejects_stale_fencing_token() -> None:
    item = integer_item()
    backend = SimulatedBackend({item.id: 60})

    assert backend.apply(item, 10, fencing_token=3).succeeded
    stale = backend.apply(item, 20, fencing_token=2)

    assert stale.status == OperationStatus.FAILED
    assert "stale fencing token" in str(stale.message)
    assert backend.state()[item.id] == 10


def test_real_backends_are_disabled_by_default() -> None:
    local = LocalLinuxBackend(system_name="linux")
    remote = SSHRemoteBackend("tester@example.test", target_id="cvm-1")

    assert not local.capabilities.enabled
    assert not remote.capabilities.enabled
    assert local.probe(integer_item(), fencing_token=0).status == OperationStatus.UNAVAILABLE
    assert remote.probe(integer_item(), fencing_token=0).status == OperationStatus.UNAVAILABLE


def test_command_construction_uses_argv_and_allowlisted_placeholders() -> None:
    integer = integer_item()
    boolean = boolean_item()

    assert LocalLinuxBackend.build_apply_command(integer, 10) == [
        "sysctl",
        "-w",
        "vm.swappiness=10",
    ]
    assert LocalLinuxBackend.build_apply_command(boolean, False) == [
        "sysctl",
        "-w",
        "kernel.numa_balancing=0",
    ]

    remote = SSHRemoteBackend("tester@example.test", target_id="cvm-1")
    transport = remote.build_apply_command(integer, 10)
    payload = remote.decode_transport_payload(transport)
    assert payload == {
        "operation": "apply",
        "argv": ["sysctl", "-w", "vm.swappiness=10"],
    }
    assert transport[-1] != "vm.swappiness=10"


def test_ssh_disconnect_is_unknown_and_not_retried() -> None:
    runner = FakeRunner(CommandResult(status=OperationStatus.UNKNOWN, stderr="connection lost"))
    remote = SSHRemoteBackend("tester@example.test", target_id="cvm-1", enabled=True, runner=runner)

    result = remote.probe(integer_item(), fencing_token=4)

    assert result.status == OperationStatus.UNKNOWN
    assert result.message == "connection lost"
    assert len(runner.calls) == 1


def test_readback_parser_handles_boolean_and_bracket_selected_values() -> None:
    assert parse_readback(boolean_item(), "1\n") is True
    assert parse_readback(categorical_item(), "always [madvise] never\n") == "madvise"


def test_local_preflight_reports_missing_required_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("looper_core.system_opt.executor.local_linux.shutil.which", lambda _: None)
    result = LocalLinuxBackend(system_name="linux").preflight_check(integer_item())

    assert result.status == OperationStatus.UNAVAILABLE
    assert "sysctl" in str(result.message)
