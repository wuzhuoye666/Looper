"""Materialize immutable Benchmark packages delivered by the control plane."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import secrets
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_FILES = 256
MAX_EXPANDED_BYTES = 64 * 1024 * 1024


class PackageCacheError(ValueError):
    pass


def materialize_package(bundle: dict[str, Any], cache_root: Path) -> Path:
    if bundle.get("encoding") != "base64+zip":
        raise PackageCacheError("unsupported Benchmark package encoding")
    digest = str(bundle.get("digest", ""))
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise PackageCacheError("Benchmark package digest is invalid")
    try:
        archive_bytes = base64.b64decode(str(bundle["data"]), validate=True)
    except (KeyError, ValueError) as error:
        raise PackageCacheError("Benchmark package payload is invalid") from error
    if len(archive_bytes) > MAX_PACKAGE_BYTES:
        raise PackageCacheError("Benchmark package exceeds 32 MiB")
    observed = f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}"
    if observed != digest:
        raise PackageCacheError("Benchmark package digest verification failed")

    destination = (cache_root / digest.removeprefix("sha256:")).resolve()
    if (destination / ".ready").is_file():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".package-{secrets.token_hex(8)}"
    temporary.mkdir(mode=0o777 if os.name == "nt" else 0o700)
    try:
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                if not members or len(members) > MAX_PACKAGE_FILES:
                    raise PackageCacheError("Benchmark package file count is invalid")
                if sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                    raise PackageCacheError("expanded Benchmark package exceeds 64 MiB")
                for member in members:
                    if member.flag_bits & 0x1 or stat.S_ISLNK(member.external_attr >> 16):
                        raise PackageCacheError("Benchmark package contains an unsafe entry")
                    if "\\" in member.filename:
                        raise PackageCacheError("Benchmark package paths must use POSIX separators")
                    relative = PurePosixPath(member.filename)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise PackageCacheError(f"unsafe package path: {member.filename}")
                    target = (temporary / relative.as_posix()).resolve()
                    if temporary not in target.parents:
                        raise PackageCacheError(f"unsafe package path: {member.filename}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(member))
        except zipfile.BadZipFile as error:
            raise PackageCacheError("Benchmark package is not a valid ZIP archive") from error
        (temporary / ".ready").write_text(digest, encoding="ascii")
        if destination.exists():
            shutil.rmtree(destination)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination
