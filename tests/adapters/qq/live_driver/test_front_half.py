from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from messenger_ai.adapters.qq.live_driver.environment import CertifiedQQProfile
from messenger_ai.adapters.qq.live_driver.front_half import (
    FrontHalfNotReadyError,
    FrontHalfStatus,
    QQReadOnlyFrontHalf,
)
from messenger_ai.adapters.qq.live_driver.identity import (
    ConfirmationAction,
    ConversationType,
    HumanBindingConfirmation,
    IdentityEvidenceSet,
)
from messenger_ai.adapters.qq.live_driver.observation import (
    VisibleConversationSnapshot,
    VisibleDirection,
    VisibleMessage,
)
from messenger_ai.adapters.qq.live_driver.probe_bridge import ingest_probe_report
from scripts.qq_live_readonly_assess import summarize_candidate

NOW = datetime(2026, 9, 8, 10, tzinfo=UTC)
HASH = "a" * 64


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


def build_service(value: dict | None = None) -> tuple[QQReadOnlyFrontHalf, dict]:
    report = value or probe_report()
    runtime, _ = ingest_probe_report(report, "q0-q3-v1")
    profile = CertifiedQQProfile(
        profile_id="qq-certified-layout-a",
        allowed_executable_paths=(runtime.executable_path,),
        allowed_executable_signatures=(runtime.executable_signature,),
        allowed_process_signatures=(runtime.process_signature,),
        allowed_window_classes=(runtime.window_class,),
        certified_fingerprint=runtime.fingerprint,
    )
    return (
        QQReadOnlyFrontHalf(
            profile=profile,
            fixture_suite_version="q0-q3-v1",
            clock=lambda: NOW + timedelta(seconds=1),
        ),
        report,
    )


def evidence(assessment) -> IdentityEvidenceSet:
    return IdentityEvidenceSet(
        window_handle=assessment.runtime.window_handle,
        platform_conversation_id="qq-conversation-1",
        conversation_type=ConversationType.DIRECT,
        structure_path=("main_window", "conversation_item"),
        participant_signature="participant-stable-1",
        profile_id="qq-profile-1",
        avatar_hash="e" * 64,
        recent_message_anchor="anchor-1",
        environment_fingerprint=assessment.environment.fingerprint.digest,
        selector_pack_version=assessment.selector_pack_version,
        confidence=0.99,
        display_name="presentation-only",
        list_position=7,
    )


def test_q0_q1_ready_assessment_compiles_read_only_selector_pack() -> None:
    service, report = build_service()

    result = service.assess_probe(report)

    assert result.status is FrontHalfStatus.READY
    assert result.observation_ready is True
    assert result.selector_pack_version.startswith("q1:")
    assert {item.role.value for item in result.selector_result.pack.selectors} == {
        "main_window",
        "conversation_list",
        "conversation_item",
        "message_region",
    }


def test_dynamic_messages_and_runtime_ids_do_not_drift_selector_version() -> None:
    service, report = build_service()
    baseline = service.assess_probe(report)
    changed = copy.deepcopy(report)
    changed["topology"]["digest"] = "9" * 64
    rename = {
        "list": "list-restarted",
        "item": "item-restarted",
        "messages": "messages-restarted",
    }
    for item in changed["topology"]["nodes"]:
        item["runtime_id"] = rename[item["runtime_id"]]
        parent = item["parent_runtime_id"]
        item["parent_runtime_id"] = rename.get(parent, parent)
    changed["topology"]["nodes"].append(
        {
            "runtime_id": "new-visible-message",
            "parent_runtime_id": "messages-restarted",
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

    after_message = service.assess_probe(changed)

    assert baseline.topology.tree_digest != after_message.topology.tree_digest
    assert (
        baseline.selector_result.pack.topology_digest
        == after_message.selector_result.pack.topology_digest
    )
    assert baseline.selector_pack_version == after_message.selector_pack_version


def test_selected_role_structure_change_drifts_selector_version() -> None:
    service, report = build_service()
    baseline = service.assess_probe(report)
    changed = copy.deepcopy(report)
    changed["topology"]["digest"] = "8" * 64
    message_region = changed["topology"]["nodes"][2]
    message_region["class_name"] = "changed-region-class"
    message_region["normalized_bounds"]["x"] = 0.3
    message_region["normalized_bounds"]["width"] = 0.7

    altered = service.assess_probe(changed)

    assert altered.status is FrontHalfStatus.READY
    assert baseline.selector_pack_version != altered.selector_pack_version
    assert (
        baseline.selector_result.pack.topology_digest
        != altered.selector_result.pack.topology_digest
    )


def test_runtime_or_topology_failure_never_reaches_observation() -> None:
    service, report = build_service()
    minimized = copy.deepcopy(report)
    minimized["host_environment"]["presentation"] = "minimized"
    minimized["window_bounds"].update(
        minimized=True, maximized=False, geometry_usable=False
    )
    assessment = service.assess_probe(minimized)
    assert assessment.status is FrontHalfStatus.RUNTIME_BLOCKED
    with pytest.raises(FrontHalfNotReadyError):
        service.bind(
            assessment,
            local_contact_id="contact-1",
            hub_conversation_id="hub-1",
            account_id="account-1",
            evidence_set=evidence(assessment),
            confirmation=HumanBindingConfirmation(
                action=ConfirmationAction.BIND,
                confirmed_by="local-user",
                confirmed_at=NOW,
                reason="explicit local confirmation",
            ),
        )

    ambiguous = copy.deepcopy(report)
    duplicate = copy.deepcopy(ambiguous["topology"]["nodes"][1])
    duplicate["runtime_id"] = "item-two"
    ambiguous["topology"]["nodes"].append(duplicate)
    assert service.assess_probe(ambiguous).status is FrontHalfStatus.TOPOLOGY_BLOCKED


def test_human_binding_then_readonly_observation_and_deduplication() -> None:
    service, report = build_service()
    assessment = service.assess_probe(report)
    identity = evidence(assessment)
    binding = service.bind(
        assessment,
        local_contact_id="contact-1",
        hub_conversation_id="hub-1",
        account_id="account-1",
        evidence_set=identity,
        confirmation=HumanBindingConfirmation(
            action=ConfirmationAction.BIND,
            confirmed_by="local-user",
            confirmed_at=NOW - timedelta(seconds=1),
            reason="explicit local workbench confirmation",
        ),
    )
    assert binding.automatic_eligible is True
    message = VisibleMessage(
        message_watermark="watermark-1",
        source_evidence_hash="f" * 64,
        direction=VisibleDirection.INBOUND,
        text="你好",
        observed_at=NOW,
        observer_confidence=0.99,
        direction_confidence=0.99,
        time_confidence=0.99,
    )
    snapshot = VisibleConversationSnapshot(
        process_id=assessment.runtime.process_id,
        window_handle=assessment.runtime.window_handle,
        captured_at=NOW,
        expires_at=NOW + timedelta(seconds=5),
        is_minimized=False,
        identity_evidence=identity,
        conversation_confidence=0.99,
        messages=(message,),
    )

    first = service.observe(assessment, snapshot, account_id="account-1")
    second = service.observe(assessment, snapshot, account_id="account-1")

    assert len(first.candidates) == 1
    assert first.candidates[0].read_only is True
    assert second.candidates == ()


def test_identity_scope_cannot_cross_environment_or_selector_pack() -> None:
    service, report = build_service()
    assessment = service.assess_probe(report)
    mismatched = evidence(assessment).model_copy(
        update={"environment_fingerprint": "0" * 64}
    )
    with pytest.raises(FrontHalfNotReadyError, match="environment"):
        service.bind(
            assessment,
            local_contact_id="contact-1",
            hub_conversation_id="hub-1",
            account_id="account-1",
            evidence_set=mismatched,
            confirmation=HumanBindingConfirmation(
                action=ConfirmationAction.BIND,
                confirmed_by="local-user",
                confirmed_at=NOW,
                reason="explicit local confirmation",
            ),
        )


def test_facade_has_no_write_or_send_surface() -> None:
    forbidden = {"write", "compose", "commit", "send", "invoke", "click"}
    assert forbidden.isdisjoint(dir(QQReadOnlyFrontHalf))


def test_candidate_summary_never_self_certifies_or_sends() -> None:
    summary = summarize_candidate(probe_report())

    assert summary["certification"] == "candidate_only"
    assert summary["observation_ready"] is False
    assert summary["real_send_attempted"] is False
    assert summary["next_gate"] == "review_and_certify_profile"
    assert set(summary["q1"]["roles"]) == {
        "main_window",
        "conversation_list",
        "conversation_item",
        "message_region",
    }


def test_candidate_summary_reports_minimized_runtime_as_unsuitable() -> None:
    report = copy.deepcopy(probe_report())
    report["host_environment"]["presentation"] = "minimized"
    report["window_bounds"].update(
        minimized=True, maximized=False, geometry_usable=False
    )

    summary = summarize_candidate(report)

    assert summary["runtime"]["presentation"] == "minimized"
    assert summary["candidate_runtime_suitable"] is False
    assert summary["next_gate"] == "fix_runtime_or_topology"
