from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from messenger_ai.policy import (
    AuthorizationService,
    CapabilitySnapshot,
    ConversationType,
    DraftSnapshot,
    LivePolicyState,
    PlannerAssessment,
    PolicyEngine,
    PolicyRequest,
    SQLiteAuthorizationStore,
    SupportLevel,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def supported_capability() -> CapabilitySnapshot:
    return CapabilitySnapshot(
        capability_version="qq-cap-v1",
        environment_fingerprint="env-v1",
        send_background=SupportLevel.SUPPORTED,
        verify_background=SupportLevel.SUPPORTED,
        healthy=True,
        client_version="9.9.26",
    )


@pytest.fixture
def make_request(clock, supported_capability):
    def factory(**changes):
        body = changes.pop("body", "今天过得还好吗")
        draft_changes = changes.pop("draft_changes", {})
        state_changes = changes.pop("state_changes", {})
        assessment = changes.pop("assessment", PlannerAssessment())
        approval = changes.pop("approval", None)
        inbound_text = changes.pop("inbound_text", "刚忙完 你呢")
        if changes:
            raise AssertionError(f"unknown request changes: {changes}")

        draft_values = {
            "draft_id": "draft-1",
            "platform": "qq",
            "account_id": "account-1",
            "conversation_id": "conversation-1",
            "contact_id": "contact-1001",
            "body": body,
            "expected_last_message_key": "message-1",
            "source_message_keys": ("message-1",),
            "rulepack_version": "rule-v1",
            "pacing_plan_id": "pacing-1",
            "pacing_rule_version": "pacing-v1",
            "capability_snapshot_hash": supported_capability.snapshot_hash,
            "policy_state_version": "policy-state-v1",
            "created_at": clock.now(),
            "expires_at": clock.now() + timedelta(minutes=5),
        }
        draft_values.update(draft_changes)
        draft = DraftSnapshot(**draft_values)
        state_values = {
            "observed_at": clock.now(),
            "last_message_key": "message-1",
            "active_rulepack_version": "rule-v1",
            "active_pacing_rule_version": "pacing-v1",
            "capability": supported_capability,
            "policy_state_version": "policy-state-v1",
            "conversation_type": ConversationType.DIRECT,
            "contact_whitelisted": True,
            "automation_enabled": True,
        }
        state_values.update(state_changes)
        state = LivePolicyState(**state_values)
        return PolicyRequest(
            draft=draft,
            inbound_text=inbound_text,
            assessment=assessment,
            state=state,
            scheduled_due_at=clock.now() + timedelta(seconds=8),
            plan_expires_at=clock.now() + timedelta(minutes=5),
            approval=approval,
        )

    return factory


@pytest.fixture
def policy_stack(clock):
    engine = PolicyEngine(clock.now)
    store = SQLiteAuthorizationStore()
    service = AuthorizationService(engine, store, b"a" * 32, clock.now)
    try:
        yield engine, store, service
    finally:
        store.close()
