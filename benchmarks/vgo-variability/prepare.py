from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

LOCK_SCHEMA = "looper.vgo-source-lock/v1"
MARKER_SCHEMA = "looper.vgo-provisioning-marker/v1"
PACKAGE_MANAGER_LOCKS = (
    "/var/lib/dpkg/lock-frontend",
    "/var/lib/dpkg/lock",
    "/var/lib/apt/lists/lock",
    "/var/cache/apt/archives/lock",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def load_source_lock(benchmark_root: Path) -> dict[str, Any]:
    lock_path = benchmark_root / "source-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("schemaVersion") != LOCK_SCHEMA:
        raise RuntimeError("the VGO source lock has an unsupported schemaVersion")
    if not isinstance(lock.get("files"), dict) or not lock["files"]:
        raise RuntimeError("the VGO source lock does not declare its source files")
    return lock


def verify_source_archive(benchmark_root: Path) -> tuple[Path, dict[str, Any]]:
    lock = load_source_lock(benchmark_root)
    archive = benchmark_root / str(lock["archive"]["path"])
    if not archive.is_file():
        raise RuntimeError(f"the bundled VGO source archive is missing: {archive}")
    observed = sha256(archive)
    if observed != lock["archive"]["digest"]:
        raise RuntimeError(
            f"the bundled VGO source archive digest changed: expected "
            f"{lock['archive']['digest']}, observed {observed}"
        )
    return archive, lock


def extract_verified_source(archive_path: Path, lock: dict[str, Any], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if not members or len(members) > 128:
            raise RuntimeError("the VGO source archive has an invalid file count")
        for member in members:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"unsafe VGO source path: {member.name}")
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise RuntimeError(f"unsupported VGO source entry: {member.name}")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"could not read VGO source entry: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                shutil.copyfileobj(source, output)

    expected_files = {str(name): str(digest) for name, digest in lock["files"].items()}
    for name, expected in expected_files.items():
        path = destination / Path(PurePosixPath(name).as_posix())
        if not path.is_file():
            raise RuntimeError(f"the VGO source snapshot is missing {name}")
        observed = sha256(path)
        if observed != expected:
            raise RuntimeError(
                f"the VGO source snapshot digest changed for {name}: "
                f"expected {expected}, observed {observed}"
            )


def run_stage(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    allowed: set[int] | None = None,
    retry_codes: set[int] | None = None,
    max_attempts: int = 1,
    retry_delay_seconds: float = 15.0,
) -> int:
    allowed = allowed or {0}
    retry_codes = retry_codes or set()
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    for attempt in range(1, max_attempts + 1):
        print(
            f"[vgo-prepare] running ({attempt}/{max_attempts}): {' '.join(command)}",
            flush=True,
        )
        completed = subprocess.run(command, cwd=cwd, check=False, timeout=timeout)
        if completed.returncode in allowed:
            return completed.returncode
        if completed.returncode in retry_codes and attempt < max_attempts:
            print(
                f"[vgo-prepare] stage returned {completed.returncode}; "
                f"waiting {retry_delay_seconds:g}s for the package manager and retrying",
                flush=True,
            )
            time.sleep(retry_delay_seconds)
            continue
        raise RuntimeError(
            f"VGO preparation stage exited with code {completed.returncode}: {' '.join(command)}"
        )
    raise AssertionError("unreachable preparation retry state")


def wait_for_package_manager(
    *, timeout_seconds: float = 1800.0, poll_seconds: float = 10.0
) -> None:
    fuser = shutil.which("fuser")
    if fuser is None:
        print(
            "[vgo-prepare] fuser is unavailable; relying on bounded apt retry handling",
            flush=True,
        )
        return
    deadline = time.monotonic() + timeout_seconds
    while True:
        completed = subprocess.run(
            ["sudo", "-n", fuser, *PACKAGE_MANAGER_LOCKS],
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode != 0:
            print("[vgo-prepare] package manager locks are available", flush=True)
            return
        now = time.monotonic()
        if now >= deadline:
            holders = f"{completed.stdout}\n{completed.stderr}".strip()
            raise RuntimeError(
                "timed out waiting for Ubuntu package manager locks; "
                f"holders: {holders or 'unknown'}"
            )
        holders = f"{completed.stdout}\n{completed.stderr}".strip().replace("\n", " ")
        remaining = max(0.0, deadline - now)
        delay = min(poll_seconds, remaining)
        print(
            f"[vgo-prepare] Ubuntu package manager is busy ({holders or 'holder unknown'}); "
            f"waiting {delay:g}s",
            flush=True,
        )
        time.sleep(delay)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision the bundled VGO reproduction scripts")
    parser.add_argument("--benchmark-root", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    benchmark_root = args.benchmark_root.resolve()
    archive, lock = verify_source_archive(benchmark_root)
    if args.verify_only:
        temporary = benchmark_root / ".verify-vgo-source"
        if temporary.exists():
            raise RuntimeError(f"verification directory already exists: {temporary}")
        try:
            extract_verified_source(archive, lock, temporary)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        print(json.dumps({"verified": True, "digest": sha256(archive)}, sort_keys=True))
        return 0

    if os.name != "posix" or not Path("/etc/os-release").is_file():
        raise RuntimeError("the VGO Benchmark can only be provisioned on an Ubuntu Linux target")
    for executable in ("bash", "sudo", "python3"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"required host executable is unavailable: {executable}")
    if subprocess.run(["sudo", "-n", "true"], check=False).returncode != 0:
        raise RuntimeError("the VGO Benchmark requires passwordless non-interactive sudo")

    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    source_root = cache / "vgo-source"
    marker_path = cache / "prepared.json"
    archive_digest = sha256(archive)
    if marker_path.is_file() and source_root.is_dir():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if (
            marker.get("schemaVersion") == MARKER_SCHEMA
            and marker.get("sourceDigest") == archive_digest
            and (source_root / "scripts" / "run_case.sh").is_file()
            and (source_root / "config" / "matmul_calibration.json").is_file()
        ):
            print(json.dumps(marker, sort_keys=True))
            return 0

    stage = cache / ".vgo-source-stage"
    if stage.exists():
        shutil.rmtree(stage)
    try:
        extract_verified_source(archive, lock, stage)
        run_stage(["bash", "scripts/check_environment.sh"], cwd=stage, timeout=300)
        wait_for_package_manager()
        run_stage(
            ["bash", "scripts/setup_ubuntu.sh"],
            cwd=stage,
            timeout=7200,
            retry_codes={100},
            max_attempts=3,
            retry_delay_seconds=15,
        )
        run_stage(
            ["bash", "scripts/validate_machine.sh"],
            cwd=stage,
            timeout=600,
            allowed={0, 2},
        )
        run_stage(
            [
                str(stage / ".venv" / "bin" / "python"),
                "benchmarks/matmul/calibrate.py",
                "--root",
                str(stage),
                "--reuse",
            ],
            cwd=stage,
            timeout=900,
        )
        gate = read_env(stage / "data" / "metadata" / "gate.env")
        if gate.get("VGO_PARTIAL_GO") != "1":
            raise RuntimeError(
                "the VGO machine gate did not pass; this target cannot execute the original "
                "baseline workloads reliably"
            )
        machine_gate = "full" if gate.get("VGO_FULL_GO") == "1" else "partial"
        if source_root.exists():
            shutil.rmtree(source_root)
        stage.replace(source_root)
        marker = {
            "schemaVersion": MARKER_SCHEMA,
            "benchmark": "looper.vgo.variability",
            "sourceDigest": archive_digest,
            "sourceRoot": str(source_root),
            "machineGate": machine_gate,
            "provides": [
                "vgo-runtime",
                "parboil-2.5",
                "sharp",
                "sharp-3.0.0",
                "p7zip",
                "tcmalloc",
            ],
        }
        marker_path.write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(marker, sort_keys=True))
        return 0
    finally:
        if stage.exists():
            shutil.rmtree(stage)


if __name__ == "__main__":
    raise SystemExit(main())
