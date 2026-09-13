"""Shared read-only fingerprint for supervised QQ diagnostics."""
from __future__ import annotations

import hashlib


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def capture_current_session(access, window, composer_selector) -> dict[str, object]:
    from messenger_ai.adapters.qq.vm_driver.session_identity import _header_digest

    root = access._window(window)
    composers = access._select(root, composer_selector)
    if len(composers) != 1:
        raise RuntimeError("COMPOSER_NOT_UNIQUE")
    runtime_id = tuple(composers[0].GetRuntimeId() or ())
    if not runtime_id:
        raise RuntimeError("COMPOSER_RUNTIME_ID_MISSING")
    return {
        "schema": "pmai-qq-supervised-send-evidence-v1",
        "process_id": window.process_id,
        "window_handle": window.window_handle,
        "expected_header_digest": _header_digest(access, window, composer_selector),
        "expected_composer_runtime_id_hash": _sha(repr(runtime_id)),
    }
