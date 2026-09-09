from __future__ import annotations

import json

from messenger_ai.observability import REDACTED, RedactingLogger, sanitize


def test_recursive_sanitizer_removes_body_names_paths_tokens_and_pii():
    payload = {
        "body": "今晚见面吗",
        "nested": [
            {
                "nickname": "小雨",
                "safe_code": "FAILED_SAFE",
                "leak": "Bearer abcdefghijklmnop",
                "email_value": "alice@example.com",
                "phone_value": "+86 138-0013-8000",
                "debug": r"failed at C:\Users\alice\private\chat.png",
            }
        ],
        "cookie": "session=supersecret",
    }
    cleaned = sanitize(payload)
    encoded = json.dumps(cleaned, ensure_ascii=False)
    for forbidden in (
        "今晚见面吗",
        "小雨",
        "abcdefghijklmnop",
        "alice@example.com",
        "138-0013-8000",
        "Users\\alice",
        "supersecret",
    ):
        assert forbidden not in encoded
    assert cleaned["body"] == REDACTED
    assert cleaned["nested"][0]["safe_code"] == "FAILED_SAFE"


def test_confusable_keys_obfuscated_keys_and_nested_cycles_fail_safe():
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    value = {
        "a\u200bpi＿key": "not-visible",
        "To KeN": "not-visible-either",
        "tоken_backup": "cyrillic-o-hidden",
        "freeform": "s k - proj - ABCDEFGHIJKLMNOP and B e a r e r abcdefghijkl",
        "cycle": cyclic,
    }
    encoded = json.dumps(sanitize(value), ensure_ascii=False)
    assert "not-visible" not in encoded
    assert "cyrillic-o-hidden" not in encoded
    assert "ABCDEFGHIJKLMNOP" not in encoded
    assert "abcdefghijkl" not in encoded
    assert "[CYCLE]" in encoded


def test_logger_does_not_emit_traceback_repr_or_sensitive_exception_paths():
    lines: list[str] = []
    logger = RedactingLogger(lines.append)
    try:
        raise RuntimeError(r"token=abcd1234 failed in C:\Users\alice\secret.py")
    except RuntimeError as error:
        encoded = logger.error("adapter.failed", code="FAILED_SAFE", exception=error)
    assert len(lines) == 1
    assert "abcd1234" not in encoded
    assert "Users\\alice" not in encoded
    assert "Traceback" not in encoded
    assert "FAILED_SAFE" in encoded


def test_unknown_object_repr_is_never_called():
    class Hostile:
        def __repr__(self):
            raise AssertionError("repr must not run")

    cleaned = sanitize({"unknown": Hostile()})
    assert cleaned["unknown"] == "[UNSERIALIZABLE:Hostile]"
