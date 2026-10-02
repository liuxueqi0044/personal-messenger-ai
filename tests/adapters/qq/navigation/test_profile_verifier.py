import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import subprocess
import traceback

import pytest
from messenger_ai.adapters.qq.navigation.profile_verifier import (
    ProfileAcquisitionError, ProfileCurrentChatVerifier, ProfileVerificationContext,
    ValidatedProfileAcquisition, assemble_current_chat_evidence,
    capture_profile_acquisition, parse_profile_acquisition_report,
)
from messenger_ai.adapters.qq.vm_driver.profile_identity import (
    ProfileCaptureError, capture_current_profile,
)
from .test_identity import BASE, NS, PROCESS_START, case  # Shared value-only contract fixture.


@pytest.fixture
def report(case):
    target, frame, expectation, evidence = case
    def fence(seconds):
        return dict(
            captured_at=(BASE + timedelta(seconds=seconds)).isoformat(),
            captured_monotonic_ns=int(seconds * NS), header_digest="f" * 64,
            selected_row_candidate_count=1, selected_row_runtime_id_hash="c" * 64,
            selected_row_selection_source="selected_class", active_chat_structure_digest="e" * 64,
        )
    return dict(
        probe_version="qq-uia-guest-foreground-identity-v1",
        mode="guest_foreground_current_chat_identity", succeeded=True, status="STABLE_IDENTITY_CAPTURED",
        process_id=frame.process_id, window_handle=frame.window_handle, is_maximized=True,
        is_foreground_before=True, is_foreground_after=True,
        guest_environment=dict(certified=True, machine="PMAI-QQVM", user="qqbot", hypervisor="virtualbox"),
        active_header_digest="f" * 64, right_region_structure_digest="e" * 64,
        header_candidate_count=1, identity_candidate_count=1,
        profile_id_hmac=expectation.expected_profile_hmac, identity_evidence_type="explicit_labeled_qq_id",
        profile_structure_digest="d" * 64,
        privacy=dict(exact_hwnd=True, raw_profile_id_emitted=False, hmac_key_from_stdin_only=True,
                     emitted_chat_text=False, emitted_control_names=False, desktop_capture_supported=False,
                     mouse_input_used=False, keyboard_input_used=False, clipboard_used=False,
                     foreground_requested=True, composer_send_attempted=False, composer_or_send_accessed=False,
                     write_actions_supported=False, hmac_key_emitted=False, transient_navigation_performed=True),
        recovery=dict(attempted=True, original_view_restored=True, foreground_changed=False),
        acquisition=dict(
            version="qq_profile_acquisition_v2", process_started_at_100ns=PROCESS_START,
            profile_window_handle=789, profile_process_id=frame.process_id,
            profile_window_candidate_count=1, profile_window_was_new=True,
            profile_captured_at=(BASE + timedelta(seconds=2)).isoformat(),
            profile_captured_monotonic_ns=2 * NS, profile_window_closed=True,
            original_chat_restored=True, foreground_restored=True,
            before=fence(1.25), after=fence(2.75),
        ),
    )


def parse(report):
    return parse_profile_acquisition_report(
        report, pid=123, hwnd=456, environment_fingerprint="e" * 64,
        selector_pack_version="selectors", expected_header_digest="f" * 64,
        expected_right_region_structure_digest="e" * 64,
    )


class Rig:
    def __init__(self, case, captured):
        self.target, self.frame, self.expectation, evidence = case
        self.context = ProfileVerificationContext(
            process_started_at_100ns=PROCESS_START, observation_epoch="observation",
        )
        self.witnesses = [evidence.before, evidence.after]
        self.captured = captured
        self.now, self.tick = BASE, 0
        self.events = []
        self.local_count = 0
        self.on_snapshot = None
        self.on_capture = None
        self.verifier = ProfileCurrentChatVerifier(
            expectation_lookup=lambda _: self.expectation,
            context_source=lambda *_: self.context,
            local_witness_source=self, profile_source=self,
            clock=lambda: self.now, monotonic_ns=lambda: self.tick,
        )

    async def snapshot(self, target, frame, *, deadline_at):
        index = self.local_count
        self.local_count += 1
        self.events.append((f"local{index}", deadline_at))
        self.now = BASE + timedelta(seconds=1 if index == 0 else 3)
        self.tick = (1 if index == 0 else 3) * NS
        if self.on_snapshot:
            self.on_snapshot(index)
        return self.witnesses[index]

    async def capture(self, target, frame, expectation, *, deadline_at):
        self.events.append(("profile", deadline_at))
        self.now, self.tick = BASE + timedelta(seconds=2.75), int(2.75 * NS)
        if self.on_capture:
            self.on_capture()
        return self.captured

    async def run(self, *, deadline=None):
        return await self.verifier.verify_with_evidence(
            self.target, self.frame, deadline_at=deadline or BASE + timedelta(seconds=30),
        )


@pytest.fixture
def rig(case, report):
    return Rig(case, parse(report))


def refuse(attempt, code):
    assert not attempt.result.verified
    assert attempt.result.lease is None
    assert attempt.result.error_code == code


def test_strict_projection_preserves_actual_metadata_without_enrollment(report):
    original = deepcopy(report)
    result = parse(report)
    assert isinstance(result, ValidatedProfileAcquisition)
    assert result.acquisition.before.captured_monotonic_ns == int(1.25 * NS)
    assert result.acquisition.profile_window_handle == 789
    assert result.profile.profile_id_hmac == "a" * 64
    assert "binding" not in result.model_dump_json()
    assert report == original


@pytest.mark.parametrize("path,value", [
    (("acquisition",), None),
    (("acquisition", "version"), "v1"),
    (("acquisition", "unexpected"), "private value"),
    (("acquisition", "process_started_at_100ns"), True),
    (("acquisition", "profile_window_candidate_count"), "1"),
    (("acquisition", "profile_window_was_new"), 1),
    (("acquisition", "profile_captured_at"), "2026-10-02T00:00:02"),
    (("acquisition", "before", "captured_monotonic_ns"), -1),
    (("acquisition", "before", "monotonic_ns"), 1),
    (("acquisition", "before", "header_digest"), "private value"),
    (("acquisition", "after", "selected_row_selection_source"), "model"),
    (("acquisition", "after", "selected_row_runtime_id_hash"), "private value"),
    (("acquisition", "profile_captured_monotonic_ns"), 4 * NS),
    (("acquisition", "profile_captured_at"), (BASE + timedelta(seconds=4)).isoformat()),
])
def test_closed_acquisition_schema_rejects_unknown_or_inconsistent_metadata(report, path, value):
    current = report
    for component in path[:-1]:
        current = current[component]
    current[path[-1]] = value
    with pytest.raises(ProfileAcquisitionError) as error:
        parse(report)
    assert error.value.code == "identity_profile_acquisition_invalid"
    assert "private value" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("field,value", [("profile_process_id", 999), ("profile_window_handle", 456)])
def test_profile_must_be_distinct_exact_window_of_same_process(report, field, value):
    report["acquisition"][field] = value
    with pytest.raises(ProfileAcquisitionError, match="identity_profile_window_mismatch"):
        parse(report)


@pytest.mark.parametrize("path,value", [
    (("recovery", "original_view_restored"), False),
    (("privacy", "emitted_chat_text"), True),
    (("privacy", "clipboard_used"), True),
    (("raw_profile_id",), "private number"),
    (("acquisition", "before", "raw_profile_id"), "private number"),
    (("process_id",), 124),
    (("right_region_structure_digest",), "0" * 64),
    (("identity_candidate_count",), True),
])
def test_legacy_recovery_privacy_and_scope_parser_remains_authority(report, path, value):
    current = report
    for component in path[:-1]:
        current = current[component]
    current[path[-1]] = value
    with pytest.raises(ProfileAcquisitionError) as error:
        parse(report)
    assert error.value.code == "identity_profile_report_invalid"
    assert "private number" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("raw", [None, [], "private report", 123])
def test_report_input_must_be_mapping(raw):
    with pytest.raises(ProfileAcquisitionError, match="identity_profile_report_invalid"):
        parse(raw)


async def test_complete_cycle_issues_full_scope_lease_without_cross_algorithm_hash_comparison(rig):
    # The helper and Python witness algorithms intentionally return different hashes.
    assert rig.captured.profile.active_header_digest != rig.witnesses[0].header_digest
    assert rig.captured.profile.right_region_structure_digest != rig.witnesses[0].active_chat_structure_digest
    original = rig.expectation.model_dump_json()
    attempt = await rig.run()
    assert attempt.result.verified and attempt.acquisition == rig.captured
    lease = attempt.result.lease
    for name in ("account_id", "conversation_id", "binding_id", "binding_revision"):
        assert getattr(lease, name) == getattr(rig.target, name)
    for name in ("run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
                 "control_revision", "process_id", "window_handle"):
        assert getattr(lease, name) == getattr(rig.frame, name)
    assert lease.observation_epoch == rig.context.observation_epoch
    assert lease.process_started_at_100ns == PROCESS_START
    assert lease.expires_at == BASE + timedelta(seconds=12)
    assert lease.expires_monotonic_ns == 12 * NS
    assert [event[0] for event in rig.events] == ["local0", "profile", "local1"]
    assert original == rig.expectation.model_dump_json()


async def test_verify_exposes_existing_coordinator_result_contract(rig):
    result = await rig.verifier.verify(rig.target, rig.frame, deadline_at=BASE + timedelta(seconds=30))
    assert result.verified and result.lease and result.error_code is None


async def initial(rig, *, deadline=None):
    return await rig.verifier.verify_if_current(
        rig.target, rig.frame, deadline_at=deadline or BASE + timedelta(seconds=30),
    )


def matching_header(rig, label=None):
    digest = hashlib.sha256((label or rig.target.display_name).encode("utf-8")).hexdigest()
    rig.witnesses = [w.model_copy(update={"header_digest": digest}) for w in rig.witnesses]


def refuse_result(result, code):
    assert not result.verified and result.lease is None and result.error_code == code


async def test_initial_other_contact_uses_one_complete_local_read_and_no_profile(rig, report):
    # The other contact really would have a different profile; do not open it.
    report["profile_id_hmac"] = "9" * 64
    rig.captured = parse(report)
    refuse_result(await initial(rig), "active_chat_not_current")
    assert [event[0] for event in rig.events] == ["local0"]


def non_chat_witness(witness, *, selected=1):
    return witness.model_copy(update={
        "surface_kind": "non_chat", "header_candidate_count": 0,
        "message_candidate_count": 0, "composer_candidate_count": 0,
        "header_digest": None, "active_chat_structure_digest": None,
        "conversation_type": "unknown", "latest_tail": None, "composer_empty": None,
        "selected_row_candidate_count": selected,
        "selected_row_runtime_id_hash": witness.selected_row_runtime_id_hash if selected else None,
        "selected_row_selection_source": witness.selected_row_selection_source if selected else None,
    })


def other_title_witness(witness):
    return non_chat_witness(witness).model_copy(update={
        "surface_kind": "unknown", "header_candidate_count": 1,
        "header_digest": hashlib.sha256(b"Synthetic service title").hexdigest(),
    })


async def test_initial_other_title_with_zero_messages_and_composers_allows_only_navigation(rig):
    rig.witnesses[0] = other_title_witness(rig.witnesses[0])
    refuse_result(await initial(rig), "active_chat_not_current")
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("label", ["Registered label", "trusted alias"])
async def test_initial_title_only_matching_any_target_label_is_not_a_noncurrent_proof(rig, label):
    rig.target = rig.target.model_copy(update={"search_aliases": ("trusted alias",)})
    rig.witnesses[0] = other_title_witness(rig.witnesses[0]).model_copy(update={
        "header_digest": hashlib.sha256(label.encode()).hexdigest()})
    refuse_result(await initial(rig), "identity_chat_correlation_unproven")
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("field,value", [
    ("surface_kind", "non_chat"), ("surface_kind", "chat"),
    ("header_candidate_count", None), ("header_candidate_count", 0), ("header_candidate_count", 2),
    ("message_candidate_count", None), ("message_candidate_count", 1), ("message_candidate_count", 2),
    ("composer_candidate_count", None), ("composer_candidate_count", 1), ("composer_candidate_count", 2),
    ("header_candidate_count", True), ("message_candidate_count", "0"),
    ("header_digest", None), ("header_digest", "malformed"),
    ("selected_row_candidate_count", 0), ("selected_row_candidate_count", 2),
    ("selected_row_runtime_id_hash", None), ("selected_row_selection_source", None),
    ("active_chat_structure_digest", "d" * 64), ("conversation_type", "direct"),
    ("conversation_type", "group"), ("group_marker_probe_complete", False), ("group_marker_count", 1),
    ("latest_tail", False), ("latest_tail", True), ("composer_empty", False), ("composer_empty", True),
])
async def test_initial_other_title_does_not_relax_partial_ambiguous_or_group_evidence(rig, field, value):
    rig.witnesses[0] = other_title_witness(rig.witnesses[0]).model_copy(update={field: value})
    result = await initial(rig)
    assert not result.verified and result.lease is None and result.error_code != "active_chat_not_current"
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("selected", [0, 1])
async def test_initial_proven_non_chat_surface_allows_navigation_without_profile_or_lease(rig, selected):
    rig.witnesses[0] = non_chat_witness(rig.witnesses[0], selected=selected)
    refuse_result(await initial(rig), "active_chat_not_current")
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("witness_factory", [non_chat_witness, other_title_witness])
@pytest.mark.parametrize("proven", [True, False])
async def test_real_coordinator_enters_navigation_only_for_explicit_non_chat_proof(rig, proven, witness_factory):
    from messenger_ai.adapters.qq.navigation.contracts import NavigationDecision, NavigationProviderResult
    from messenger_ai.runtime.navigation import NavigationCoordinator
    from messenger_ai.runtime.navigation_state import NavigationTaskStore
    rig.witnesses[0] = witness_factory(rig.witnesses[0])
    if not proven:
        rig.witnesses[0] = rig.witnesses[0].model_copy(update={"header_candidate_count": None})
    calls = []
    class Desktop:
        async def capture(self, target, *, deadline_at):
            return rig.frame
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("this test grants no desktop input")
    class Navigator:
        async def decide(self, request, *, cancel_event=None):
            calls.append(request.target)
            return NavigationProviderResult(frame_id=request.frame.frame_id, model="synthetic", latency_ms=0,
                decision=NavigationDecision(frame_id=request.frame.frame_id, action="unable"))
    store = NavigationTaskStore()
    try:
        coordinator = NavigationCoordinator(Navigator(), Desktop(), rig.verifier, store,
            clock=lambda: rig.now, monotonic=lambda: rig.tick / NS, monotonic_ns_clock=lambda: rig.tick)
        result = await coordinator.navigate(rig.target, pending_input_key="synthetic-pending")
        assert result.outcome.model_requests == int(proven) and result.outcome.desktop_actions == 0
        assert result.outcome.error_code == ("navigation_model_unable" if proven else "identity_chat_correlation_unproven")
        assert result.active_chat_lease is None and len(calls) == int(proven)
        assert [event[0] for event in rig.events] == ["local0"]
    finally:
        store.close()


@pytest.mark.parametrize("field,value", [
    ("surface_kind", "unknown"), ("surface_kind", "chat"), ("surface_kind", "service"),
    ("header_candidate_count", None), ("header_candidate_count", 1), ("header_candidate_count", 2),
    ("message_candidate_count", None), ("message_candidate_count", 1), ("message_candidate_count", 2),
    ("composer_candidate_count", None), ("composer_candidate_count", 1), ("composer_candidate_count", 2),
    ("header_candidate_count", False), ("message_candidate_count", "0"),
    ("selected_row_candidate_count", 2), ("selected_row_candidate_count", True),
    ("selected_row_runtime_id_hash", None), ("selected_row_selection_source", None),
    ("selected_row_runtime_id_hash", "malformed"),
    ("header_digest", "f" * 64), ("active_chat_structure_digest", "e" * 64),
    ("conversation_type", "direct"), ("conversation_type", "group"),
    ("group_marker_count", 1), ("group_marker_probe_complete", False),
    ("latest_tail", True), ("composer_empty", True), ("composer_empty", False),
])
async def test_initial_non_chat_requires_complete_unambiguous_negative_surface_proof(rig, field, value):
    rig.witnesses[0] = non_chat_witness(rig.witnesses[0]).model_copy(update={field: value})
    result = await initial(rig)
    assert not result.verified and result.lease is None
    assert result.error_code != "active_chat_not_current"
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("field,value", [
    ("selected_row_runtime_id_hash", "c" * 64), ("selected_row_selection_source", "selected_class"),
])
async def test_initial_non_chat_without_selection_cannot_carry_a_stale_selected_token(rig, field, value):
    rig.witnesses[0] = non_chat_witness(rig.witnesses[0], selected=0).model_copy(update={field: value})
    refuse_result(await initial(rig), "identity_chat_correlation_unproven")
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("field,value,code", [
    ("account_id", "other", "identity_target_mismatch"),
    ("conversation_id", "other", "identity_target_mismatch"),
    ("binding_id", "other", "identity_target_mismatch"),
    ("binding_revision", 8, "identity_target_mismatch"),
    ("run_id", "other", "identity_scope_mismatch"),
    ("session_epoch", "other", "identity_scope_mismatch"),
    ("surface_epoch", "other", "identity_scope_mismatch"),
    ("worker_epoch", "other", "identity_scope_mismatch"),
    ("desktop_lease_id", "other", "identity_scope_mismatch"),
    ("control_revision", 4, "identity_scope_mismatch"),
    ("process_id", 124, "identity_scope_mismatch"),
    ("window_handle", 457, "identity_scope_mismatch"),
    ("process_started_at_100ns", 987, "identity_process_lifetime_mismatch"),
    ("observation_epoch", "other", "identity_observation_mismatch"),
])
@pytest.mark.parametrize("witness_factory", [non_chat_witness, other_title_witness])
async def test_initial_non_chat_still_checks_exact_target_frame_and_process(rig, field, value, code, witness_factory):
    rig.witnesses[0] = witness_factory(rig.witnesses[0]).model_copy(update={field: value})
    refuse_result(await initial(rig), code)
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("kind", ["context", "expectation"])
@pytest.mark.parametrize("witness_factory", [non_chat_witness, other_title_witness])
async def test_initial_non_chat_rechecks_registry_after_local_read(rig, kind, witness_factory):
    rig.witnesses[0] = witness_factory(rig.witnesses[0])
    def change(_):
        if kind == "context":
            rig.context = rig.context.model_copy(update={"observation_epoch": "new"})
        else:
            rig.expectation = rig.expectation.model_copy(update={"expected_profile_hmac": "9" * 64})
    rig.on_snapshot = change
    refuse_result(await initial(rig), "identity_scope_mismatch")
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("clock", ["utc", "monotonic"])
@pytest.mark.parametrize("seconds,code", [(.5, "identity_capture_order_invalid"),
    (11, "identity_evidence_stale"), (30, "identity_deadline_exhausted")])
@pytest.mark.parametrize("witness_factory", [non_chat_witness, other_title_witness])
async def test_initial_non_chat_retains_both_original_clocks(rig, clock, seconds, code, witness_factory):
    rig.witnesses[0] = witness_factory(rig.witnesses[0])
    def stall(_):
        if clock == "utc":
            rig.now = BASE + timedelta(seconds=seconds)
        else:
            rig.tick = int(seconds * NS)
    rig.on_snapshot = stall
    refuse_result(await initial(rig), code)
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("witness_factory", [non_chat_witness, other_title_witness])
async def test_initial_non_chat_must_have_been_captured_after_original_frame(rig, witness_factory):
    rig.witnesses[0] = witness_factory(rig.witnesses[0]).model_copy(update={"captured_at": BASE - timedelta(seconds=1)})
    refuse_result(await initial(rig), "identity_capture_order_invalid")
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("method", ["verify", "verify_with_evidence"])
@pytest.mark.parametrize("witness_factory", [non_chat_witness, other_title_witness])
async def test_non_chat_never_enters_full_verification_or_opens_profile(rig, method, witness_factory):
    rig.witnesses[0] = witness_factory(rig.witnesses[0])
    outcome = await getattr(rig.verifier, method)(rig.target, rig.frame, deadline_at=BASE + timedelta(seconds=30))
    refuse_result(outcome if method == "verify" else outcome.result, "identity_chat_correlation_unproven")
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("profile_matches", [True, False])
@pytest.mark.parametrize("witness_factory", [non_chat_witness, other_title_witness])
async def test_after_initial_non_chat_candidate_requires_complete_fresh_profile_proof(rig, report, profile_matches, witness_factory):
    original_witnesses = list(rig.witnesses)
    rig.witnesses[0] = witness_factory(rig.witnesses[0])
    refuse_result(await initial(rig), "active_chat_not_current")
    rig.witnesses, rig.local_count, rig.events = original_witnesses, 0, []
    rig.now, rig.tick = BASE, 0
    if not profile_matches:
        report["profile_id_hmac"] = "9" * 64
        rig.captured = parse(report)
    outcome = await rig.verifier.verify(rig.target, rig.frame, deadline_at=BASE + timedelta(seconds=30))
    assert outcome.verified is profile_matches
    if not profile_matches:
        refuse_result(outcome, "identity_profile_mismatch")
    assert [event[0] for event in rig.events] == ["local0", "profile", "local1"]


@pytest.mark.parametrize("label", ["Registered label", "可信别名"])
async def test_initial_matching_trusted_label_reuses_before_and_still_proves_identity(rig, label):
    rig.target = rig.target.model_copy(update={"search_aliases": ("可信别名",)})
    matching_header(rig, label)
    result = await initial(rig)
    assert result.verified and result.lease and result.error_code is None
    assert result.lease.expires_at == BASE + timedelta(seconds=12)
    assert rig.local_count == 2
    assert [event[0] for event in rig.events] == ["local0", "profile", "local1"]


async def test_initial_same_label_impostor_remains_hard_identity_mismatch(rig, report):
    matching_header(rig)
    report["profile_id_hmac"] = "9" * 64
    rig.captured = parse(report)
    refuse_result(await initial(rig), "identity_profile_mismatch")
    assert [event[0] for event in rig.events] == ["local0", "profile", "local1"]


async def test_full_verify_does_not_treat_opened_wrong_label_as_noncurrent(rig, report):
    report["profile_id_hmac"] = "9" * 64
    rig.captured = parse(report)
    result = await rig.verifier.verify(rig.target, rig.frame, deadline_at=BASE + timedelta(seconds=30))
    refuse_result(result, "identity_profile_mismatch")
    assert [event[0] for event in rig.events] == ["local0", "profile", "local1"]


@pytest.mark.parametrize("field,value,code", [
    ("account_id", "other", "identity_target_mismatch"),
    ("conversation_id", "other", "identity_target_mismatch"),
    ("binding_id", "other", "identity_target_mismatch"),
    ("binding_revision", 8, "identity_target_mismatch"),
    ("run_id", "other", "identity_scope_mismatch"),
    ("session_epoch", "other", "identity_scope_mismatch"),
    ("surface_epoch", "other", "identity_scope_mismatch"),
    ("worker_epoch", "other", "identity_scope_mismatch"),
    ("desktop_lease_id", "other", "identity_scope_mismatch"),
    ("control_revision", 4, "identity_scope_mismatch"),
    ("process_id", 124, "identity_scope_mismatch"),
    ("window_handle", 457, "identity_scope_mismatch"),
    ("process_started_at_100ns", 987, "identity_process_lifetime_mismatch"),
    ("observation_epoch", "other", "identity_observation_mismatch"),
    ("selected_row_candidate_count", 0, "identity_chat_correlation_unproven"),
    ("selected_row_candidate_count", 2, "identity_chat_correlation_unproven"),
    ("selected_row_runtime_id_hash", None, "identity_chat_correlation_unproven"),
    ("selected_row_selection_source", None, "identity_chat_correlation_unproven"),
    ("header_digest", None, "identity_chat_correlation_unproven"),
    ("active_chat_structure_digest", None, "identity_chat_correlation_unproven"),
    ("conversation_type", "unknown", "identity_conversation_not_direct"),
    ("group_marker_count", 1, "identity_conversation_not_direct"),
    ("group_marker_probe_complete", False, "identity_group_probe_incomplete"),
    ("latest_tail", False, "identity_message_tail_unproven"),
    ("latest_tail", None, "identity_message_tail_unproven"),
    ("composer_empty", False, "identity_composer_not_empty"),
    ("composer_empty", None, "identity_composer_not_empty"),
    ("header_digest", "private malformed value", "identity_profile_evidence_invalid"),
    ("active_chat_structure_digest", "private malformed value", "identity_profile_evidence_invalid"),
    ("selected_row_runtime_id_hash", "private malformed value", "identity_profile_evidence_invalid"),
    ("selected_row_candidate_count", True, "identity_profile_evidence_invalid"),
    ("group_marker_probe_complete", 1, "identity_profile_evidence_invalid"),
])
async def test_initial_wrong_label_never_hides_invalid_local_evidence(rig, field, value, code):
    rig.witnesses[0] = rig.witnesses[0].model_copy(update={field: value})
    result = await initial(rig)
    refuse_result(result, code)
    assert "private malformed value" not in result.model_dump_json()
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("kind", ["context", "expectation"])
async def test_initial_wrong_label_does_not_hide_registry_change_during_local_read(rig, kind):
    def change(_):
        if kind == "context":
            rig.context = rig.context.model_copy(update={"observation_epoch": "new"})
        else:
            rig.expectation = rig.expectation.model_copy(update={"expected_profile_hmac": "9" * 64})
    rig.on_snapshot = change
    refuse_result(await initial(rig), "identity_scope_mismatch")
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("kind,code", [
    ("missing", "identity_profile_expectation_missing"),
    ("session_bound", "identity_mode_unproven"),
    ("wrong_target", "identity_target_mismatch"),
])
async def test_initial_optimization_keeps_independent_expectation_requirement(rig, kind, code):
    if kind == "missing":
        rig.expectation = None
    elif kind == "session_bound":
        rig.target = rig.target.model_copy(update={"identity_mode": "session_bound"})
    else:
        rig.expectation = rig.expectation.model_copy(update={"binding_revision": 8})
    refuse_result(await initial(rig), code)
    assert rig.events == []


@pytest.mark.parametrize("clock", ["utc", "monotonic"])
@pytest.mark.parametrize("seconds,code", [(11, "identity_evidence_stale"), (30, "identity_deadline_exhausted")])
async def test_initial_noncurrent_decision_obeys_both_freshness_and_original_deadline(rig, clock, seconds, code):
    def stall(_):
        if clock == "utc":
            rig.now = BASE + timedelta(seconds=seconds)
        else:
            rig.tick = seconds * NS
    rig.on_snapshot = stall
    refuse_result(await initial(rig), code)
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("field,value", [
    ("captured_at", BASE - timedelta(seconds=1)),
    ("captured_at", BASE + timedelta(seconds=2)),
    ("captured_monotonic_ns", 2 * NS),
])
async def test_initial_noncurrent_decision_cannot_use_preframe_or_future_witness(rig, field, value):
    rig.witnesses[0] = rig.witnesses[0].model_copy(update={field: value})
    refuse_result(await initial(rig), "identity_capture_order_invalid")
    assert [event[0] for event in rig.events] == ["local0"]


async def test_initial_rechecks_deadline_after_final_scope_lookup(rig):
    calls = 0
    def context(*_):
        nonlocal calls
        calls += 1
        if calls == 2:
            rig.tick = 30 * NS
        return rig.context
    rig.verifier.context_source = context
    refuse_result(await initial(rig), "identity_deadline_exhausted")
    assert [event[0] for event in rig.events] == ["local0"]


async def test_initial_matching_label_keeps_original_budget_and_post_profile_drift_guard(rig):
    matching_header(rig)
    rig.witnesses[1] = rig.witnesses[1].model_copy(update={"selected_row_runtime_id_hash": "9" * 64})
    deadline = BASE + timedelta(seconds=5)
    refuse_result(await initial(rig, deadline=deadline), "identity_chat_drift")
    assert rig.events == [("local0", deadline), ("profile", deadline), ("local1", deadline)]


async def test_initial_matching_label_cannot_reset_deadline_before_profile(rig):
    matching_header(rig)
    def expire():
        rig.tick = 5 * NS
    rig.on_capture = expire
    refuse_result(await initial(rig, deadline=BASE + timedelta(seconds=5)), "identity_deadline_exhausted")
    assert [event[0] for event in rig.events] == ["local0", "profile"]


async def test_initial_local_cancellation_propagates_for_desktop_cleanup(rig):
    def cancel(_):
        raise asyncio.CancelledError()
    rig.on_snapshot = cancel
    with pytest.raises(asyncio.CancelledError):
        await initial(rig)
    assert [event[0] for event in rig.events] == ["local0"]


@pytest.mark.parametrize("field,value,code", [
    ("composer_empty", False, "identity_composer_not_empty"),
    ("composer_empty", None, "identity_composer_not_empty"),
    ("latest_tail", False, "identity_message_tail_unproven"),
    ("latest_tail", None, "identity_message_tail_unproven"),
    ("conversation_type", "group", "identity_conversation_not_direct"),
    ("group_marker_count", 1, "identity_conversation_not_direct"),
    ("group_marker_probe_complete", False, "identity_group_probe_incomplete"),
    ("selected_row_candidate_count", 2, "identity_chat_correlation_unproven"),
    ("selected_row_selection_source", None, "identity_chat_correlation_unproven"),
    ("selected_row_runtime_id_hash", None, "identity_chat_correlation_unproven"),
    ("header_digest", None, "identity_chat_correlation_unproven"),
    ("active_chat_structure_digest", None, "identity_chat_correlation_unproven"),
])
@pytest.mark.parametrize("side", [0, 1])
async def test_local_real_read_requirements_and_before_read_prevents_profile_action(rig, field, value, code, side):
    rig.witnesses[side] = rig.witnesses[side].model_copy(update={field: value})
    attempt = await rig.run()
    refuse(attempt, code)
    assert len(rig.events) == (1 if side == 0 else 3)
    assert (attempt.acquisition is None) == (side == 0)


@pytest.mark.parametrize("field,value,code", [
    ("account_id", "other", "identity_target_mismatch"),
    ("conversation_id", "other", "identity_target_mismatch"),
    ("binding_id", "other", "identity_target_mismatch"),
    ("binding_revision", 8, "identity_target_mismatch"),
    ("run_id", "other", "identity_scope_mismatch"),
    ("session_epoch", "other", "identity_scope_mismatch"),
    ("surface_epoch", "other", "identity_scope_mismatch"),
    ("worker_epoch", "other", "identity_scope_mismatch"),
    ("desktop_lease_id", "other", "identity_scope_mismatch"),
    ("control_revision", 4, "identity_scope_mismatch"),
    ("process_id", 124, "identity_scope_mismatch"),
    ("window_handle", 457, "identity_scope_mismatch"),
    ("process_started_at_100ns", 123, "identity_process_lifetime_mismatch"),
    ("observation_epoch", "other", "identity_observation_mismatch"),
])
@pytest.mark.parametrize("side", [0, 1])
async def test_outer_snapshots_cannot_forge_live_scope(rig, field, value, code, side):
    rig.witnesses[side] = rig.witnesses[side].model_copy(update={field: value})
    refuse(await rig.run(), code)
    assert len(rig.events) == (1 if side == 0 else 3)


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("field,value,code", [
    ("selected_row_candidate_count", 0, "identity_chat_correlation_unproven"),
    ("selected_row_runtime_id_hash", None, "identity_chat_correlation_unproven"),
    ("selected_row_selection_source", None, "identity_chat_correlation_unproven"),
    ("header_digest", None, "identity_chat_correlation_unproven"),
    ("active_chat_structure_digest", None, "identity_chat_correlation_unproven"),
    ("selected_row_runtime_id_hash", "9" * 64, "identity_chat_drift"),
    ("header_digest", "9" * 64, "identity_chat_drift"),
    ("active_chat_structure_digest", "9" * 64, "identity_chat_drift"),
])
async def test_each_helper_witness_must_close_the_four_way_correlation(rig, report, side, field, value, code):
    report["acquisition"][side][field] = value
    rig.captured = parse(report)  # Partial observations remain typed, never authorized.
    attempt = await rig.run()
    refuse(attempt, code)
    assert attempt.acquisition == rig.captured


@pytest.mark.parametrize("field", ["header_digest", "selected_row_runtime_id_hash", "active_chat_structure_digest"])
async def test_local_after_drift_is_not_hidden_by_matching_helper_pair(rig, field):
    rig.witnesses[1] = rig.witnesses[1].model_copy(update={field: "9" * 64})
    refuse(await rig.run(), "identity_chat_drift")


async def test_matching_inner_structure_pair_must_match_legacy_restored_structure(rig, report):
    for side in ("before", "after"):
        report["acquisition"][side]["active_chat_structure_digest"] = "9" * 64
    rig.captured = parse(report)
    refuse(await rig.run(), "identity_chat_drift")


@pytest.mark.parametrize("field,value,code", [
    ("process_started_at_100ns", 987, "identity_profile_scope_mismatch"),
    ("profile_window_candidate_count", 2, "identity_chat_correlation_unproven"),
    ("profile_window_was_new", False, "identity_chat_correlation_unproven"),
    ("profile_window_closed", False, "identity_profile_recovery_failed"),
    ("original_chat_restored", False, "identity_profile_recovery_failed"),
    ("foreground_restored", False, "identity_profile_recovery_failed"),
])
async def test_actual_profile_metadata_is_not_replaced_with_success_booleans(rig, report, field, value, code):
    report["acquisition"][field] = value
    rig.captured = parse(report)
    refuse(await rig.run(), code)


async def test_observed_identity_never_enrolls_or_replaces_expected_hmac(rig, report):
    original = rig.expectation
    report["profile_id_hmac"] = "9" * 64
    rig.captured = parse(report)
    refuse(await rig.run(), "identity_profile_mismatch")
    assert rig.expectation == original


@pytest.mark.parametrize("kind", ["missing", "session_bound", "wrong_target"])
async def test_independent_expectation_required_before_any_ui(rig, kind):
    if kind == "missing":
        rig.expectation = None
    elif kind == "session_bound":
        rig.target = rig.target.model_copy(update={"identity_mode": "session_bound"})
    else:
        rig.expectation = rig.expectation.model_copy(update={"binding_revision": 8})
    codes = dict(missing="identity_profile_expectation_missing", session_bound="identity_mode_unproven",
                 wrong_target="identity_target_mismatch")
    refuse(await rig.run(), codes[kind])
    assert rig.events == []


@pytest.mark.parametrize("kind", ["context", "expectation"])
async def test_registry_or_scope_change_during_profile_cycle_invalidates_result(rig, kind):
    def change():
        if kind == "context":
            rig.context = rig.context.model_copy(update={"observation_epoch": "new"})
        else:
            rig.expectation = rig.expectation.model_copy(update={"expected_profile_hmac": "9" * 64})
    rig.on_capture = change
    refuse(await rig.run(), "identity_scope_mismatch")


async def test_deadline_is_rechecked_after_final_scope_lookup_before_issuing_lease(rig):
    calls = 0
    def context(*_):
        nonlocal calls
        calls += 1
        if calls == 2:
            rig.tick = 30 * NS
        return rig.context
    rig.verifier.context_source = context
    refuse(await rig.run(), "identity_deadline_exhausted")


@pytest.mark.parametrize("field,value,code", [
    ("process_id", 124, "identity_profile_scope_mismatch"),
    ("window_handle", 457, "identity_profile_scope_mismatch"),
    ("selector_pack_version", "other", "identity_evidence_version_mismatch"),
    ("environment_fingerprint", "9" * 64, "identity_evidence_version_mismatch"),
])
async def test_typed_capture_is_still_bound_to_live_main_window_and_approved_environment(rig, field, value, code):
    rig.captured = rig.captured.model_copy(update={
        "profile": rig.captured.profile.model_copy(update={field: value}),
    })
    refuse(await rig.run(), code)


@pytest.mark.parametrize("side,field,seconds", [
    (0, "captured_at", 1.5), (0, "captured_monotonic_ns", 1.5),
    (1, "captured_at", 2.5), (1, "captured_monotonic_ns", 2.5),
])
async def test_outer_reads_must_bracket_helper_cycle_on_both_clocks(rig, side, field, seconds):
    value = BASE + timedelta(seconds=seconds) if field == "captured_at" else int(seconds * NS)
    rig.witnesses[side] = rig.witnesses[side].model_copy(update={field: value})
    # Future outer-before is rejected even before opening a profile.
    code = "identity_capture_order_invalid" if side == 0 else "identity_profile_capture_order_invalid"
    refuse(await rig.run(), code)


async def test_stale_initial_local_snapshot_does_not_open_profile(rig):
    def stall(index):
        rig.now, rig.tick = BASE + timedelta(seconds=11), 11 * NS
    rig.on_snapshot = stall
    refuse(await rig.run(), "identity_evidence_stale")
    assert len(rig.events) == 1


@pytest.mark.parametrize("clock", ["utc", "monotonic", "both"])
async def test_late_outer_snapshot_cannot_renew_old_profile_proof(rig, clock):
    def stall(index):
        if index:
            if clock in ("utc", "both"):
                rig.now = BASE + timedelta(seconds=13)
            if clock in ("monotonic", "both"):
                rig.tick = 13 * NS
            rig.witnesses[1] = rig.witnesses[1].model_copy(update={
                "captured_at": rig.now, "captured_monotonic_ns": rig.tick,
            })
    rig.on_snapshot = stall
    attempt = await rig.run()
    refuse(attempt, "identity_evidence_stale")
    assert attempt.acquisition is not None


async def test_partial_acquisition_survives_post_read_timeout_but_never_creates_lease(rig):
    def stall(index):
        if index:
            rig.tick = 30 * NS
    rig.on_snapshot = stall
    attempt = await rig.run()
    refuse(attempt, "identity_deadline_exhausted")
    assert attempt.acquisition == rig.captured


async def test_deadline_expired_at_entry_has_no_side_effects(rig):
    refuse(await rig.run(deadline=BASE), "identity_deadline_exhausted")
    assert rig.events == []


async def test_deadline_exhausted_after_profile_does_not_start_local_post_read(rig):
    def expire():
        rig.tick = 5 * NS
    rig.on_capture = expire
    attempt = await rig.run(deadline=BASE + timedelta(seconds=5))
    refuse(attempt, "identity_deadline_exhausted")
    assert [event[0] for event in rig.events] == ["local0", "profile"]
    assert attempt.acquisition is not None


async def test_utc_rollback_never_extends_monotonic_budget_passed_to_next_source(rig):
    def rollback():
        rig.now = BASE + timedelta(seconds=2)
        rig.tick = 4 * NS
    rig.on_capture = rollback
    def finish(index):
        if index:
            rig.tick = int(4.5 * NS)
            rig.witnesses[1] = rig.witnesses[1].model_copy(update={"captured_monotonic_ns": rig.tick})
    rig.on_snapshot = finish
    # Remaining monotonic budget is 1s, so post read receives UTC 3s, not 5s.
    attempt = await rig.run(deadline=BASE + timedelta(seconds=5))
    assert rig.events[2] == ("local1", BASE + timedelta(seconds=3))
    assert attempt.result.verified
    assert attempt.result.lease.expires_at == BASE + timedelta(seconds=3.5)
    assert attempt.result.lease.expires_monotonic_ns == 5 * NS


async def test_monotonic_regression_between_sources_never_restores_budget(rig):
    def regress(index):
        if index:
            rig.tick = 2 * NS  # Still after entry, but before the profile completion.
    rig.on_snapshot = regress
    refuse(await rig.run(), "identity_capture_order_invalid")


async def test_slow_helper_restore_cannot_freshen_profile_identity(rig, report):
    report["acquisition"]["after"].update(
        captured_at=(BASE + timedelta(seconds=12)).isoformat(), captured_monotonic_ns=12 * NS,
    )
    rig.captured = parse(report)
    def complete_restore():
        rig.now, rig.tick = BASE + timedelta(seconds=12), 12 * NS
    def complete_outer(index):
        if index:
            rig.now, rig.tick = BASE + timedelta(seconds=13), 13 * NS
            rig.witnesses[1] = rig.witnesses[1].model_copy(update={
                "captured_at": rig.now, "captured_monotonic_ns": rig.tick,
            })
    rig.on_capture, rig.on_snapshot = complete_restore, complete_outer
    attempt = await rig.run()
    refuse(attempt, "identity_evidence_stale")
    assert attempt.acquisition is not None


@pytest.mark.parametrize("error,code", [
    (ProfileCaptureError("HELPER_TIMEOUT"), "identity_profile_capture_failed"),
    (TimeoutError("private native text"), "identity_deadline_exhausted"),
    (RuntimeError("private native text"), "identity_profile_evidence_unavailable"),
    (ValueError("private native text"), "identity_profile_evidence_invalid"),
])
async def test_source_errors_are_fixed_content_free_results(rig, error, code):
    def fail():
        raise error
    rig.on_capture = fail
    attempt = await rig.run()
    refuse(attempt, code)
    assert attempt.acquisition is None and "private native text" not in attempt.model_dump_json()


async def test_cancellation_propagates_for_desktop_owner_to_finish_process_isolation(rig):
    def cancel():
        raise asyncio.CancelledError()
    rig.on_capture = cancel
    with pytest.raises(asyncio.CancelledError):
        await rig.run()


async def test_unprojected_profile_report_is_never_returned_as_diagnostic(rig, report):
    rig.captured = report
    attempt = await rig.run()
    refuse(attempt, "identity_profile_acquisition_invalid")
    assert attempt.acquisition is None


async def test_untyped_local_snapshot_rejected_before_profile_action(rig):
    rig.witnesses[0] = {"private": "text"}
    refuse(await rig.run(), "identity_profile_evidence_invalid")
    assert len(rig.events) == 1


def test_evidence_assembly_only_accepts_typed_projection(case, report):
    target, frame, expectation, evidence = case
    context = ProfileVerificationContext(process_started_at_100ns=PROCESS_START, observation_epoch="observation")
    with pytest.raises(ProfileAcquisitionError, match="identity_profile_acquisition_invalid"):
        assemble_current_chat_evidence(target, frame, expectation, report, evidence.before, evidence.after, context)


def test_capture_adapter_preserves_legacy_result_and_shared_subprocess_budget(report, tmp_path):
    now, tick = BASE, 0.0
    calls = []
    class Store:
        def get_secret(self, name):
            assert name == "qq.identity.hmac"
            return b"k" * 32
        def get_or_create_hmac_key(self, *_):
            raise AssertionError("never enroll a key")
    header = dict(
        probe_version="qq-uia-guest-header-v1", mode="guest_foreground_header_inspect",
        succeeded=True, status="HEADER_CAPTURED", header_candidate_count=1,
        process_id=123, window_handle=456, guest_environment=report["guest_environment"],
        active_header_digest="f" * 64, right_region_structure_digest="e" * 64,
        privacy=dict(exact_hwnd=True, emitted_control_names=False, emitted_chat_text=False,
                     mouse_input_used=False, keyboard_input_used=False, clipboard_used=False,
                     transient_navigation_performed=False, composer_or_send_accessed=False),
    )
    def runner(command, **kwargs):
        nonlocal now, tick
        calls.append((command, kwargs))
        now += timedelta(seconds=1)
        tick += 1
        payload = header if len(calls) % 2 else report
        return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode(), b"")
    kwargs = dict(
        pid=123, hwnd=456, vault=tmp_path / "nonexistent", environment_fingerprint="e" * 64,
        selector_pack_version="selectors", deadline=BASE + timedelta(seconds=30),
        clock=lambda: now, monotonic=lambda: tick, runner=runner, secret_store=Store(),
    )
    typed = capture_profile_acquisition("helper.exe", **kwargs)
    legacy = capture_current_profile("helper.exe", **kwargs)
    assert typed.profile.model_dump() == legacy
    assert "acquisition" not in legacy and typed == parse(report)
    assert [kwargs["timeout"] for _, kwargs in calls] == [30, 29, 28, 27]
    assert not (tmp_path / "nonexistent").exists()
    for command, kwargs in calls:
        assert b"k" * 32 not in repr(command).encode()
    with pytest.raises(ProfileAcquisitionError, match="identity_profile_capture_input_invalid"):
        capture_profile_acquisition("helper.exe", **kwargs, validated_projector=lambda *_: report)
