"""Idempotently prepare sysbench on a clean Linux target."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path


class PrepareError(RuntimeError):
    pass


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or completed.stdout).split())[-1200:]
        raise PrepareError(f"{' '.join(argv)} failed: {detail or completed.returncode}")
    return completed


def _privileged(argv: list[str]) -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return argv
    if shutil.which("sudo"):
        return ["sudo", "-n", *argv]
    raise PrepareError("installing sysbench requires root or passwordless sudo")


def _sysbench_version(binary: str) -> str:
    output = _run([binary, "--version"], timeout=30).stdout.strip()
    if "1.0" not in output:
        raise PrepareError(f"expected sysbench 1.0.x, received: {output or 'no version'}")
    return output


def prepare(cache: Path) -> dict[str, str]:
    if platform.system().lower() != "linux":
        raise PrepareError("the managed sysbench package currently supports Linux targets")
    cache.mkdir(parents=True, exist_ok=True)
    binary = shutil.which("sysbench")
    if binary is None:
        if not shutil.which("apt-get"):
            raise PrepareError("sysbench is missing and no supported package manager was found")
        _run(_privileged(["apt-get", "update", "-qq"]), timeout=300)
        _run(
            _privileged([
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "apt-get",
                "install",
                "-y",
                "-qq",
                "sysbench",
            ]),
            timeout=600,
        )
        binary = shutil.which("sysbench")
    if binary is None:
        raise PrepareError("sysbench installation completed without an executable")
    result = {"binary": str(Path(binary).resolve()), "version": _sysbench_version(binary)}
    (cache / "prepared.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", required=True)
    args = parser.parse_args()
    result = prepare(Path(args.cache))
    print(f"prepared {result['version']} at {result['binary']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PrepareError, subprocess.TimeoutExpired) as error:
        print(f"sysbench prepare failed: {error}")
        raise SystemExit(2) from None
