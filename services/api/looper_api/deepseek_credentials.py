"""Encrypted local-at-rest storage for the operator-managed DeepSeek API key."""

from __future__ import annotations

import base64
import ctypes
import os
import sys
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from looper_api.config import Settings

_DPAPI_PREFIX = b"dpapi-v1:"
_FILE_PREFIX = b"file-v1:"


class DeepSeekCredentialError(RuntimeError):
    """The credential cannot be stored or recovered safely."""

    status_code = 422
    code = "deepseek_credential_error"


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
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description = "Looper DeepSeek credential" if protect else None
    result = function(
        ctypes.byref(source), description, None, None, None, 0x1, ctypes.byref(output)
    )
    if not result and protect:
        ctypes.set_last_error(0)
        output = _DataBlob()
        result = function(
            ctypes.byref(source), description, None, None, None, 0x5, ctypes.byref(output)
        )
    if not result:
        raise DeepSeekCredentialError(
            f"Windows DPAPI {'encryption' if protect else 'decryption'} failed"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _encode_key(key: bytes) -> bytes:
    if sys.platform == "win32":
        try:
            return _DPAPI_PREFIX + base64.b64encode(_windows_dpapi(key, protect=True))
        except DeepSeekCredentialError:
            # Service and sandbox accounts may not have a loaded DPAPI profile. The
            # credential remains Fernet encrypted with an owner-only local key file.
            return _FILE_PREFIX + key
    return _FILE_PREFIX + key


def _decode_key(payload: bytes) -> bytes:
    if payload.startswith(_DPAPI_PREFIX):
        if sys.platform != "win32":
            raise DeepSeekCredentialError("DPAPI credential key requires Windows")
        try:
            protected = base64.b64decode(payload.removeprefix(_DPAPI_PREFIX), validate=True)
        except ValueError as error:
            raise DeepSeekCredentialError("DeepSeek credential key is invalid") from error
        return _windows_dpapi(protected, protect=False)
    if payload.startswith(_FILE_PREFIX):
        return payload.removeprefix(_FILE_PREFIX)
    raise DeepSeekCredentialError("DeepSeek credential key has an unknown format")


class EncryptedDeepSeekCredentialStore:
    """Keep the provider key outside the database in a restricted encrypted file."""

    _lock = threading.RLock()

    def __init__(self, settings: Settings) -> None:
        self.key_path = settings.deepseek_credential_key_path
        self.store_path = settings.deepseek_credential_store_path

    @staticmethod
    def _restrict(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError as error:
            raise DeepSeekCredentialError(
                f"cannot restrict credential file permissions: {path.name}"
            ) from error

    def _key(self, *, create: bool) -> bytes:
        if self.key_path.exists():
            key = _decode_key(self.key_path.read_bytes().strip())
            try:
                Fernet(key)
            except (TypeError, ValueError) as error:
                raise DeepSeekCredentialError("DeepSeek credential key is invalid") from error
            return key
        if not create:
            raise DeepSeekCredentialError("DeepSeek credential key is missing")
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

    def exists(self) -> bool:
        return self.store_path.is_file()

    def save(self, api_key: str) -> None:
        value = api_key.strip()
        if len(value) < 20 or len(value) > 512 or "\n" in value or "\r" in value:
            raise DeepSeekCredentialError("DeepSeek API key must contain 20 to 512 characters")
        with self._lock:
            ciphertext = Fernet(self._key(create=True)).encrypt(value.encode("utf-8"))
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.store_path.with_name(f".{self.store_path.name}.tmp")
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(ciphertext + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._restrict(temporary)
                os.replace(temporary, self.store_path)
                self._restrict(self.store_path)
            finally:
                temporary.unlink(missing_ok=True)

    def load(self) -> str | None:
        if not self.store_path.exists():
            return None
        with self._lock:
            try:
                plaintext = Fernet(self._key(create=False)).decrypt(
                    self.store_path.read_bytes().strip()
                )
                return plaintext.decode("utf-8")
            except (InvalidToken, UnicodeError, OSError, ValueError) as error:
                raise DeepSeekCredentialError(
                    "stored DeepSeek credential could not be decrypted"
                ) from error

    def delete(self) -> bool:
        with self._lock:
            existed = self.store_path.exists()
            self.store_path.unlink(missing_ok=True)
            return existed


def effective_deepseek_key(settings: Settings) -> tuple[str, str | None]:
    store = EncryptedDeepSeekCredentialStore(settings)
    stored = store.load()
    if stored:
        return stored, "stored"
    environment = settings.deepseek_api_key.strip()
    return environment, "environment" if environment else None


def effective_deepseek_settings(settings: Settings) -> Settings:
    api_key, _source = effective_deepseek_key(settings)
    return settings.model_copy(update={"deepseek_api_key": api_key})
