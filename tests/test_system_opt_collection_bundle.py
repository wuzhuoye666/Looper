from __future__ import annotations

import io
import json
import zipfile
from hashlib import sha256

import pytest
from looper_core.canonical import canonical_digest
from looper_core.system_opt.collector import (
    COLLECTION_BUNDLE_MANIFEST_NAME,
    CollectionArtifactBundleManifest,
    CollectionArtifactBundleMember,
    verify_collection_artifact_bundle,
)


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _manifest(files: dict[str, tuple[str, bytes]]) -> CollectionArtifactBundleManifest:
    return CollectionArtifactBundleManifest(
        members=[
            CollectionArtifactBundleMember(
                path=path,
                media_type=media_type,
                size_bytes=len(content),
                digest=_digest(content),
            )
            for path, (media_type, content) in files.items()
        ]
    )


def _bundle(
    manifest: CollectionArtifactBundleManifest,
    files: dict[str, tuple[str, bytes]],
    *,
    timestamp: tuple[int, int, int, int, int, int] = (2026, 8, 23, 12, 0, 0),
    extra: dict[str, bytes] | None = None,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest_info = zipfile.ZipInfo(COLLECTION_BUNDLE_MANIFEST_NAME, timestamp)
        archive.writestr(
            manifest_info,
            json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False).encode("utf-8"),
        )
        for path, (_, content) in files.items():
            info = zipfile.ZipInfo(path, timestamp)
            archive.writestr(info, content)
        for path, content in (extra or {}).items():
            archive.writestr(path, content)
    return output.getvalue()


def test_manifest_digest_is_stable_across_zip_timestamps_and_member_order() -> None:
    first_files = {
        "raw/run-2.json": ("application/json", b'{"value":2}'),
        "raw/run-1.txt": ("text/plain", b"value=1\n"),
    }
    second_files = dict(reversed(list(first_files.items())))
    first_manifest = _manifest(first_files)
    second_manifest = _manifest(second_files)

    first = verify_collection_artifact_bundle(
        _bundle(first_manifest, first_files, timestamp=(2026, 8, 23, 12, 0, 0)),
        expected_digest=first_manifest.digest,
    )
    second = verify_collection_artifact_bundle(
        _bundle(second_manifest, second_files, timestamp=(2030, 1, 1, 0, 0, 0)),
        expected_digest=first_manifest.digest,
    )

    assert first.manifest.digest == second.manifest.digest == first_manifest.digest
    assert sha256(first.bundle_bytes).digest() != sha256(second.bundle_bytes).digest()


def test_bundle_rejects_changed_member_bytes_even_when_manifest_is_unchanged() -> None:
    files = {"raw/run.json": ("application/json", b'{"value":1}')}
    manifest = _manifest(files)
    changed = {"raw/run.json": ("application/json", b'{"value":9}')}

    with pytest.raises(ValueError, match="member digest mismatch"):
        verify_collection_artifact_bundle(
            _bundle(manifest, changed), expected_digest=manifest.digest
        )


def test_bundle_rejects_manifest_identity_tampering() -> None:
    files = {"raw/run.json": ("application/json", b'{"value":1}')}
    manifest = _manifest(files)
    tampered_payload = manifest.model_dump(mode="python")
    tampered_payload["members"][0]["media_type"] = "application/vnd.changed+json"
    tampered = CollectionArtifactBundleManifest.model_validate(tampered_payload)

    with pytest.raises(ValueError, match="manifest digest mismatch"):
        verify_collection_artifact_bundle(_bundle(tampered, files), expected_digest=manifest.digest)


@pytest.mark.parametrize("path", ["../escape.json", "/absolute.json", "raw\\escape.json"])
def test_manifest_rejects_unsafe_member_paths(path: str) -> None:
    with pytest.raises(ValueError, match="safe relative POSIX path"):
        CollectionArtifactBundleMember(
            path=path,
            media_type="application/json",
            size_bytes=2,
            digest=_digest(b"{}"),
        )


def test_bundle_rejects_undeclared_zip_member() -> None:
    files = {"raw/run.json": ("application/json", b'{"value":1}')}
    manifest = _manifest(files)

    with pytest.raises(ValueError, match="member set does not exactly match"):
        verify_collection_artifact_bundle(
            _bundle(manifest, files, extra={"raw/extra.log": b"extra"}),
            expected_digest=manifest.digest,
        )


def test_bundle_rejects_duplicate_zip_member_path() -> None:
    files = {"raw/run.json": ("application/json", b'{"value":1}')}
    manifest = _manifest(files)
    output = io.BytesIO()
    # Writing a duplicate member is exactly the malformed case under test;
    # assert zipfile's own warning instead of leaking it into the suite log.
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(output, "w") as archive,
    ):
        archive.writestr(
            COLLECTION_BUNDLE_MANIFEST_NAME,
            json.dumps(manifest.model_dump(mode="json")),
        )
        archive.writestr("raw/run.json", files["raw/run.json"][1])
        archive.writestr("raw/run.json", files["raw/run.json"][1])

    with pytest.raises(ValueError, match="duplicate ZIP member path"):
        verify_collection_artifact_bundle(output.getvalue(), expected_digest=manifest.digest)


def test_manifest_digest_is_canonical_member_order_independent() -> None:
    files = {
        "raw/b": ("application/octet-stream", b"b"),
        "raw/a": ("application/octet-stream", b"a"),
    }
    manifest = _manifest(files)
    payload = manifest.model_dump(mode="json")
    payload["members"] = list(reversed(payload["members"]))

    assert manifest.digest == canonical_digest(
        {
            "schema_version": manifest.schema_version,
            "members": sorted(payload["members"], key=lambda item: item["path"]),
        }
    )
