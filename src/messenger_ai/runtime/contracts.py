from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from messenger_ai.domain import AuthorizedSendCommand, SendOperation


class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    UNKNOWN = "unknown"


class SegmentRef(RuntimeModel):
    pacing_plan_id: UUID
    segment_index: int = Field(ge=0, le=2)

    def __str__(self) -> str:
        return f"{self.pacing_plan_id}:{self.segment_index}"


class ObservedMessage(RuntimeModel):
    local_message_key: str = Field(min_length=1)
    direction: Direction
    text: str
    observed_at: datetime
    evidence_ref: str | None = None
    operation_id: UUID | None = None


class ObservationBatch(RuntimeModel):
    account_id: str = Field(min_length=1)
    contact_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    binding_revision: int = Field(ge=1)
    conversation_revision: int = Field(ge=1)
    complete: bool
    gap_reason: str | None = None
    messages: tuple[ObservedMessage, ...] = ()


class V5MessengerDriver(Protocol):
    async def prepare_send(
        self,
        command: AuthorizedSendCommand,
        *,
        operation_id: UUID,
        segment_ref: str,
        binding_revision: int,
        conversation_revision: int,
    ) -> SendOperation: ...

    async def commit_send(self, operation: SendOperation) -> SendOperation: ...
    async def verify_send(self, operation: SendOperation) -> SendOperation: ...

