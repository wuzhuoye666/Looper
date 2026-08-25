"""Idempotently prepare sysbench on a clean Linux target."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path


class PrepareError(RuntimeError):
    pass


APT_LOCK_MARKERS = (
    "Could not get lock /var/lib/dpkg/lock-frontend",
    "Could not get lock /var/lib/dpkg/lock",
    "Unable to acquire the dpkg frontend lock",
)
SUPPORTED_VERSION_PATTERN = re.compile(r"^sysbench\s+1\.0\.(\d+)(?:\s|$)", re.MULTILINE)


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


def _run_package_manager(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Wait through a transient unattended-upgrades dpkg lock."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _run(argv, timeout=min(60, max(1, int(deadline - time.monotonic()))))
        except PrepareError as error:
            if not any(marker in str(error) for marker in APT_LOCK_MARKERS):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(5, remaining))


def _sysbench_version(binary: str) -> str:
    output = _run([binary, "--version"], timeout=30).stdout.strip()
    if not SUPPORTED_VERSION_PATTERN.search(output):
        raise PrepareError(f"expected sysbench 1.0.x, received: {output or 'no version'}")
    return output


def _find_supported_binary(candidates: list[str | Path]) -> tuple[str, str] | None:
    """Return the first executable that satisfies the manifest's 1.0.x pin.

    A developer may have a source-built 1.1.x binary earlier in ``PATH`` (the
    common case is ``/usr/local/bin/sysbench``).  Never silently use it: the
    1.1 output format is not the contract parsed by this adapter.  The caller
    can install the distro's 1.0.x package and retry the candidate list.
    """

    seen: set[str] = set()
    diagnostics: list[str] = []
    for candidate in candidates:
        path = Path(candidate).expanduser()
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        try:
            version = _sysbench_version(str(path))
        except (OSError, PrepareError) as error:
            diagnostics.append(f"{path}: {error}")
            continue
        return str(path.resolve()), version
    if diagnostics:
        raise PrepareError(
            "no supported sysbench 1.0.x executable found; "
            + "; ".join(diagnostics)
        )
    return None


def _install_sysbench() -> tuple[str, str]:
    apt = shutil.which("apt-get")
    if not apt:
        raise PrepareError(
            "sysbench 1.0.x is not available and apt-get was not found; "
            "remove the incompatible sysbench or install a supported 1.0.x binary"
        )
    _run_package_manager(_privileged([apt, "update", "-qq"]), timeout=300)
    _run_package_manager(
        _privileged(
            [
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                apt,
                "install",
                "-y",
                "-qq",
                "sysbench",
            ]
        ),
        timeout=600,
    )
    # Prefer the package-managed location over a stale source build that is
    # still earlier in PATH.  The explicit PATH lookup is a fallback for
    # distributions that install into a different bin directory.
    candidates: list[str | Path] = [
        "/usr/bin/sysbench",
        "/bin/sysbench",
    ]
    found = shutil.which("sysbench")
    if found:
        candidates.append(found)
    selected = _find_supported_binary(candidates)
    if selected is None:
        raise PrepareError("sysbench installation completed without a supported 1.0.x executable")
    return selected


def prepare(cache: Path) -> dict[str, str]:
    if platform.system().lower() != "linux":
        raise PrepareError("the managed sysbench package currently supports Linux targets")
    cache.mkdir(parents=True, exist_ok=True)
    found = shutil.which("sysbench")
    try:
        selected = _find_supported_binary([found] if found else [])
    except PrepareError:
        # A stale source build (usually 1.1.x) is not fatal by itself; install
        # the managed distro package and select that binary explicitly below.
        selected = None
    if selected is None:
        selected = _install_sysbench()
    binary, version = selected

    # The run command receives this cache-local path, so a later PATH change
    # (or another developer's /usr/local/bin installation) cannot change the
    # executable after preparation has succeeded.
    run_dir = cache / "bin"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_binary = run_dir / "sysbench"
    run_binary.unlink(missing_ok=True)
    try:
        run_binary.symlink_to(Path(binary))
    except OSError:
        shutil.copy2(binary, run_binary)
        run_binary.chmod(run_binary.stat().st_mode | 0o111)

    result = {
        "binary": binary,
        "runBinary": str(run_binary.resolve()),
        "version": version,
    }
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
