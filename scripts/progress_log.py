#!/usr/bin/env python3
"""Append multi-agent progress events without lost updates or claim races."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_NAME = "\u5168\u5c40\u8bb0\u5f55.txt"
LOCK_PATH = ROOT / ".looper" / "progress.lock"
STATUSES = ("START", "UPDATE", "BLOCKED", "HANDOFF", "DONE")
TERMINAL_STATUSES = ("HANDOFF", "DONE")


@contextmanager
def _exclusive_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    """Hold one cross-process lock for reading claims and appending an event."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + timeout

        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {path}")
                    time.sleep(0.05)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {path}")
                    time.sleep(0.05)

        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_text(value: str, label: str) -> str:
    value = value.strip().replace("\r", " ").replace("\n", " ")
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _relative_path(value: str) -> str:
    raw = _safe_text(value, "file")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    candidate = candidate.resolve(strict=False)
    try:
        relative = candidate.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(f"file must be inside the repository: {value}") from exc

    normalized = relative.as_posix()
    if candidate.exists() and candidate.is_dir() and normalized != ".":
        normalized += "/"
    return normalized


def _task_key(agent: str, task: str) -> tuple[str, str]:
    return (agent.casefold(), task.casefold())


def _load_records(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    records: list[dict] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("v") == 1:
            records.append(value)
    return records


def _active_tasks(records: list[dict]) -> dict[tuple[str, str], dict]:
    active: dict[tuple[str, str], dict] = {}
    for record in records:
        agent = record.get("agent")
        task = record.get("task")
        status = record.get("status")
        if not isinstance(agent, str) or not isinstance(task, str):
            continue
        key = _task_key(agent, task)
        if status in TERMINAL_STATUSES:
            active.pop(key, None)
            continue
        if status not in ("START", "UPDATE", "BLOCKED"):
            continue
        state = active.setdefault(
            key,
            {"agent": agent, "task": task, "status": status, "files": set()},
        )
        state["status"] = status
        for file_path in record.get("files", []):
            if isinstance(file_path, str):
                state["files"].add(file_path)
    return active


def _overlaps(left: str, right: str) -> bool:
    left_base = left.rstrip("/").casefold()
    right_base = right.rstrip("/").casefold()
    return (
        left_base == right_base
        or left_base.startswith(right_base + "/")
        or right_base.startswith(left_base + "/")
    )


def _conflicts(active: dict[tuple[str, str], dict], current: tuple[str, str], files: list[str]) -> list[str]:
    owners: list[str] = []
    for state_key, state in active.items():
        if state_key == current:
            continue
        for claimed in state["files"]:
            for requested in files:
                if _overlaps(claimed, requested):
                    owners.append(
                        f"{requested} is claimed by {state['agent']} / {state['task']}"
                    )
    return sorted(set(owners))


def _append(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with log_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() > 0:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                handle.seek(0, os.SEEK_END)
                handle.write(b"\n")
        handle.seek(0, os.SEEK_END)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _show_active(log_path: Path) -> int:
    with _exclusive_lock(LOCK_PATH):
        active = _active_tasks(_load_records(log_path))
    if not active:
        print("No active tasks.")
        return 0
    for state in sorted(active.values(), key=lambda item: (item["agent"], item["task"])):
        files = ", ".join(sorted(state["files"])) or "(no file claims)"
        print(f"{state['agent']} | {state['task']} | {state['status']} | {files}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, help="override the repository-root log path")
    parser.add_argument("--show-active", action="store_true", help="show active task claims")
    parser.add_argument("--agent", help="unique agent id")
    parser.add_argument("--status", choices=STATUSES)
    parser.add_argument("--task", help="coherent task id")
    parser.add_argument("--file", dest="files", action="append", default=[], help="repo-relative file claim; repeatable")
    parser.add_argument("--message", default="", help="short single-line progress message")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    log_path = (args.log or (ROOT / DEFAULT_LOG_NAME)).resolve(strict=False)
    if args.show_active:
        return _show_active(log_path)
    if not args.agent or not args.status or not args.task:
        print("--agent, --status, and --task are required unless --show-active is used", file=sys.stderr)
        return 2

    try:
        agent = _safe_text(args.agent, "agent")
        task = _safe_text(args.task, "task")
        message = args.message.strip().replace("\r", " ").replace("\n", " ")
        files = sorted({_relative_path(value) for value in args.files})
        current = _task_key(agent, task)
        with _exclusive_lock(LOCK_PATH):
            active = _active_tasks(_load_records(log_path))
            conflicts = _conflicts(active, current, files)
            if conflicts:
                print("PROGRESS CONFLICT: " + "; ".join(conflicts), file=sys.stderr)
                return 3
            record = {
                "v": 1,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "agent": agent,
                "status": args.status,
                "task": task,
                "files": files,
                "message": message,
            }
            _append(log_path, record)
        print(f"Recorded {args.status} for {agent}/{task} ({len(files)} file claim(s)).")
        return 0
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"progress_log error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
