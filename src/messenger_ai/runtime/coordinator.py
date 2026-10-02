from __future__ import annotations

from datetime import timedelta
from contextlib import nullcontext
from uuid import NAMESPACE_URL, UUID, uuid5

from messenger_ai.domain import EventEnvelope, InboundMessage, Platform
from messenger_ai.hub.service import HubService
from messenger_ai.memory.models import MemoryMessage, MemoryMessageDirection
from messenger_ai.memory.service import MemoryService
from messenger_ai.pacing import PacingScheduler

from .contracts import ObservationBatch, ObservedMessage
from .state import RuntimeState


class RuntimeCoordinator:
    """Reliable bridge between persisted observations and the existing authorities services."""

    def __init__(
        self,
        *,
        state: RuntimeState,
        hub: HubService,
        pacing: PacingScheduler,
        memory: MemoryService | None = None,
    ) -> None:
        self.state = state
        self.hub = hub
        self.pacing = pacing
        self.memory = memory

    def observe(self, batch: ObservationBatch) -> tuple[str, ...]:
        return self.state.apply_observation(batch)

    async def observe_driver(self, driver, conversation_id: str) -> tuple[str, ...]:
        binding_revision, conversation_revision = self.state.revisions(conversation_id)
        factory = getattr(driver, "observation_recovery_context", None)
        scope = factory(conversation_id, binding_revision=binding_revision,
            conversation_revision=conversation_revision) if factory else nullcontext()
        async with scope as recovery:
            batch = await driver.observe_conversation(
                conversation_id, binding_revision=binding_revision,
                conversation_revision=conversation_revision,
            )
            events = self.state.apply_observation(batch)
            if recovery is not None and not batch.complete:
                return events
            acknowledged = driver.acknowledge_observation(
                conversation_id, tuple(message.local_message_key for message in batch.messages)
            )
            if recovery is not None:
                recovery.finish(batch, events, acknowledged)
            return events

    def dispatch_events(
        self,
        *,
        limit: int = 100,
        conversation_id: str | None = None,
        one_shot_attempt_id: UUID | None = None,
    ) -> int:
        if (conversation_id is None) != (one_shot_attempt_id is None):
            raise ValueError(
                "exact event dispatch requires conversation and one-shot attempt"
            )
        delivered = 0
        for _ in range(limit):
            claimed = (
                self.state.claim_events(limit=1)
                if conversation_id is None
                else self.state.claim_events_for(
                    conversation_id,
                    one_shot_attempt_id=one_shot_attempt_id,
                    limit=1,
                )
            )
            if not claimed:
                break
            row = claimed[0]
            ok = False
            try:
                event_type = row["event_type"]
                if event_type == "new_message":
                    message = ObservedMessage.model_validate_json(row["payload_json"])
                    namespaced_key = f"qq-uia/{row['aggregate_id']}/{message.local_message_key}"
                    inbound = InboundMessage(
                        event_id=uuid5(NAMESPACE_URL, f"pmai-v5:{row['dedupe_key']}"),
                        platform=Platform.QQ,
                        account_id=row["account_id"],
                        conversation_id=row["aggregate_id"],
                        contact_id=row["contact_id"],
                        platform_message_key=namespaced_key,
                        observed_at=message.observed_at,
                        text=message.text,
                        evidence_ref=message.evidence_ref,
                        adapter_fingerprint="qq-vm-v5",
                    )
                    envelope = EventEnvelope(
                        event_id=inbound.event_id,
                        event_type="message.observed",
                        occurred_at=message.observed_at,
                        observed_at=message.observed_at,
                        aggregate_type="conversation",
                        aggregate_id=row["aggregate_id"],
                        payload=inbound,
                        producer="runtime.coordinator",
                    )
                    self.hub.ingest(inbound, envelope=envelope)
                    if self.memory is not None:
                        self.memory.consume_inbound(envelope)
                    self.pacing.on_new_inbound(row["aggregate_id"])
                elif event_type == "human_outbound":
                    self._record_outbound(row, MemoryMessageDirection.HUMAN_OUTBOUND)
                    self.pacing.on_user_takeover(row["aggregate_id"])
                elif event_type == "direction_unknown":
                    self.pacing.on_health_changed(row["aggregate_id"])
                elif event_type == "contact_pause":
                    self.pacing.on_paused(row["aggregate_id"])
                elif event_type == "global_pause":
                    conversations = self.state.connection.execute(
                        "SELECT conversation_id FROM runtime_conversations"
                    ).fetchall()
                    for item in conversations:
                        self.pacing.on_paused(item["conversation_id"])
                elif event_type in {"global_resume", "contact_resume"}:
                    pass
                elif event_type == "bot_observed":
                    self._record_outbound(row, MemoryMessageDirection.BOT_OUTBOUND)
                else:
                    raise ValueError(f"unsupported runtime event: {event_type}")
                # bot_observed intentionally does not cancel M10 segments.
                ok = True
                delivered += 1
            except Exception:  # noqa: BLE001 - durable event returns to pending
                # Current event returns to pending. No later row was preclaimed,
                # so another contact cannot become permanently dispatching.
                break
            finally:
                self.state.complete_event(row["event_id"], delivered=ok)
        return delivered

    def _record_outbound(self, row, direction: MemoryMessageDirection) -> None:
        message = ObservedMessage.model_validate_json(row["payload_json"])
        event_id = uuid5(NAMESPACE_URL, f"pmai-v5:{row['dedupe_key']}")
        key = f"qq-uia/{row['aggregate_id']}/{message.local_message_key}"
        self.hub.record_directional_message(
            event_id=event_id, conversation_id=row["aggregate_id"], contact_id=row["contact_id"],
            platform_message_key=key, direction=direction.value, text=message.text,
            observed_at=message.observed_at, operation_id=message.operation_id)
        if self.memory is None:
            return
        self.memory.record_message(MemoryMessage(
            contact_id=row["contact_id"], conversation_id=row["aggregate_id"],
            source_event_id=event_id,
            platform_message_key=key,
            text=message.text, observed_at=message.observed_at,
            expires_at=message.observed_at + timedelta(days=30), direction=direction,
        ))

    def recover(self) -> dict[str, int]:
        recovered = {
            "runtime_events": self.state.recover_events(),
            "pacing_due": self.pacing.recover_due_outbox(),
            **self.hub.recover(),
        }
        recovered["runtime_verified"] = self.state.recover_verified_segments()
        return recovered
