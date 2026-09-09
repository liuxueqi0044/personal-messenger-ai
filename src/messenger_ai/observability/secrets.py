"""Fail-closed secret storage backed by Windows user-scoped DPAPI."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
from pathlib import Path
from secrets import token_bytes
from typing import Protocol
from uuid import uuid4


class SecretStoreError(RuntimeError):
    """Safe public error that never embeds a filesystem path or secret."""


class SecretNotFoundError(SecretStoreError):
    pass


class SecretStore(Protocol):
    def set_secret(self, name: str, value: bytes) -> None: ...

    def get_secret(self, name: str) -> bytes: ...

    def delete_secret(self, name: str) -> bool: ...


_SECRET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAGIC = b"PMAI-DPAPI-1\x00"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value, len(value))
    return (
        _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        ),
        buffer,
    )


class WindowsDPAPISecretStore:
    """Stores only DPAPI ciphertext under opaque hashed filenames.

    DPAPI is deliberately user-scoped: copying the encrypted files to another
    Windows account or machine must not make them readable.
    """

    def __init__(self, root: str | Path, *, optional_entropy: bytes = b"") -> None:
        if os.name != "nt":
            raise SecretStoreError(
                "Windows DPAPI is unavailable; secret store is closed"
            )
        self._root = Path(root)
        self._entropy = bytes(optional_entropy)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise SecretStoreError("secret store initialization failed") from None

    def set_secret(self, name: str, value: bytes) -> None:
        self._validate_name(name)
        if not isinstance(value, bytes) or not value:
            raise SecretStoreError("secret must be non-empty bytes")
        protected = self.protect_bytes(value)
        target = self._path_for(name)
        temporary = self._root / f".{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(_MAGIC)
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise SecretStoreError("secret persistence failed") from None

    def get_secret(self, name: str) -> bytes:
        self._validate_name(name)
        try:
            payload = self._path_for(name).read_bytes()
        except FileNotFoundError:
            raise SecretNotFoundError("secret does not exist") from None
        except OSError:
            raise SecretStoreError("secret read failed") from None
        if not payload.startswith(_MAGIC) or len(payload) <= len(_MAGIC):
            raise SecretStoreError("secret envelope is invalid")
        return self.unprotect_bytes(payload[len(_MAGIC) :])

    def delete_secret(self, name: str) -> bool:
        self._validate_name(name)
        try:
            self._path_for(name).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise SecretStoreError("secret deletion failed") from None

    def get_or_create_hmac_key(self, name: str = "m9.authorization.hmac") -> bytes:
        """Return a persistent 256-bit key suitable for M9 AuthorizationService."""

        try:
            key = self.get_secret(name)
        except SecretNotFoundError:
            key = token_bytes(32)
            self.set_secret(name, key)
        if len(key) < 32:
            raise SecretStoreError("stored HMAC key is invalid")
        return key

    def protect_bytes(self, value: bytes) -> bytes:
        if not value:
            raise SecretStoreError("cannot protect an empty value")
        input_blob, input_buffer = _blob(value)
        entropy_blob = None
        entropy_buffer = None
        if self._entropy:
            entropy_blob, entropy_buffer = _blob(self._entropy)
        output_blob = _DataBlob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        entropy_pointer = (
            ctypes.byref(entropy_blob) if entropy_blob is not None else None
        )
        result = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "Personal Messenger AI",
            entropy_pointer,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        _ = input_buffer, entropy_buffer
        if not result:
            raise SecretStoreError("DPAPI protection failed")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)

    def unprotect_bytes(self, value: bytes) -> bytes:
        if not value:
            raise SecretStoreError("cannot unprotect an empty value")
        input_blob, input_buffer = _blob(value)
        entropy_blob = None
        entropy_buffer = None
        if self._entropy:
            entropy_blob, entropy_buffer = _blob(self._entropy)
        output_blob = _DataBlob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        entropy_pointer = (
            ctypes.byref(entropy_blob) if entropy_blob is not None else None
        )
        result = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            entropy_pointer,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        _ = input_buffer, entropy_buffer
        if not result:
            raise SecretStoreError("DPAPI unprotection failed")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)

    def _path_for(self, name: str) -> Path:
        return self._root / f"{hashlib.sha256(name.encode('utf-8')).hexdigest()}.dpapi"

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _SECRET_NAME.fullmatch(name):
            raise SecretStoreError("secret name is invalid")
