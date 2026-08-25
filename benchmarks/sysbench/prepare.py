"""Build and cache the exact pinned Sysbench release on a Linux target."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from versioning import EXPECTED_VERSION_TEXT, PREPARED_SCHEMA, require_expected_version


class PrepareError(RuntimeError):
    pass


APT_LOCK_MARKERS = (
    "Could not get lock /var/lib/dpkg/lock-frontend",
    "Could not get lock /var/lib/dpkg/lock",
    "Unable to acquire the dpkg frontend lock",
)
SOURCE_ASSET_ID = "sysbench-1.0.20-source"
BUILD_PACKAGES = (
    "build-essential",
    "automake",
    "libtool",
    "pkg-config",
    "libaio-dev",
    "ca-certificates",
)


def _run(
    argv: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
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
    try:
        return require_expected_version(output)
    except ValueError as error:
        raise PrepareError(str(error)) from error


def _load_lock(benchmark_root: Path) -> dict[str, Any]:
    lock_path = benchmark_root / "dependency-lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PrepareError(f"cannot read dependency lock: {error}") from error
    if lock.get("schemaVersion") != "looper.dependency-lock/v1":
        raise PrepareError("unsupported dependency lock schema")
    assets = lock.get("assets")
    if not isinstance(assets, list):
        raise PrepareError("dependency lock does not declare assets")
    return lock


def _source_asset(lock: dict[str, Any]) -> dict[str, Any]:
    for item in lock["assets"]:
        if isinstance(item, dict) and item.get("id") == SOURCE_ASSET_ID:
            return item
    raise PrepareError(f"dependency lock has no asset {SOURCE_ASSET_ID}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _verify_asset(path: Path, asset: dict[str, Any]) -> None:
    expected_bytes = int(asset.get("bytes") or 0)
    if expected_bytes <= 0:
        raise PrepareError("source asset does not declare a positive byte count")
    actual_bytes = path.stat().st_size
    if actual_bytes != expected_bytes:
        raise PrepareError(
            f"source asset byte count mismatch: expected {expected_bytes}, got {actual_bytes}"
        )
    expected_digest = str(asset.get("sha256") or "")
    actual_digest = _sha256_file(path)
    if actual_digest != expected_digest:
        raise PrepareError(
            f"source asset digest mismatch: expected {expected_digest}, got {actual_digest}"
        )


def _fetch_source(asset: dict[str, Any], destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{SOURCE_ASSET_ID}.tar.gz"
    if archive.is_file():
        try:
            _verify_asset(archive, asset)
            return archive
        except PrepareError:
            archive.unlink(missing_ok=True)
    temporary = archive.with_suffix(archive.suffix + ".part")
    temporary.unlink(missing_ok=True)
    url = str(asset.get("url") or "")
    if not url:
        raise PrepareError("source asset URL is missing")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Looper-Sysbench/1"})
        with urllib.request.urlopen(request, timeout=180) as response, temporary.open(
            "wb"
        ) as stream:
            shutil.copyfileobj(response, stream, length=1024 * 1024)
        _verify_asset(temporary, asset)
        temporary.replace(archive)
    except (OSError, urllib.error.URLError) as error:
        temporary.unlink(missing_ok=True)
        raise PrepareError(f"cannot download pinned Sysbench source: {error}") from error
    return archive


def _extract_source(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not members:
            raise PrepareError("pinned Sysbench source archive is empty")
        for member in members:
            if member.issym() or member.islnk():
                raise PrepareError(f"source archive contains a link: {member.name}")
            target = (destination / member.name).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise PrepareError(f"unsafe source archive path: {member.name}")
        bundle.extractall(destination)
    roots = [item for item in destination.iterdir() if item.is_dir()]
    if len(roots) != 1:
        raise PrepareError("pinned Sysbench source archive has an unexpected layout")
    return roots[0]


def _write_marker(cache: Path, binary: Path, asset: dict[str, Any], version: str) -> None:
    payload = {
        "schemaVersion": PREPARED_SCHEMA,
        "binary": str(binary.resolve()),
        "version": version,
        "sourceCommit": asset.get("commit"),
        "sourceDigest": asset.get("sha256"),
    }
    temporary = cache / "prepared.json.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(cache / "prepared.json")


def _cached_binary(cache: Path, asset: dict[str, Any]) -> tuple[Path, str] | None:
    marker = cache / "prepared.json"
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        binary = Path(str(state["binary"])).resolve()
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    cache_root = cache.resolve()
    if binary != cache_root and cache_root not in binary.parents:
        return None
    if (
        state.get("schemaVersion") != PREPARED_SCHEMA
        or state.get("sourceCommit") != asset.get("commit")
        or state.get("sourceDigest") != asset.get("sha256")
        or not binary.is_file()
    ):
        return None
    try:
        version = _sysbench_version(str(binary))
    except (OSError, PrepareError, subprocess.TimeoutExpired):
        return None
    return binary, version


def _build_source(cache: Path, archive: Path) -> Path:
    stage = Path(tempfile.mkdtemp(prefix=".sysbench-build-", dir=cache))
    runtime = cache / "runtime" / f"sysbench-{EXPECTED_VERSION_TEXT}"
    try:
        source = _extract_source(archive, stage / "source")
        install_root = stage / "install"
        _run(["./autogen.sh"], cwd=source, timeout=300)
        _run(
            ["./configure", "--without-mysql", f"--prefix={install_root}"],
            cwd=source,
            timeout=300,
        )
        _run(
            ["make", f"-j{max(1, os.cpu_count() or 1)}"],
            cwd=source,
            timeout=1800,
        )
        _run(["make", "install"], cwd=source, timeout=600)
        staged_binary = install_root / "bin" / "sysbench"
        if not staged_binary.is_file():
            raise PrepareError("Sysbench build completed without a cached executable")
        _sysbench_version(str(staged_binary))
        runtime.parent.mkdir(parents=True, exist_ok=True)
        if runtime.exists():
            shutil.rmtree(runtime)
        install_root.replace(runtime)
        return runtime / "bin" / "sysbench"
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def prepare(benchmark_root: Path, cache: Path) -> dict[str, str]:
    if platform.system().lower() != "linux":
        raise PrepareError("the managed sysbench package currently supports Linux targets")
    cache.mkdir(parents=True, exist_ok=True)
    lock = _load_lock(benchmark_root)
    asset = _source_asset(lock)
    cached = _cached_binary(cache, asset)
    if cached is not None:
        binary, version = cached
        return {"binary": str(binary), "version": version}
    if not shutil.which("apt-get"):
        raise PrepareError("building pinned Sysbench requires apt-get")
    _run_package_manager(_privileged(["apt-get", "update", "-qq"]), timeout=600)
    _run_package_manager(
        _privileged(
            [
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "apt-get",
                "install",
                "-y",
                "-qq",
                *BUILD_PACKAGES,
            ]
        ),
        timeout=900,
    )
    archive = _fetch_source(asset, cache / "assets")
    binary = _build_source(cache, archive)
    version = _sysbench_version(str(binary))
    _write_marker(cache, binary, asset, version)
    result = {"binary": str(binary.resolve()), "version": version}
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", required=True)
    parser.add_argument("--cache", required=True)
    args = parser.parse_args()
    result = prepare(Path(args.benchmark_root), Path(args.cache))
    print(f"prepared {result['version']} at {result['binary']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PrepareError, subprocess.TimeoutExpired) as error:
        print(f"sysbench prepare failed: {error}")
        raise SystemExit(2) from None
