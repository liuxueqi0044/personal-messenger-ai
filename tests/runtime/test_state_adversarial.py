"""Adversarial tests for the durable RuntimeState boundary.

These tests use the real SQLite-backed state implementation.  They deliberately
exercise restart, identity, revision and outbox semantics rather than mirrors
of the implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from messenger_ai.runtime.contracts import Direction, ObservationBatch, ObservedMessage
from messenger_ai.runtime.state import RuntimeState


def _batch(
    *,
    account_id="account",
    contact_id="contact",
    conversation_id="conversation",
    binding_revision=1,
    messages=(),
    complete=True,
):
    return ObservationBatch(
        account_id=account_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        binding_revision=binding_revision,
        conversation_revision=1,
        complete=complete,
        messages=tuple(messages),
    )


def _message(key: str, direction: Direction, *, operation_id=None) -> ObservedMessage:
    return ObservedMessage(
        local_message_key=key,
        direction=direction,
        text="test message",
        observed_at=datetime.now(UTC),
        operation_id=operation_id,
    )


def _register(state: RuntimeState, *, contact="contact", conversation="conversation", binding=1):
    state.register(
        account_id="account",
        contact_id=contact,
        conversation_id=conversation,
        binding_revision=binding,
    )


def test_manual_pause_survives_complete_observation_and_cannot_resume_implicitly():
    state = RuntimeState()
    _register(state)
    state.pause("conversation")
    paused_revision = state.revisions("conversation")[1]

    assert state.apply_observation(_batch(messages=[_message("m1", Direction.INBOUND)])) == ("new_message",)
    row = state.connection.execute(
        "SELECT paused,pause_reason,conversation_revision FROM runtime_conversations WHERE conversation_id=?",
        ("conversation",),
    ).fetchone()
    assert (row["paused"], row["pause_reason"]) == (1, "manual_pause")
    assert row["conversation_revision"] > paused_revision
    # No call to ``resume`` was made: the observation itself must never clear
    # a manual pause.  Explicit operator resume remains a separate command.
    assert row["paused"] == 1


def test_stale_binding_persists_pause_and_does_not_store_messages_after_reopen(tmp_path: Path):
    db = tmp_path / "runtime.sqlite"
    state = RuntimeState(db)
    _register(state, binding=2)
    assert state.apply_observation(
        _batch(binding_revision=1, messages=[_message("stale", Direction.INBOUND)])
    ) == ("binding_changed",)
    count = state.connection.execute("SELECT COUNT(*) FROM runtime_observations").fetchone()[0]
    row = state.connection.execute("SELECT paused,pause_reason FROM runtime_conversations").fetchone()
    assert count == 0
    assert (row["paused"], row["pause_reason"]) == (1, "binding_revision_changed")
    state.connection.close()

    reopened = RuntimeState(db)
    row = reopened.connection.execute("SELECT paused,pause_reason FROM runtime_conversations").fetchone()
    assert (row["paused"], row["pause_reason"]) == (1, "binding_revision_changed")
    assert reopened.connection.execute("SELECT COUNT(*) FROM runtime_observations").fetchone()[0] == 0


def test_operation_known_to_a_cannot_be_bot_echo_in_b():
    state = RuntimeState()
    _register(state, contact="a", conversation="conversation-a")
    _register(state, contact="b", conversation="conversation-b")
    operation_id = uuid4()
    assert state.create_segment_execution(
        pacing_plan_id=uuid4(),
        segment_index=0,
        conversation_id="conversation-a",
        body_hash="a" * 64,
        binding_revision=1,
        conversation_revision=1,
    )
    # Bind this known operation to A's segment only.
    assert state.bind_verified_operation(
        pacing_plan_id=state.connection.execute(
            "SELECT pacing_plan_id FROM runtime_segment_executions"
        ).fetchone()[0],
        segment_index=0,
        operation_id=operation_id,
        authorization_id="auth-a",
    )
    events = state.apply_observation(
        _batch(
            contact_id="b",
            conversation_id="conversation-b",
            messages=[_message("b-spoof", Direction.OUTBOUND, operation_id=operation_id)],
        )
    )
    assert events == ("direction_unknown",)
    row = state.connection.execute(
        "SELECT paused,pause_reason FROM runtime_conversations WHERE conversation_id='conversation-b'"
    ).fetchone()
    assert (row["paused"], row["pause_reason"]) == (1, "unmatched_operation_echo")
    assert state.revisions("conversation-a")[1] == 1


def test_repeated_observation_is_idempotent_for_events_and_revision():
    state = RuntimeState()
    _register(state)
    batch = _batch(messages=[_message("same", Direction.INBOUND)])
    assert state.apply_observation(batch) == ("new_message",)
    revision = state.revisions("conversation")[1]
    assert state.apply_observation(batch) == ()
    assert state.revisions("conversation")[1] == revision
    assert state.connection.execute("SELECT COUNT(*) FROM runtime_event_outbox").fetchone()[0] == 1


def test_claimed_event_is_recovered_after_reopen_and_ack_is_idempotent(tmp_path: Path):
    db = tmp_path / "outbox.sqlite"
    state = RuntimeState(db)
    _register(state)
    state.apply_observation(_batch(messages=[_message("event", Direction.INBOUND)]))
    claimed = state.claim_events()
    assert len(claimed) == 1
    event_id = int(claimed[0]["event_id"])
    state.connection.close()

    recovered = RuntimeState(db)
    assert recovered.recover_events() == 1
    retried = recovered.claim_events()
    assert [int(row["event_id"]) for row in retried] == [event_id]
    recovered.complete_event(event_id, delivered=True)
    recovered.complete_event(event_id, delivered=True)
    assert recovered.recover_events() == 0
    assert recovered.claim_events() == []
