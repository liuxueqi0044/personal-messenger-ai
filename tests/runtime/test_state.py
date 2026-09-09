from datetime import UTC, datetime
from uuid import uuid4

import pytest

from messenger_ai.runtime.contracts import Direction, ObservationBatch, ObservedMessage
from messenger_ai.runtime.state import RuntimeState


def batch(*messages, revision=1, complete=True):
    return ObservationBatch(
        account_id="account", contact_id="contact", conversation_id="conversation",
        binding_revision=revision, conversation_revision=1, complete=complete,
        messages=messages,
    )


def message(key, direction, *, operation_id=None):
    return ObservedMessage(local_message_key=key, direction=direction, text="same",
                           observed_at=datetime.now(UTC), operation_id=operation_id)


def test_repeated_text_keeps_distinct_local_keys_and_human_outbound_invalidates():
    state = RuntimeState()
    state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=1)
    events = state.apply_observation(batch(
        message("1", Direction.INBOUND), message("2", Direction.INBOUND),
        message("3", Direction.OUTBOUND),
    ))
    assert events == ("new_message", "new_message", "human_outbound")
    assert state.revisions("conversation") == (1, 4)


def test_bot_echo_requires_known_operation_and_does_not_advance_revision():
    state = RuntimeState()
    state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=1)
    operation_id = uuid4()
    plan_id = uuid4()
    assert state.create_segment_execution(pacing_plan_id=plan_id, segment_index=0,
                                          conversation_id="conversation",
                                          body_hash="a" * 64, binding_revision=1,
                                          conversation_revision=1)
    assert state.bind_verified_operation(pacing_plan_id=plan_id, segment_index=0,
                                         operation_id=operation_id, authorization_id=uuid4())
    assert state.apply_observation(batch(message("echo", Direction.OUTBOUND, operation_id=operation_id))) == ("bot_observed",)
    assert state.revisions("conversation") == (1, 1)
    unknown = state.apply_observation(batch(message("spoof", Direction.OUTBOUND, operation_id=uuid4())))
    assert unknown == ("direction_unknown",)
    assert state.revisions("conversation") == (1, 2)


def test_stale_binding_is_rejected_and_manual_pause_is_not_cleared_by_observe():
    state = RuntimeState()
    state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=2)
    assert state.apply_observation(batch(revision=1)) == ("binding_changed",)
    stale = state.connection.execute("SELECT paused,pause_reason FROM runtime_conversations").fetchone()
    assert (stale["paused"], stale["pause_reason"]) == (1, "binding_revision_changed")
    state.pause("conversation")
    revision = state.revisions("conversation")[1]
    state.apply_observation(batch(revision=2))
    row = state.connection.execute("SELECT paused,pause_reason,conversation_revision FROM runtime_conversations").fetchone()
    assert (row["paused"], row["pause_reason"], row["conversation_revision"]) == (1, "manual_pause", revision)
