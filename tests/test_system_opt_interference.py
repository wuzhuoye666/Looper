from __future__ import annotations

from pathlib import Path

from looper_core.system_opt.interference import detect_forbidden_processes


def _process(root: Path, pid: int, ppid: int, comm: str, command: bytes) -> None:
    directory = root / str(pid)
    directory.mkdir()
    (directory / "stat").write_text(
        f"{pid} ({comm}) S {ppid} 0 0 0\n",
        encoding="utf-8",
    )
    (directory / "comm").write_text(f"{comm}\n", encoding="utf-8")
    (directory / "cmdline").write_bytes(command)


def test_interference_guard_ignores_its_ancestors_and_redacts_commands(
    tmp_path: Path,
) -> None:
    _process(tmp_path, 1, 0, "init", b"init\x00")
    _process(tmp_path, 20, 1, "shell", b"shell\x00phpbench\x00")
    _process(tmp_path, 30, 20, "python3", b"guard\x00phpbench\x00")
    _process(tmp_path, 40, 1, "php", b"php\x00phpbench.php\x00-i\x002000000\x00")

    evidence = detect_forbidden_processes(
        ["phpbench", "Phoronix Test Suite"], proc_root=tmp_path, own_pid=30
    )

    assert evidence.ignored_ancestor_pids == [1, 20, 30]
    assert [(item.pid, item.process_name) for item in evidence.matches] == [(40, "php")]
    assert evidence.matches[0].command_digest.startswith("sha256:")
    assert "2000000" not in evidence.model_dump_json()


def test_interference_guard_rejects_duplicate_patterns(tmp_path: Path) -> None:
    try:
        detect_forbidden_processes(["fio", "fio"], proc_root=tmp_path, own_pid=1)
    except ValueError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("duplicate process patterns must fail closed")
