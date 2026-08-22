from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from looper_api.source_manager import (
    APPROVED_LICENSES,
    APPROVED_STATUSES,
    SourcePolicyError,
    _archive_license_evidence,
    fetch_source,
    load_source_lock,
)

LOCK_PATH = Path(__file__).resolve().parents[1] / "third_party" / "sources.lock.yaml"


def test_source_lock_approvals_require_verified_allowlisted_licenses() -> None:
    lock = load_source_lock(LOCK_PATH)
    for source in lock["sources"]:
        if source["inclusion_status"] in APPROVED_STATUSES:
            assert source["license_status"] == "verified", source["id"]
            assert source["license"] in APPROVED_LICENSES, source["id"]


def test_downloaded_sources_bind_commit_archive_and_license_evidence() -> None:
    lock = load_source_lock(LOCK_PATH)
    downloaded = [
        source
        for source in lock["sources"]
        if source["resolution_status"] == "downloaded-and-verified"
    ]
    assert {source["id"] for source in downloaded} >= {"dcperf", "atrex-bench", "sharp-2024"}
    for source in downloaded:
        commit = source["commit"]
        assert isinstance(commit, str) and len(commit) == 40, source["id"]
        assert commit in source["download_url"], source["id"]
        assert source["download_sha256"].startswith("sha256:"), source["id"]
        assert len(source["download_sha256"]) == 71, source["id"]
        assert source["download_bytes"] > 0, source["id"]
        evidence = source["license_evidence"]
        assert evidence["sha256"].startswith("sha256:"), source["id"]
        assert len(evidence["sha256"]) == 71, source["id"]
        assert evidence["bytes"] > 0, source["id"]


def test_archive_license_evidence_prefers_root_license(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    root_license = b"Apache License\nVersion 2.0\n"
    nested_license = b"dependency license\n"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, content in (
            ("repo/dependency/LICENSE", nested_license),
            ("repo/LICENSE", root_license),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
    evidence = _archive_license_evidence(archive)
    assert evidence["member"] == "repo/LICENSE"
    assert evidence["bytes"] == len(root_license)
    assert evidence["sha256"].startswith("sha256:")


def test_archive_license_evidence_fails_without_license(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    content = b"readme\n"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("repo/README.md")
        member.size = len(content)
        bundle.addfile(member, io.BytesIO(content))
    with pytest.raises(SourcePolicyError, match="root license"):
        _archive_license_evidence(archive)


def test_vgo_download_fails_closed_before_network_access(tmp_path: Path) -> None:
    with pytest.raises(SourcePolicyError, match="does not approve"):
        fetch_source(LOCK_PATH, "vgo-2026", tmp_path)


def test_vgo_is_paper_metadata_not_a_sharp_source_release() -> None:
    lock = load_source_lock(LOCK_PATH)
    vgo = next(source for source in lock["sources"] if source["id"] == "vgo-2026")
    assert vgo["inclusion_status"] == "metadata-only"
    assert vgo["availability"] == "paper-only"
    assert vgo["commit"] is None
    assert vgo["code_mapping_status"] == "unverified-no-independent-release"


def test_original_tailbench_and_plusplus_candidate_are_distinct_and_blocked(
    tmp_path: Path,
) -> None:
    lock = load_source_lock(LOCK_PATH)
    sources = {source["id"]: source for source in lock["sources"]}
    original = sources["tailbench-original"]
    candidate = sources["tailbenchplusplus-candidate"]

    assert original["upstream_url"] == "https://tailbench.csail.mit.edu/"
    assert original["paper_doi"] == "10.1109/IISWC.2016.7581261"
    assert original["license_status"] == "package-and-workloads-unverified"
    assert original["source_bytes"] == 144643825
    assert original["dataset_bytes"] == 10230769002
    assert candidate["upstream_url"] == "https://github.com/zliUPV/Tailbenchplusplus"
    assert candidate["official_status"] == "unverified"
    assert candidate["original_tailbench_relation"] == "not-authoritatively-verified"

    for source_id in ("tailbench-original", "tailbenchplusplus-candidate"):
        with pytest.raises(SourcePolicyError, match="does not approve"):
            fetch_source(LOCK_PATH, source_id, tmp_path / source_id)
