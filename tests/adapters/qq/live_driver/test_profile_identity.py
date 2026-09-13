from __future__ import annotations

import pytest

from messenger_ai.adapters.qq.live_driver.identity import (
    ConversationType,
    IdentityEvidenceSet,
)
from messenger_ai.adapters.qq.live_driver.profile_identity import (
    ProfileIdentityError,
    assess_pending_application,
    parse_guest_foreground_profile_report,
    parse_profile_report,
)

HASH = "a" * 64


def evidence() -> IdentityEvidenceSet:
    return IdentityEvidenceSet(
        window_handle=9001,
        platform_conversation_id="qq-conversation",
        conversation_type=ConversationType.DIRECT,
        structure_path=("main_window", "conversation_item"),
        participant_signature="participant-structural-digest",
        environment_fingerprint=HASH,
        selector_pack_version="selector-v1",
        confidence=0.99,
    )


def report(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "probe_version": "qq-uia-current-identity-v1",
        "mode": "current_chat_identity",
        "succeeded": True,
        "status": "STABLE_IDENTITY_CAPTURED",
        "process_id": 1234,
        "window_handle": 9001,
        "is_maximized": True,
        "is_foreground_before": False,
        "is_foreground_after": False,
        "active_header_digest": HASH,
        "right_region_structure_digest": "b" * 64,
        "header_candidate_count": 1,
        "identity_candidate_count": 1,
        "profile_id_hmac": "c" * 64,
        "identity_evidence_type": "explicit_labeled_qq_id",
        "profile_structure_digest": "d" * 64,
        "privacy": {
            "exact_hwnd": True,
            "raw_profile_id_emitted": False,
            "hmac_key_from_stdin_only": True,
            "emitted_chat_text": False,
            "emitted_control_names": False,
            "desktop_capture_supported": False,
            "mouse_input_used": False,
            "keyboard_input_used": False,
            "clipboard_used": False,
            "foreground_requested": False,
            "composer_send_attempted": False,
            "composer_or_send_accessed": False,
            "write_actions_supported": False,
            "hmac_key_emitted": False,
            "transient_navigation_performed": True,
        },
        "recovery": {
            "attempted": True,
            "original_view_restored": True,
            "foreground_changed": False,
        },
    }
    value.update(changes)
    return value


def test_guest_foreground_profile_report_has_distinct_vm_and_recovery_contract():
    value = report(
        probe_version="qq-uia-guest-foreground-identity-v1",
        mode="guest_foreground_current_chat_identity",
        is_foreground_before=True,
        is_foreground_after=True,
        guest_environment={"certified": True, "machine": "PMAI-QQVM", "user": "qqbot", "hypervisor": "virtualbox"},
    )
    value["privacy"] = dict(value["privacy"], foreground_requested=True)
    parsed = parse_guest_foreground_profile_report(value, window_handle=9001,
        environment_fingerprint=HASH, selector_pack_version="selector-v1",
        expected_header_digest=HASH, expected_process_id=1234)
    assert parsed["profile_id_hmac"] == "c" * 64


@pytest.mark.parametrize("recovery", [
    {"attempted": True, "original_view_restored": True},
    {"attempted": True, "original_view_restored": True, "foreground_changed": True},
])
def test_guest_profile_rejects_missing_or_failed_recovery(recovery):
    value = report(probe_version="qq-uia-guest-foreground-identity-v1",
        mode="guest_foreground_current_chat_identity", is_foreground_before=True,
        is_foreground_after=True, recovery=recovery,
        guest_environment={"certified": True, "machine": "PMAI-QQVM", "user": "qqbot", "hypervisor": "virtualbox"})
    value["privacy"] = dict(value["privacy"], foreground_requested=True)
    with pytest.raises(ProfileIdentityError, match="RECOVERY_CONTRACT_FAILED"):
        parse_guest_foreground_profile_report(value, window_handle=9001,
            environment_fingerprint=HASH, selector_pack_version="selector-v1",
            expected_header_digest=HASH, expected_process_id=1234)


def test_guest_profile_binds_the_preflight_right_region_digest():
    value = report(probe_version="qq-uia-guest-foreground-identity-v1",
        mode="guest_foreground_current_chat_identity", is_foreground_before=True,
        is_foreground_after=True,
        guest_environment={"certified": True, "machine": "PMAI-QQVM", "user": "qqbot", "hypervisor": "virtualbox"})
    value["privacy"] = dict(value["privacy"], foreground_requested=True)
    with pytest.raises(ProfileIdentityError, match="RIGHT_REGION_MISMATCH"):
        parse_guest_foreground_profile_report(value, window_handle=9001,
            environment_fingerprint=HASH, selector_pack_version="selector-v1",
            expected_header_digest=HASH, expected_process_id=1234,
            expected_right_region_structure_digest="e" * 64)


def application() -> dict[str, object]:
    return {
        "schema_version": "qq-q3-binding-application-v1",
        "status": "pending_human_binding",
        "automatic_eligible": False,
        "binding_created": False,
    }


def parse(value: dict[str, object] | None = None) -> dict[str, object]:
    return parse_profile_report(
        value or report(),
        window_handle=9001,
        environment_fingerprint=HASH,
        selector_pack_version="selector-v1",
        expected_header_digest=HASH,
    )


def test_profile_hmac_is_consumed_as_second_stable_signal_only() -> None:
    updated = assess_pending_application(
        application(), parse(), identity_evidence=evidence()
    )
    assert updated["profile_id"] == "hmac-sha256:" + "c" * 64
    assert updated["binding_created"] is False
    assert updated["automatic_eligible"] is False
    assert updated["binding_evidence_assessment"]["bindable"] is True
    assert updated["identity_evidence"]["profile_id"] == "hmac-sha256:" + "c" * 64
    assert "local_contact_id" not in updated


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"identity_candidate_count": 0}, "PROFILE_CANDIDATE_NOT_UNIQUE"),
        ({"identity_candidate_count": 2}, "PROFILE_CANDIDATE_NOT_UNIQUE"),
        ({"active_header_digest": "d" * 64}, "HEADER_MISMATCH"),
        ({"is_foreground_before": True}, "QQ_FOREGROUND"),
        ({"is_maximized": False}, "WINDOW_NOT_MAXIMIZED"),
        (
            {
                "recovery": {
                    "attempted": True,
                    "original_view_restored": False,
                    "foreground_changed": True,
                }
            },
            "RECOVERY_CONTRACT_FAILED",
        ),
        (
            {
                "privacy": {
                    "exact_hwnd": True,
                    "raw_profile_id_emitted": True,
                    "hmac_key_from_stdin_only": True,
                    "emitted_chat_text": False,
                    "emitted_control_names": False,
                    "desktop_capture_supported": False,
                    "mouse_input_used": False,
                    "keyboard_input_used": False,
                    "clipboard_used": False,
                    "foreground_requested": False,
                    "composer_send_attempted": False,
                    "composer_or_send_accessed": False,
                    "write_actions_supported": False,
                    "hmac_key_emitted": False,
                    "transient_navigation_performed": True,
                }
            },
            "PRIVACY_CONTRACT_FAILED",
        ),
    ],
)
def test_profile_gate_rejects_unsafe_or_ambiguous_report(
    changes: dict[str, object], code: str
) -> None:
    with pytest.raises(ProfileIdentityError, match=code):
        parse_profile_report(
            report(**changes),
            window_handle=9001,
            environment_fingerprint=HASH,
            selector_pack_version="selector-v1",
            expected_header_digest=HASH,
        )


def test_profile_id_digest_or_raw_id_is_not_accepted_or_leaked() -> None:
    value = report(profile_id_digest="c" * 64)
    value.pop("profile_id_hmac")
    with pytest.raises(ProfileIdentityError, match="MISSING_FIELD") as caught:
        parse_profile_report(
            value,
            window_handle=9001,
            environment_fingerprint=HASH,
            selector_pack_version="selector-v1",
            expected_header_digest=HASH,
        )
    assert "c" * 64 not in str(caught.value)
    with pytest.raises(ProfileIdentityError, match="PRIVACY_CONTRACT_FAILED"):
        parse(report(profile_id="123456"))


def test_existing_active_application_is_never_updated() -> None:
    with pytest.raises(ProfileIdentityError, match="APPLICATION_NOT_PENDING"):
        assess_pending_application(
            {**application(), "status": "active"}, parse(), identity_evidence=evidence()
        )
