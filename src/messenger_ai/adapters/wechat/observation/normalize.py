"""Normalize accepted visual observations into M0 inbound events."""

from __future__ import annotations

from datetime import datetime

from messenger_ai.domain.models import ContentType, InboundMessage, Platform

from .models import (
    MessageDirection,
    NormalizedWechatEvent,
    VisualContentType,
    VisualObservation,
    utc_now,
)


class WeChatEventNormalizer:
    def normalize_events(
        self, observation: VisualObservation, *, now: datetime | None = None
    ) -> tuple[NormalizedWechatEvent, ...]:
        if not observation.publishable or observation.evidence.is_expired(
            now or utc_now()
        ):
            return ()
        events: list[NormalizedWechatEvent] = []
        for message in observation.messages:
            if message.content_type != VisualContentType.TEXT:
                continue
            if message.direction != MessageDirection.INBOUND or not message.text:
                continue
            events.append(
                NormalizedWechatEvent(
                    event_type="MessageObserved",
                    conversation_id=observation.conversation_id,
                    platform_message_key=message.platform_message_key,
                    direction=message.direction,
                    text=message.text,
                    content_type=message.content_type,
                    observed_at=observation.observed_at,
                    evidence_ref=observation.evidence.evidence_id,
                    confidence=min(message.confidence, observation.confidence.overall),
                )
            )
        return tuple(events)

    def normalize_messages(
        self,
        observation: VisualObservation,
        *,
        account_id: str = "wechat",
        contact_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[InboundMessage, ...]:
        return tuple(
            InboundMessage(
                platform=Platform.WECHAT,
                account_id=account_id,
                conversation_id=observation.conversation_id,
                contact_id=contact_id or observation.conversation_id,
                platform_message_key=event.platform_message_key,
                observed_at=event.observed_at,
                text=event.text,
                content_type=ContentType.TEXT,
                displayed_time=next(
                    (
                        item.displayed_time
                        for item in observation.messages
                        if item.platform_message_key == event.platform_message_key
                    ),
                    None,
                ),
                extraction_confidence=event.confidence,
                evidence_ref=event.evidence_ref,
                adapter_fingerprint=observation.binding_id,
            )
            for event in self.normalize_events(observation, now=now)
        )


EventNormalizer = WeChatEventNormalizer
