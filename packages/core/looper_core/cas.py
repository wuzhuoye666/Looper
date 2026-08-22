from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


class ArtifactError(RuntimeError):
    pass


class ArtifactTooLarge(ArtifactError):
    pass


class ArtifactCorrupt(ArtifactError):
    pass


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    digest: str
    size: int
    path: Path


class FileSystemCAS:
    """Atomic, content-addressed artifact storage on one local filesystem."""

    def __init__(self, root: Path, max_bytes: int = 256 * 1024 * 1024) -> None:
        self.root = root.resolve()
        self.blob_root = self.root / "sha256"
        self.temp_root = self.root / ".tmp"
        self.max_bytes = max_bytes
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        algorithm, separator, hexdigest = digest.partition(":")
        if separator != ":" or algorithm != "sha256" or len(hexdigest) != 64:
            raise ArtifactError(f"invalid digest: {digest}")
        if any(character not in "0123456789abcdef" for character in hexdigest):
            raise ArtifactError(f"invalid digest: {digest}")
        return self.blob_root / hexdigest[:2] / hexdigest[2:]

    def put_bytes(self, value: bytes) -> StoredArtifact:
        from io import BytesIO

        return self.put_stream(BytesIO(value))

    def put_file(self, source: Path) -> StoredArtifact:
        with source.open("rb") as stream:
            return self.put_stream(stream)

    def put_stream(self, stream: BinaryIO) -> StoredArtifact:
        hasher = hashlib.sha256()
        size = 0
        descriptor, temporary_name = tempfile.mkstemp(prefix="upload-", dir=self.temp_root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ArtifactTooLarge(f"artifact exceeds {self.max_bytes} bytes")
                    hasher.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())

            digest = f"sha256:{hasher.hexdigest()}"
            final_path = self.path_for(digest)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if final_path.exists():
                temporary.unlink(missing_ok=True)
                self.verify(digest, expected_size=size)
            else:
                os.replace(temporary, final_path)
                self._sync_directory(final_path.parent)
            return StoredArtifact(digest=digest, size=size, path=final_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def open(self, digest: str) -> BinaryIO:
        path = self.path_for(digest)
        if not path.is_file():
            raise FileNotFoundError(digest)
        return path.open("rb")

    def verify(self, digest: str, expected_size: int | None = None) -> StoredArtifact:
        path = self.path_for(digest)
        if not path.is_file():
            raise ArtifactCorrupt(f"missing blob {digest}")
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                hasher.update(chunk)
        actual = f"sha256:{hasher.hexdigest()}"
        if actual != digest or (expected_size is not None and size != expected_size):
            raise ArtifactCorrupt(f"blob verification failed for {digest}")
        return StoredArtifact(digest=digest, size=size, path=path)

    def remove(self, digest: str) -> None:
        path = self.path_for(digest)
        path.unlink(missing_ok=True)
        with suppress(OSError):
            path.parent.rmdir()

    def clear_temporary(self) -> int:
        count = 0
        for child in self.temp_root.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
            else:
                shutil.rmtree(child, ignore_errors=True)
            count += 1
        return count

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
