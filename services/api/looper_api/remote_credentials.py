"""Encrypted, local-at-rest storage for restart-safe SSH tunnel recovery."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from looper_api.config import Settings
from looper_api.external_targets import ConnectExternalTargetRequest

_STORE_VERSION = 1
_DPAPI_PREFIX = b"dpapi-v1:"
_PLAIN_PREFIX = b"file-v1:"


class RemoteCredentialError(RuntimeError):
    """Credential storage is unavailable or cannot be decrypted safely."""

    status_code = 409
    code = "remote_credential_error"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_dpapi(data: bytes, *, protect: bool) -> bytes:
    """Protect or unprotect bytes for the current Windows service account."""

    if sys.platform != "win32":
        return data
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description = "Looper remote Worker credentials" if protect else None
    result = function(
        ctypes.byref(source),
        description,
        None,
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(output),
    )
    if not result:
        raise RemoteCredentialError(
            f"Windows DPAPI {'encryption' if protect else 'decryption'} failed"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _encode_key(key: bytes) -> bytes:
    if sys.platform == "win32":
        return _DPAPI_PREFIX + base64.b64encode(_windows_dpapi(key, protect=True))
    return _PLAIN_PREFIX + key


def _decode_key(payload: bytes) -> bytes:
    if payload.startswith(_DPAPI_PREFIX):
        if sys.platform != "win32":
            raise RemoteCredentialError("DPAPI credential key requires Windows")
        try:
            protected = base64.b64decode(payload.removeprefix(_DPAPI_PREFIX), validate=True)
        except ValueError as error:
            raise RemoteCredentialError("remote Worker credential key is invalid") from error
        return _windows_dpapi(protected, protect=False)
    if payload.startswith(_PLAIN_PREFIX):
        return payload.removeprefix(_PLAIN_PREFIX)
    raise RemoteCredentialError("remote Worker credential key has an unknown format")


class EncryptedSshCredentialStore:
    """Keep SSH credentials encrypted outside the target inventory database.

    The random Fernet key and ciphertext live in separate restricted files
    under the configured Looper data directory. Windows additionally protects
    the key with current-user DPAPI. This protects database dumps and accidental
    disclosure, but not a compromise of the running control-plane account.
    """

    _lock = threading.RLock()

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.remember_ssh_credentials
        self.key_path = settings.remote_credential_key_path
        self.store_path = settings.remote_credential_store_path

    @staticmethod
    def _restrict(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError as error:
            raise RemoteCredentialError(
                f"cannot restrict credential file permissions: {path.name}"
            ) from error

    def _key(self, *, create: bool) -> bytes:
        if self.key_path.exists():
            key = _decode_key(self.key_path.read_bytes().strip())
            try:
                Fernet(key)
            except (TypeError, ValueError) as error:
                raise RemoteCredentialError("remote Worker credential key is invalid") from error
            return key
        if not create:
            raise RemoteCredentialError("remote Worker credential key is missing")
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        try:
            descriptor = os.open(
                self.key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
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

    def _document(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return {"version": _STORE_VERSION, "credentials": {}}
        try:
            document = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RemoteCredentialError("remote Worker credential store is invalid") from error
        if (
            not isinstance(document, dict)
            or document.get("version") != _STORE_VERSION
            or not isinstance(document.get("credentials"), dict)
        ):
            raise RemoteCredentialError("remote Worker credential store has an unknown format")
        return document

    def _write_document(self, document: dict[str, Any]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.store_path.with_name(f".{self.store_path.name}.tmp")
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._restrict(temporary)
            os.replace(temporary, self.store_path)
            self._restrict(self.store_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _payload(
        request: ConnectExternalTargetRequest,
        host_key_sha256: str,
    ) -> dict[str, Any]:
        def secret(value: Any) -> str | None:
            return value.get_secret_value() if value is not None else None

        return {
            "endpoint": request.endpoint,
            "port": request.port,
            "username": request.username,
            "auth_method": request.auth_method,
            "password": secret(request.password),
            "private_key": secret(request.private_key),
            "passphrase": secret(request.passphrase),
            # Recovery always pins the key verified during the original import.
            "expected_host_key_sha256": host_key_sha256,
            "timeout_seconds": request.timeout_seconds,
            "deploy_worker": True,
        }

    def save(
        self,
        target_id: str,
        request: ConnectExternalTargetRequest,
        host_key_sha256: str,
    ) -> bool:
        if not self.enabled:
            return False
        if not host_key_sha256.startswith("SHA256:"):
            raise RemoteCredentialError(
                "verified SSH host key is required before saving credentials"
            )
        plaintext = json.dumps(
            self._payload(request, host_key_sha256),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with self._lock:
            ciphertext = Fernet(self._key(create=True)).encrypt(plaintext).decode("ascii")
            document = self._document()
            document["credentials"][target_id] = ciphertext
            self._write_document(document)
        return True

    def target_ids(self) -> list[str]:
        if not self.enabled or not self.store_path.exists():
            return []
        with self._lock:
            return sorted(self._document()["credentials"])

    def load(self, target_id: str) -> ConnectExternalTargetRequest:
        if not self.enabled:
            raise RemoteCredentialError("SSH credential persistence is disabled")
        with self._lock:
            ciphertext = self._document()["credentials"].get(target_id)
            if not isinstance(ciphertext, str):
                raise RemoteCredentialError("remembered SSH credentials were not found")
            try:
                plaintext = Fernet(self._key(create=False)).decrypt(
                    ciphertext.encode("ascii")
                )
                payload = json.loads(plaintext)
                return ConnectExternalTargetRequest.model_validate(payload)
            except (InvalidToken, UnicodeError, json.JSONDecodeError, ValueError) as error:
                raise RemoteCredentialError(
                    "remembered SSH credentials could not be decrypted or validated"
                ) from error
