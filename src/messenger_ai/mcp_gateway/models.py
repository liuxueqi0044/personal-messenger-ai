"""Strict public contracts for M12's minimal local management gateway."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class GatewayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolPermission(StrEnum):
    READ_ONLY = "read_only"
    WRITE_NO_EXTERNAL_EFFECT = "write_no_external_effect"
    ADMIN_WRITE = "admin_write"
    HUMAN_RECONCILIATION = "human_reconciliation"


class GatewayErrorCode(StrEnum):
    TOOL_NOT_FOUND = "MCP_TOOL_NOT_FOUND"
    INVALID_ARGUMENT = "MCP_INVALID_ARGUMENT"
    FORBIDDEN = "MCP_FORBIDDEN"
    NOT_FOUND = "MCP_NOT_FOUND"
    CROSS_CONTACT_DENIED = "MCP_CROSS_CONTACT_DENIED"
    HUB_TIMEOUT = "MCP_HUB_TIMEOUT"
    HUB_UNAVAILABLE = "MCP_HUB_UNAVAILABLE"
    SEND_UNCERTAIN = "MCP_SEND_UNCERTAIN"
    SAFE_FAILURE = "MCP_SAFE_FAILURE"
    SDK_UNAVAILABLE = "MCP_SDK_UNAVAILABLE"


STABLE_ID = r"^[A-Za-z0-9][A-Za-z0-9_:\-/]{1,127}$"


class CallerIdentity(GatewayModel):
    caller_id: str = Field(pattern=STABLE_ID)
    scopes: tuple[str, ...] = ()
    allowed_contact_ids: tuple[str, ...] = ()


class EmptyInput(GatewayModel):
    pass


class CursorInput(GatewayModel):
    cursor: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=20, ge=1, le=100)


class ConversationInput(GatewayModel):
    conversation_id: str = Field(pattern=STABLE_ID)


class ContactInput(GatewayModel):
    contact_id: str = Field(pattern=STABLE_ID)


class DraftsInput(CursorInput):
    conversation_id: str | None = Field(default=None, pattern=STABLE_ID)


class PacingPlanInput(GatewayModel):
    pacing_plan_id: str = Field(pattern=STABLE_ID)


class RulepackReportInput(GatewayModel):
    rulepack_id: str = Field(pattern=STABLE_ID)
    version: str = Field(pattern=STABLE_ID)


class CreateDraftInput(GatewayModel):
    """No contact or body text is accepted over MCP in the first release."""

    conversation_id: str = Field(pattern=STABLE_ID)
    source_message_keys: tuple[str, ...] = Field(min_length=1, max_length=20)
    rule_version: str = Field(pattern=STABLE_ID)


class ReviewInput(GatewayModel):
    draft_id: str = Field(pattern=STABLE_ID)


class ValidateRulepackInput(GatewayModel):
    rulepack_draft_id: str = Field(pattern=STABLE_ID)


class PauseResumeInput(GatewayModel):
    scope: Literal["global", "platform", "contact"]
    target_id: str | None = Field(default=None, pattern=STABLE_ID)


class ReconcileUncertainInput(GatewayModel):
    operation_id: str = Field(pattern=STABLE_ID)
    evidence_ref: str = Field(pattern=STABLE_ID)


class GatewayError(GatewayModel):
    code: GatewayErrorCode
    message: str


class GatewayResponse(GatewayModel):
    ok: bool
    data: dict[str, Any] | None = None
    error: GatewayError | None = None
    audit_id: str = Field(default_factory=lambda: str(uuid4()))


class GatewayAuditEvent(GatewayModel):
    audit_id: str
    caller_id: str
    tool_name: str
    permission: ToolPermission
    parameter_summary: dict[str, Any]
    outcome: str
