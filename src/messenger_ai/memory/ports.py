from __future__ import annotations

from typing import Protocol

from messenger_ai.domain import EventEnvelope, InboundMessage

from .models import ContactContext, ContactFact, ContactPreference, IdentityBinding


class MemoryPort(Protocol):
    """The only M6 surface needed by planner/policy code."""

    def consume_inbound(self, event: EventEnvelope[InboundMessage]) -> bool: ...

    def context(
        self, contact_id: str, conversation_id: str, *, budget_chars: int
    ) -> ContactContext: ...

    def trace_fact(
        self, contact_id: str, conversation_id: str, fact_id: str
    ) -> ContactFact: ...

    def bind_identity(
        self, binding: IdentityBinding, *, expected_evidence_hash: str | None = None
    ) -> IdentityBinding: ...

    def set_preference(self, preference: ContactPreference) -> ContactPreference: ...
