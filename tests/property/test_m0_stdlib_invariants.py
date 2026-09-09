"""Deterministic fallback invariants for environments without Hypothesis."""

import random
from uuid import uuid4

import pytest

from messenger_ai.domain.errors import DomainError, ErrorCode
from messenger_ai.domain.models import Draft, SendOperation, SendStatus
from messenger_ai.domain.state_machines import SendStateMachine
from messenger_ai.testing.fakes import InMemoryStore


@pytest.mark.parametrize("text", ["", "hello", "你好，世界", "x" * 256])
def test_authorized_draft_fields_are_immutable(text):
    draft = Draft(
        conversation_id="c",
        contact_id="contact-a",
        text=text,
        source_message_keys=("m-1",),
        rule_version="r1",
    )
    draft.authorize()
    for field, value in {
        "text": "changed",
        "contact_id": "contact-b",
        "conversation_id": "other",
        "source_message_keys": ("m-2",),
        "rule_version": "r2",
    }.items():
        with pytest.raises(ValueError):
            setattr(draft, field, value)


def test_uncertain_cannot_be_retried_or_recommitted():
    operation = SendOperation(idempotency_key="idem-1", draft_id=uuid4())
    SendStateMachine.transition(operation, SendStatus.PREPARED)
    SendStateMachine.transition(operation, SendStatus.UNCERTAIN)
    for target in (SendStatus.PREPARED, SendStatus.COMMITTED, SendStatus.VERIFIED):
        with pytest.raises(DomainError) as exc:
            SendStateMachine.transition(operation, target)
        assert exc.value.code == ErrorCode.SEND_UNCERTAIN


def test_fixed_seed_idempotency_never_claims_twice():
    rng = random.Random(20260907)
    store = InMemoryStore()
    keys = [f"k-{rng.randrange(20)}" for _ in range(500)]
    claimed = sum(store.claim_idempotency(key) for key in keys)
    assert claimed == len(set(keys))
