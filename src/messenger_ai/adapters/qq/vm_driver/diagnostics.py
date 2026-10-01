"""Bounded, content-free diagnostics, independent of a console or UI worker IPC."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path

from messenger_ai.runtime.config_publication import atomic_bytes

_TOKEN = re.compile(r"^[A-Za-z0-9_./:+-]{1,160}$")
_TOKEN_FIELDS = {
    "schema", "run_id", "worker_epoch", "request_id", "operation_id", "kind", "binding_id",
    "stage", "event", "status", "error_code", "exception_type", "parent_terminate_reason",
    "recorded_at", "started_at", "completed_at", "frame_sha256", "model", "visual_decision",
    "visual_reason", "provider_error_category",
}
_NUMBER_FIELDS = {"elapsed_ms", "worker_process_id", "worker_exit_code", "com_hresult", "latency_ms",
                  "binding_revision", "conversation_revision", "visual_confidence"}
_ATTESTATION_FIELDS = {
    "schema_version", "profile_id", "client_version", "selector_pack_version",
    "environment_fingerprint", "process_id", "window_handle",
    "target_runtime_id_digest", "row_rect",
}


def bounded_selection_attestation(value: object) -> dict[str, object]:
    """Accept only fixed field names and bounded geometry, never proof content."""
    if not isinstance(value, dict):
        return {}
    comparison = value.get("comparison")
    changed = value.get("changed_fields")
    attempt = value.get("attempt")
    retrying = value.get("retrying")
    if not (
        isinstance(comparison, str) and comparison in {"expected_scope", "before_after"}
        and isinstance(changed, list) and 1 <= len(changed) <= len(_ATTESTATION_FIELDS)
        and all(isinstance(item, str) and item in _ATTESTATION_FIELDS for item in changed)
        and len(set(changed)) == len(changed)
        and type(attempt) is int and attempt in (1, 2)
        and isinstance(retrying, bool)
    ):
        return {}
    result: dict[str, object] = {
        "comparison": comparison, "changed_fields": list(changed),
        "attempt": attempt, "retrying": retrying,
    }
    rect_names = ("before_rect", "after_rect") if comparison == "before_after" else ("after_rect",)
    for name in rect_names:
        rect = value.get(name)
        if not (
            isinstance(rect, list) and len(rect) == 4
            and all(type(item) is int and -(2**31) <= item < 2**31 for item in rect)
            and rect[0] < rect[2] and rect[1] < rect[3]
        ):
            return {}
        result[name] = list(rect)
    return result


def bounded_event(event: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in _TOKEN_FIELDS:
        value = event.get(key)
        if isinstance(value, str) and _TOKEN.fullmatch(value):
            result[key] = value
    for key in _NUMBER_FIELDS:
        value = event.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) <= 10**12:
            result[key] = value
    frames = event.get("project_frames")
    if isinstance(frames, list):
        result["project_frames"] = [
            {"file": frame["file"], "function": frame["function"], "line": frame["line"]}
            for frame in frames[-8:] if isinstance(frame, dict)
            and isinstance(frame.get("file"), str) and frame["file"].startswith("messenger_ai/")
            and len(frame["file"]) <= 320 and ".." not in frame["file"]
            and isinstance(frame.get("function"), str) and _TOKEN.fullmatch(frame["function"])
            and isinstance(frame.get("line"), int) and 0 < frame["line"] <= 10_000_000
        ]
    attestation = bounded_selection_attestation(event.get("selection_attestation"))
    if attestation:
        result["selection_attestation"] = attestation
    return result


def emit_console(event: dict[str, object]) -> None:
    # pythonw has no stdout. Broken/redirection-closed consoles must never turn
    # successful UI execution into an exception (especially after a click).
    if sys.stdout is not None:
        try:
            print(json.dumps(bounded_event(event), ensure_ascii=False), flush=True)
        except (OSError, ValueError):
            pass


class WorkerDiagnosticSink:
    """One writer per component; parent and child never rotate the same file."""

    def __init__(self, directory: Path, component: str, *, max_bytes: int = 2_000_000,
                 backups: int = 3) -> None:
        if component not in {"parent", "child"}:
            raise ValueError("unsupported worker diagnostic component")
        self.directory = Path(directory)
        self.path = self.directory / f"qq-worker-{component}.jsonl"
        self.max_bytes, self.backups = max_bytes, backups
        self.errors = 0
        self._lock = threading.Lock()

    def emit(self, event: dict[str, object], *, freeze_failure: bool = False) -> None:
        event = bounded_event(event)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    for index in range(self.backups, 0, -1):
                        source = self.path if index == 1 else self.path.with_suffix(f".jsonl.{index - 1}")
                        target = self.path.with_suffix(f".jsonl.{index}")
                        if source.exists():
                            os.replace(source, target)
                with self.path.open("ab") as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
                if freeze_failure:
                    first_path = self.directory / "qq-worker-first-failure.json"
                    try:
                        first = json.loads(first_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        first = {}
                    if not first or first.get("run_id") != event.get("run_id"):
                        atomic_bytes(first_path, line)
                    atomic_bytes(self.directory / "qq-worker-last-failure.json", line)
            except OSError:
                # Diagnostics never replace the execution journal's decision.
                self.errors += 1
