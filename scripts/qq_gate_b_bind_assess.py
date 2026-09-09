"""Gate B orchestration for the QQ UIA selection helper.

The helper is the only component that touches UI Automation.  This bridge
passes the user's selection phrase on the helper's stdin and keeps only the
helper's redacted structural evidence.  An application produced here is a
pending Q3 identity-binding request, never an active binding.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO


class GateBAssessmentError(RuntimeError):
    """A fail-closed helper protocol or persistence error."""

    def __init__(self, code: str, message: str = "Gate B assessment rejected") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRIVACY = {
    "target_from_stdin_only": True,
    "emitted_target": False,
    "emitted_control_names": False,
    "emitted_chat_text": False,
    "emitted_content_hashes_are_aggregate": True,
}
_RIGHT_DIGESTS = (
    "structure_digest",
    "content_digest",
    "active_header_digest",
    "target_match_evidence_digest",
)


@dataclass(frozen=True)
class RightRegionEvidence:
    structure_digest: str | None = None
    content_digest: str | None = None
    active_header_digest: str | None = None
    target_match_evidence_digest: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "structure_digest": self.structure_digest,
            "content_digest": self.content_digest,
            "active_header_digest": self.active_header_digest,
            "target_match_evidence_digest": self.target_match_evidence_digest,
        }


@dataclass(frozen=True)
class SelectionEvidence:
    match_evidence_digest: str | None
    right_region_evidence: RightRegionEvidence


@dataclass(frozen=True)
class SelectionResult:
    status: str
    match_count: int
    selection_attempted: bool
    root_visible_text_match_count: int
    left_visible_text_match_count: int
    right_visible_text_match_count: int
    evidence: SelectionEvidence


def read_selection_phrase(stream: TextIO) -> str:
    """Read the phrase from stdin; never include it in diagnostics."""

    try:
        # One line is the complete selector.  Avoid waiting for EOF so an
        # interactive caller can keep the phrase off argv and shell history.
        phrase = stream.readline()
    except Exception as exc:  # pragma: no cover - unusual stream failures
        raise GateBAssessmentError("SELECTION_READ_FAILED") from exc
    if not isinstance(phrase, str):
        raise GateBAssessmentError("SELECTION_READ_FAILED")
    phrase = phrase.strip()
    if not phrase:
        raise GateBAssessmentError("EMPTY_SELECTION_PHRASE")
    if len(phrase) > 256:
        raise GateBAssessmentError("SELECTION_PHRASE_TOO_LONG")
    return phrase


def _required(value: Mapping[str, Any], key: str) -> Any:
    if key not in value:
        raise GateBAssessmentError("HELPER_SCHEMA_INVALID")
    return value[key]


def _digest(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise GateBAssessmentError("HELPER_EVIDENCE_INVALID")
    return value


def _right_region(value: Any, *, required: bool) -> RightRegionEvidence:
    if value is None:
        if required:
            raise GateBAssessmentError("POST_EVIDENCE_INVALID")
        return RightRegionEvidence()
    if not isinstance(value, Mapping):
        raise GateBAssessmentError("POST_EVIDENCE_INVALID")
    # Ignore helper counters and retain only the four aggregate digest fields.
    structure = _digest(value.get("structure_digest"), nullable=not required)
    content = _digest(value.get("content_digest"), nullable=not required)
    header = _digest(value.get("active_header_digest"), nullable=True)
    target = _digest(value.get("target_match_evidence_digest"), nullable=True)
    if required and (structure is None or content is None):
        raise GateBAssessmentError("POST_EVIDENCE_INVALID")
    return RightRegionEvidence(structure, content, header, target)


def parse_helper_output(
    output: str | bytes | Mapping[str, Any], phase: str
) -> SelectionResult:
    """Validate one exact ``qq-uia-selection-v1`` helper report."""

    if isinstance(output, Mapping):
        value = output
    else:
        if isinstance(output, bytes):
            try:
                output = output.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GateBAssessmentError("HELPER_OUTPUT_INVALID") from exc
        if not isinstance(output, str):
            raise GateBAssessmentError("HELPER_OUTPUT_INVALID")
        try:
            value = json.loads(output.lstrip("\ufeff"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise GateBAssessmentError("HELPER_OUTPUT_INVALID") from exc
    if not isinstance(value, Mapping):
        raise GateBAssessmentError("HELPER_SCHEMA_INVALID")
    if _required(value, "probe_version") != "qq-uia-selection-v1":
        raise GateBAssessmentError("HELPER_VERSION_MISMATCH")
    if _required(value, "mode") != "conversation_selection":
        raise GateBAssessmentError("HELPER_MODE_MISMATCH")
    if _required(value, "succeeded") is not True:
        raise GateBAssessmentError("HELPER_FAILED")

    privacy = _required(value, "privacy")
    if not isinstance(privacy, Mapping) or any(
        privacy.get(key) is not expected for key, expected in _PRIVACY.items()
    ):
        raise GateBAssessmentError("PRIVACY_CONTRACT_FAILED")

    status = _required(value, "status")
    match_count = _required(value, "match_count")
    attempted = _required(value, "selection_attempted")
    if (
        not isinstance(status, str)
        or isinstance(match_count, bool)
        or not isinstance(match_count, int)
        or match_count < 0
        or not isinstance(attempted, bool)
    ):
        raise GateBAssessmentError("HELPER_SCHEMA_INVALID")
    if phase == "dry-run":
        if status not in {"MATCH_READY_DRY_RUN", "CURRENT_RIGHT_REGION_MATCH"}:
            raise GateBAssessmentError("DRY_RUN_NOT_ACCEPTABLE")
        if attempted:
            raise GateBAssessmentError("DRY_RUN_ACTION_ATTEMPTED")
        if status == "MATCH_READY_DRY_RUN" and match_count != 1:
            raise GateBAssessmentError("SELECTION_NOT_EXACTLY_ONE")
        if status == "CURRENT_RIGHT_REGION_MATCH":
            if match_count != 0:
                raise GateBAssessmentError("HELPER_SCHEMA_INVALID")
            root_count = _required(value, "root_visible_text_match_count")
            right_count = _required(value, "right_visible_text_match_count")
            if (
                isinstance(root_count, bool)
                or isinstance(right_count, bool)
                or not isinstance(root_count, int)
                or not isinstance(right_count, int)
                or root_count != 1
                or right_count != 1
            ):
                raise GateBAssessmentError("CURRENT_REGION_NOT_CONFIRMED")
    elif phase == "authorized":
        if status != "SELECTED_WITH_POST_EVIDENCE" or not attempted or match_count != 1:
            raise GateBAssessmentError("AUTHORIZED_SELECTION_INVALID")
    else:
        raise GateBAssessmentError("HELPER_PHASE_INVALID")

    counts: list[int] = []
    for key in (
        "root_visible_text_match_count",
        "left_visible_text_match_count",
        "right_visible_text_match_count",
    ):
        count = _required(value, key)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise GateBAssessmentError("HELPER_SCHEMA_INVALID")
        counts.append(count)
    match_digest = _digest(_required(value, "match_evidence_digest"), nullable=True)
    if phase == "dry-run" and status == "MATCH_READY_DRY_RUN" and match_digest is None:
        raise GateBAssessmentError("HELPER_EVIDENCE_INVALID")
    if phase == "authorized" and match_digest is None:
        raise GateBAssessmentError("HELPER_EVIDENCE_INVALID")
    right = _right_region(
        _required(value, "right_region_evidence"),
        required=phase == "authorized" or status == "CURRENT_RIGHT_REGION_MATCH",
    )
    return SelectionResult(
        status, match_count, attempted, *counts, SelectionEvidence(match_digest, right)
    )


Runner = Callable[..., subprocess.CompletedProcess[str]]


def invoke_helper(
    command: Sequence[str],
    selection_phrase: str,
    *,
    authorized: bool = False,
    runner: Runner = subprocess.run,
) -> SelectionResult:
    """Invoke the C# helper with protocol flags and phrase only on stdin."""

    if not command or any(not isinstance(part, str) or not part for part in command):
        raise GateBAssessmentError("HELPER_COMMAND_INVALID")
    if not isinstance(selection_phrase, str) or not selection_phrase.strip():
        raise GateBAssessmentError("EMPTY_SELECTION_PHRASE")
    helper_command = [*command, "--match-from-stdin"]
    if authorized:
        helper_command.append("--select-authorized")
    if any(selection_phrase in part for part in helper_command):
        raise GateBAssessmentError("SELECTION_PHRASE_LEAK")
    try:
        completed = runner(
            helper_command,
            input=selection_phrase,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GateBAssessmentError("HELPER_TIMEOUT") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise GateBAssessmentError("HELPER_INVOKE_FAILED") from exc
    if getattr(completed, "returncode", 1) != 0:
        raise GateBAssessmentError("HELPER_FAILED")
    return parse_helper_output(
        getattr(completed, "stdout", ""),
        phase="authorized" if authorized else "dry-run",
    )


def build_binding_application(
    result: SelectionResult,
    confirmation_id: str,
    *,
    environment_fingerprint: str | None = None,
    selector_pack_version: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Create a redacted pending application, not a ``ConversationBinding``."""

    if not isinstance(confirmation_id, str) or not confirmation_id.strip():
        raise GateBAssessmentError("CONFIRMATION_REQUIRED")
    if environment_fingerprint is not None and not _SHA256.fullmatch(
        environment_fingerprint
    ):
        raise GateBAssessmentError("ENVIRONMENT_FINGERPRINT_INVALID")
    if selector_pack_version is not None and not selector_pack_version.strip():
        raise GateBAssessmentError("SELECTOR_VERSION_INVALID")
    application: dict[str, Any] = {
        "schema_version": "qq-q3-binding-application-v1",
        "status": "pending_human_binding",
        "platform": "qq",
        "automatic_eligible": False,
        "binding_created": False,
        "binding_evidence_assessment": {
            "bindable": False,
            "automatic_eligible": False,
            "reason_codes": ["binding_stable_second_signal_required"],
        },
        "match_evidence_digest": result.evidence.match_evidence_digest,
        "right_region_evidence": result.evidence.right_region_evidence.as_dict(),
        "human_confirmation": {
            "required": True,
            "confirmation_id": confirmation_id,
            "action": "bind",
            "approved": True,
            "scope": "conversation_selection_only",
        },
        "created_at": (created_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
    }
    if environment_fingerprint is not None:
        application["environment_fingerprint"] = environment_fingerprint
    if selector_pack_version is not None:
        application["selector_pack_version"] = selector_pack_version
    return application


def save_binding_application(application: Mapping[str, Any], output_path: Path) -> None:
    """Atomically save only the redacted candidate application."""

    try:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(application, ensure_ascii=False, sort_keys=True, indent=2)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encoded)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, output_path)
    except (OSError, TypeError, ValueError) as exc:
        try:
            if "temporary_path" in locals():
                temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise GateBAssessmentError("APPLICATION_SAVE_FAILED") from exc


def assess_gate_b(
    *,
    command: Sequence[str],
    selection_phrase: str,
    authorized: bool = False,
    confirmation_id: str | None = None,
    output_path: Path | None = None,
    environment_fingerprint: str | None = None,
    selector_pack_version: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Dry-run first; only an exact match may enter the authorized phase."""

    if authorized and not confirmation_id:
        raise GateBAssessmentError("CONFIRMATION_REQUIRED")
    if not authorized and confirmation_id:
        raise GateBAssessmentError("AUTHORIZED_FLAG_REQUIRED")
    if authorized and output_path is None:
        raise GateBAssessmentError("OUTPUT_REQUIRED")
    for metadata in (
        confirmation_id,
        environment_fingerprint,
        selector_pack_version,
    ):
        if metadata and selection_phrase in metadata:
            raise GateBAssessmentError("SELECTION_PHRASE_LEAK")
    if output_path is not None and selection_phrase in str(output_path):
        raise GateBAssessmentError("SELECTION_PHRASE_LEAK")

    dry = invoke_helper(command, selection_phrase, runner=runner)
    result: dict[str, Any] = {
        "succeeded": True,
        "status": dry.status,
        "match_count": dry.match_count,
        "selection_attempted": dry.selection_attempted,
        "authorized": False,
        "application_saved": False,
    }
    if dry.status == "CURRENT_RIGHT_REGION_MATCH":
        # The current right region is already the human-visible candidate;
        # never invoke a second UI selection for this state.
        if authorized:
            assert confirmation_id is not None and output_path is not None
            application = build_binding_application(
                dry,
                confirmation_id,
                environment_fingerprint=environment_fingerprint,
                selector_pack_version=selector_pack_version,
            )
            save_binding_application(application, output_path)
            result.update(
                authorized=True,
                application_saved=True,
                application_path=str(output_path),
            )
        return result
    if not authorized:
        return result

    assert confirmation_id is not None and output_path is not None
    selected = invoke_helper(
        command,
        selection_phrase,
        authorized=True,
        runner=runner,
    )
    if selected.evidence.match_evidence_digest != dry.evidence.match_evidence_digest:
        raise GateBAssessmentError("EVIDENCE_CHANGED")
    application = build_binding_application(
        selected,
        confirmation_id,
        environment_fingerprint=environment_fingerprint,
        selector_pack_version=selector_pack_version,
    )
    save_binding_application(application, output_path)
    result.update(
        status=selected.status,
        match_count=selected.match_count,
        selection_attempted=selected.selection_attempted,
        authorized=True,
        application_saved=True,
        application_path=str(output_path),
    )
    return result


def _default_helper_command() -> list[str]:
    project = Path(__file__).with_name("qq_uia_probe_helper") / "QQ.UiaProbe.csproj"
    return [
        "dotnet",
        "run",
        "--project",
        str(project),
        "--configuration",
        "Release",
        "--verbosity",
        "quiet",
        "--",
    ]


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate B QQ selection assessment; phrase is stdin-only"
    )
    parser.add_argument(
        "--helper-command", nargs="+", default=_default_helper_command()
    )
    parser.add_argument("--select-authorized", action="store_true")
    parser.add_argument("--confirmation-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--environment-fingerprint")
    parser.add_argument("--selector-pack-version")
    args = parser.parse_args(argv)
    try:
        phrase = read_selection_phrase(sys.stdin)
        result = assess_gate_b(
            command=args.helper_command,
            selection_phrase=phrase,
            authorized=args.select_authorized,
            confirmation_id=args.confirmation_id,
            output_path=args.output,
            environment_fingerprint=args.environment_fingerprint,
            selector_pack_version=args.selector_pack_version,
        )
    except GateBAssessmentError as exc:
        _emit({"succeeded": False, "error_code": exc.code})
        return 2
    except Exception:  # noqa: BLE001 - a gate must fail closed on any bug
        _emit({"succeeded": False, "error_code": "INTERNAL_ERROR"})
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "GateBAssessmentError",
    "RightRegionEvidence",
    "SelectionEvidence",
    "SelectionResult",
    "assess_gate_b",
    "build_binding_application",
    "invoke_helper",
    "main",
    "parse_helper_output",
    "read_selection_phrase",
    "save_binding_application",
]
