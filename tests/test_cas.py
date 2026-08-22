from __future__ import annotations

from pathlib import Path

import pytest
from looper_core.cas import ArtifactTooLarge, FileSystemCAS


def test_cas_deduplicates_and_verifies(tmp_path: Path) -> None:
    cas = FileSystemCAS(tmp_path, max_bytes=1024)
    first = cas.put_bytes(b"evidence")
    second = cas.put_bytes(b"evidence")
    assert first.digest == second.digest
    assert first.path == second.path
    assert cas.verify(first.digest, expected_size=8).size == 8


def test_cas_rejects_oversized_artifact(tmp_path: Path) -> None:
    cas = FileSystemCAS(tmp_path, max_bytes=3)
    with pytest.raises(ArtifactTooLarge):
        cas.put_bytes(b"four")
    assert not list((tmp_path / ".tmp").iterdir())
