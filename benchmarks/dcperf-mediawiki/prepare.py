#!/usr/bin/env python3
"""Provision the pinned DCPerf MediaWiki appliance on a target host.

The provisioner is intentionally fail-closed: it validates the operating system,
architecture, privilege boundary, and systemd before installing anything. All
large upstream inputs are downloaded into the Worker dependency cache and are
verified before extraction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

SOURCE_REVISION = "9308c3e3c404e0466f0a2929f15ddcf62b2215f6"
EXPECTED_PLATFORM = {"ID": "ubuntu", "ARCH": "x86_64"}
SUPPORTED_UBUNTU_RELEASES = {"22.04", "24.04"}
HHVM_BIN = Path("/usr/local/hphpi/legacy/bin/hhvm")
HHVM_LIB = Path("/opt/local/hhvm-3.30/lib")
MARKER_NAME = "dcperf-mediawiki-ready.json"
DOWNLOAD_BLOCK_BYTES = 1024 * 1024
DOWNLOAD_ROUTE_PROBE_SECONDS = 15.0
DOWNLOAD_ROUTE_MIN_MIB_PER_SECOND = 0.25
HHVM_VERSION_PATTERN = re.compile(
    r"(?:HipHop\s+VM|HHVM)\s+(?P<major>\d+)\.(?P<minor>\d+)"
    r"(?:\.(?P<patch>\d+))?",
    re.IGNORECASE,
)


class PrepareError(RuntimeError):
    pass


def log(message: str) -> None:
    print("[dcperf-prepare] " + message, flush=True)


def fail(message: str) -> None:
    raise PrepareError(message)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 3600,
) -> subprocess.CompletedProcess[str]:
    log("$ " + " ".join(command))
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        fail(f"command failed to start or timed out: {command[0]}: {error}")
    if completed.stdout:
        print(completed.stdout.rstrip(), flush=True)
    if completed.returncode != 0:
        if completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr, flush=True)
        fail(f"command exited {completed.returncode}: {' '.join(command)}")
    return completed


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    path = Path("/etc/os-release")
    if not path.is_file():
        fail("/etc/os-release is missing")
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def check_host() -> None:
    if tuple(sys.version_info[:2]) < (3, 10):
        fail(
            "unsupported Python runtime: expected >=3.10, got "
            f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
        )
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        fail(
            "managed DCPerf provisioning requires root so it can install system packages "
            "and services"
        )
    release = read_os_release()
    if release.get("ID") != EXPECTED_PLATFORM["ID"]:
        fail(f"unsupported distribution: expected Ubuntu, got {release.get('ID', 'unknown')}")
    release_id = release.get("VERSION_ID", "")
    if release_id not in SUPPORTED_UBUNTU_RELEASES:
        fail(
            "unsupported Ubuntu release: expected one of "
            f"{sorted(SUPPORTED_UBUNTU_RELEASES)}, got {release_id or 'unknown'}"
        )
    architecture = os.uname().machine if hasattr(os, "uname") else "unknown"
    if architecture not in {"x86_64", "amd64"}:
        fail(f"unsupported architecture: expected x86_64, got {architecture}")
    if not Path("/run/systemd/system").is_dir():
        fail("systemd is required for the pinned DCPerf service lifecycle")
    for command in ("apt-get", "systemctl", "tar"):
        if shutil.which(command) is None:
            fail(f"required host command is missing: {command}")


def load_lock(root: Path) -> dict[str, Any]:
    path = root / "dependency-lock.json"
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot read dependency lock: {error}")
    if lock.get("schemaVersion") != "looper.dependency-lock/v1":
        fail("unsupported dependency lock schema")
    platform = lock.get("platform", {})
    supported_releases = set(platform.get("compatibleReleases", []))
    if not supported_releases:
        supported_releases = {str(platform.get("release", ""))}
    if not supported_releases >= SUPPORTED_UBUNTU_RELEASES:
        fail("dependency lock does not cover all supported Ubuntu releases")
    assets = lock.get("assets")
    if not isinstance(assets, list) or not assets:
        fail("dependency lock does not declare assets")
    return lock


def asset(lock: dict[str, Any], asset_id: str) -> dict[str, Any]:
    for item in lock["assets"]:
        if isinstance(item, dict) and item.get("id") == asset_id:
            return item
    fail(f"dependency lock has no asset {asset_id}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(DOWNLOAD_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(asset_spec: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    safe_id = str(asset_spec["id"]).replace("/", "_")
    destination = directory / (safe_id + ".download")
    temporary = destination.with_suffix(".part")
    expected = str(asset_spec["sha256"]).removeprefix("sha256:")
    declared = asset_spec.get("bytes")
    expected_bytes = int(declared) if declared is not None else None
    if destination.is_file() and sha256_file(destination) == expected:
        log(f"reusing verified asset {asset_spec['id']}")
        return destination
    if destination.exists():
        destination.unlink()

    offset = temporary.stat().st_size if temporary.is_file() else 0
    if offset:
        log(f"resuming {asset_spec['id']} from {offset / (1024 * 1024):.1f} MiB")
    else:
        log(f"downloading {asset_spec['id']}")
    source_urls = [
        *(str(item) for item in asset_spec.get("mirrors", [])),
        str(asset_spec["url"]),
    ]
    route_errors: list[str] = []
    completed = False
    total = offset
    for route_number, source_url in enumerate(source_urls, start=1):
        headers = {"User-Agent": "Looper-DCPerf-provisioner/2"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(source_url, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=180)
            log(f"using download route {route_number}/{len(source_urls)}")
        except Exception as error:
            route_errors.append(str(error))
            log(f"download route {route_number}/{len(source_urls)} unavailable: {error}")
            continue
        try:
            with response:
                resumed = offset > 0 and getattr(response, "status", 200) == 206
                if offset and not resumed:
                    log("upstream does not support range requests; restarting this asset")
                    offset = 0
                total = offset
                started = time.monotonic()
                last_report = started
                mode = "ab" if resumed else "wb"
                with temporary.open(mode) as stream:
                    while True:
                        block = response.read(DOWNLOAD_BLOCK_BYTES)
                        if not block:
                            completed = True
                            break
                        stream.write(block)
                        stream.flush()
                        total += len(block)
                        now = time.monotonic()
                        elapsed = max(now - started, 0.001)
                        speed = (total - offset) / elapsed / (1024 * 1024)
                        if now - last_report >= 5:
                            downloaded = total / (1024 * 1024)
                            if expected_bytes:
                                percent = min(100.0, total * 100 / expected_bytes)
                                log(
                                    f"downloading {asset_spec['id']}: {downloaded:.1f} MiB / "
                                    f"{expected_bytes / (1024 * 1024):.1f} MiB "
                                    f"({percent:.1f}%) at {speed:.2f} MiB/s"
                                )
                            else:
                                log(
                                    f"downloading {asset_spec['id']}: {downloaded:.1f} MiB "
                                    f"at {speed:.2f} MiB/s"
                                )
                            last_report = now
                        if (
                            route_number < len(source_urls)
                            and elapsed >= DOWNLOAD_ROUTE_PROBE_SECONDS
                            and speed < DOWNLOAD_ROUTE_MIN_MIB_PER_SECOND
                        ):
                            log(
                                f"download route {route_number}/{len(source_urls)} is slow "
                                f"({speed:.2f} MiB/s); trying the next route"
                            )
                            completed = False
                            break
        except Exception as error:
            route_errors.append(str(error))
            log(
                f"download route {route_number}/{len(source_urls)} interrupted: {error}; "
                "trying the next route"
            )
        offset = temporary.stat().st_size if temporary.exists() else 0
        if completed and (expected_bytes is None or total == expected_bytes):
            break
        if completed and expected_bytes is not None and total != expected_bytes:
            route_errors.append(f"route ended after {total} of {expected_bytes} bytes")
            completed = False
    if not completed:
        saved = temporary.stat().st_size if temporary.exists() else 0
        fail(
            f"all download routes failed for {asset_spec['id']} after "
            f"{saved / (1024 * 1024):.1f} MiB; next run will resume: "
            f"{'; '.join(route_errors) or 'no route completed the asset'}"
        )
    if expected_bytes is not None and total != expected_bytes:
        if total > expected_bytes:
            temporary.unlink(missing_ok=True)
        fail(
            f"incomplete download for {asset_spec['id']}: expected {expected_bytes}, "
            f"got {total}; next run will resume"
        )
    actual = sha256_file(temporary)
    if actual != expected:
        temporary.unlink(missing_ok=True)
        fail(f"checksum mismatch for {asset_spec['id']}: expected {expected}, got {actual}")
    temporary.replace(destination)
    log(f"verified download {asset_spec['id']} ({total / (1024 * 1024):.1f} MiB)")
    return destination


def safe_member_path(destination: Path, member_name: str) -> Path:
    relative = Path(member_name)
    if relative.is_absolute() or ".." in relative.parts:
        fail(f"unsafe archive member: {member_name}")
    candidate = (destination / relative).resolve()
    base = destination.resolve()
    if candidate != base and base not in candidate.parents:
        fail(f"archive member escapes destination: {member_name}")
    return candidate


def extract_archive(archive: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, mode="r:*") as stream:
            members = stream.getmembers()
            if not members:
                fail(f"archive is empty: {archive}")
            root_prefix = members[0].name.split("/", 1)[0]
            for member in members:
                name = member.name
                if name == root_prefix:
                    continue
                if name.startswith(root_prefix + "/"):
                    name = name[len(root_prefix) + 1 :]
                if not name:
                    continue
                target = safe_member_path(destination, name)
                if member.issym():
                    link_name = str(member.linkname)
                    link_target = (target.parent / link_name).resolve()
                    base = destination.resolve()
                    if (
                        not link_name
                        or Path(link_name).is_absolute()
                        or link_target != base and base not in link_target.parents
                    ):
                        fail(f"unsafe symbolic link in dependency archive: {member.name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(link_name)
                    continue
                if member.islnk():
                    fail(f"hard links are not accepted in dependency archive: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = stream.extractfile(member)
                if source is None:
                    fail(f"cannot read archive member: {member.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(target, member.mode & 0o777 or 0o644)
    except (OSError, tarfile.TarError) as error:
        fail(f"cannot extract {archive.name}: {error}")


def find_one(root: Path, name: str) -> Path | None:
    for candidate in root.rglob(name):
        if candidate.is_file():
            return candidate
    return None


def apply_patch(patch: Path, directory: Path) -> None:
    # git apply handles the upstream patches' rename and mode metadata; the
    # fallback keeps the package usable on hosts that only ship patch(1).
    git = shutil.which("git")
    if git:
        check = subprocess.run(
            [git, "-C", str(directory), "apply", "--check", "--unsafe-paths", str(patch)],
            text=True,
            capture_output=True,
            check=False,
        )
        if check.returncode == 0:
            run([git, "-C", str(directory), "apply", "--unsafe-paths", str(patch)])
            return
    patch_command = shutil.which("patch")
    if patch_command is None:
        fail(f"cannot apply patch without git or patch: {patch}")
    run([patch_command, "-p1", "--batch", "--forward", "-i", str(patch)], cwd=directory)


def install_system_packages(lock: dict[str, Any], marker: Path) -> None:
    required_commands = ("apt-get", "make", "gcc", "mysql", "php", "sysctl", "perf")
    if marker.is_file() and all(shutil.which(command) for command in required_commands):
        log("verified required system commands are already installed")
        return
    packages = [str(item) for item in lock.get("apt", {}).get("packages", [])]
    if not packages:
        fail("dependency lock contains no apt package list")
    run(["apt-get", "update"], timeout=1800)
    run(["apt-get", "install", "-y", "--no-install-recommends", *packages], timeout=3600)


def hhvm_version() -> str:
    if not HHVM_BIN.is_file() or not os.access(HHVM_BIN, os.X_OK):
        return ""
    try:
        environment = os.environ.copy()
        environment["LD_LIBRARY_PATH"] = (
            str(HHVM_LIB)
            + (":" + environment["LD_LIBRARY_PATH"] if environment.get("LD_LIBRARY_PATH") else "")
        )
        completed = subprocess.run(
            [str(HHVM_BIN), "--version"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or completed.stderr or "").splitlines()[0].strip()


def parse_hhvm_version(output: str) -> tuple[int, int, int | None] | None:
    match = HHVM_VERSION_PATTERN.search(output.strip())
    if match is None:
        return None
    patch = match.group("patch")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(patch) if patch is not None else None,
    )


def is_expected_hhvm_version(output: str) -> bool:
    version = parse_hhvm_version(output)
    return version is not None and version[:2] == (3, 30)


def install_hhvm(archive: Path) -> None:
    existing_version = hhvm_version()
    if is_expected_hhvm_version(existing_version):
        log(f"verified existing HHVM: {existing_version}")
        return
    if HHVM_BIN.exists():
        log("existing HHVM executable is not the pinned 3.30 build; reinstalling")
    stage = Path(tempfile.mkdtemp(prefix="looper-hhvm-"))
    try:
        log("extracting hhvm-3.30 archive")
        extract_archive(archive, stage)
        log("HHVM archive extracted; locating installer")
        installer = find_one(stage, "pour-hhvm.sh")
        if installer is not None:
            log("installing HHVM with pour-hhvm.sh")
            run(["bash", str(installer)], cwd=installer.parent, timeout=3600)
        else:
            installer = find_one(stage, "install.sh")
            if installer is not None:
                log("installing HHVM with install.sh")
                run(["bash", str(installer)], cwd=installer.parent, timeout=3600)
        if not HHVM_BIN.is_file():
            candidate = find_one(stage, "hhvm")
            if candidate is None:
                fail("HHVM archive did not provide an installer or hhvm executable")
            HHVM_BIN.parent.mkdir(parents=True, exist_ok=True)
            HHVM_BIN.unlink(missing_ok=True)
            shutil.copy2(candidate, HHVM_BIN)
            HHVM_BIN.chmod(0o755)
        if not HHVM_LIB.is_dir():
            library_root = next((item for item in stage.rglob("lib") if item.is_dir()), None)
            if library_root is not None:
                HHVM_LIB.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(library_root, HHVM_LIB, dirs_exist_ok=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    first_line = hhvm_version()
    if not is_expected_hhvm_version(first_line):
        fail(f"unexpected HHVM version: {first_line or 'unavailable'}")


def configure_database(dcperf_root: Path) -> None:
    update_password = dcperf_root / "packages/mediawiki/update_mariadb_pwd.sql"
    grants = dcperf_root / "packages/mediawiki/grant_privileges.sql"
    if not update_password.is_file() or not grants.is_file():
        fail("pinned DCPerf MariaDB setup scripts are missing")
    run(["systemctl", "enable", "--now", "mariadb"], timeout=180)
    probe = subprocess.run(
        ["mysql", "-u", "root", "--password=password", "-e", ";"],
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        run(
            ["bash", "-c", f"mysql -u root --password='' < {shlex_quote(str(update_password))}"],
            timeout=180,
        )
    run(
        ["bash", "-c", f"mysql -u root --password=password < {shlex_quote(str(grants))}"],
        timeout=180,
    )


def configure_mediawiki_measurement_hook(dcperf_root: Path) -> None:
    """Keep the pinned RecentChanges request valid with the 2014 fixture DB."""

    hook = dcperf_root / "packages/mediawiki/perf-record.sh"
    if not hook.is_file():
        fail(f"pinned DCPerf perf-record hook is missing: {hook}")
    hook.write_text(
        """#!/bin/bash
set -e

# The pinned MediaWiki fixture contains recentchanges rows from 2014. Refresh
# only their timestamps after workload warmup so Special:RecentChanges remains
# a successful upstream request without changing the URL mix or row count.
mysql --user=root --password=password mw_bench \\
  --execute="UPDATE recentchanges SET rc_timestamp = DATE_FORMAT(UTC_TIMESTAMP(), '%Y%m%d%H%i%s');"

sleep 30
if [ -f "perf.data" ]; then
  exit 0
fi
if [ "${DCPERF_PERF_RECORD}" = "1" ]; then
  sudo nohup bash -xec "sleep 30; timeout -s INT 5 perf record -a -g;" \\
    > /tmp/mw-perf-record.log 2>&1 &
fi
""",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def configure_mediawiki_recentchanges_compatibility(dcperf_root: Path) -> None:
    """Avoid HHVM repo-authoritative evals while retaining RecentChanges rows."""

    settings = dcperf_root / "oss-performance/targets/mediawiki/LocalSettings.php"
    if not settings.is_file():
        fail(f"pinned MediaWiki LocalSettings source is missing: {settings}")
    marker = "// Looper: use the non-eval RecentChanges renderer under Repo.Authoritative."
    content = settings.read_text(encoding="utf-8")
    if marker not in content:
        settings.write_text(
            content.rstrip()
            + "\n\n"
            + marker
            + "\n$wgDefaultUserOptions['usenewrc'] = 0;\n",
            encoding="utf-8",
        )


def configure_mediawiki_runtime(dcperf_root: Path) -> None:
    configure_mediawiki_measurement_hook(dcperf_root)
    configure_mediawiki_recentchanges_compatibility(dcperf_root)


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'''") + "'"


def build_dependencies(lock: dict[str, Any], cache: Path) -> Path:
    assets_dir = cache / "assets"
    dcperf_archive = fetch(asset(lock, "dcperf-source"), assets_dir)
    oss_archive = fetch(asset(lock, "oss-performance-source"), assets_dir)
    wrk_archive = fetch(asset(lock, "wrk-4.2.0"), assets_dir)
    memcached_archive = fetch(asset(lock, "memcached-1.5.12"), assets_dir)
    composer_archive = fetch(asset(lock, "composer-2.2.24"), assets_dir)

    runtime = cache / "runtime"
    dcperf_root = runtime / "dcperf"
    extract_archive(dcperf_archive, dcperf_root)
    benchmark_root = dcperf_root / "benchmarks/oss_performance_mediawiki"
    benchmark_root.mkdir(parents=True, exist_ok=True)
    oss_root = dcperf_root / "oss-performance"
    extract_archive(oss_archive, oss_root)

    wrk_root = benchmark_root / "wrk"
    extract_archive(wrk_archive, wrk_root)
    apply_patch(dcperf_root / "packages/mediawiki/0004-wrk.diff", wrk_root)
    run(["make", "-j", str(max(1, os.cpu_count() or 1))], cwd=wrk_root, timeout=1800)

    memcached_root = dcperf_root / "memcached-1.5.12"
    extract_archive(memcached_archive, memcached_root)
    apply_patch(
        dcperf_root / "packages/mediawiki/0002-memcached-centos9-compat.diff", memcached_root
    )
    apply_patch(dcperf_root / "packages/mediawiki/0003-memcached-signal.diff", memcached_root)
    run(["./autogen.sh"], cwd=memcached_root, timeout=600)
    run(["./configure", "--prefix=/usr/local/memcached"], cwd=memcached_root, timeout=600)
    run(["make", "-j", str(max(1, os.cpu_count() or 1))], cwd=memcached_root, timeout=1800)
    run(["make", "install"], cwd=memcached_root, timeout=600)

    apply_patch(
        dcperf_root / "packages/mediawiki/0001-oss-performance-scalable-hhvm.diff", oss_root
    )
    for source_name, destination in (
        ("Wrk.php", oss_root / "base/Wrk.php"),
        ("WrkStats.php", oss_root / "base/WrkStats.php"),
        ("multi-request-txt.lua", oss_root / "scripts/multi-request-txt.lua"),
    ):
        source = dcperf_root / "packages/mediawiki" / source_name
        if not source.is_file():
            fail(f"pinned DCPerf support file is missing: {source_name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copy2(composer_archive, oss_root / "composer.phar")
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(HHVM_LIB) + ":" + env.get("LD_LIBRARY_PATH", "")
    composer_home = oss_root / ".composer-home"
    composer_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(composer_home)
    env["COMPOSER_HOME"] = str(composer_home)
    run(
        [str(HHVM_BIN), "composer.phar", "install", "--no-interaction", "--prefer-dist"],
        cwd=oss_root,
        env=env,
        timeout=3600,
    )
    configure_database(dcperf_root)
    run(["sysctl", "-w", "net.ipv4.tcp_tw_reuse=1"], timeout=60)
    (dcperf_root / "benchmark_installs.txt").write_text(
        "./packages/mediawiki/install_oss_performance_mediawiki.sh\n",
        encoding="utf-8",
    )
    return dcperf_root


def prepared_cache_valid(cache: Path) -> bool:
    marker = cache / MARKER_NAME
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    root = Path(str(state.get("root", "")))
    return bool(
        state.get("schemaVersion") == "looper.dcperf.prepare/v1"
        and state.get("sourceRevision") == SOURCE_REVISION
        and root.is_dir()
        and (cache / "runtime/dcperf/benchpress_cli.py").is_file()
        and (cache / "runtime/dcperf/benchmarks/oss_performance_mediawiki/wrk/wrk").is_file()
        and is_expected_hhvm_version(hhvm_version())
    )


def write_marker(cache: Path, dcperf_root: Path, lock: dict[str, Any]) -> None:
    marker = cache / MARKER_NAME
    marker.write_text(
        json.dumps(
            {
                "schemaVersion": "looper.dcperf.prepare/v1",
                "sourceRevision": SOURCE_REVISION,
                "preparedAt": time.time(),
                "root": str(dcperf_root),
                "hhvm": str(HHVM_BIN),
                "lockAssets": [item["id"] for item in lock["assets"]],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        check_host()
        lock = load_lock(arguments.benchmark_root.resolve())
        cache = arguments.cache.resolve()
        cache.mkdir(parents=True, exist_ok=True)
        marker = cache / MARKER_NAME
        if prepared_cache_valid(cache):
            configure_mediawiki_runtime(cache / "runtime/dcperf")
            log("verified managed DCPerf environment is already prepared")
            return 0
        install_system_packages(lock, marker)
        install_hhvm(fetch(asset(lock, "hhvm-3.30"), cache / "assets"))
        dcperf_root = build_dependencies(lock, cache)
        configure_mediawiki_runtime(dcperf_root)
        run(
            [sys.executable, "-m", "compileall", "-q", str(dcperf_root / "benchpress")], timeout=300
        )
        write_marker(cache, dcperf_root, lock)
        log("managed DCPerf environment is ready")
        return 0
    except PrepareError as error:
        print(f"[dcperf-prepare] ERROR: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
