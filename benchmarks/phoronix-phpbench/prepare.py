"""Idempotently prepare the pinned PTS/PHPBench runtime on a clean Linux target."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

PTS_URL = (
    "https://github.com/phoronix-test-suite/phoronix-test-suite/archive/"
    "f977d6e270d5eb9eebfa26d3ca62385c00a547a6.zip"
)
PTS_CODELOAD_URL = (
    "https://codeload.github.com/phoronix-test-suite/phoronix-test-suite/zip/"
    "f977d6e270d5eb9eebfa26d3ca62385c00a547a6"
)
PTS_SHA256 = "9d4b811a1ff4710ac89d19b34ba7ef4188b8ce1bfe61d5d276b0ee689dba1dc4"
PTS_VERSION = "10.8.6"
PAYLOAD_URL = "http://phoronix-test-suite.com/benchmark-files/phpbench-081-patched2.zip"
PAYLOAD_SHA256 = "32503bd4ace0c8429493de864ca48bb16febed867e52b75f4369d7145f797718"
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024

# Some cloud targets have slow or flaky egress to GitHub/codeload. Keep the
# digest-pinned sources ordered so a slow (but not failed) connection can fall
# back to the equivalent codeload endpoint. A mirror can be injected through
# LOOPER_PTS_ARCHIVE_URL / LOOPER_PHPBENCH_PAYLOAD_URL; the SHA-256 digest is
# still enforced, so a mirror serving different bytes fails closed.
PTS_URLS = tuple(
    url
    for url in dict.fromkeys(
        [os.environ.get("LOOPER_PTS_ARCHIVE_URL"), PTS_URL, PTS_CODELOAD_URL]
    )
    if url
)
PAYLOAD_URLS = tuple(
    url
    for url in dict.fromkeys(
        [os.environ.get("LOOPER_PHPBENCH_PAYLOAD_URL"), PAYLOAD_URL]
    )
    if url
)

DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_READ_TIMEOUT = 60
DOWNLOAD_TOTAL_TIMEOUT = 600
DOWNLOAD_CHUNK_BYTES = 128 * 1024
DOWNLOAD_RETRY_DELAYS = (2, 5, 10)


class PreparationError(RuntimeError):
    pass


def _run(argv: list[str], *, timeout: int = 600) -> None:
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )
    if completed.returncode != 0:
        detail = " ".join(completed.stderr.split())[-1000:]
        raise PreparationError(f"command failed ({completed.returncode}): {detail}")


def _ensure_system_packages() -> tuple[str, str]:
    php = shutil.which("php")
    unzip = shutil.which("unzip")
    if php and unzip:
        return php, unzip
    apt = shutil.which("apt-get")
    if not apt:
        raise PreparationError("PHP CLI/unzip are missing and apt-get is unavailable")
    prefix: list[str] = []
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            raise PreparationError("PHP CLI/unzip installation requires root or passwordless sudo")
        prefix = [sudo, "-n"]
    _run([*prefix, apt, "update", "-qq"])
    _run(
        [
            *prefix,
            apt,
            "install",
            "-y",
            "-qq",
            "php-cli",
            "php-xml",
            "unzip",
            "ca-certificates",
        ]
    )
    php = shutil.which("php")
    unzip = shutil.which("unzip")
    if not php or not unzip:
        raise PreparationError("package manager completed but PHP CLI/unzip are still unavailable")
    return php, unzip


def _download_once(url: str, destination: Path, expected_sha256: str) -> None:
    """Stream one digest-pinned download to destination.

    Reads in fixed chunks so a slow-but-steady connection is allowed to finish
    within DOWNLOAD_TOTAL_TIMEOUT while each socket read still respects
    DOWNLOAD_READ_TIMEOUT. A digest mismatch or size overflow is fatal.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "Looper/1 PTS adapter"})
    deadline = time.monotonic() + DOWNLOAD_TOTAL_TIMEOUT
    total = 0
    with (
        urllib.request.urlopen(request, timeout=DOWNLOAD_READ_TIMEOUT) as response,
        destination.open("wb") as stream,
    ):
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"download exceeded {DOWNLOAD_TOTAL_TIMEOUT}s wall-clock: {url}"
                )
            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise PreparationError(
                    f"dependency download exceeded {MAX_DOWNLOAD_BYTES} bytes: {url}"
                )
            stream.write(chunk)
    if total == 0:
        raise PreparationError(f"dependency download returned no bytes: {url}")
    observed = hashlib.sha256(destination.read_bytes()).hexdigest()
    if observed != expected_sha256:
        raise PreparationError(
            f"dependency digest mismatch for {url}: expected {expected_sha256}, got {observed}"
        )


def _download(
    urls: tuple[str, ...] | list[str] | str,
    destination: Path,
    expected_sha256: str,
) -> None:
    """Try each source URL with retries; digest is always enforced."""
    if isinstance(urls, str):
        urls = [urls]
    if (
        destination.is_file()
        and hashlib.sha256(destination.read_bytes()).hexdigest() == expected_sha256
    ):
        print(f"using cached {destination.name} (sha256 ok)", flush=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for url in urls:
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
                try:
                    print(
                        f"downloading {destination.name} from {url} "
                        f"(attempt {attempt}/{DOWNLOAD_ATTEMPTS})",
                        flush=True,
                    )
                    _download_once(url, temporary, expected_sha256)
                    temporary.replace(destination)
                    print(
                        f"downloaded {destination.name} "
                        f"({destination.stat().st_size} bytes)",
                        flush=True,
                    )
                    return
                except PreparationError:
                    temporary.unlink(missing_ok=True)
                    raise
                except (OSError, TimeoutError, urllib.error.URLError) as error:
                    # socket.timeout is a TimeoutError/OSError subclass.
                    last_error = error
                    temporary.unlink(missing_ok=True)
                    if attempt < DOWNLOAD_ATTEMPTS:
                        delay = DOWNLOAD_RETRY_DELAYS[
                            min(attempt - 1, len(DOWNLOAD_RETRY_DELAYS) - 1)
                        ]
                        print(
                            f"download from {url} failed: {error}; retrying in {delay}s",
                            flush=True,
                        )
                        time.sleep(delay)
                    else:
                        print(f"source exhausted: {url}", flush=True)
        except PreparationError:
            raise
    raise PreparationError(f"dependency download failed for {destination.name}: {last_error}")


def _extract_pts(archive_path: Path, destination: Path) -> Path:
    ready = destination / ".ready.json"
    launcher = destination / "phoronix-test-suite"
    if launcher.is_file() and ready.is_file():
        return launcher
    with tempfile.TemporaryDirectory(prefix="pts-extract-", dir=destination.parent) as temporary:
        temporary_root = Path(temporary)
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                    raise PreparationError(f"unsafe PTS archive path: {member.filename}")
                archive.extract(member, temporary_root)
        roots = [item for item in temporary_root.iterdir() if item.is_dir()]
        if len(roots) != 1 or not (roots[0] / "phoronix-test-suite").is_file():
            raise PreparationError("PTS source archive layout is unexpected")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(roots[0]), destination)
    ready.write_text(
        json.dumps({"version": PTS_VERSION, "archiveSha256": PTS_SHA256}),
        encoding="utf-8",
    )
    return launcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare PTS PHPBench dependencies")
    parser.add_argument("--cache", required=True)
    args = parser.parse_args(argv)
    cache = Path(args.cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)

    php, _unzip = _ensure_system_packages()
    pts_archive = cache / "phoronix-test-suite.zip"
    payload = cache / "phpbench-081-patched2.zip"
    _download(PTS_URLS, pts_archive, PTS_SHA256)
    _download(PAYLOAD_URLS, payload, PAYLOAD_SHA256)
    launcher = _extract_pts(pts_archive, cache / "phoronix-test-suite")

    bin_dir = cache / "bin"
    bin_dir.mkdir(exist_ok=True)
    php_link = bin_dir / "php"
    php_link.unlink(missing_ok=True)
    php_link.symlink_to(Path(php).resolve())
    core_entrypoint = launcher.parent / "pts-core" / "phoronix-test-suite.php"
    if not core_entrypoint.is_file():
        raise PreparationError("prepared PTS core PHP entrypoint is missing")
    completed = subprocess.run(
        [str(php_link), str(core_entrypoint), "version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "PTS_SILENT_MODE": "1",
            "PTS_USER_PATH_OVERRIDE": str((cache / "version-check-user").resolve()),
        },
    )
    if completed.returncode != 0 or PTS_VERSION not in completed.stdout + completed.stderr:
        raise PreparationError("prepared PTS launcher did not report the pinned version")
    print(f"prepared Phoronix Test Suite {PTS_VERSION} and pinned PHPBench payload")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, PreparationError, subprocess.TimeoutExpired, zipfile.BadZipFile) as error:
        print(f"PTS dependency preparation failed: {error}", file=sys.stderr)
        raise SystemExit(2) from None
