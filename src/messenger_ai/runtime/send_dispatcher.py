from __future__ import annotations

from dataclasses import dataclass

from messenger_ai.domain import AuthorizedSendCommand, SendOperation, SendStatus
from messenger_ai.hub.service import HubService
from messenger_ai.pacing import PacingScheduler
from messenger_ai.pacing.models import DueForRevalidation
from messenger_ai.policy import AuthorizationBinding, AuthorizationService, PolicyRequest

from .contracts import V5MessengerDriver
from .state import RuntimeState


@dataclass(frozen=True)
class AuthorizedDueExecution:
    due: DueForRevalidation
    command: AuthorizedSendCommand
    token: str
    binding: AuthorizationBinding
    live_policy_request: PolicyRequest
    binding_revision: int
    conversation_revision: int


class SendDispatcher:
    """The single M9/M10/Hub/driver send path.

    Preparation is reversible. M9 is consumed only after preparation and a
    fresh revision check. A second check immediately precedes commit.
    """

    def __init__(self, *, state: RuntimeState, hub: HubService, pacing: PacingScheduler,
                 authorization: AuthorizationService, driver: V5MessengerDriver) -> None:
        self.state, self.hub, self.pacing = state, hub, pacing
        self.authorization, self.driver = authorization, driver

    def _current(self, item: AuthorizedDueExecution) -> bool:
        return self.state.revisions(item.due.conversation_id) == (
            item.binding_revision, item.conversation_revision
        )

    async def execute(self, item: AuthorizedDueExecution) -> SendOperation:
        due = item.due
        self.state.create_segment_execution(
            pacing_plan_id=due.pacing_plan_id, segment_index=due.segment_index,
            conversation_id=due.conversation_id, body_hash=due.body_hash,
            binding_revision=item.binding_revision,
            conversation_revision=item.conversation_revision,
        )
        if not self._current(item):
            raise RuntimeError("stale revisions before prepare")
        operation = self.hub.create_send_operation(item.command)
        self.state.bind_segment_operation(
            pacing_plan_id=due.pacing_plan_id, segment_index=due.segment_index,
            authorization_id=str(item.command.authorization_id), operation_id=operation.operation_id,
        )
        operation = await self.hub.prepare_send(
            operation.operation_id, self.driver, segment_ref=f"{due.pacing_plan_id}:{due.segment_index}",
            binding_revision=item.binding_revision, conversation_revision=item.conversation_revision,
        )
        if operation.status is not SendStatus.PREPARED:
            self.pacing.record_revalidation_result(
                due.pacing_plan_id, segment_sent_and_verified=False,
                segment_index=due.segment_index, operation_id=operation.operation_id,
            )
            return operation
        if not self._current(item):
            abort = getattr(self.driver, "abort_send", None)
            if abort is not None:
                await abort(operation)
            operation.status = SendStatus.CANCELLED
            operation = self.hub._persist_operation(operation)  # local authority bookkeeping
            self.pacing.record_revalidation_result(
                due.pacing_plan_id, segment_sent_and_verified=False,
                segment_index=due.segment_index, operation_id=operation.operation_id,
            )
            return operation
        consumed = self.authorization.consume(
            item.token, expected_binding=item.binding, live_request=item.live_policy_request
        )
        if not consumed.accepted or not self._current(item):
            abort = getattr(self.driver, "abort_send", None)
            if abort is not None:
                await abort(operation)
            operation.status = SendStatus.CANCELLED
            operation = self.hub._persist_operation(operation)
            self.pacing.record_revalidation_result(
                due.pacing_plan_id, segment_sent_and_verified=False,
                segment_index=due.segment_index, operation_id=operation.operation_id,
            )
            return operation
        operation = await self.hub.commit_prepared(operation.operation_id, self.driver)
        verified = operation.status is SendStatus.VERIFIED
        if verified:
            self.state.bind_verified_operation(
                pacing_plan_id=due.pacing_plan_id, segment_index=due.segment_index,
                operation_id=operation.operation_id,
                authorization_id=item.command.authorization_id,
            )
        self.pacing.record_revalidation_result(
            due.pacing_plan_id, segment_sent_and_verified=verified,
            segment_index=due.segment_index, operation_id=operation.operation_id,
        )
        return operation
