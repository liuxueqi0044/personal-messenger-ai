from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from messenger_ai.domain import (
    AuthorizedSendCommand,
    ContentType,
    ErrorCode,
    InboundMessage,
    Platform,
    SendOperation,
    SendStateMachine,
    SendStatus,
)
from messenger_ai.execution_guard import (
    ActionPhase,
    ExecutionGuard,
    GuardedAction,
    GuardedActionType,
    GuardedResult,
)

from .models import (
    BubbleDirection,
    QQBubble,
    QQConversation,
    QQIdentityBinding,
    QQPreparedEvidence,
    QQPreparedSend,
    QQSelectorPack,
    QQWindow,
)
from .ports import QQAccessibilityPort


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class _ResolveOutcome:
    window: QQWindow | None = None
    conversation: QQConversation | None = None
    error: ErrorCode | None = None


@dataclass(frozen=True)
class _TargetOutcome:
    window: QQWindow | None = None
    conversation: QQConversation | None = None
    error: ErrorCode | None = None


class QQAdapter:
    """Fail-closed QQ backend adapter implemented entirely through injected UIA."""

    def __init__(
        self,
        *,
        accessibility: QQAccessibilityPort,
        guard: ExecutionGuard,
        selector_pack: QQSelectorPack,
        environment_fingerprint: str,
        capability_version: str,
        bindings: tuple[QQIdentityBinding, ...],
        text_provider: Callable[[AuthorizedSendCommand], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if selector_pack.environment_fingerprint != environment_fingerprint:
            raise ValueError("selector pack must exactly match adapter environment")
        if len({binding.hub_conversation_id for binding in bindings}) != len(bindings):
            raise ValueError("one Hub conversation must have one QQ identity binding")
        self.accessibility = accessibility
        self.guard = guard
        self.selector_pack = selector_pack
        self.environment_fingerprint = environment_fingerprint
        self.capability_version = capability_version
        self.bindings = {item.hub_conversation_id: item for item in bindings}
        self.text_provider = text_provider
        self.now = now or (lambda: datetime.now(UTC))
        self._prepared: dict[str, QQPreparedSend] = {}
        self._operations: dict[str, SendOperation] = {}
        self._seen_message_keys: set[str] = set()

    async def poll_events(self, cursor: str | None = None) -> list[InboundMessage]:
        del (
            cursor
        )  # QQ UIA exposes a bounded visible tree; normalization de-duplicates it.
        result = await self._run(
            GuardedActionType.OBSERVE,
            ActionPhase.READ,
            ("uia.find", "uia.read"),
            self._observe,
        )
        if not result.succeeded:
            return []
        messages: list[InboundMessage] = []
        for bubble, binding in result.value:
            if bubble.direction is not BubbleDirection.INBOUND:
                continue
            if bubble.message_key in self._seen_message_keys:
                continue
            self._seen_message_keys.add(bubble.message_key)
            messages.append(
                InboundMessage(
                    platform=Platform.QQ,
                    account_id=binding.account_id,
                    conversation_id=binding.hub_conversation_id,
                    contact_id=binding.contact_id,
                    platform_message_key=bubble.message_key,
                    observed_at=bubble.observed_at,
                    text=bubble.text,
                    content_type=ContentType.TEXT,
                    extraction_confidence=1,
                    evidence_ref=f"qq:{bubble.tree_digest}:{bubble.message_key}",
                    adapter_fingerprint=self.environment_fingerprint,
                )
            )
        return messages

    async def prepare_send(self, command: AuthorizedSendCommand) -> SendOperation:
        existing = self._operations.get(command.idempotency_key)
        if existing is not None:
            return existing
        operation = SendOperation(
            idempotency_key=command.idempotency_key, draft_id=command.draft_id
        )
        self._operations[command.idempotency_key] = operation
        if command.expires_at <= self.now() or self.text_provider is None:
            return self._fail(operation, ErrorCode.STALE_CONTEXT)
        text = self.text_provider(command)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != command.text_hash:
            return self._fail(operation, ErrorCode.FAILED_SAFE)
        binding = self.bindings.get(command.conversation_id)
        if binding is None:
            return self._fail(operation, ErrorCode.IDENTITY_AMBIGUOUS)

        resolved = await self._run(
            GuardedActionType.RESOLVE,
            ActionPhase.PREPARE,
            ("uia.find", "uia.read", "uia.selection_item_pattern.select"),
            lambda: self._resolve(binding, command.expected_last_message_key),
        )
        if not resolved.succeeded:
            return self._fail(operation, self._safe_code(resolved))
        if resolved.value.error is not None:
            await self._quarantine_if_needed(resolved.value.error)
            return self._fail(operation, resolved.value.error)
        window, conversation = resolved.value.window, resolved.value.conversation
        assert window is not None and conversation is not None
        composed = await self._run(
            GuardedActionType.COMPOSE,
            ActionPhase.PREPARE,
            ("uia.read", "uia.value_pattern.set", "uia.text_pattern.get"),
            lambda: self._compose(window, conversation, text),
        )
        if not composed.succeeded:
            return self._fail(operation, self._safe_code(composed))
        evidence = composed.value
        if evidence.last_message_key != command.expected_last_message_key:
            return self._fail(operation, ErrorCode.STALE_CONTEXT)
        SendStateMachine.transition(operation, SendStatus.PREPARED)
        self._prepared[str(operation.operation_id)] = QQPreparedSend(
            operation_id=operation.operation_id,
            idempotency_key=operation.idempotency_key,
            text=text,
            evidence=evidence,
        )
        return operation

    async def commit_send(self, operation: SendOperation) -> SendOperation:
        if operation.status is SendStatus.UNCERTAIN:
            return operation
        prepared = self._prepared.get(str(operation.operation_id))
        if operation.status is not SendStatus.PREPARED or prepared is None:
            return self._fail(operation, ErrorCode.FAILED_SAFE)
        result = await self._run(
            GuardedActionType.SEND,
            ActionPhase.COMMIT,
            ("uia.find", "uia.read", "uia.invoke_pattern.invoke"),
            lambda: self._commit(prepared),
        )
        if not result.succeeded:
            return self._uncertain(operation)
        if result.value is not None:
            await self._quarantine_if_needed(result.value)
            return self._fail(operation, result.value)
        SendStateMachine.transition(operation, SendStatus.COMMITTED)
        return operation

    async def verify_send(self, operation: SendOperation) -> SendOperation:
        if operation.status is SendStatus.UNCERTAIN:
            return operation
        prepared = self._prepared.get(str(operation.operation_id))
        if operation.status is not SendStatus.COMMITTED or prepared is None:
            return self._fail(operation, ErrorCode.FAILED_SAFE)
        result = await self._run(
            GuardedActionType.VERIFY,
            ActionPhase.VERIFY,
            ("uia.find", "uia.read"),
            lambda: self._verify(prepared),
        )
        if not result.succeeded or result.value is not True:
            return self._uncertain(operation)
        SendStateMachine.transition(operation, SendStatus.VERIFIED)
        return operation

    async def _run(
        self,
        action_type: GuardedActionType,
        phase: ActionPhase,
        operations: tuple[str, ...],
        action: Callable[[], Any],
    ) -> GuardedResult:
        fingerprint = await self.guard.fingerprinter.capture(Platform.QQ)
        request = GuardedAction(
            platform=Platform.QQ,
            action_type=action_type,
            phase=phase,
            requested_operations=operations,
            capability_version=self.capability_version,
            environment_fingerprint=self.environment_fingerprint,
            target_process_id=fingerprint.process_id,
            target_window_handle=fingerprint.window_handle,
        )
        return await self.guard.run(request, lambda _token: action())

    async def _observe(self) -> list[tuple[QQBubble, QQIdentityBinding]]:
        window = await self._bind_window()
        tree_digest = await _await(self.accessibility.tree_digest(window))
        bubbles = await _await(
            self.accessibility.list_bubbles(
                window, self.selector_pack.selector("bubbles")
            )
        )
        rows: list[tuple[QQBubble, QQIdentityBinding]] = []
        for bubble in bubbles:
            if bubble.tree_digest != tree_digest:
                continue
            matches = [
                binding
                for binding in self.bindings.values()
                if binding.platform_conversation_id == bubble.conversation_internal_id
            ]
            if len(matches) == 1:
                rows.append((bubble, matches[0]))
        return rows

    async def _resolve(
        self, binding: QQIdentityBinding, expected_last_key: str
    ) -> _ResolveOutcome:
        try:
            window = await self._bind_window()
        except LookupError:
            return _ResolveOutcome(error=ErrorCode.ADAPTER_QUARANTINED)
        conversations = await _await(
            self.accessibility.list_conversations(
                window, self.selector_pack.selector("conversations")
            )
        )
        matches = [item for item in conversations if binding.matches(item)]
        if len(matches) != 1:
            return _ResolveOutcome(error=ErrorCode.IDENTITY_AMBIGUOUS)
        conversation = matches[0]
        if conversation.last_message_key != expected_last_key:
            return _ResolveOutcome(error=ErrorCode.STALE_CONTEXT)
        await _await(
            self.accessibility.select_conversation(
                window, conversation, self.selector_pack.selector("conversation_item")
            )
        )
        return _ResolveOutcome(window=window, conversation=conversation)

    async def _compose(
        self, window: QQWindow, conversation: QQConversation, text: str
    ) -> QQPreparedEvidence:
        await _await(
            self.accessibility.write_composer(
                window, text, self.selector_pack.selector("composer")
            )
        )
        actual = await _await(
            self.accessibility.read_composer(
                window, self.selector_pack.selector("composer")
            )
        )
        if (
            hashlib.sha256(actual.encode("utf-8")).hexdigest()
            != hashlib.sha256(text.encode("utf-8")).hexdigest()
        ):
            raise ValueError("composer readback hash differs")
        digest = await _await(self.accessibility.tree_digest(window))
        return QQPreparedEvidence(
            process_id=window.process_id,
            window_handle=window.window_handle,
            conversation_internal_id=conversation.internal_id,
            participant_signature=conversation.participant_signature,
            last_message_key=conversation.last_message_key,
            tree_digest=digest,
            text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            bound_at=self.now(),
        )

    async def _commit(self, prepared: QQPreparedSend) -> ErrorCode | None:
        target = await self._current_target(prepared.evidence)
        if target.error is not None:
            return target.error
        window = target.window
        assert window is not None
        await _await(
            self.accessibility.invoke_send(window, self.selector_pack.selector("send"))
        )
        return None

    async def _verify(self, prepared: QQPreparedSend) -> bool:
        target = await self._current_target(prepared.evidence)
        if target.error is not None:
            return False
        window, conversation = target.window, target.conversation
        assert window is not None and conversation is not None
        bubbles = await _await(
            self.accessibility.list_bubbles(
                window, self.selector_pack.selector("bubbles")
            )
        )
        return any(
            bubble.conversation_internal_id == conversation.internal_id
            and bubble.direction is BubbleDirection.OUTBOUND
            and bubble.text_hash == prepared.evidence.text_hash
            and bubble.observed_at > prepared.evidence.bound_at
            for bubble in bubbles
        )

    async def _current_target(self, evidence: QQPreparedEvidence) -> _TargetOutcome:
        try:
            window = await self._bind_window()
        except LookupError:
            return _TargetOutcome(error=ErrorCode.ADAPTER_QUARANTINED)
        if (window.process_id, window.window_handle) != (
            evidence.process_id,
            evidence.window_handle,
        ):
            return _TargetOutcome(error=ErrorCode.ADAPTER_QUARANTINED)
        digest = await _await(self.accessibility.tree_digest(window))
        if digest != evidence.tree_digest:
            return _TargetOutcome(error=ErrorCode.ADAPTER_QUARANTINED)
        conversations = await _await(
            self.accessibility.list_conversations(
                window, self.selector_pack.selector("conversations")
            )
        )
        matches = [
            item
            for item in conversations
            if item.internal_id == evidence.conversation_internal_id
            and item.participant_signature == evidence.participant_signature
            and item.last_message_key == evidence.last_message_key
            and item.tree_digest == evidence.tree_digest
        ]
        if len(matches) != 1:
            return _TargetOutcome(error=ErrorCode.STALE_CONTEXT)
        return _TargetOutcome(window=window, conversation=matches[0])

    async def _bind_window(self) -> QQWindow:
        windows = await _await(
            self.accessibility.find_main_windows(
                self.selector_pack.selector("main_window")
            )
        )
        fingerprint = await self.guard.fingerprinter.capture(Platform.QQ)
        matches = [
            window
            for window in windows
            if window.process_id == fingerprint.process_id
            and window.window_handle == fingerprint.window_handle
        ]
        if len(matches) != 1:
            raise LookupError("QQ WindowBinder found zero or multiple exact windows")
        return matches[0]

    @staticmethod
    def _safe_code(result: GuardedResult) -> ErrorCode:
        if result.error_code is ErrorCode.IDENTITY_AMBIGUOUS:
            return ErrorCode.IDENTITY_AMBIGUOUS
        if result.error_code is ErrorCode.FOREGROUND_REQUIRED:
            return ErrorCode.FOREGROUND_REQUIRED
        if result.error_code is ErrorCode.ADAPTER_QUARANTINED:
            return ErrorCode.ADAPTER_QUARANTINED
        return ErrorCode.FAILED_SAFE

    async def _quarantine_if_needed(self, code: ErrorCode) -> None:
        if code is ErrorCode.ADAPTER_QUARANTINED:
            await self.guard.quarantine(
                Platform.QQ, "QQ control tree changed or vanished"
            )

    @staticmethod
    def _fail(operation: SendOperation, code: ErrorCode) -> SendOperation:
        if operation.status in {SendStatus.PENDING, SendStatus.PREPARED}:
            SendStateMachine.transition(operation, SendStatus.FAILED, code.value)
        return operation

    @staticmethod
    def _uncertain(operation: SendOperation) -> SendOperation:
        if operation.status in {SendStatus.PREPARED, SendStatus.COMMITTED}:
            SendStateMachine.transition(
                operation, SendStatus.UNCERTAIN, ErrorCode.SEND_UNCERTAIN.value
            )
        return operation
