from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from messenger_ai.domain import ErrorCode, Platform
from messenger_ai.execution_guard import (
    ActionPhase,
    CancellationToken,
    ExecutionGuard,
    GuardedAction,
    GuardedActionType,
    GuardedResult,
    GuardErrorCode,
    GuardResultStatus,
)

from .models import (
    AuthorizedWechatSend,
    CapturedSendEvidence,
    OutboundBubble,
    PreparedSend,
    RawSendReceipt,
    SendResult,
    SendResultStatus,
    TargetRef,
    make_prepared_send,
)
from .ports import Clock, CurrentPolicyVersionPort, WechatSendDriver


class _OperationState(StrEnum):
    PREPARED = "prepared"
    COMMITTING = "committing"
    COMMITTED = "committed"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    TERMINAL = "terminal"


@dataclass
class _TrackedOperation:
    state: _OperationState
    receipt: RawSendReceipt | None = None
    terminal_error: ErrorCode | None = None


@dataclass(frozen=True)
class _PreparedDriverValue:
    evidence: CapturedSendEvidence


@dataclass(frozen=True)
class _CommitDriverValue:
    evidence: CapturedSendEvidence
    commit_token: str
    commit_started_at: datetime
    commit_returned_at: datetime


@dataclass(frozen=True)
class _VerifyDriverValue:
    evidence: CapturedSendEvidence
    bubbles: tuple[OutboundBubble, ...]


class _UnavailableCurrentPolicyVersions:
    async def current_versions(self, conversation_id: str):
        return None


class WechatBackgroundSendAdapter:
    """Fail-closed D0 WeChat sender with prepare/commit/verify evidence barriers."""

    def __init__(
        self,
        *,
        guard: ExecutionGuard,
        driver: WechatSendDriver,
        clock: Clock,
        current_policy_versions: CurrentPolicyVersionPort | None = None,
        capability_version: str,
        action_timeout_seconds: float = 3.0,
    ) -> None:
        if not capability_version:
            raise ValueError("capability_version is required")
        if not 0 < action_timeout_seconds <= 30:
            raise ValueError("action timeout must be in (0, 30]")
        self._guard = guard
        self._driver = driver
        self._clock = clock
        self._current_policy_versions = (
            current_policy_versions or _UnavailableCurrentPolicyVersions()
        )
        self._capability_version = capability_version
        self._action_timeout = action_timeout_seconds
        self._state_lock = asyncio.Lock()
        self._operations: dict[str, _TrackedOperation] = {}
        self._idempotency: dict[str, str] = {}

        # Validate the entire declared boundary at construction. It may not contain a
        # hidden second route: ActionInterceptor rejects any fallback independently.
        route = self._driver.route
        self._guard.interceptor.validate(
            tuple(
                dict.fromkeys(
                    route.prepare_operations
                    + route.commit_operations
                    + route.verify_operations
                )
            )
        )

    async def prepare_send(self, request: AuthorizedWechatSend) -> SendResult:
        now = self._clock.now()
        if request.command.expires_at <= now:
            return self._rejected(ErrorCode.STALE_CONTEXT, "authorization expired")
        if request.observation.frame_expires_at <= now:
            return self._rejected(ErrorCode.STALE_CONTEXT, "observation frame expired")
        if self._driver.route.requires_foreground:
            return self._rejected(
                ErrorCode.FOREGROUND_REQUIRED,
                "the declared driver route requires a foreground window",
            )

        reservation = f"preparing:{uuid4()}"
        async with self._state_lock:
            existing = self._idempotency.get(request.command.idempotency_key)
            if existing is not None:
                return self._rejected(
                    ErrorCode.DUPLICATE,
                    f"idempotency key is already reserved by {existing}",
                )
            self._idempotency[request.command.idempotency_key] = reservation

        target = request.observation.target
        guarded = await self._guard.run(
            self._action(
                action_type=GuardedActionType.COMPOSE,
                phase=ActionPhase.PREPARE,
                operations=self._driver.route.prepare_operations,
                environment_fingerprint=request.observation.environment_fingerprint,
                process_id=request.observation.process_id,
                window_handle=request.observation.window_handle,
            ),
            lambda token: self._prepare_action(request, target, token),
        )
        if not guarded.succeeded:
            await self._release_prepare_reservation(
                request.command.idempotency_key, reservation
            )
            return self._from_guard(guarded)
        value = guarded.value
        if isinstance(value, SendResult):
            await self._release_prepare_reservation(
                request.command.idempotency_key, reservation
            )
            return value
        if not isinstance(value, _PreparedDriverValue):
            await self._release_prepare_reservation(
                request.command.idempotency_key, reservation
            )
            return self._rejected(
                ErrorCode.FAILED_SAFE, "invalid prepare driver result"
            )

        evidence = value.evidence
        expires_at = min(
            request.command.expires_at,
            request.observation.frame_expires_at,
            evidence.frame_expires_at,
        )
        if expires_at <= self._clock.now():
            await self._release_prepare_reservation(
                request.command.idempotency_key, reservation
            )
            return self._rejected(
                ErrorCode.STALE_CONTEXT, "prepared evidence expired during composition"
            )
        prepared = make_prepared_send(
            idempotency_key=request.command.idempotency_key,
            draft_id=request.command.draft_id,
            authorization_id=request.command.authorization_id,
            conversation_id=evidence.conversation_id,
            conversation_session_id=evidence.conversation_session_id,
            identity_digest=evidence.identity_digest,
            expected_last_message_key=evidence.last_inbound_message_key,
            body_text=request.body_text,
            body_hash=request.command.text_hash,
            policy_version=request.command.policy_version,
            rule_version=request.rule_version,
            capability_version=self._capability_version,
            client_version=evidence.client_version,
            environment_fingerprint=evidence.environment_fingerprint,
            process_id=evidence.process_id,
            window_handle=evidence.window_handle,
            dpi_scale=evidence.dpi_scale,
            prepared_frame_hash=evidence.frame_hash,
            prepared_at=self._clock.now(),
            expires_at=expires_at,
            baseline_outbound_sequence=evidence.outbound_sequence,
            route=self._driver.route,
        )
        operation_key = str(prepared.operation_id)
        async with self._state_lock:
            existing = self._idempotency.get(prepared.idempotency_key)
            if existing != reservation:
                return self._rejected(
                    ErrorCode.DUPLICATE,
                    f"idempotency key is already bound to operation {existing}",
                )
            self._idempotency[prepared.idempotency_key] = operation_key
            self._operations[operation_key] = _TrackedOperation(
                _OperationState.PREPARED
            )
        return SendResult(status=SendResultStatus.PREPARED, prepared=prepared)

    async def commit_send(self, prepared: PreparedSend) -> SendResult:
        operation_key = str(prepared.operation_id)
        if not prepared.seal_is_valid():
            return self._rejected(ErrorCode.STALE_CONTEXT, "PreparedSend seal mismatch")
        if prepared.expires_at <= self._clock.now():
            await self._terminal(operation_key, ErrorCode.STALE_CONTEXT)
            return self._rejected(ErrorCode.STALE_CONTEXT, "PreparedSend expired")
        if prepared.route != self._driver.route:
            await self._terminal(operation_key, ErrorCode.STALE_CONTEXT)
            return self._rejected(ErrorCode.STALE_CONTEXT, "driver route changed")

        async with self._state_lock:
            tracked = self._operations.get(operation_key)
            if tracked is None:
                return self._rejected(
                    ErrorCode.INVALID_STATE, "PreparedSend is not owned by this adapter"
                )
            if tracked.state is not _OperationState.PREPARED:
                code = tracked.terminal_error or ErrorCode.DUPLICATE
                return self._rejected(
                    code, "commit is single-attempt and already consumed"
                )
            tracked.state = _OperationState.COMMITTING

        evidence = self._evidence_from_prepared(prepared)
        guarded = await self._guard.run(
            self._action(
                action_type=GuardedActionType.SEND,
                phase=ActionPhase.COMMIT,
                operations=prepared.route.commit_operations,
                environment_fingerprint=evidence.environment_fingerprint,
                process_id=evidence.process_id,
                window_handle=evidence.window_handle,
            ),
            lambda token: self._commit_action(prepared, token),
        )
        if not guarded.succeeded:
            if guarded.status in {
                GuardResultStatus.REJECTED,
                GuardResultStatus.QUARANTINED,
            }:
                code = self._guard_error(guarded, commit_started=False)
                await self._terminal(operation_key, code)
                return self._rejected(code, guarded.reason)
            # Once a commit action may have started, cancellation, timeout, dependency
            # failure, or desktop contention cannot prove the side effect did not occur.
            await self._terminal(operation_key, ErrorCode.SEND_UNCERTAIN)
            return self._rejected(
                ErrorCode.SEND_UNCERTAIN,
                f"commit outcome cannot be proven: {guarded.reason}",
            )
        value = guarded.value
        if isinstance(value, SendResult):
            code = value.error_code or ErrorCode.SEND_UNCERTAIN
            await self._terminal(operation_key, ErrorCode(code))
            return value
        if not isinstance(value, _CommitDriverValue):
            await self._terminal(operation_key, ErrorCode.SEND_UNCERTAIN)
            return self._rejected(
                ErrorCode.SEND_UNCERTAIN, "invalid commit driver result"
            )

        receipt = RawSendReceipt(
            operation_id=prepared.operation_id,
            idempotency_key=prepared.idempotency_key,
            conversation_id=prepared.conversation_id,
            conversation_session_id=prepared.conversation_session_id,
            identity_digest=prepared.identity_digest,
            body_hash=prepared.body_hash,
            process_id=prepared.process_id,
            window_handle=prepared.window_handle,
            environment_fingerprint=prepared.environment_fingerprint,
            commit_started_at=value.commit_started_at,
            commit_returned_at=value.commit_returned_at,
            baseline_outbound_sequence=prepared.baseline_outbound_sequence,
            driver_commit_token=value.commit_token,
        )
        async with self._state_lock:
            tracked = self._operations[operation_key]
            tracked.state = _OperationState.COMMITTED
            tracked.receipt = receipt
        return SendResult(
            status=SendResultStatus.COMMITTED_PENDING_VERIFY,
            receipt=receipt,
        )

    async def verify_send(self, receipt: RawSendReceipt) -> SendResult:
        operation_key = str(receipt.operation_id)
        async with self._state_lock:
            tracked = self._operations.get(operation_key)
            if tracked is None or tracked.receipt != receipt:
                return self._rejected(
                    ErrorCode.INVALID_STATE, "receipt is not owned by this adapter"
                )
            if tracked.state is _OperationState.VERIFIED:
                return self._rejected(ErrorCode.DUPLICATE, "receipt already verified")
            if tracked.state is not _OperationState.COMMITTED:
                return self._rejected(
                    tracked.terminal_error or ErrorCode.INVALID_STATE,
                    "operation is not awaiting verification",
                )
            tracked.state = _OperationState.VERIFYING

        guarded = await self._guard.run(
            self._action(
                action_type=GuardedActionType.VERIFY,
                phase=ActionPhase.VERIFY,
                operations=self._driver.route.verify_operations,
                environment_fingerprint=receipt.environment_fingerprint,
                process_id=receipt.process_id,
                window_handle=receipt.window_handle,
            ),
            lambda token: self._verify_action(receipt, token),
        )
        if not guarded.succeeded or not isinstance(guarded.value, _VerifyDriverValue):
            await self._terminal(operation_key, ErrorCode.SEND_UNCERTAIN)
            return self._rejected(
                ErrorCode.SEND_UNCERTAIN,
                "post-commit verification could not be completed",
            )
        value = guarded.value
        if not self._same_receipt_target(receipt, value.evidence):
            await self._terminal(operation_key, ErrorCode.SEND_UNCERTAIN)
            return self._rejected(
                ErrorCode.SEND_UNCERTAIN,
                "verification capture no longer proves the committed target",
            )

        matching = tuple(
            bubble
            for bubble in value.bubbles
            if self._is_verified_match(receipt, bubble)
        )
        if len(matching) != 1:
            await self._terminal(operation_key, ErrorCode.SEND_UNCERTAIN)
            return self._rejected(
                ErrorCode.SEND_UNCERTAIN,
                "exactly one new matching outbound bubble was not proven",
            )
        async with self._state_lock:
            self._operations[operation_key].state = _OperationState.VERIFIED
        return SendResult(
            status=SendResultStatus.SENT_VERIFIED,
            receipt=receipt,
            verified_bubble_sequence=matching[0].sequence,
        )

    async def _prepare_action(
        self,
        request: AuthorizedWechatSend,
        target: TargetRef,
        token: CancellationToken,
    ) -> _PreparedDriverValue | SendResult:
        token.raise_if_cancelled()
        selected = await self._invoke(
            self._driver.route.select_operation,
            self._driver.select_target,
            target,
            token,
        )
        if not selected:
            return self._rejected(
                ErrorCode.IDENTITY_AMBIGUOUS, "target could not be selected uniquely"
            )
        before = await self._capture(target, token)
        mismatch = self._strict_mismatch(request.observation, before)
        if mismatch:
            return self._rejected(ErrorCode.STALE_CONTEXT, mismatch)
        token.raise_if_cancelled()
        await self._invoke(
            self._driver.route.compose_operation,
            self._driver.compose_text,
            target,
            request.body_text,
            token,
        )
        composer_hash = await self._invoke(
            self._driver.route.composer_read_operation,
            self._driver.read_composer_hash,
            target,
            token,
        )
        if composer_hash != request.command.text_hash:
            return self._rejected(
                ErrorCode.FAILED_SAFE, "composer hash does not match authorized body"
            )
        after = await self._capture(target, token)
        mismatch = self._stable_target_mismatch(request.observation, after)
        if mismatch:
            return self._rejected(ErrorCode.STALE_CONTEXT, mismatch)
        return _PreparedDriverValue(after)

    async def _commit_action(
        self, prepared: PreparedSend, token: CancellationToken
    ) -> _CommitDriverValue | SendResult:
        target = TargetRef(
            conversation_id=prepared.conversation_id,
            conversation_session_id=prepared.conversation_session_id,
            identity_digest=prepared.identity_digest,
        )
        token.raise_if_cancelled()
        try:
            versions = await self._current_policy_versions.current_versions(
                prepared.conversation_id
            )
        except Exception:  # noqa: BLE001 - version authority must fail closed
            return self._rejected(
                ErrorCode.STALE_CONTEXT,
                "current policy versions could not be read",
            )
        if versions is None:
            return self._rejected(
                ErrorCode.STALE_CONTEXT,
                "current policy versions are unavailable",
            )
        if (
            versions.rule_version != prepared.rule_version
            or versions.policy_version != prepared.policy_version
        ):
            return self._rejected(
                ErrorCode.STALE_CONTEXT,
                "rule or policy version changed after preparation",
            )
        token.raise_if_cancelled()
        fresh = await self._capture(target, token)
        if fresh.frame_expires_at <= self._clock.now():
            return self._rejected(
                ErrorCode.STALE_CONTEXT, "commit recapture is already expired"
            )
        mismatch = self._prepared_mismatch(prepared, fresh)
        if mismatch:
            return self._rejected(ErrorCode.STALE_CONTEXT, mismatch)
        token.raise_if_cancelled()
        started = self._clock.now()
        commit_token = await self._invoke(
            prepared.route.commit_operation, self._driver.commit, target, token
        )
        returned = self._clock.now()
        if not commit_token:
            return self._rejected(
                ErrorCode.SEND_UNCERTAIN, "driver did not return a commit marker"
            )
        return _CommitDriverValue(fresh, str(commit_token), started, returned)

    async def _verify_action(
        self, receipt: RawSendReceipt, token: CancellationToken
    ) -> _VerifyDriverValue:
        target = TargetRef(
            conversation_id=receipt.conversation_id,
            conversation_session_id=receipt.conversation_session_id,
            identity_digest=receipt.identity_digest,
        )
        token.raise_if_cancelled()
        fresh = await self._capture(target, token)
        bubbles = await self._invoke(
            self._driver.route.verify_operation,
            self._driver.read_outbound_since,
            target,
            receipt.baseline_outbound_sequence,
            token,
        )
        return _VerifyDriverValue(fresh, tuple(bubbles))

    async def _capture(
        self, target: TargetRef, token: CancellationToken
    ) -> CapturedSendEvidence:
        return await self._invoke(
            self._driver.route.capture_operation, self._driver.capture, target, token
        )

    async def _invoke(self, operation: str, call, *args):
        return await self._guard.interceptor.invoke(operation, call, *args)

    def _action(
        self,
        *,
        action_type: GuardedActionType,
        phase: ActionPhase,
        operations: tuple[str, ...],
        environment_fingerprint: str,
        process_id: int,
        window_handle: int,
    ) -> GuardedAction:
        return GuardedAction(
            platform=Platform.WECHAT,
            action_type=action_type,
            phase=phase,
            requested_operations=operations,
            fallback_operations=(),
            capability_version=self._capability_version,
            environment_fingerprint=environment_fingerprint,
            target_process_id=process_id,
            target_window_handle=window_handle,
            timeout_seconds=self._action_timeout,
        )

    @staticmethod
    def _strict_mismatch(
        expected: CapturedSendEvidence, actual: CapturedSendEvidence
    ) -> str:
        stable = WechatBackgroundSendAdapter._stable_target_mismatch(expected, actual)
        if stable:
            return stable
        if expected.frame_hash != actual.frame_hash:
            return "visual frame hash changed"
        return ""

    @staticmethod
    def _stable_target_mismatch(
        expected: CapturedSendEvidence, actual: CapturedSendEvidence
    ) -> str:
        fields = (
            "conversation_id",
            "conversation_session_id",
            "identity_digest",
            "last_inbound_message_key",
            "client_version",
            "environment_fingerprint",
            "process_id",
            "window_handle",
            "dpi_scale",
            "outbound_sequence",
        )
        for field in fields:
            if getattr(expected, field) != getattr(actual, field):
                return f"evidence changed: {field}"
        return ""

    @staticmethod
    def _prepared_mismatch(prepared: PreparedSend, actual: CapturedSendEvidence) -> str:
        pairs = (
            ("conversation_id", prepared.conversation_id),
            ("conversation_session_id", prepared.conversation_session_id),
            ("identity_digest", prepared.identity_digest),
            ("last_inbound_message_key", prepared.expected_last_message_key),
            ("client_version", prepared.client_version),
            ("environment_fingerprint", prepared.environment_fingerprint),
            ("process_id", prepared.process_id),
            ("window_handle", prepared.window_handle),
            ("dpi_scale", prepared.dpi_scale),
            ("frame_hash", prepared.prepared_frame_hash),
            ("outbound_sequence", prepared.baseline_outbound_sequence),
        )
        for field, expected in pairs:
            if getattr(actual, field) != expected:
                return f"PreparedSend evidence changed: {field}"
        return ""

    @staticmethod
    def _same_receipt_target(
        receipt: RawSendReceipt, actual: CapturedSendEvidence
    ) -> bool:
        return (
            actual.conversation_id == receipt.conversation_id
            and actual.conversation_session_id == receipt.conversation_session_id
            and actual.identity_digest == receipt.identity_digest
            and actual.environment_fingerprint == receipt.environment_fingerprint
            and actual.process_id == receipt.process_id
            and actual.window_handle == receipt.window_handle
        )

    @staticmethod
    def _is_verified_match(receipt: RawSendReceipt, bubble: OutboundBubble) -> bool:
        return (
            bubble.conversation_id == receipt.conversation_id
            and bubble.conversation_session_id == receipt.conversation_session_id
            and bubble.identity_digest == receipt.identity_digest
            and bubble.direction == "outbound"
            and bubble.body_hash == receipt.body_hash
            and bubble.sequence > receipt.baseline_outbound_sequence
            and bubble.observed_at > receipt.commit_started_at
        )

    def _evidence_from_prepared(self, prepared: PreparedSend) -> CapturedSendEvidence:
        return CapturedSendEvidence(
            conversation_id=prepared.conversation_id,
            conversation_session_id=prepared.conversation_session_id,
            identity_digest=prepared.identity_digest,
            last_inbound_message_key=prepared.expected_last_message_key,
            frame_hash=prepared.prepared_frame_hash,
            frame_captured_at=prepared.prepared_at,
            frame_expires_at=prepared.expires_at,
            client_version=prepared.client_version,
            environment_fingerprint=prepared.environment_fingerprint,
            process_id=prepared.process_id,
            window_handle=prepared.window_handle,
            dpi_scale=prepared.dpi_scale,
            outbound_sequence=prepared.baseline_outbound_sequence,
        )

    async def _terminal(self, operation_key: str, code: ErrorCode) -> None:
        async with self._state_lock:
            tracked = self._operations.get(operation_key)
            if tracked is not None:
                tracked.state = _OperationState.TERMINAL
                tracked.terminal_error = code

    async def _release_prepare_reservation(
        self, idempotency_key: str, reservation: str
    ) -> None:
        async with self._state_lock:
            if self._idempotency.get(idempotency_key) == reservation:
                self._idempotency.pop(idempotency_key, None)

    @staticmethod
    def _rejected(code, reason: str) -> SendResult:
        return SendResult(
            status=SendResultStatus.REJECTED,
            error_code=code,
            reason=reason,
            automatic_retry_allowed=False,
        )

    def _from_guard(self, result: GuardedResult) -> SendResult:
        code = self._guard_error(result, commit_started=False)
        return self._rejected(code, result.reason)

    @staticmethod
    def _guard_error(result: GuardedResult, *, commit_started: bool) -> ErrorCode:
        if commit_started:
            return ErrorCode.SEND_UNCERTAIN
        if isinstance(result.error_code, ErrorCode):
            return result.error_code
        if result.error_code in {
            GuardErrorCode.CAPABILITY_VERSION_MISMATCH,
            GuardErrorCode.ENVIRONMENT_FINGERPRINT_MISMATCH,
            GuardErrorCode.TARGET_MISMATCH,
        }:
            return ErrorCode.STALE_CONTEXT
        if result.error_code is GuardErrorCode.ACTION_NOT_ALLOWED:
            return ErrorCode.CAPABILITY_UNSUPPORTED
        return ErrorCode.FAILED_SAFE
