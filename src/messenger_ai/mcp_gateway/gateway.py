"""Framework-neutral M12 registry: one call in, one Hub command out."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .models import (
    CallerIdentity,
    ContactInput,
    ConversationInput,
    CreateDraftInput,
    CursorInput,
    DraftsInput,
    EmptyInput,
    GatewayAuditEvent,
    GatewayError,
    GatewayErrorCode,
    GatewayResponse,
    PacingPlanInput,
    PauseResumeInput,
    ReconcileUncertainInput,
    ReviewInput,
    RulepackReportInput,
    ToolPermission,
    ValidateRulepackInput,
)


class HubGateway(Protocol):
    """The only M12 dependency; implementation owns authorization and data access."""

    def call(
        self, operation: str, payload: Mapping[str, Any], caller: CallerIdentity
    ) -> Mapping[str, Any]: ...


class HubUnavailableError(RuntimeError):
    pass


class HubUncertainError(RuntimeError):
    pass


InputModel = TypeVar("InputModel", bound=BaseModel)


class ToolSpec:
    def __init__(
        self,
        *,
        name: str,
        operation: str,
        input_model: type[InputModel],
        permission: ToolPermission,
        required_scope: str,
        paginated: bool = False,
    ) -> None:
        self.name = name
        self.operation = operation
        self.input_model = input_model
        self.permission = permission
        self.required_scope = required_scope
        self.paginated = paginated

    @property
    def schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema["additionalProperties"] = False
        return {
            "name": self.name,
            "description": f"{self.permission.value}; routed through the local application boundary",
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": self.permission == ToolPermission.READ_ONLY
            },
        }


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="messenger.health",
        operation="health",
        input_model=EmptyInput,
        permission=ToolPermission.READ_ONLY,
        required_scope="messenger.read",
    ),
    ToolSpec(
        name="messenger.get_capabilities",
        operation="get_capabilities",
        input_model=EmptyInput,
        permission=ToolPermission.READ_ONLY,
        required_scope="messenger.read",
    ),
    ToolSpec(
        name="messenger.list_unread",
        operation="list_unread",
        input_model=CursorInput,
        permission=ToolPermission.READ_ONLY,
        required_scope="messenger.read",
        paginated=True,
    ),
    ToolSpec(
        name="messenger.get_conversation",
        operation="get_conversation",
        input_model=ConversationInput,
        permission=ToolPermission.READ_ONLY,
        required_scope="messenger.read",
    ),
    ToolSpec(
        name="messenger.get_contact_profile",
        operation="get_contact_profile",
        input_model=ContactInput,
        permission=ToolPermission.READ_ONLY,
        required_scope="messenger.read",
    ),
    ToolSpec(
        name="messenger.list_drafts",
        operation="list_drafts",
        input_model=DraftsInput,
        permission=ToolPermission.READ_ONLY,
        required_scope="messenger.read",
        paginated=True,
    ),
    ToolSpec(
        name="messenger.get_pacing_plan",
        operation="get_pacing_plan",
        input_model=PacingPlanInput,
        permission=ToolPermission.READ_ONLY,
        required_scope="messenger.read",
    ),
    ToolSpec(
        name="messenger.get_rulepack_report",
        operation="get_rulepack_report",
        input_model=RulepackReportInput,
        permission=ToolPermission.READ_ONLY,
        required_scope="messenger.read",
    ),
    ToolSpec(
        name="messenger.create_draft",
        operation="create_draft",
        input_model=CreateDraftInput,
        permission=ToolPermission.WRITE_NO_EXTERNAL_EFFECT,
        required_scope="messenger.draft.write",
    ),
    ToolSpec(
        name="messenger.submit_for_review",
        operation="submit_for_review",
        input_model=ReviewInput,
        permission=ToolPermission.WRITE_NO_EXTERNAL_EFFECT,
        required_scope="messenger.review.submit",
    ),
    ToolSpec(
        name="messenger.validate_rulepack",
        operation="validate_rulepack",
        input_model=ValidateRulepackInput,
        permission=ToolPermission.WRITE_NO_EXTERNAL_EFFECT,
        required_scope="messenger.rules.validate",
    ),
    ToolSpec(
        name="messenger.pause",
        operation="pause",
        input_model=PauseResumeInput,
        permission=ToolPermission.ADMIN_WRITE,
        required_scope="messenger.automation.admin",
    ),
    ToolSpec(
        name="messenger.resume",
        operation="resume",
        input_model=PauseResumeInput,
        permission=ToolPermission.ADMIN_WRITE,
        required_scope="messenger.automation.admin",
    ),
    ToolSpec(
        name="messenger.reconcile_uncertain",
        operation="reconcile_uncertain",
        input_model=ReconcileUncertainInput,
        permission=ToolPermission.HUMAN_RECONCILIATION,
        required_scope="messenger.reconcile",
    ),
)
_SPEC_BY_NAME = {item.name: item for item in SPECS}


class MCPGateway:
    """Minimal-privilege registry.  It has no retry loop and no execution endpoint."""

    def __init__(self, hub: HubGateway) -> None:
        self.hub = hub
        self.audit_events: list[GatewayAuditEvent] = []

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [item.schema for item in SPECS]

    def invoke(
        self, tool_name: str, arguments: Mapping[str, Any], caller: CallerIdentity
    ) -> GatewayResponse:
        spec = _SPEC_BY_NAME.get(tool_name)
        if spec is None:
            return self._failure(
                tool_name,
                caller,
                ToolPermission.READ_ONLY,
                {},
                GatewayErrorCode.TOOL_NOT_FOUND,
            )
        if spec.required_scope not in caller.scopes:
            return self._failure(
                tool_name,
                caller,
                spec.permission,
                self._summary(arguments),
                GatewayErrorCode.FORBIDDEN,
            )
        try:
            parsed = spec.input_model.model_validate(arguments)
        except ValidationError:
            return self._failure(
                tool_name,
                caller,
                spec.permission,
                self._summary(arguments),
                GatewayErrorCode.INVALID_ARGUMENT,
            )
        payload = parsed.model_dump(mode="json")
        if not self._requested_contact_is_allowed(payload, caller):
            return self._failure(
                tool_name,
                caller,
                spec.permission,
                self._summary(payload),
                GatewayErrorCode.CROSS_CONTACT_DENIED,
            )
        try:
            # Intentionally one call exactly: timeout, unavailable and uncertain are terminal.
            result = dict(self.hub.call(spec.operation, payload, caller))
        except TimeoutError:
            return self._failure(
                tool_name,
                caller,
                spec.permission,
                self._summary(payload),
                GatewayErrorCode.HUB_TIMEOUT,
            )
        except HubUnavailableError:
            return self._failure(
                tool_name,
                caller,
                spec.permission,
                self._summary(payload),
                GatewayErrorCode.HUB_UNAVAILABLE,
            )
        except HubUncertainError:
            return self._failure(
                tool_name,
                caller,
                spec.permission,
                self._summary(payload),
                GatewayErrorCode.SEND_UNCERTAIN,
            )
        except KeyError:
            return self._failure(
                tool_name,
                caller,
                spec.permission,
                self._summary(payload),
                GatewayErrorCode.NOT_FOUND,
            )
        except Exception:  # noqa: BLE001 - sanitize unknown Hub implementation errors
            return self._failure(
                tool_name,
                caller,
                spec.permission,
                self._summary(payload),
                GatewayErrorCode.SAFE_FAILURE,
            )
        result = self._minimal_result(result, spec, payload)
        if result.get("status") == "SEND_UNCERTAIN":
            return self._failure(
                tool_name,
                caller,
                spec.permission,
                self._summary(payload),
                GatewayErrorCode.SEND_UNCERTAIN,
            )
        if not self._result_contacts_allowed(result, caller):
            return self._failure(
                tool_name,
                caller,
                spec.permission,
                self._summary(payload),
                GatewayErrorCode.CROSS_CONTACT_DENIED,
            )
        response = GatewayResponse(ok=True, data=result)
        self._audit(
            response.audit_id,
            caller,
            tool_name,
            spec.permission,
            self._summary(payload),
            "accepted",
        )
        return response

    @staticmethod
    def _minimal_result(
        result: Mapping[str, Any], spec: ToolSpec, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Bound list responses and remove accidental secret/path-like fields."""
        protected = {
            "api_key",
            "authorization",
            "credential",
            "database_path",
            "file_path",
            "screenshot",
            "token",
        }

        def redact(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {
                    str(key): redact(child)
                    for key, child in value.items()
                    if str(key).casefold() not in protected
                }
            if isinstance(value, Sequence) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                return [redact(item) for item in value]
            return value

        clean = redact(result)
        if spec.paginated and isinstance(clean.get("items"), list):
            clean["items"] = clean["items"][: int(payload["limit"])]
        return clean

    @staticmethod
    def _requested_contact_is_allowed(
        payload: Mapping[str, Any], caller: CallerIdentity
    ) -> bool:
        contact_id = payload.get("contact_id")
        return (
            not contact_id
            or not caller.allowed_contact_ids
            or contact_id in caller.allowed_contact_ids
        )

    @staticmethod
    def _result_contacts_allowed(
        result: Mapping[str, Any], caller: CallerIdentity
    ) -> bool:
        if not caller.allowed_contact_ids:
            return True
        contacts = MCPGateway._contact_ids(result)
        return all(contact in caller.allowed_contact_ids for contact in contacts)

    @staticmethod
    def _contact_ids(value: Any) -> set[str]:
        if isinstance(value, Mapping):
            found = {str(value["contact_id"])} if value.get("contact_id") else set()
            for child in value.values():
                found.update(MCPGateway._contact_ids(child))
            return found
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return (
                set().union(*(MCPGateway._contact_ids(item) for item in value))
                if value
                else set()
            )
        return set()

    @staticmethod
    def _summary(payload: Mapping[str, Any]) -> dict[str, Any]:
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        return {
            "keys": sorted(payload),
            "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        }

    def _failure(
        self,
        tool_name: str,
        caller: CallerIdentity,
        permission: ToolPermission,
        summary: dict[str, Any],
        code: GatewayErrorCode,
    ) -> GatewayResponse:
        response = GatewayResponse(
            ok=False, error=GatewayError(code=code, message=code.value)
        )
        self._audit(
            response.audit_id, caller, tool_name, permission, summary, code.value
        )
        return response

    def _audit(
        self,
        audit_id: str,
        caller: CallerIdentity,
        tool_name: str,
        permission: ToolPermission,
        summary: dict[str, Any],
        outcome: str,
    ) -> None:
        self.audit_events.append(
            GatewayAuditEvent(
                audit_id=audit_id,
                caller_id=caller.caller_id,
                tool_name=tool_name,
                permission=permission,
                parameter_summary=summary,
                outcome=outcome,
            )
        )
