"""Safe, deterministic Benchmark package storage and Worker delivery."""

from __future__ import annotations

import hashlib
import io
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_FILES = 256
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MANIFEST_NAMES = {"benchmark.yaml", "benchmark.yml", "benchmark.json"}


class BenchmarkPackageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedBenchmarkPackage:
    manifest_name: str
    manifest_bytes: bytes
    archive_bytes: bytes
    package_digest: str


def _safe_member_name(value: str) -> PurePosixPath:
    if "\\" in value:
        raise BenchmarkPackageError("package paths must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise BenchmarkPackageError(f"unsafe package path: {value}")
    return path


def _canonical_archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    return output.getvalue()


def parse_benchmark_package(raw: bytes) -> ParsedBenchmarkPackage:
    if len(raw) > MAX_PACKAGE_BYTES:
        raise BenchmarkPackageError("Benchmark package exceeds 32 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if not members or len(members) > MAX_PACKAGE_FILES:
                raise BenchmarkPackageError("Benchmark package must contain 1 to 256 files")
            if any(item.flag_bits & 0x1 for item in members):
                raise BenchmarkPackageError("encrypted Benchmark packages are not supported")
            if any(stat.S_ISLNK(item.external_attr >> 16) for item in members):
                raise BenchmarkPackageError("Benchmark packages cannot contain symbolic links")
            if sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                raise BenchmarkPackageError("expanded Benchmark package exceeds 64 MiB")

            paths = [_safe_member_name(item.filename) for item in members]
            manifests = [path for path in paths if path.name.casefold() in MANIFEST_NAMES]
            if len(manifests) != 1:
                raise BenchmarkPackageError(
                    "Benchmark package must contain exactly one benchmark.yaml"
                )
            package_root = manifests[0].parent
            files: dict[str, bytes] = {}
            for member, path in zip(members, paths, strict=True):
                try:
                    relative = path.relative_to(package_root)
                except ValueError as error:
                    raise BenchmarkPackageError(
                        "all Benchmark package files must be below the manifest directory"
                    ) from error
                normalized = relative.as_posix()
                if normalized in files:
                    raise BenchmarkPackageError(f"duplicate package path: {normalized}")
                files[normalized] = archive.read(member)
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise BenchmarkPackageError("Benchmark package must be a valid ZIP archive") from error

    manifest_name = manifests[0].name
    archive_bytes = _canonical_archive(files)
    return ParsedBenchmarkPackage(
        manifest_name=manifest_name,
        manifest_bytes=files[manifest_name],
        archive_bytes=archive_bytes,
        package_digest=f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}",
    )


def install_benchmark_package(
    data_dir: Path, parsed: ParsedBenchmarkPackage
) -> Path:
    """Install a validated package atomically and return its manifest path."""

    digest = parsed.package_digest.removeprefix("sha256:")
    package_root = (data_dir / "benchmark-packages" / digest).resolve()
    manifest_path = package_root / parsed.manifest_name
    if manifest_path.is_file() and (package_root / "package.zip").is_file():
        return manifest_path

    package_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{digest[:12]}-", dir=package_root.parent))
    try:
        with zipfile.ZipFile(io.BytesIO(parsed.archive_bytes)) as archive:
            for member in archive.infolist():
                relative = _safe_member_name(member.filename)
                destination = (temporary / relative.as_posix()).resolve()
                if temporary not in destination.parents:
                    raise BenchmarkPackageError(f"unsafe package path: {member.filename}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(member))
        (temporary / "package.zip").write_bytes(parsed.archive_bytes)
        if package_root.exists():
            shutil.rmtree(package_root)
        temporary.replace(package_root)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest_path


def build_directory_package(package_root: Path) -> tuple[bytes, str]:
    """Build the same bounded archive for a locally installed package directory."""

    files: dict[str, bytes] = {}
    expanded = 0
    for path in sorted(package_root.rglob("*")):
        if not path.is_file() or path.name == "package.zip" or "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise BenchmarkPackageError("Benchmark packages cannot contain symbolic links")
        relative = path.relative_to(package_root).as_posix()
        content = path.read_bytes()
        expanded += len(content)
        if len(files) >= MAX_PACKAGE_FILES or expanded > MAX_EXPANDED_BYTES:
            raise BenchmarkPackageError("Benchmark package exceeds delivery limits")
        files[relative] = content
    archive = _canonical_archive(files)
    if len(archive) > MAX_PACKAGE_BYTES:
        raise BenchmarkPackageError("Benchmark package exceeds 32 MiB")
    return archive, f"sha256:{hashlib.sha256(archive).hexdigest()}"
