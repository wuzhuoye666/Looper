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
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

PTS_URL = (
    "https://github.com/phoronix-test-suite/phoronix-test-suite/archive/"
    "f977d6e270d5eb9eebfa26d3ca62385c00a547a6.zip"
)
PTS_SHA256 = "9d4b811a1ff4710ac89d19b34ba7ef4188b8ce1bfe61d5d276b0ee689dba1dc4"
PTS_VERSION = "10.8.6"
PAYLOAD_URL = "http://phoronix-test-suite.com/benchmark-files/phpbench-081-patched2.zip"
PAYLOAD_SHA256 = "32503bd4ace0c8429493de864ca48bb16febed867e52b75f4369d7145f797718"
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024


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


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    if (
        destination.is_file()
        and hashlib.sha256(destination.read_bytes()).hexdigest() == expected_sha256
    ):
        return
    request = urllib.request.Request(url, headers={"User-Agent": "Looper/1 PTS adapter"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - pinned digest
        content = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise PreparationError(f"dependency download exceeded {MAX_DOWNLOAD_BYTES} bytes")
    observed = hashlib.sha256(content).hexdigest()
    if observed != expected_sha256:
        raise PreparationError(
            f"dependency digest mismatch for {url}: expected {expected_sha256}, got {observed}"
        )
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(destination)


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
    _download(PTS_URL, pts_archive, PTS_SHA256)
    _download(PAYLOAD_URL, payload, PAYLOAD_SHA256)
    launcher = _extract_pts(pts_archive, cache / "phoronix-test-suite")

    bin_dir = cache / "bin"
    bin_dir.mkdir(exist_ok=True)
    php_link = bin_dir / "php"
    php_link.unlink(missing_ok=True)
    php_link.symlink_to(Path(php).resolve())
    completed = subprocess.run(
        [str(php_link), str(launcher), "version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
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
