from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from messenger_ai.domain import DomainModel


class WorkerKind(StrEnum):
    HEALTH = "health"
    OBSERVE = "observe"
    PREPARE = "prepare"
    COMMIT = "commit"
    VERIFY = "verify"
    ABORT = "abort"
    STOP = "stop"


class WorkerStatus(StrEnum):
    OK = "ok"
    FAILED_SAFE = "failed_safe"
    UNCERTAIN = "uncertain"
    UNAVAILABLE = "unavailable"


class WorkerCommand(DomainModel):
    """A closed, auditable request vocabulary for the guest UI process.

    The main process allocates operation_id and durable message keys.  No request
    accepts a process command, a window handle, or a free-form recipient lookup.
    """

    request_id: UUID = Field(default_factory=uuid4)
    kind: WorkerKind
    binding_id: str | None = None
    binding_revision: int = Field(default=0, ge=0)
    conversation_revision: int = Field(default=0, ge=0)
    operation_id: UUID | None = None
    segment_ref: str | None = None
    text: str | None = None
    prepared_evidence: dict[str, object] | None = None
    deadline: datetime | None = None

    @field_validator("deadline")
    @classmethod
    def aware_deadline(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("deadline must be timezone-aware")
        return value

    def requires_binding(self) -> bool:
        return self.kind in {WorkerKind.OBSERVE, WorkerKind.PREPARE, WorkerKind.COMMIT, WorkerKind.VERIFY}


class WorkerResult(DomainModel):
    request_id: UUID
    kind: WorkerKind
    status: WorkerStatus
    worker_epoch: UUID
    operation_id: UUID | None = None
    binding_id: str | None = None
    binding_revision: int = Field(default=0, ge=0)
    conversation_revision: int = Field(default=0, ge=0)
    evidence: dict[str, object] = Field(default_factory=dict)
    error_code: str | None = None
