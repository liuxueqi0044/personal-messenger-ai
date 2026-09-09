from __future__ import annotations

import os

import pytest

from messenger_ai.observability import SecretStoreError, WindowsDPAPISecretStore


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is Windows-only")
def test_dpapi_round_trip_never_writes_plaintext_or_secret_name(tmp_path):
    store = WindowsDPAPISecretStore(tmp_path / "vault")
    value = ("s" + "k-proj-this-must-never-appear-on-disk").encode()
    store.set_secret("openai.api_key", value)

    files = tuple((tmp_path / "vault").iterdir())
    assert len(files) == 1
    assert store.get_secret("openai.api_key") == value
    assert value not in files[0].read_bytes()
    assert "openai" not in files[0].name


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is Windows-only")
def test_m9_hmac_key_is_persistent_and_dpapi_protected(tmp_path):
    store = WindowsDPAPISecretStore(tmp_path / "vault")
    first = store.get_or_create_hmac_key()
    second = store.get_or_create_hmac_key()
    assert first == second
    assert len(first) == 32
    assert all(
        first not in item.read_bytes() for item in (tmp_path / "vault").iterdir()
    )


def test_non_windows_initialization_fails_closed(monkeypatch, tmp_path):
    import messenger_ai.observability.secrets as module

    monkeypatch.setattr(module.os, "name", "posix")
    with pytest.raises(SecretStoreError, match="closed"):
        module.WindowsDPAPISecretStore(tmp_path / "vault")


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is Windows-only")
def test_invalid_secret_names_cannot_escape_store(tmp_path):
    store = WindowsDPAPISecretStore(tmp_path / "vault")
    with pytest.raises(SecretStoreError):
        store.set_secret("../escape", b"value")
    with pytest.raises(SecretStoreError):
        store.set_secret("empty", b"")
