from __future__ import annotations

import subprocess
import time
from pathlib import Path

from looper_core.system_opt.executor import CommandResult, OperationStatus


class SubprocessCommandRunner:
    """Linux argv runner with an explicit executable and file-root allowlist."""

    def __init__(
        self,
        *,
        allowed_executables: set[str],
        writable_file_roots: list[Path],
    ) -> None:
        self._allowed_executables = set(allowed_executables)
        self._writable_file_roots = [path.resolve() for path in writable_file_roots]

    def _allowed_file(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(
            resolved == root or root in resolved.parents for root in self._writable_file_roots
        )

    def run(self, argv: list[str], *, timeout_seconds: float) -> CommandResult:
        started = time.monotonic()
        if not argv or argv[0] not in self._allowed_executables:
            return CommandResult(
                status=OperationStatus.FAILED,
                stderr="command is not in the explicit runner allowlist",
                elapsed_seconds=time.monotonic() - started,
            )
        try:
            if argv[0] == "read-file":
                if len(argv) != 2:
                    raise ValueError("read-file requires exactly one path")
                output = Path(argv[1]).read_text(encoding="utf-8")
                return CommandResult(
                    status=OperationStatus.SUCCEEDED,
                    exit_code=0,
                    stdout=output,
                    elapsed_seconds=time.monotonic() - started,
                )
            if argv[0] == "write-file":
                if len(argv) != 3:
                    raise ValueError("write-file requires a path and value")
                target = Path(argv[1])
                if not self._allowed_file(target):
                    raise PermissionError("write-file target is outside explicit writable roots")
                target.write_bytes(argv[2].encode("utf-8"))
                return CommandResult(
                    status=OperationStatus.SUCCEEDED,
                    exit_code=0,
                    elapsed_seconds=time.monotonic() - started,
                )
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            return CommandResult(
                status=OperationStatus.TIMEOUT,
                stdout=str(error.stdout or ""),
                stderr=str(error.stderr or "command timed out"),
                elapsed_seconds=time.monotonic() - started,
            )
        except PermissionError as error:
            return CommandResult(
                status=OperationStatus.PERMISSION_DENIED,
                stderr=str(error),
                elapsed_seconds=time.monotonic() - started,
            )
        except FileNotFoundError as error:
            return CommandResult(
                status=OperationStatus.UNAVAILABLE,
                stderr=str(error),
                elapsed_seconds=time.monotonic() - started,
            )
        except (OSError, UnicodeError, ValueError) as error:
            return CommandResult(
                status=OperationStatus.FAILED,
                stderr=str(error),
                elapsed_seconds=time.monotonic() - started,
            )
        return CommandResult(
            status=(
                OperationStatus.SUCCEEDED if completed.returncode == 0 else OperationStatus.FAILED
            ),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            elapsed_seconds=time.monotonic() - started,
        )


__all__ = ["SubprocessCommandRunner"]
