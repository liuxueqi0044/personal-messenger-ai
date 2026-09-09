from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from messenger_ai.policy import (
    AuthorizationKind,
    HumanApproval,
    InvalidationEvent,
    InvalidationKind,
    PolicyOutcome,
    PolicyReason,
)


def issue_auto(make_request, policy_stack, clock):
    engine, _, service = policy_stack
    request = make_request()
    eligibility = engine.evaluate_eligibility(request)
    clock.advance(8)
    due_request = request.model_copy(
        update={"state": request.state.model_copy(update={"observed_at": clock.now()})}
    )
    revalidation, authorization = service.authorize_due(eligibility, due_request)
    assert revalidation.outcome is PolicyOutcome.AUTO_ELIGIBLE
    assert authorization is not None
    return service, due_request, authorization


def test_due_time_revalidation_is_required(make_request, policy_stack):
    engine, _, service = policy_stack
    request = make_request()
    eligibility = engine.evaluate_eligibility(request)
    revalidation, authorization = service.authorize_due(eligibility, request)
    assert authorization is None
    assert PolicyReason.TOO_EARLY in revalidation.reason_codes


@pytest.mark.parametrize(
    ("draft_update", "state_update", "reason"),
    [
        ({}, {"last_message_key": "message-2"}, PolicyReason.STALE_LAST_MESSAGE),
        ({}, {"active_rulepack_version": "rule-v2"}, PolicyReason.STALE_RULEPACK),
        (
            {},
            {"active_pacing_rule_version": "pacing-v2"},
            PolicyReason.STALE_PACING_RULE,
        ),
        (
            {},
            {"policy_state_version": "policy-state-v2"},
            PolicyReason.STALE_POLICY_STATE,
        ),
    ],
)
def test_drift_during_wait_cannot_issue(
    make_request, policy_stack, clock, draft_update, state_update, reason
):
    engine, _, service = policy_stack
    request = make_request()
    eligibility = engine.evaluate_eligibility(request)
    clock.advance(8)
    changed = request.model_copy(
        update={
            "draft": request.draft.model_copy(update=draft_update),
            "state": request.state.model_copy(
                update={"observed_at": clock.now(), **state_update}
            ),
        }
    )
    decision, authorization = service.authorize_due(eligibility, changed)
    assert authorization is None
    assert decision.outcome is PolicyOutcome.BLOCKED
    assert (
        reason in decision.reason_codes
        or PolicyReason.STALE_ELIGIBILITY in decision.reason_codes
    )


def test_capability_change_during_wait_cannot_issue(
    make_request, policy_stack, clock, supported_capability
):
    engine, _, service = policy_stack
    request = make_request()
    eligibility = engine.evaluate_eligibility(request)
    clock.advance(8)
    changed_cap = supported_capability.model_copy(update={"client_version": "9.9.27"})
    changed = request.model_copy(
        update={
            "state": request.state.model_copy(
                update={"observed_at": clock.now(), "capability": changed_cap}
            )
        }
    )
    decision, authorization = service.authorize_due(eligibility, changed)
    assert authorization is None
    assert PolicyReason.STALE_CAPABILITY in decision.reason_codes


def test_draft_body_change_invalidates_eligibility(
    make_request, policy_stack, clock, supported_capability
):
    engine, _, service = policy_stack
    request = make_request()
    eligibility = engine.evaluate_eligibility(request)
    clock.advance(8)
    replacement = make_request(body="换了一段正文")
    replacement = replacement.model_copy(
        update={
            "scheduled_due_at": request.scheduled_due_at,
            "state": replacement.state.model_copy(update={"observed_at": clock.now()}),
        }
    )
    decision, authorization = service.authorize_due(eligibility, replacement)
    assert authorization is None
    assert PolicyReason.STALE_ELIGIBILITY in decision.reason_codes


def test_authorization_binds_every_execution_dimension(
    make_request, policy_stack, clock
):
    service, request, authorization = issue_auto(make_request, policy_stack, clock)
    binding = authorization.binding
    assert binding.authorization_kind is AuthorizationKind.POLICY
    assert binding.body_hash == request.draft.body_hash
    assert binding.contact_id == request.draft.contact_id
    assert binding.conversation_id == request.draft.conversation_id
    assert binding.expected_last_message_key == request.state.last_message_key
    assert binding.rulepack_version == request.state.active_rulepack_version
    assert binding.pacing_rule_version == request.state.active_pacing_rule_version
    assert binding.capability_snapshot_hash == request.state.capability.snapshot_hash
    assert binding.policy_state_version == request.state.policy_state_version
    assert service


def test_valid_token_consumes_once_and_replay_fails(make_request, policy_stack, clock):
    service, request, authorization = issue_auto(make_request, policy_stack, clock)
    first = service.consume(
        authorization.token,
        expected_binding=authorization.binding,
        live_request=request,
    )
    second = service.consume(
        authorization.token,
        expected_binding=authorization.binding,
        live_request=request,
    )
    assert first.accepted and first.reason is PolicyReason.AUTHORIZATION_CONSUMED
    assert not second.accepted and second.reason is PolicyReason.AUTHORIZATION_REPLAYED


def test_concurrent_consumption_has_exactly_one_winner(
    make_request, policy_stack, clock
):
    service, request, authorization = issue_auto(make_request, policy_stack, clock)

    def consume():
        return service.consume(
            authorization.token,
            expected_binding=authorization.binding,
            live_request=request,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: consume(), range(64)))
    assert sum(result.accepted for result in results) == 1
    assert all(
        result.reason
        in {PolicyReason.AUTHORIZATION_CONSUMED, PolicyReason.AUTHORIZATION_REPLAYED}
        for result in results
    )


def test_token_tampering_and_cross_draft_binding_fail(
    make_request, policy_stack, clock
):
    service, request, authorization = issue_auto(make_request, policy_stack, clock)
    tampered = authorization.token[:-1] + (
        "A" if authorization.token[-1] != "A" else "B"
    )
    bad_signature = service.consume(
        tampered,
        expected_binding=authorization.binding,
        live_request=request,
    )
    cross_binding = authorization.binding.model_copy(update={"draft_id": "draft-other"})
    crossed = service.consume(
        authorization.token,
        expected_binding=cross_binding,
        live_request=request,
    )
    assert not bad_signature.accepted
    assert bad_signature.reason is PolicyReason.AUTHORIZATION_TAMPERED
    assert not crossed.accepted
    assert crossed.reason is PolicyReason.EXECUTION_BINDING_MISMATCH


def test_expired_token_fails_without_consuming(make_request, policy_stack, clock):
    service, request, authorization = issue_auto(make_request, policy_stack, clock)
    clock.advance(11)
    result = service.consume(
        authorization.token,
        expected_binding=authorization.binding,
        live_request=request.model_copy(
            update={
                "state": request.state.model_copy(update={"observed_at": clock.now()})
            }
        ),
    )
    assert not result.accepted
    assert result.reason is PolicyReason.AUTHORIZATION_EXPIRED


@pytest.mark.parametrize(
    "event",
    [
        InvalidationEvent(kind=InvalidationKind.GLOBAL_PAUSE, reason="pause"),
        InvalidationEvent(
            kind=InvalidationKind.PLATFORM_PAUSE, platform="qq", reason="pause"
        ),
        InvalidationEvent(
            kind=InvalidationKind.CONTACT_PAUSE,
            contact_id="contact-1001",
            reason="pause",
        ),
        InvalidationEvent(
            kind=InvalidationKind.AUTO_MODE_DISABLED, platform="qq", reason="off"
        ),
        InvalidationEvent(
            kind=InvalidationKind.DRAFT_CHANGED, draft_id="draft-1", reason="edited"
        ),
        InvalidationEvent(
            kind=InvalidationKind.NEW_MESSAGE,
            conversation_id="conversation-1",
            reason="new",
        ),
        InvalidationEvent(
            kind=InvalidationKind.RULE_CHANGED,
            rulepack_version="rule-v2",
            reason="new rules",
        ),
        InvalidationEvent(
            kind=InvalidationKind.PACING_CHANGED,
            pacing_rule_version="pacing-v2",
            reason="new pacing",
        ),
        InvalidationEvent(
            kind=InvalidationKind.CAPABILITY_CHANGED,
            capability_snapshot_hash="b" * 64,
            reason="degraded",
        ),
    ],
)
def test_state_change_events_revoke_unconsumed_authorization(
    make_request, policy_stack, clock, event
):
    service, request, authorization = issue_auto(make_request, policy_stack, clock)
    assert service.invalidate(event) == 1
    result = service.consume(
        authorization.token,
        expected_binding=authorization.binding,
        live_request=request,
    )
    assert not result.accepted
    assert result.reason is PolicyReason.AUTHORIZATION_REVOKED


def test_human_approval_still_requires_fresh_execution_state(
    make_request, policy_stack, clock
):
    engine, _, service = policy_stack
    initial = make_request(
        state_changes={"is_new_contact": True, "automation_enabled": False}
    )
    approval = HumanApproval(
        approver_id="user",
        subject_hash=initial.draft.snapshot_hash,
        approved_at=clock.now(),
        expires_at=clock.now() + timedelta(minutes=1),
        reason="reviewed exact L2 draft",
    )
    approved = initial.model_copy(update={"approval": approval})
    eligibility = engine.evaluate_eligibility(approved)
    assert eligibility.outcome is PolicyOutcome.HUMAN_ELIGIBLE
    clock.advance(8)
    due = approved.model_copy(
        update={"state": approved.state.model_copy(update={"observed_at": clock.now()})}
    )
    _, authorization = service.authorize_due(eligibility, due)
    assert authorization is not None

    new_message = due.model_copy(
        update={"state": due.state.model_copy(update={"last_message_key": "message-2"})}
    )
    result = service.consume(
        authorization.token,
        expected_binding=authorization.binding,
        live_request=new_message,
    )
    assert not result.accepted
    assert result.reason is PolicyReason.EXECUTION_BINDING_MISMATCH


def test_bad_signing_key_and_excessive_ttl_are_rejected(policy_stack, clock):
    engine, store, _ = policy_stack
    from messenger_ai.policy import AuthorizationService

    with pytest.raises(ValueError):
        AuthorizationService(engine, store, b"short", clock.now)
    with pytest.raises(ValueError):
        AuthorizationService(engine, store, b"a" * 32, clock.now, ttl_seconds=31)
