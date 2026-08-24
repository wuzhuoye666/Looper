"""Encrypted, expiring storage for source archives used by capacity studies."""

from __future__ import annotations

import base64
import ctypes
import os
import sys
import threading
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from looper_core.canonical import utc_now

from looper_api.config import Settings

_DPAPI_PREFIX = b"dpapi-v1:"
_FILE_PREFIX = b"file-v1:"


class SourceArchiveError(RuntimeError):
    status_code = 409
    code = "source_archive_error"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_dpapi(data: bytes, *, protect: bool) -> bytes:
    if sys.platform != "win32":
        return data
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    description = wintypes.LPWSTR()
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        arguments = (ctypes.byref(source), "Looper source archive")
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        arguments = (ctypes.byref(source), ctypes.byref(description))
    function.restype = wintypes.BOOL
    succeeded = function(*arguments, None, None, None, 0x1, ctypes.byref(output))
    if not succeeded and protect:
        # Windows services can have no user profile. Machine-scoped DPAPI remains encrypted
        # by Windows and is the appropriate service-account fallback.
        ctypes.set_last_error(0)
        output = _DataBlob()
        succeeded = function(*arguments, None, None, None, 0x5, ctypes.byref(output))
    if not succeeded:
        error_code = ctypes.get_last_error()
        message = ctypes.FormatError(error_code).strip()
        raise SourceArchiveError(
            f"Windows DPAPI {'encryption' if protect else 'decryption'} failed: "
            f"{error_code} {message}"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))


def _encode_key(key: bytes) -> bytes:
    if sys.platform == "win32":
        try:
            return _DPAPI_PREFIX + base64.b64encode(_windows_dpapi(key, protect=True))
        except SourceArchiveError:
            # Windows service and sandbox accounts can lack a loaded DPAPI profile. Keep
            # the archive encrypted and use the owner-key-file model used on Linux.
            return _FILE_PREFIX + key
    return _FILE_PREFIX + key


def _decode_key(payload: bytes) -> bytes:
    if payload.startswith(_DPAPI_PREFIX):
        if sys.platform != "win32":
            raise SourceArchiveError("DPAPI source archive key requires Windows")
        try:
            protected = base64.b64decode(payload.removeprefix(_DPAPI_PREFIX), validate=True)
        except ValueError as error:
            raise SourceArchiveError("source archive key is invalid") from error
        return _windows_dpapi(protected, protect=False)
    if payload.startswith(_FILE_PREFIX):
        return payload.removeprefix(_FILE_PREFIX)
    raise SourceArchiveError("source archive key has an unknown format")


class EncryptedSourceArchiveStore:
    """Encrypt ZIP bytes outside the database and never expose a download operation."""

    _lock = threading.RLock()

    def __init__(self, settings: Settings) -> None:
        self.key_path = settings.source_archive_key_path
        self.root = settings.source_archive_dir
        self.retention_seconds = settings.source_archive_retention_seconds

    @staticmethod
    def _restrict(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError as error:
            raise SourceArchiveError(
                f"cannot restrict source archive file permissions: {path.name}"
            ) from error

    def _key(self, *, create: bool) -> bytes:
        if self.key_path.exists():
            key = _decode_key(self.key_path.read_bytes().strip())
            try:
                Fernet(key)
            except (TypeError, ValueError) as error:
                raise SourceArchiveError("source archive key is invalid") from error
            return key
        if not create:
            raise SourceArchiveError("source archive key is missing")
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        try:
            descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self._key(create=False)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_encode_key(key) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            self.key_path.unlink(missing_ok=True)
            raise
        self._restrict(self.key_path)
        return key

    def path(self, discovery_id: str) -> Path:
        if not discovery_id.startswith("discovery_") or any(
            part in discovery_id for part in ("/", "\\", "..")
        ):
            raise SourceArchiveError("invalid source discovery identifier")
        return self.root / f"{discovery_id}.zip.enc"

    def save(self, discovery_id: str, payload: bytes) -> datetime:
        with self._lock:
            ciphertext = Fernet(self._key(create=True)).encrypt(payload)
            self.root.mkdir(parents=True, exist_ok=True)
            destination = self.path(discovery_id)
            temporary = destination.with_name(f".{destination.name}.tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(ciphertext)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._restrict(temporary)
                os.replace(temporary, destination)
                self._restrict(destination)
            finally:
                temporary.unlink(missing_ok=True)
        return utc_now() + timedelta(seconds=self.retention_seconds)

    def load(self, discovery_id: str) -> bytes:
        with self._lock:
            path = self.path(discovery_id)
            if not path.is_file():
                raise SourceArchiveError("retained source archive is unavailable")
            try:
                return Fernet(self._key(create=False)).decrypt(path.read_bytes())
            except (InvalidToken, OSError, ValueError) as error:
                raise SourceArchiveError(
                    "retained source archive could not be decrypted"
                ) from error

    def delete(self, discovery_id: str) -> bool:
        with self._lock:
            path = self.path(discovery_id)
            existed = path.exists()
            path.unlink(missing_ok=True)
            return existed

    def exists(self, discovery_id: str) -> bool:
        return self.path(discovery_id).is_file()

    def key_protection(self) -> str:
        if not self.key_path.is_file():
            return "unavailable"
        payload = self.key_path.read_bytes().strip()
        if payload.startswith(_DPAPI_PREFIX):
            return "windows-dpapi"
        if payload.startswith(_FILE_PREFIX):
            return "owner-key-file"
        return "unknown"
