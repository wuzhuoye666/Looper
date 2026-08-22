from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml


class SourcePolicyError(ValueError):
    pass


APPROVED_STATUSES = {
    "approved-dependency",
    "approved-optional",
    "approved-fetchable",
}
APPROVED_LICENSES = {"MIT", "Apache-2.0", "BSD-3-Clause"}


def load_source_lock(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        raise SourcePolicyError("invalid source lock file")
    return value


def source_entry(lock: dict[str, Any], source_id: str) -> dict[str, Any]:
    try:
        return next(item for item in lock["sources"] if item["id"] == source_id)
    except StopIteration as error:
        raise SourcePolicyError(f"unknown source: {source_id}") from error


def _github_repository(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        raise SourcePolicyError("automatic resolution currently supports GitHub only")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise SourcePolicyError("source URL must identify a GitHub repository")
    return parts[0], parts[1].removesuffix(".git")


def resolve_source(path: Path, source_id: str) -> dict[str, Any]:
    lock = load_source_lock(path)
    entry = source_entry(lock, source_id)
    _require_fetchable(entry)
    owner, repository = _github_repository(entry["upstream_url"])
    headers = {"User-Agent": "Looper-source-manager", "Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        metadata = client.get(f"https://api.github.com/repos/{owner}/{repository}")
        metadata.raise_for_status()
        default_branch = metadata.json()["default_branch"]
        commit_response = client.get(
            f"https://api.github.com/repos/{owner}/{repository}/commits/{default_branch}"
        )
        commit_response.raise_for_status()
        commit = commit_response.json()["sha"]
    if len(commit) != 40:
        raise SourcePolicyError("GitHub returned an invalid commit id")
    entry["commit"] = commit
    entry["resolution_status"] = "live-resolved"
    entry["resolved_default_branch"] = default_branch
    lock["policy"]["commit_resolution"] = "partial-live-resolution"
    path.write_text(yaml.safe_dump(lock, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return entry


def _hash_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
            size += len(chunk)
    return f"sha256:{hasher.hexdigest()}", size


def _archive_license_evidence(archive: Path) -> dict[str, Any]:
    priorities = {"license": 0, "license.txt": 1, "license.md": 2, "copying": 3}
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            candidates = [
                member
                for member in bundle.getmembers()
                if member.isfile()
                and PurePosixPath(member.name).name.lower() in priorities
                and 0 < member.size <= 1024 * 1024
            ]
            if not candidates:
                raise SourcePolicyError("source archive does not contain a root license file")
            member = min(
                candidates,
                key=lambda item: (
                    len(PurePosixPath(item.name).parts),
                    priorities[PurePosixPath(item.name).name.lower()],
                    item.name,
                ),
            )
            stream = bundle.extractfile(member)
            if stream is None:
                raise SourcePolicyError("cannot read source archive license file")
            content = stream.read()
    except (tarfile.TarError, OSError) as error:
        raise SourcePolicyError(f"cannot inspect source archive license: {error}") from error
    return {
        "member": member.name,
        "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        "bytes": len(content),
    }


def fetch_source(
    lock_path: Path,
    source_id: str,
    cache_root: Path,
    *,
    max_bytes: int = 1024 * 1024 * 1024,
) -> dict[str, Any]:
    lock = load_source_lock(lock_path)
    entry = source_entry(lock, source_id)
    _require_fetchable(entry)
    commit = entry.get("commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise SourcePolicyError("resolve and review an exact commit before downloading")
    owner, repository = _github_repository(entry["upstream_url"])
    destination_dir = cache_root / source_id
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{commit}.tar.gz"
    temporary = destination.with_suffix(".partial")
    url = f"https://api.github.com/repos/{owner}/{repository}/tarball/{commit}"
    expected_digest = entry.get("download_sha256")
    expected_size = entry.get("download_bytes")
    digest, size = _hash_file(destination) if destination.is_file() else ("", 0)
    cached = digest == expected_digest and size == expected_size and size <= max_bytes
    if not cached:
        hasher = hashlib.sha256()
        size = 0
        headers = {
            "User-Agent": "Looper-source-manager",
            "Accept": "application/vnd.github+json",
        }
        try:
            with httpx.stream(
                "GET", url, headers=headers, timeout=120, follow_redirects=True
            ) as response:
                response.raise_for_status()
                with temporary.open("wb") as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise SourcePolicyError(
                                f"source archive exceeds {max_bytes} bytes"
                            )
                        stream.write(chunk)
                        hasher.update(chunk)
            temporary.replace(destination)
        except (httpx.HTTPError, OSError, SourcePolicyError):
            temporary.unlink(missing_ok=True)
            raise
        digest = f"sha256:{hasher.hexdigest()}"
    license_evidence = _archive_license_evidence(destination)
    entry["download_url"] = url
    entry["download_sha256"] = digest
    entry["download_bytes"] = size
    entry["license_evidence"] = license_evidence
    entry["resolution_status"] = "downloaded-and-verified"
    lock["policy"]["external_downloads_performed"] = True
    lock_path.write_text(
        yaml.safe_dump(lock, sort_keys=False, allow_unicode=False), encoding="utf-8"
    )
    return {
        "path": str(destination),
        "url": url,
        "digest": digest,
        "bytes": size,
        "commit": commit,
        "license_evidence": license_evidence,
        "cache_hit": cached,
    }


def extract_source(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            member_path = (destination / member.name).resolve()
            try:
                member_path.relative_to(root)
            except ValueError as error:
                raise SourcePolicyError(f"unsafe archive member: {member.name}") from error
            if member.issym() or member.islnk():
                raise SourcePolicyError(f"links are not extracted: {member.name}")
        bundle.extractall(destination, filter="data")


def _require_fetchable(entry: dict[str, Any]) -> None:
    if entry.get("inclusion_status") not in APPROVED_STATUSES:
        raise SourcePolicyError("source policy does not approve this entry for download")
    if entry.get("license") not in APPROVED_LICENSES:
        raise SourcePolicyError("source license is not in the approved allowlist")
    if entry.get("license_status") != "verified":
        raise SourcePolicyError("source license has not been verified")
