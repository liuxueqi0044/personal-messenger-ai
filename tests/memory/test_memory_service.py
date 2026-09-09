from __future__ import annotations

import concurrent.futures
import hashlib
import random
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from messenger_ai.domain import DomainError, EventEnvelope, InboundMessage, Platform
from messenger_ai.memory import (
    Contact,
    ContactFact,
    ContactPreference,
    FactStatus,
    HumanApproval,
    IdentityBinding,
    MemoryService,
    RelationshipState,
    SourceEvidence,
    SourceKind,
    SQLiteMemoryStore,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, days: int) -> None:
        self.value += timedelta(days=days)


@pytest.fixture
def memory(tmp_path):
    clock = Clock()
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    yield MemoryService(store, clock=clock), clock
    store.close()


def approval(clock: Clock, actor: str = "owner") -> HumanApproval:
    return HumanApproval(
        verified_by=actor, verified_at=clock.now(), reason="human review"
    )


def contact_and_binding(
    memory, clock, suffix: str = "1"
) -> tuple[Contact, IdentityBinding]:
    service = memory
    contact = Contact(
        contact_id=f"contact-{suffix}",
        display_name="Alex",
        relationship_tags=("friend",),
        created_at=clock.now(),
    )
    service.create_contact(contact)
    binding = IdentityBinding(
        contact_id=contact.contact_id,
        platform=Platform.QQ,
        account_id="account-1",
        conversation_id=f"conversation-{suffix}",
        platform_evidence_hash=hashlib.sha256(f"proof-{suffix}".encode()).hexdigest(),
        approval=approval(clock),
    )
    service.bind_identity(binding)
    return contact, binding


def inbound(
    clock: Clock, binding: IdentityBinding, key: str = "message-1"
) -> EventEnvelope[InboundMessage]:
    message = InboundMessage(
        platform=binding.platform,
        account_id=binding.account_id,
        conversation_id=binding.conversation_id,
        contact_id=binding.contact_id,
        platform_message_key=key,
        observed_at=clock.now(),
        text=f"text:{key}",
    )
    return EventEnvelope(
        event_type="message.observed",
        occurred_at=clock.now(),
        observed_at=clock.now(),
        aggregate_type="conversation",
        aggregate_id=binding.conversation_id,
        payload=message,
        producer="test",
    )


def message_id(service: MemoryService, contact_id: str, conversation_id: str) -> UUID:
    row = service.store.connection.execute(
        "SELECT message_id FROM memory_messages WHERE contact_id=? AND conversation_id=?",
        (contact_id, conversation_id),
    ).fetchone()
    return UUID(row["message_id"])


def test_event_consumer_is_idempotent_and_context_is_pair_scoped(memory) -> None:
    service, clock = memory
    first, first_binding = contact_and_binding(service, clock, "a")
    _second, second_binding = contact_and_binding(service, clock, "b")
    assert service.consume_inbound(inbound(clock, first_binding, "a-1"))
    assert service.consume_inbound(inbound(clock, second_binding, "b-1"))
    context = service.context(
        first.contact_id, first_binding.conversation_id, budget_chars=10_000
    )
    assert [message.platform_message_key for message in context.recent_messages] == [
        "a-1"
    ]
    assert all(
        message.contact_id == first.contact_id for message in context.recent_messages
    )
    with pytest.raises(DomainError):
        service.context(
            first.contact_id, second_binding.conversation_id, budget_chars=100
        )


def test_10000_fixed_seed_same_name_and_similar_identity_never_cross_binds(
    memory,
) -> None:
    service, clock = memory
    randomizer = random.Random(20260908)
    for number in range(10_000):
        suffix = str(number)
        contact = Contact(
            contact_id=f"same-name-{suffix}",
            display_name="Ａlex" if randomizer.randrange(2) else "Alex",
            created_at=clock.now(),
        )
        service.create_contact(contact)
        binding = IdentityBinding(
            contact_id=contact.contact_id,
            platform=Platform.QQ,
            account_id="account-1",
            conversation_id=f"conversation-{suffix}",
            platform_evidence_hash=hashlib.sha256(
                f"proof-{suffix}".encode()
            ).hexdigest(),
            approval=approval(clock),
        )
        assert service.bind_identity(binding).contact_id == contact.contact_id
    collision_contact = Contact(
        contact_id="collision", display_name="Alex", created_at=clock.now()
    )
    service.create_contact(collision_contact)
    with pytest.raises(DomainError) as exc:
        service.bind_identity(
            IdentityBinding(
                contact_id="collision",
                platform=Platform.QQ,
                account_id="account-1",
                conversation_id="conversation-777",
                platform_evidence_hash=hashlib.sha256(b"changed").hexdigest(),
                approval=approval(clock),
            )
        )
    assert exc.value.code.value == "IDENTITY_AMBIGUOUS"


def test_fact_traceability_and_context_budget_preserves_hard_rules_and_preferences(
    memory,
) -> None:
    service, clock = memory
    contact, binding = contact_and_binding(service, clock)
    service.consume_inbound(inbound(clock, binding))
    source = message_id(service, contact.contact_id, binding.conversation_id)
    fact = ContactFact(
        contact_id=contact.contact_id,
        conversation_id=binding.conversation_id,
        key="allergy",
        value="peanuts",
        confidence=1,
        status=FactStatus.CONFIRMED,
        evidence=SourceEvidence(
            kind=SourceKind.MESSAGE,
            source_message_id=source,
            recorded_by="extractor",
            recorded_at=clock.now(),
        ),
        hard_rule=True,
        created_at=clock.now(),
    )
    service.record_fact(fact)
    preference = ContactPreference(
        contact_id=contact.contact_id,
        conversation_id=binding.conversation_id,
        key="language",
        value="zh-CN",
        approval=approval(clock),
    )
    service.set_preference(preference)
    assert (
        service.trace_fact(
            contact.contact_id, binding.conversation_id, str(fact.fact_id)
        ).evidence.source_message_id
        == source
    )
    context = service.context(
        contact.contact_id, binding.conversation_id, budget_chars=1
    )
    assert context.hard_rules == (fact,)
    assert context.confirmed_preferences == (preference,)
    assert context.recent_messages == ()
    assert context.truncated


def test_ttl_relationship_evidence_and_cross_contact_source_rejection(memory) -> None:
    service, clock = memory
    first, first_binding = contact_and_binding(service, clock, "a")
    second, second_binding = contact_and_binding(service, clock, "b")
    service.consume_inbound(inbound(clock, first_binding, "a-message"))
    source = message_id(service, first.contact_id, first_binding.conversation_id)
    relationship = RelationshipState(
        contact_id=first.contact_id,
        conversation_id=first_binding.conversation_id,
        state="close friend",
        evidence=SourceEvidence(
            kind=SourceKind.MESSAGE,
            source_message_id=source,
            recorded_by="owner",
            recorded_at=clock.now(),
        ),
        approval=approval(clock),
    )
    service.set_relationship(relationship)
    with pytest.raises(DomainError):
        service.record_fact(
            ContactFact(
                contact_id=second.contact_id,
                conversation_id=second_binding.conversation_id,
                key="leak",
                value="no",
                confidence=1,
                status=FactStatus.CONFIRMED,
                evidence=SourceEvidence(
                    kind=SourceKind.MESSAGE,
                    source_message_id=source,
                    recorded_by="extractor",
                    recorded_at=clock.now(),
                ),
                created_at=clock.now(),
            )
        )
    clock.advance(31)
    assert service.purge_expired_messages() == 1
    assert (
        service.context(
            first.contact_id, first_binding.conversation_id, budget_chars=1000
        ).recent_messages
        == ()
    )


def test_delete_cascades_audits_and_restores_within_the_declared_window(memory) -> None:
    service, clock = memory
    contact, binding = contact_and_binding(service, clock)
    service.consume_inbound(inbound(clock, binding))
    receipt = service.delete_contact(contact.contact_id, deleted_by="owner")
    for table in (
        "memory_messages",
        "memory_bindings",
        "memory_summaries",
        "memory_facts",
        "memory_preferences",
        "memory_relationships",
    ):
        assert (
            service.store.connection.execute(
                f"SELECT COUNT(1) FROM {table} WHERE contact_id=?",
                (contact.contact_id,),
            ).fetchone()[0]
            == 0
        )
    audit = service.store.connection.execute(
        "SELECT action FROM memory_audit WHERE contact_id=? ORDER BY created_at DESC",
        (contact.contact_id,),
    ).fetchall()
    assert any(row["action"] == "contact.deleted" for row in audit)
    restored = service.restore_contact(receipt.deletion_id, restored_by="owner")
    assert restored.status.value == "active"
    assert service.context(
        contact.contact_id, binding.conversation_id, budget_chars=1000
    ).recent_messages


def test_concurrent_same_event_has_one_memory_insert(memory) -> None:
    service, clock = memory
    _contact, binding = contact_and_binding(service, clock)
    event = inbound(clock, binding)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(service.consume_inbound, [event] * 100))
    assert sum(results) == 1
    assert (
        service.store.connection.execute(
            "SELECT COUNT(1) FROM memory_messages"
        ).fetchone()[0]
        == 1
    )
