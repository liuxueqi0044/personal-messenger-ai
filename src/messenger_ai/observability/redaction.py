"""Recursive structured-log redaction with no traceback or raw-object fallback."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any

REDACTED = "[REDACTED]"


def _canonical_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = "".join(
        char for char in normalized if unicodedata.category(char) != "Cf"
    )
    normalized = normalized.translate(
        str.maketrans(
            {
                "а": "a",
                "е": "e",
                "і": "i",
                "к": "k",
                "о": "o",
                "р": "p",
                "с": "c",
                "т": "t",
                "Α": "a",
                "Ε": "e",
                "Ι": "i",
                "Κ": "k",
                "Ο": "o",
                "Ρ": "p",
                "Τ": "t",
                "α": "a",
                "ε": "e",
                "ι": "i",
                "κ": "k",
                "ο": "o",
                "ρ": "p",
                "τ": "t",
            }
        )
    )
    return re.sub(r"[^a-z0-9]", "", normalized.casefold())


_SENSITIVE_KEYS = {
    "text",
    "body",
    "content",
    "message",
    "messagetext",
    "replytext",
    "rawmessage",
    "rawtext",
    "nickname",
    "displayname",
    "contactname",
    "sendername",
    "recipientname",
    "path",
    "filepath",
    "screenshotpath",
    "framepath",
    "apikey",
    "openaiapikey",
    "token",
    "accesstoken",
    "refreshtoken",
    "authorization",
    "authorizationid",
    "authorizationtoken",
    "cookie",
    "setcookie",
    "password",
    "passwd",
    "secret",
    "credential",
    "credentials",
    "phone",
    "phonenumber",
    "email",
    "errormessage",
    "exceptionmessage",
}
_SENSITIVE_KEY_MARKERS = (
    "apikey",
    "token",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "credential",
    "nickname",
    "displayname",
    "contactname",
    "sendername",
    "recipientname",
    "filepath",
    "screenshotpath",
    "framepath",
)

_SECRET_VALUE_PATTERNS = (
    re.compile(
        r"(?i)s[\s\u200b._-]*k[\s\u200b._-]*(?:proj[\s\u200b._-]*)?[A-Za-z0-9_-]{12,}"
    ),
    re.compile(
        r"(?i)b[\s\u200b._-]*e[\s\u200b._-]*a[\s\u200b._-]*r[\s\u200b._-]*e[\s\u200b._-]*r\s+[A-Za-z0-9._~+/=-]{8,}"
    ),
    re.compile(
        r"(?i)(?:api[_ -]?key|password|passwd|secret|token|authorization|cookie)\s*[:=]\s*[^\s,;]{4,}"
    ),
    re.compile(r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(?:cookie|set-cookie)\s*:\s*[^\r\n]+"),
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9](?:[\s-]?\d){9}(?!\d)")
_WINDOWS_PATH = re.compile(r"(?i)(?:[A-Z]:\\|\\\\)[^\r\n\t<>|\"']+")
_POSIX_PATH = re.compile(r"(?<![\w])/(?:Users|home|tmp|var|etc|opt)/[^\s\r\n]+")


def redact_string(value: str, *, max_length: int = 512) -> str:
    result = unicodedata.normalize("NFC", value)
    for pattern in _SECRET_VALUE_PATTERNS:
        result = pattern.sub(REDACTED, result)
    result = _EMAIL.sub(REDACTED, result)
    result = _PHONE.sub(REDACTED, result)
    result = _WINDOWS_PATH.sub(REDACTED, result)
    result = _POSIX_PATH.sub(REDACTED, result)
    if len(result) > max_length:
        result = f"{result[:max_length]}[TRUNCATED]"
    return result


def sanitize(value: Any, *, max_depth: int = 12) -> Any:
    """Return JSON-safe data, recursively redacting sensitive keys and values."""

    return _sanitize(value, depth=0, max_depth=max_depth, ancestors=set())


def _sanitize(value: Any, *, depth: int, max_depth: int, ancestors: set[int]) -> Any:
    if depth > max_depth:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, bytes):
        return f"[BINARY_REDACTED:{len(value)}]"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return sanitize(value.value, max_depth=max_depth - depth)

    identity = id(value)
    if identity in ancestors:
        return "[CYCLE]"
    next_ancestors = {*ancestors, identity}

    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)[:128]
            canonical = _canonical_key(raw_key)
            if canonical in _SENSITIVE_KEYS or any(
                marker in canonical for marker in _SENSITIVE_KEY_MARKERS
            ):
                output[key] = REDACTED
            else:
                output[key] = _sanitize(
                    raw_value,
                    depth=depth + 1,
                    max_depth=max_depth,
                    ancestors=next_ancestors,
                )
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _sanitize(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                ancestors=next_ancestors,
            )
            for item in value
        ]
    if hasattr(value, "model_dump"):
        try:
            return _sanitize(
                value.model_dump(mode="json"),
                depth=depth + 1,
                max_depth=max_depth,
                ancestors=next_ancestors,
            )
        except Exception:  # noqa: BLE001 - hostile serializers must not escape redaction
            return f"[UNSERIALIZABLE:{type(value).__name__[:64]}]"
    return f"[UNSERIALIZABLE:{type(value).__name__[:64]}]"


class RedactingLogger:
    """Emits one-line JSON events after recursive sanitization."""

    _EVENT = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
    _LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink

    def emit(self, level: str, event: str, **fields: Any) -> str:
        normalized_level = level.casefold()
        if normalized_level not in self._LEVELS:
            raise ValueError("log level is not allowed")
        if not self._EVENT.fullmatch(event):
            raise ValueError("event name is not allowed")
        record = {
            "level": normalized_level,
            "event": event,
            "fields": sanitize(fields),
        }
        encoded = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self._sink(encoded)
        return encoded

    def error(
        self, event: str, *, code: str, exception: BaseException | None = None
    ) -> str:
        fields: dict[str, Any] = {"code": code}
        if exception is not None:
            fields["exception_type"] = type(exception).__name__[:64]
            fields["exception_message_redacted"] = True
        # Deliberately do not serialize repr(exception), __dict__, __traceback__,
        # traceback text, or chained exceptions.
        return self.emit("error", event, **fields)
