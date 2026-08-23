from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel


class InterferingProcess(StrictModel):
    pid: int = Field(gt=0)
    process_name: str = Field(min_length=1, max_length=256)
    command_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    matched_patterns: list[str] = Field(min_length=1)


class ProcessInterferenceEvidence(StrictModel):
    schema_version: str = "looper.process-interference-evidence/v1alpha1"
    forbidden_patterns: list[str] = Field(min_length=1)
    ignored_ancestor_pids: list[int]
    matches: list[InterferingProcess]
    recorded_at: datetime
    counting_basis: str

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def _read_ppid(proc_dir: Path) -> int | None:
    try:
        value = (proc_dir / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    closing = value.rfind(")")
    if closing < 0:
        return None
    fields = value[closing + 1 :].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _ancestor_pids(proc_root: Path, pid: int) -> set[int]:
    result: set[int] = set()
    current = pid
    while current > 0 and current not in result:
        result.add(current)
        parent = _read_ppid(proc_root / str(current))
        if parent is None or parent == current:
            break
        current = parent
    return result


def detect_forbidden_processes(
    patterns: list[str],
    *,
    proc_root: Path = Path("/proc"),
    own_pid: int | None = None,
) -> ProcessInterferenceEvidence:
    if not patterns or len(patterns) != len(set(patterns)):
        raise ValueError("forbidden process patterns must be non-empty and unique")
    try:
        compiled = [(pattern, re.compile(pattern, re.IGNORECASE)) for pattern in patterns]
    except re.error as error:
        raise ValueError(f"invalid forbidden process pattern: {error}") from error
    ignored = _ancestor_pids(proc_root, own_pid or os.getpid())
    matches: list[InterferingProcess] = []
    for proc_dir in sorted(
        (path for path in proc_root.iterdir() if path.name.isdigit()),
        key=lambda path: int(path.name),
    ):
        pid = int(proc_dir.name)
        if pid in ignored:
            continue
        try:
            command_bytes = (proc_dir / "cmdline").read_bytes()
            process_name = (proc_dir / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        command = command_bytes.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        haystack = f"{process_name} {command}"
        matched = [pattern for pattern, regex in compiled if regex.search(haystack)]
        if matched:
            matches.append(
                InterferingProcess(
                    pid=pid,
                    process_name=process_name,
                    command_digest="sha256:" + hashlib.sha256(command_bytes).hexdigest(),
                    matched_patterns=matched,
                )
            )
    return ProcessInterferenceEvidence(
        forbidden_patterns=patterns,
        ignored_ancestor_pids=sorted(ignored),
        matches=matches,
        recorded_at=datetime.now(UTC),
        counting_basis=(
            "one observation per live numeric /proc PID whose comm or cmdline matches at "
            "least one explicit pattern; the checker and all ancestors are excluded; raw "
            "command lines are not persisted"
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forbid-regex", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "posix" or not Path("/proc").is_dir():
        raise SystemExit("process interference guard requires Linux /proc")
    evidence = detect_forbidden_processes(args.forbid_regex)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence.model_dump(mode="json"), sort_keys=True, indent=2),
        encoding="utf-8",
    )
    if evidence.matches:
        names = sorted({match.process_name for match in evidence.matches})
        raise SystemExit(f"exclusive pressure window is busy: {names}")


if __name__ == "__main__":
    main()


__all__ = [
    "InterferingProcess",
    "ProcessInterferenceEvidence",
    "detect_forbidden_processes",
]
