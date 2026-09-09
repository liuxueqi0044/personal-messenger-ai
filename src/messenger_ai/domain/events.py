from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventEnvelope[T](BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID = Field(default_factory=uuid4)
    event_type: str
    occurred_at: datetime
    observed_at: datetime
    aggregate_type: str
    aggregate_id: str
    causation_id: UUID | None = None
    correlation_id: UUID = Field(default_factory=uuid4)
    schema_version: int = 1
    payload: T
    producer: str
    trace_id: str = ""
