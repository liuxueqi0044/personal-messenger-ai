from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from messenger_ai.adapters.qq.live_driver import (
    CertifiedQQProfile,
    CurrentSessionInspectionError,
    CurrentSessionSnapshot,
    QQReadOnlyFrontHalf,
    ingest_probe_report,
)
from messenger_ai.qq_session_lease_runtime import (
    QQManualSessionScopeReader,
    QQSessionScopeError,
)

HASH = "a" * 64
STARTED = datetime(2026, 9, 9, 1, 2, 3, tzinfo=UTC)


def probe_report() -> dict:
    return {
        "succeeded": True,
        "read_only": True,
        "process_id": 42,
        "window_handle": 9001,
        "host_environment": {
            "executable_path": "sha256:qq-path",
            "executable_signature": HASH,
            "process_signature": "b" * 64,
            "client_version": "9.9.26.44343",
            "windows_version": "Windows-test",
            "dpi_scale": 1.5,
            "monitor_id": "monitor-1",
            "monitor_topology_digest": "c" * 64,
            "theme": "light",
            "window_class": "Chrome_WidgetWin_1",
            "presentation": "maximized",
            "is_occluded": True,
            "is_logged_in": True,
            "modal_state": "none",
        },
        "window_bounds": {
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1032,
            "minimized": False,
            "maximized": True,
            "foreground": False,
            "geometry_usable": True,
        },
        "root": {
            "control_type": "ControlType.Pane",
            "class_name": "Chrome_WidgetWin_1",
        },
        "topology": {
            "included": True,
            "digest": "d" * 64,
            "nodes": [
                {
                    "runtime_id": "list",
                    "parent_runtime_id": "__probe_root__",
                    "control_type": "ControlType.List",
                    "class_name": "",
                    "automation_id": "",
                    "patterns": ["selectionpattern"],
                    "semantic_anchors": ["conversation_list"],
                    "normalized_bounds": {
                        "x": 0.0,
                        "y": 0.1,
                        "width": 0.25,
                        "height": 0.8,
                    },
                },
                {
                    "runtime_id": "item",
                    "parent_runtime_id": "list",
                    "control_type": "ControlType.ListItem",
                    "class_name": "",
                    "automation_id": "",
                    "patterns": ["selectionitempattern"],
                    "semantic_anchors": ["conversation_item"],
                    "normalized_bounds": {
                        "x": 0.0,
                        "y": 0.1,
                        "width": 0.25,
                        "height": 0.1,
                    },
                },
                {
                    "runtime_id": "messages",
                    "parent_runtime_id": "__probe_root__",
                    "control_type": "ControlType.Document",
                    "class_name": "",
                    "automation_id": "",
                    "patterns": ["textpattern"],
                    "semantic_anchors": ["message_region"],
                    "normalized_bounds": {
                        "x": 0.25,
                        "y": 0.1,
                        "width": 0.75,
                        "height": 0.6,
                    },
                },
            ],
        },
        "privacy": {
            "emitted_control_names": False,
            "emitted_message_text": False,
            "changed_window_state": False,
        },
    }


def profile_for(report: dict) -> CertifiedQQProfile:
    runtime, _ = ingest_probe_report(report, "qq-uia-readonly-v2")
    return CertifiedQQProfile(
        profile_id="certified-test",
        allowed_executable_paths=(runtime.executable_path,),
        allowed_executable_signatures=(runtime.executable_signature,),
        allowed_process_signatures=(runtime.process_signature,),
        allowed_window_classes=(runtime.window_class,),
        certified_fingerprint=runtime.fingerprint,
    )


def write_application(path: Path, report: dict, profile: CertifiedQQProfile) -> str:
    assessment = QQReadOnlyFrontHalf(
        profile=profile, fixture_suite_version="qq-uia-readonly-v2"
    ).assess_probe(report)
    assert assessment.selector_pack_version is not None
    value = {
        "schema_version": "qq-q3-binding-application-v1",
        "platform": "qq",
        "status": "pending_human_binding",
        "binding_created": False,
        "automatic_eligible": False,
        "human_confirmation": {
            "action": "bind",
            "approved": True,
            "required": True,
            "scope": "conversation_selection_only",
        },
        "environment_fingerprint": assessment.environment.fingerprint.digest,
        "selector_pack_version": assessment.selector_pack_version,
        "right_region_evidence": {
            "active_header_digest": HASH,
            "structure_digest": "e" * 64,
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return assessment.selector_pack_version


class ProbePort:
    def __init__(self, value: dict) -> None:
        self.value = value

    def __call__(self) -> dict:
        return self.value


def inspected(
    _command: tuple[str, ...] | list[str], expected_header: str
) -> CurrentSessionSnapshot:
    return CurrentSessionSnapshot(
        process_id=42,
        process_started_at=STARTED,
        window_handle=9001,
        is_maximized=True,
        is_foreground=False,
        active_header_digest=expected_header,
        structure_digest="f" * 64,
    )


def test_reader_composes_exact_scope_without_contact_data(tmp_path: Path) -> None:
    report = probe_report()
    profile = profile_for(report)
    application = tmp_path / "pending.json"
    selector = write_application(application, report, profile)
    reader = QQManualSessionScopeReader(
        profile=profile,
        application_path=application,
        helper_command=["helper"],
        read_probe=lambda: report,
        inspect_session=inspected,
    )

    scope = reader()

    assert scope.process_started_at == STARTED
    assert scope.selector_pack_version == selector
    assert scope.structure_digest == "f" * 64
    assert "qq_number" not in repr(scope)


def test_dynamic_message_node_does_not_drift_the_scope(tmp_path: Path) -> None:
    baseline = probe_report()
    profile = profile_for(baseline)
    application = tmp_path / "pending.json"
    write_application(application, baseline, profile)
    port = ProbePort(baseline)
    reader = QQManualSessionScopeReader(
        profile=profile,
        application_path=application,
        helper_command=["helper"],
        read_probe=port,
        inspect_session=inspected,
    )
    before = reader()
    changed = copy.deepcopy(baseline)
    changed["topology"]["digest"] = "9" * 64
    changed["topology"]["nodes"].append(
        {
            "runtime_id": "new-message",
            "parent_runtime_id": "messages",
            "control_type": "ControlType.Text",
            "class_name": "message-bubble",
            "automation_id": "",
            "patterns": ["textpattern"],
            "semantic_anchors": [],
            "normalized_bounds": {
                "x": 0.5,
                "y": 0.5,
                "width": 0.2,
                "height": 0.1,
            },
        }
    )
    port.value = changed

    after = reader()

    assert before == after


def test_pending_application_file_change_drifts_scope_digest(tmp_path: Path) -> None:
    report = probe_report()
    profile = profile_for(report)
    application = tmp_path / "pending.json"
    write_application(application, report, profile)
    reader = QQManualSessionScopeReader(
        profile=profile,
        application_path=application,
        helper_command=["helper"],
        read_probe=lambda: report,
        inspect_session=inspected,
    )
    before = reader()
    value = json.loads(application.read_text(encoding="utf-8"))
    value["created_at"] = "2026-09-09T02:03:04+00:00"
    application.write_text(json.dumps(value), encoding="utf-8")

    after = reader()

    assert before.pending_application_digest != after.pending_application_digest
    assert before.scope_digest != after.scope_digest


def test_non_pending_or_unconfirmed_application_fails_closed(tmp_path: Path) -> None:
    report = probe_report()
    profile = profile_for(report)
    application = tmp_path / "pending.json"
    write_application(application, report, profile)
    value = json.loads(application.read_text(encoding="utf-8"))
    value["human_confirmation"]["approved"] = False
    application.write_text(json.dumps(value), encoding="utf-8")
    reader = QQManualSessionScopeReader(
        profile=profile,
        application_path=application,
        helper_command=["helper"],
        read_probe=lambda: report,
        inspect_session=inspected,
    )

    with pytest.raises(QQSessionScopeError) as failure:
        reader()

    assert failure.value.code == "APPLICATION_CONFIRMATION_INVALID"


def test_legacy_application_selector_requires_explicit_readonly_rebaseline(
    tmp_path: Path,
) -> None:
    report = probe_report()
    profile = profile_for(report)
    application = tmp_path / "pending.json"
    write_application(application, report, profile)
    value = json.loads(application.read_text(encoding="utf-8"))
    value["selector_pack_version"] = "q1:" + "7" * 64
    application.write_text(json.dumps(value), encoding="utf-8")
    strict = QQManualSessionScopeReader(
        profile=profile,
        application_path=application,
        helper_command=["helper"],
        read_probe=lambda: report,
        inspect_session=inspected,
    )
    rebased = QQManualSessionScopeReader(
        profile=profile,
        application_path=application,
        helper_command=["helper"],
        read_probe=lambda: report,
        inspect_session=inspected,
        allow_application_selector_rebaseline=True,
    )

    with pytest.raises(QQSessionScopeError) as failure:
        strict()

    assert failure.value.code == "APPLICATION_SELECTOR_MISMATCH"
    assert rebased().selector_pack_version != value["selector_pack_version"]


def test_transient_maximize_sample_is_retried_once(tmp_path: Path) -> None:
    report = probe_report()
    profile = profile_for(report)
    application = tmp_path / "pending.json"
    write_application(application, report, profile)
    calls = 0

    def flaky_inspector(
        command: tuple[str, ...], expected_header: str
    ) -> CurrentSessionSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CurrentSessionInspectionError("WINDOW_STATE_NOT_CERTIFIED")
        return inspected(command, expected_header)

    reader = QQManualSessionScopeReader(
        profile=profile,
        application_path=application,
        helper_command=["helper"],
        read_probe=lambda: report,
        inspect_session=flaky_inspector,
    )

    assert reader().window_handle == 9001
    assert calls == 2
