from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from messenger_ai.domain import AuthorizedSendCommand, SendOperation, SendStatus
from messenger_ai.hub.service import HubService
from messenger_ai.pacing.models import DueForRevalidation
from messenger_ai.policy import (
    AuthorizationBinding,
    AuthorizationService,
    PolicyRequest,
)

from .contracts import V5MessengerDriver
from .state import RuntimeState
from .staged_preparation import StagedPrepareAdapter, StagedPreparationError, StagedCleanupRequired


@dataclass(frozen=True)
class AuthorizedDueExecution:
    due: DueForRevalidation
    command: AuthorizedSendCommand
    token: str
    binding: AuthorizationBinding
    live_policy_request_factory: Callable[[], PolicyRequest]
    binding_revision: int
    conversation_revision: int
    global_revision: int
    prepared_adapter: StagedPrepareAdapter | None = None


class SendDispatcher:
    """The single M9/M10/Hub/driver send path.

    Preparation is reversible. M9 is consumed only after preparation and a
    fresh revision check. A second check immediately precedes commit.
    """

    def __init__(self, *, state: RuntimeState, hub: HubService, pacing,
                 authorization: AuthorizationService, driver: V5MessengerDriver) -> None:
        self.state, self.hub, self.pacing = state, hub, pacing
        self.authorization, self.driver = authorization, driver

    def _current(self, item: AuthorizedDueExecution) -> bool:
        binding, conversation, paused, global_revision, global_paused = self.state.execution_state(
            item.due.conversation_id
        )
        return (
            (binding, conversation, global_revision)
            == (item.binding_revision, item.conversation_revision, item.global_revision)
            and not paused and not global_paused
            and (item.prepared_adapter is None or item.prepared_adapter.current())
        )

    async def execute(self, item: AuthorizedDueExecution) -> SendOperation:
        if item.prepared_adapter is not None:
            return await self._execute_staged(item)
        due = item.due
        if not self.state.create_segment_execution(
            pacing_plan_id=due.pacing_plan_id, segment_index=due.segment_index,
            conversation_id=due.conversation_id, body_hash=due.body_hash,
            binding_revision=item.binding_revision,
            conversation_revision=item.conversation_revision,
        ):
            raise RuntimeError("runtime due segment CAS mismatch")
        if not self._current(item):
            raise RuntimeError("stale revisions before prepare")
        operation = self.hub.create_send_operation(item.command)
        if not self.state.bind_segment_operation(
            pacing_plan_id=due.pacing_plan_id, segment_index=due.segment_index,
            authorization_id=str(item.command.authorization_id), operation_id=operation.operation_id,
        ):
            raise RuntimeError("runtime authorized segment CAS mismatch")
        operation = await self.hub.prepare_send(
            operation.operation_id, self.driver, segment_ref=f"{due.pacing_plan_id}:{due.segment_index}",
            binding_revision=item.binding_revision, conversation_revision=item.conversation_revision,
        )
        if operation.status is not SendStatus.PREPARED:
            return self._settle_runtime(item, operation)
        if not self._current(item):
            abort = getattr(self.driver, "abort_send", None)
            aborted = None
            if abort is not None:
                aborted = await abort(operation)
            operation.status = SendStatus.CANCELLED if aborted is None or aborted.status is SendStatus.CANCELLED else SendStatus.UNCERTAIN
            if operation.status is SendStatus.UNCERTAIN:
                operation.error_code = "SEND_UNCERTAIN"
            operation = self.hub._persist_operation(operation)  # local authority bookkeeping
            return self._settle_runtime(item, operation)
        live_request = item.live_policy_request_factory()
        consumed = self.authorization.consume(
            item.token, expected_binding=item.binding, live_request=live_request
        )
        if not consumed.accepted or not self._current(item):
            abort = getattr(self.driver, "abort_send", None)
            aborted = None
            if abort is not None:
                aborted = await abort(operation)
            operation.status = SendStatus.CANCELLED if aborted is None or aborted.status is SendStatus.CANCELLED else SendStatus.UNCERTAIN
            if operation.status is SendStatus.UNCERTAIN:
                operation.error_code = "SEND_UNCERTAIN"
            operation = self.hub._persist_operation(operation)
            return self._settle_runtime(item, operation)
        operation = await self.hub.commit_prepared(operation.operation_id, self.driver)
        return self._settle_runtime(item, operation)

    async def _execute_staged(self, item: AuthorizedDueExecution) -> SendOperation:
        """Adopt the exact reversible draft, then use the original Hub/M9 path."""
        due, prepared = item.due, item.prepared_adapter
        operation, bound = None, False
        try:
            if not self._current(item):
                raise StagedPreparationError("staged_stale_before_operation")
            if not self.state.create_segment_execution(
                pacing_plan_id=due.pacing_plan_id, segment_index=due.segment_index,
                conversation_id=due.conversation_id, body_hash=due.body_hash,
                binding_revision=item.binding_revision, conversation_revision=item.conversation_revision,
            ):
                raise StagedPreparationError("staged_segment_cas_failed")
            operation = self.hub.create_send_operation(item.command)
            bound = self.state.bind_segment_operation(
                pacing_plan_id=due.pacing_plan_id, segment_index=due.segment_index,
                authorization_id=str(item.command.authorization_id), operation_id=operation.operation_id,
            )
            if not bound:
                raise StagedPreparationError("staged_operation_binding_failed")
            operation = await self.hub.prepare_send(operation.operation_id, prepared,
                segment_ref=f"{due.pacing_plan_id}:{due.segment_index}",
                binding_revision=item.binding_revision, conversation_revision=item.conversation_revision)
            if operation.status is not SendStatus.PREPARED or not self._current(item):
                raise StagedPreparationError("staged_prepare_unproven")
            consumed = self.authorization.consume(item.token, expected_binding=item.binding,
                live_request=item.live_policy_request_factory())
            if not consumed.accepted or not self._current(item):
                raise StagedPreparationError("staged_consume_rejected")

            dispatcher = self

            class CommitAdapter:
                async def commit_send(self, op):
                    # Hub persists commit intent immediately before this
                    # call. No UI preparation or fresh profile probe belongs
                    # in this short adoption/commit path.
                    if not dispatcher._current(item):
                        raise StagedPreparationError("staged_stale_before_commit")
                    return await dispatcher.driver.commit_send(op)

                async def verify_send(self, op):
                    # Verification may outlive the preparation lease. It
                    # observes an existing operation and grants no new input.
                    return await dispatcher.driver.verify_send(op)

            operation = await self.hub.commit_prepared(operation.operation_id, CommitAdapter())
            if operation.status is SendStatus.UNCERTAIN:
                prepared.port.retain_cleanup_hold(prepared.ticket, reason="staged_commit_uncertain")
            return self._settle_runtime(item, operation)
        except BaseException as exc:
            intent = False
            if operation is not None:
                row = self.hub.store.connection.execute(
                    "SELECT commit_intent FROM send_operations WHERE operation_id=?",
                    (str(operation.operation_id),)).fetchone()
                intent = row is not None and bool(row[0])
            if intent:
                # Once intent exists, a missing acknowledgement cannot prove
                # an unsent draft. Preserve the original operation and hold.
                prepared.port.retain_cleanup_hold(prepared.ticket, reason="staged_commit_uncertain")
                if operation is not None:
                    operation = self.hub._operation(operation.operation_id)
                    operation.status, operation.error_code = SendStatus.UNCERTAIN, "SEND_UNCERTAIN"
                    operation = self.hub._persist_operation(operation, commit_intent=True)
            else:
                clean = True
                try:
                    await prepared.abort()
                except StagedCleanupRequired:
                    clean = False
                if operation is not None:
                    operation = self.hub._operation(operation.operation_id)
                    operation.status = SendStatus.CANCELLED if clean else SendStatus.UNCERTAIN
                    operation.error_code = None if clean else "SEND_UNCERTAIN"
                    operation = self.hub._persist_operation(operation)
            if operation is not None and bound:
                operation = self._settle_runtime(item, operation)
            # Cancellation stays cancellation after exact owned cleanup; the
            # durable claim and operation are recovered explicitly on restart.
            if not isinstance(exc, Exception) or operation is None or not bound:
                raise
            return operation

    def _settle_runtime(
        self, item: AuthorizedDueExecution, operation: SendOperation
    ) -> SendOperation:
        # A Hub VERIFIED result is not, by itself, durable cross-service proof.
        # DueCoordinator first atomically records the exact pacing receipt, then
        # RuntimeState re-reads both stores before it may persist ``verified``.
        if operation.status is SendStatus.VERIFIED:
            return operation
        due = item.due
        if not self.state.settle_segment_operation(
            pacing_plan_id=due.pacing_plan_id,
            segment_index=due.segment_index,
            authorization_id=item.command.authorization_id,
            operation_id=operation.operation_id,
            operation_status=operation.status.value,
        ):
            raise RuntimeError("runtime terminal segment CAS mismatch")
        return operation
