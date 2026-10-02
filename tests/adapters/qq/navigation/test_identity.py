from datetime import UTC, datetime, timedelta
import json
import struct

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.navigation.contracts import ContactTarget, NavigationFrame
from messenger_ai.adapters.qq.navigation.identity import (
    ActiveChatLease, ActiveChatVerificationResult, CurrentChatEvidence,
    CurrentChatWitness, ProfileIdentityExpectation, verify_current_chat,
)


BASE = datetime(2026, 10, 2, tzinfo=UTC)
NOW = BASE + timedelta(seconds=4)
NS = 1_000_000_000
PROCESS_START = 123456789


@pytest.fixture
def case():
    target = ContactTarget(
        account_id="account", conversation_id="conversation", binding_id="binding",
        binding_revision=7, display_name="Registered label", identity_mode="persistent",
    )
    frame = NavigationFrame(
        frame_id="frame", run_id="run", session_epoch="session", surface_epoch="surface",
        worker_epoch="worker", desktop_lease_id="desktop", control_revision=3,
        binding_id=target.binding_id, binding_revision=target.binding_revision,
        process_id=123, window_handle=456, captured_at=BASE,
        screen_width=100, screen_height=100, crop_origin_x=0, crop_origin_y=0,
        crop_width=100, crop_height=100, dpi_scale=1.0, allowed_regions=(),
        privacy_mask_applied=True,
        png_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 100, 100),
    )
    expectation = ProfileIdentityExpectation(
        account_id=target.account_id, conversation_id=target.conversation_id,
        binding_id=target.binding_id, binding_revision=target.binding_revision,
        expected_profile_hmac="a" * 64, hmac_key_id="qq.identity.hmac.v1",
        client_version="qq-version", selector_pack_version="selectors",
        environment_fingerprint="e" * 64,
    )
    common = dict(
        account_id=target.account_id, conversation_id=target.conversation_id,
        binding_id=target.binding_id, binding_revision=target.binding_revision,
        run_id=frame.run_id, session_epoch=frame.session_epoch, surface_epoch=frame.surface_epoch,
        worker_epoch=frame.worker_epoch, observation_epoch="observation",
        desktop_lease_id=frame.desktop_lease_id, control_revision=frame.control_revision,
        process_id=frame.process_id, process_started_at_100ns=PROCESS_START,
        window_handle=frame.window_handle,
        header_digest="b" * 64, selected_row_runtime_id_hash="c" * 64,
        selected_row_candidate_count=1, selected_row_selection_source="selected_class",
        active_chat_structure_digest="d" * 64, conversation_type="direct",
        group_marker_probe_complete=True, group_marker_count=0,
        latest_tail=True, composer_empty=True,
    )
    evidence = CurrentChatEvidence(
        evidence_ref="capture", frame_id=frame.frame_id, source="normal_ui_profile",
        identity_evidence_type="explicit_labeled_qq_id",
        client_version=expectation.client_version,
        selector_pack_version=expectation.selector_pack_version,
        environment_fingerprint=expectation.environment_fingerprint,
        hmac_key_id=expectation.hmac_key_id, profile_id_hmac=expectation.expected_profile_hmac,
        profile_candidate_count=1, profile_window_candidate_count=1, profile_window_was_new=True,
        profile_window_handle=789, profile_process_id=frame.process_id,
        profile_captured_at=BASE + timedelta(seconds=2), profile_captured_monotonic_ns=2 * NS,
        correlation_method="profile_from_current_header_with_selected_row_fence",
        profile_opened_from_current_header=True, profile_window_closed=True,
        original_chat_restored=True, foreground_restored=True,
        before=CurrentChatWitness(captured_at=BASE + timedelta(seconds=1),
                                  captured_monotonic_ns=NS, **common),
        after=CurrentChatWitness(captured_at=BASE + timedelta(seconds=3),
                                 captured_monotonic_ns=3 * NS, **common),
    )
    return target, frame, expectation, evidence


def check(case, **kwargs):
    parameters = dict(
        now=NOW, deadline_at=BASE + timedelta(seconds=30),
        expected_process_started_at_100ns=PROCESS_START,
        expected_observation_epoch="observation", now_monotonic_ns=4 * NS,
    )
    parameters.update(kwargs)
    return verify_current_chat(*case, **parameters)


def with_evidence(case, **updates):
    return (*case[:3], case[3].model_copy(update=updates))


def with_witness(case, side, **updates):
    return with_evidence(case, **{side: getattr(case[3], side).model_copy(update=updates)})


def refused(result, code):
    assert result.verified is False
    assert result.lease is None
    assert result.error_code == code


def test_complete_profile_cycle_issues_target_and_full_scope_bound_lease(case):
    originals = [value.model_dump() for value in case]
    result = check(case)
    assert result.verified and result.error_code is None
    lease = result.lease
    assert lease is not None
    for field in ("account_id", "conversation_id", "binding_id", "binding_revision"):
        assert getattr(lease, field) == getattr(case[0], field)
    for field in ("run_id", "session_epoch", "surface_epoch", "worker_epoch",
                  "desktop_lease_id", "control_revision", "process_id", "window_handle"):
        assert getattr(lease, field) == getattr(case[1], field)
    assert lease.process_started_at_100ns == PROCESS_START
    assert lease.observation_epoch == "observation"
    assert lease.frame_id == "frame" and lease.evidence_ref == "capture"
    assert lease.issued_at == NOW
    assert lease.expires_at == BASE + timedelta(seconds=13)
    assert lease.expires_monotonic_ns == 13 * NS
    assert [value.model_dump() for value in case] == originals
    assert check(case) == result  # No hidden clock, randomness, registry write or renewal.


def test_fresh_row_token_is_not_a_registered_identity_or_locator(case):
    changed = with_witness(case, "before", selected_row_runtime_id_hash="f" * 64)
    changed = with_witness(changed, "after", selected_row_runtime_id_hash="f" * 64)
    assert check(changed).verified
    assert "selected_row_runtime_id_hash" not in ProfileIdentityExpectation.model_fields
    assert "participant_signature" not in ProfileIdentityExpectation.model_fields
    assert "selected_row_runtime_id_hash" not in ActiveChatLease.model_fields
    # Registered display text is not consulted to infer identity.
    other_label = case[0].model_copy(update={"display_name": "Same-name impostor"})
    assert check((other_label, *case[1:])).verified


def test_session_bound_has_no_implicit_profile_or_header_fallback(case):
    target = case[0].model_copy(update={"identity_mode": "session_bound"})
    refused(check((target, *case[1:])), "identity_mode_unproven")


@pytest.mark.parametrize("field,value", [
    ("account_id", "other"), ("conversation_id", "other"),
    ("binding_id", "other"), ("binding_revision", 8),
])
def test_expectation_cannot_be_applied_to_another_business_target(case, field, value):
    expectation = case[2].model_copy(update={field: value})
    refused(check((case[0], case[1], expectation, case[3])), "identity_target_mismatch")


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("field,value", [
    ("account_id", "other"), ("conversation_id", "other"),
    ("binding_id", "other"), ("binding_revision", 8),
])
def test_each_witness_is_bound_to_the_business_target(case, side, field, value):
    refused(check(with_witness(case, side, **{field: value})), "identity_target_mismatch")


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("field,value", [
    ("run_id", "other"), ("session_epoch", "other"), ("surface_epoch", "other"),
    ("worker_epoch", "other"), ("desktop_lease_id", "other"),
    ("control_revision", 4), ("process_id", 124), ("window_handle", 457),
])
def test_each_witness_is_bound_to_live_frame_epochs_and_desktop_ownership(case, side, field, value):
    refused(check(with_witness(case, side, **{field: value})), "identity_scope_mismatch")


@pytest.mark.parametrize("side", ["before", "after"])
def test_pid_reuse_and_observation_replay_are_rejected(case, side):
    refused(check(with_witness(case, side, process_started_at_100ns=PROCESS_START + 1)),
            "identity_process_lifetime_mismatch")
    refused(check(with_witness(case, side, observation_epoch="old-observation")),
            "identity_observation_mismatch")


def test_expected_worker_lifetime_and_observation_are_not_taken_from_evidence(case):
    refused(check(case, expected_process_started_at_100ns=PROCESS_START + 1),
            "identity_process_lifetime_mismatch")
    refused(check(case, expected_observation_epoch="fresh-worker-observation"),
            "identity_observation_mismatch")


@pytest.mark.parametrize("field,value", [("binding_id", "other"), ("binding_revision", 8)])
def test_frame_must_match_target_revision(case, field, value):
    frame = case[1].model_copy(update={field: value})
    refused(check((case[0], frame, *case[2:])), "identity_frame_mismatch")


def test_evidence_cannot_be_rebound_to_another_frame(case):
    refused(check(with_evidence(case, frame_id="previous-frame")), "identity_frame_mismatch")


@pytest.mark.parametrize("field,value", [
    ("client_version", "other"), ("selector_pack_version", "other"),
    ("environment_fingerprint", "f" * 64),
])
def test_profile_evidence_uses_expected_certified_versions(case, field, value):
    refused(check(with_evidence(case, **{field: value})), "identity_evidence_version_mismatch")


def test_key_rotation_is_not_silently_treated_as_a_new_identity(case):
    refused(check(with_evidence(case, hmac_key_id="other-key")), "identity_profile_key_mismatch")


@pytest.mark.parametrize("updates,code", [
    ({"profile_id_hmac": "f" * 64}, "identity_profile_mismatch"),
    ({"profile_id_hmac": None}, "identity_profile_unproven"),
    ({"profile_candidate_count": 0}, "identity_profile_unproven"),
    ({"profile_candidate_count": 2}, "identity_profile_unproven"),
])
def test_only_one_expected_labeled_profile_identity_is_accepted(case, updates, code):
    refused(check(with_evidence(case, **updates)), code)


@pytest.mark.parametrize("updates", [
    {"correlation_method": None}, {"profile_opened_from_current_header": False},
    {"profile_process_id": 999}, {"profile_window_handle": 456},
    {"profile_window_candidate_count": 0}, {"profile_window_candidate_count": 2},
    {"profile_window_was_new": False},
])
def test_matching_hmac_and_header_do_not_replace_current_chat_correlation(case, updates):
    refused(check(with_evidence(case, **updates)), "identity_chat_correlation_unproven")


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("updates", [
    {"header_digest": None}, {"selected_row_runtime_id_hash": None},
    {"selected_row_candidate_count": 0}, {"selected_row_candidate_count": 2},
    {"selected_row_selection_source": None}, {"active_chat_structure_digest": None},
])
def test_both_sides_require_independent_unique_selected_chat_evidence(case, side, updates):
    refused(check(with_witness(case, side, **updates)), "identity_chat_correlation_unproven")


@pytest.mark.parametrize("field", [
    "header_digest", "selected_row_runtime_id_hash", "active_chat_structure_digest",
])
def test_manual_same_name_switch_and_chat_drift_break_the_profile_fence(case, field):
    refused(check(with_witness(case, "after", **{field: "f" * 64})), "identity_chat_drift")


def test_equivalent_independent_selection_sources_are_allowed(case):
    assert check(with_witness(case, "after", selected_row_selection_source="selection_pattern")).verified


@pytest.mark.parametrize("field", [
    "profile_window_closed", "original_chat_restored", "foreground_restored",
])
def test_failed_exact_profile_recovery_never_issues_lease(case, field):
    refused(check(with_evidence(case, **{field: False})), "identity_profile_recovery_failed")


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("updates,code", [
    ({"conversation_type": "group"}, "identity_conversation_not_direct"),
    ({"conversation_type": "unknown"}, "identity_conversation_not_direct"),
    ({"group_marker_count": 1}, "identity_conversation_not_direct"),
    ({"group_marker_probe_complete": False}, "identity_group_probe_incomplete"),
    ({"latest_tail": False}, "identity_message_tail_unproven"),
    ({"latest_tail": None}, "identity_message_tail_unproven"),
    ({"composer_empty": False}, "identity_composer_not_empty"),
    ({"composer_empty": None}, "identity_composer_not_empty"),
])
def test_direct_tail_and_empty_composer_are_required_on_both_sides(case, side, updates, code):
    refused(check(with_witness(case, side, **updates)), code)


@pytest.mark.parametrize("kwargs", [
    {"now": BASE + timedelta(seconds=13)},
    {"now_monotonic_ns": 13 * NS},
    {"now": BASE + timedelta(seconds=100), "now_monotonic_ns": 100 * NS},
])
def test_evidence_expires_under_either_clock(case, kwargs):
    # A generous deadline isolates freshness from the action's budget.
    kwargs["deadline_at"] = BASE + timedelta(seconds=200)
    refused(check(case, **kwargs), "identity_evidence_stale")


@pytest.mark.parametrize("updates", [
    {"profile_captured_at": BASE},
    {"profile_captured_at": BASE + timedelta(seconds=4)},
    {"profile_captured_monotonic_ns": 0},
    {"profile_captured_monotonic_ns": 4 * NS},
])
def test_profile_read_must_be_inside_both_capture_fences(case, updates):
    refused(check(with_evidence(case, **updates)), "identity_capture_order_invalid")


def test_future_capture_clock_rollback_and_old_preframe_witness_reject(case):
    refused(check(case, now=BASE + timedelta(seconds=2)), "identity_capture_order_invalid")
    refused(check(case, now_monotonic_ns=2 * NS), "identity_capture_order_invalid")
    refused(check(with_witness(case, "before", captured_at=BASE - timedelta(seconds=1))),
            "identity_capture_order_invalid")


def test_lease_deadline_and_ttl_never_extend_the_original_budget(case):
    refused(check(case, deadline_at=NOW), "identity_deadline_exhausted")
    lease = check(case, deadline_at=NOW + timedelta(seconds=2), ttl_seconds=5).lease
    assert lease.expires_at == NOW + timedelta(seconds=2)
    assert lease.expires_monotonic_ns == 6 * NS
    short = check(case, ttl_seconds=0.5).lease
    assert short.expires_at == NOW + timedelta(seconds=0.5)
    assert short.expires_monotonic_ns == 4 * NS + NS // 2


def test_rechecking_old_evidence_does_not_renew_its_lifetime(case):
    first = check(case).lease
    second = check(case, now=BASE + timedelta(seconds=12), now_monotonic_ns=12 * NS).lease
    assert first.expires_at == second.expires_at
    assert first.expires_monotonic_ns == second.expires_monotonic_ns


@pytest.mark.parametrize("field", ["ttl_seconds", "max_evidence_age_seconds"])
@pytest.mark.parametrize("value", [0, -1, 15.01, float("nan"), float("inf"), True, "10"])
def test_freshness_policy_cannot_disable_or_inflate_bounds(case, field, value):
    with pytest.raises(ValueError):
        check(case, **{field: value})


@pytest.mark.parametrize("kwargs", [
    {"now": NOW.replace(tzinfo=None)}, {"deadline_at": NOW.replace(tzinfo=None)},
    {"now_monotonic_ns": -1}, {"now_monotonic_ns": True},
    {"expected_process_started_at_100ns": 0}, {"expected_process_started_at_100ns": True},
    {"expected_observation_epoch": ""},
])
def test_verifier_requires_explicit_well_formed_clock_and_worker_context(case, kwargs):
    with pytest.raises(ValueError):
        check(case, **kwargs)


def test_lease_consumer_checks_both_clocks_and_never_accepts_backward_time(case):
    lease = check(case).lease
    assert lease.is_fresh(now=NOW, now_monotonic_ns=4 * NS)
    assert not lease.is_fresh(now=lease.expires_at, now_monotonic_ns=4 * NS)
    assert not lease.is_fresh(now=NOW, now_monotonic_ns=lease.expires_monotonic_ns)
    assert not lease.is_fresh(now=NOW - timedelta(seconds=1), now_monotonic_ns=4 * NS)
    assert not lease.is_fresh(now=NOW, now_monotonic_ns=3 * NS)
    assert not lease.is_fresh(now=NOW, now_monotonic_ns=True)


@pytest.mark.parametrize("updates", [
    {"verified": True}, {"verified": False}, {"verified": False, "lease": "sentinel"},
    {"verified": True, "error_code": "failure"},
    {"verified": False, "error_code": "private message contents"},
])
def test_result_cannot_mix_success_failure_or_unbounded_diagnostics(case, updates):
    if updates.get("lease") == "sentinel":
        updates = dict(updates, lease=check(case).lease)
    with pytest.raises(ValidationError):
        ActiveChatVerificationResult(**updates)


def test_contracts_are_closed_content_free_strict_and_serializable(case):
    for value, name in ((case[2], "raw_qq_id"), (case[3], "message_text"),
                        (case[3].after, "composer_text"), (check(case).lease, "display_name")):
        with pytest.raises(ValidationError):
            type(value).model_validate({**value.model_dump(), name: "private contents"})
        with pytest.raises(ValidationError):
            value.__setattr__(next(iter(type(value).model_fields)), "mutated")
        assert type(value).model_validate_json(value.model_dump_json()) == value
    for field, value in (("latest_tail", "true"), ("composer_empty", 1), ("process_id", True)):
        with pytest.raises(ValidationError):
            CurrentChatWitness.model_validate({**case[3].after.model_dump(), field: value})
    for field, value in (("source", "vision_model"), ("identity_evidence_type", "display_name"),
                         ("profile_id_hmac", "12345678"), ("correlation_method", "header_only")):
        with pytest.raises(ValidationError):
            CurrentChatEvidence.model_validate({**case[3].model_dump(), field: value})
    assert "Registered label" not in json.dumps(check(case).model_dump(mode="json"))


@pytest.mark.parametrize("field,value", [("surface_kind", "service"), ("surface_kind", False),
    ("header_candidate_count", False), ("message_candidate_count", "0"),
    ("composer_candidate_count", -1), ("header_candidate_count", 10001)])
def test_local_surface_metadata_is_strict_and_defaults_to_unknown(case, field, value):
    assert case[3].before.surface_kind == "unknown"
    assert case[3].before.header_candidate_count is None
    with pytest.raises(ValidationError):
        CurrentChatWitness.model_validate({**case[3].before.model_dump(), field: value})


@pytest.mark.parametrize("updates", [{"surface_kind": "non_chat"}, {"header_candidate_count": 0},
    {"message_candidate_count": 2}, {"composer_candidate_count": 0}])
def test_local_non_chat_or_partial_surface_never_issues_lease_even_with_matching_profile(case, updates):
    target, frame, expectation, evidence = case
    changed = evidence.model_copy(update={"before": evidence.before.model_copy(update=updates)})
    result = check((target, frame, expectation, changed))
    assert not result.verified and result.lease is None
    assert result.error_code == "identity_chat_correlation_unproven"


def test_serialized_lease_cannot_claim_an_unbounded_lifetime(case):
    lease = check(case).lease
    for updates in (
        {"expires_at": lease.issued_at},
        {"expires_at": lease.issued_at + timedelta(seconds=16)},
        {"expires_monotonic_ns": lease.issued_monotonic_ns},
        {"expires_monotonic_ns": lease.issued_monotonic_ns + 16 * NS},
    ):
        with pytest.raises(ValidationError):
            ActiveChatLease.model_validate({**lease.model_dump(), **updates})
