#!/usr/bin/env python3
"""Stop DCPerf services and remove transient profiling state."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
from contextlib import suppress
from pathlib import Path


def stop_service(name: str) -> None:
    subprocess.run(
        ["systemctl", "stop", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def stop_matching_processes(fragment: str) -> None:
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command_line = (
                (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
            )
            pid = int(entry.name)
        except (OSError, ValueError):
            continue
        if pid == os.getpid() or fragment not in command_line:
            continue
        with suppress(OSError, ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.parse_args()
    stop_matching_processes("/usr/local/memcached/bin/memcached")
    stop_matching_processes("/usr/local/hphpi/legacy/bin/hhvm")
    stop_service("nginx")
    stop_service("mariadb")
    (Path("/tmp") / "mw-perf-record.log").unlink(missing_ok=True)
    print("[dcperf-cleanup] services and transient processes cleaned", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
