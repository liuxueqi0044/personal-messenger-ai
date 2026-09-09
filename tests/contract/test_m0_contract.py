from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from messenger_ai.domain import *
from messenger_ai.domain.events import EventEnvelope
from messenger_ai.domain.state_machines import SendStateMachine
from messenger_ai.testing.fakes import InMemoryStore


def test_event_envelope_roundtrip_and_consumer_dedupe():
    now = datetime.now(UTC)
    e = EventEnvelope(
        event_type="MessageObserved",
        occurred_at=now,
        observed_at=now,
        aggregate_type="conversation",
        aggregate_id="c1",
        payload={"x": 1},
        producer="test",
    )
    restored = EventEnvelope.model_validate_json(e.model_dump_json())
    s = InMemoryStore()
    assert s.append_event(restored, "x")
    assert not s.append_event(restored, "x")


def test_authorized_draft_is_immutable():
    d = Draft(
        conversation_id="c",
        contact_id="u",
        text="A",
        source_message_keys=("m",),
        rule_version="r",
    )
    d.authorize()
    with pytest.raises(ValueError):
        d.update_text("B")
    assert d.text == "A" and d.status == DraftStatus.AUTHORIZED


def test_uncertain_is_terminal():
    o = SendOperation(idempotency_key="k", draft_id=uuid4())
    SendStateMachine.transition(o, SendStatus.PREPARED)
    SendStateMachine.transition(o, SendStatus.UNCERTAIN)
    with pytest.raises(DomainError) as exc:
        SendStateMachine.transition(o, SendStatus.PREPARED)
    assert exc.value.code == ErrorCode.SEND_UNCERTAIN


def test_pacing_never_before_due():
    now = datetime.now(UTC)
    p = PacingPlan(
        conversation_id="c",
        source_message_keys=("m",),
        quiet_until=now + timedelta(seconds=6),
        earliest_send_at=now + timedelta(seconds=8),
        expires_at=now + timedelta(minutes=5),
        reading_delay_ms=2000,
        composition_delay_ms=4000,
        inter_message_gap_ms=15000,
        pacing_rule_version="1",
    )
    assert p.earliest_send_at >= p.quiet_until
