"""Durable M1 implementation.

The hub is deliberately synchronous at its storage boundary.  SQLite writes
are short, serialised transactions; callers use the async coordinator to gain
per-conversation ordering without ever holding a database transaction across a
model or adapter await.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from messenger_ai.domain import (
    Authorization,
    AuthorizationType,
    AuthorizedSendCommand,
    Draft,
    DraftStatus,
    ErrorCode,
    EventEnvelope,
    InboundMessage,
    PacingPlan,
    PlanStatus,
    SendOperation,
    SendStatus,
)
from messenger_ai.domain.clock import ClockPort, SystemClock
from messenger_ai.domain.errors import DomainError

from .schema import DDL, SCHEMA_VERSION

ACTIVE_DRAFT_STATUSES = (DraftStatus.CREATED.value, DraftStatus.AUTHORIZED.value)
ACTIVE_PLAN_STATUSES = (
    PlanStatus.PLANNED.value,
    PlanStatus.WAITING.value,
    PlanStatus.AUTHORIZED.value,
)

# A driver may only move an operation along the transition its phase owns.
# Anything else at the Hub boundary is treated as a fail-closed outcome so a
# buggy or adversarial adapter cannot fabricate a committed/verified send.
_PHASE_RESULT_STATUSES: dict[str, frozenset[SendStatus]] = {
    "prepare": frozenset(
        {
            SendStatus.PREPARED,
            SendStatus.FAILED,
            SendStatus.CANCELLED,
            SendStatus.UNCERTAIN,
        }
    ),
    "commit": frozenset(
        {
            SendStatus.COMMITTED,
            SendStatus.UNCERTAIN,
        }
    ),
    "verify": frozenset(
        {
            SendStatus.VERIFIED,
            SendStatus.UNCERTAIN,
        }
    ),
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("all Hub timestamps must be timezone-aware UTC")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    duplicate: bool
    conversation_id: str
    invalidated_draft_ids: tuple[str, ...] = ()
    invalidated_plan_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutboxItem:
    outbox_id: str
    event_type: str
    aggregate_id: str
    payload: Mapping[str, Any]
    attempt_count: int


class SQLiteHubStore:
    """SQLite WAL event store with explicit, non-nestable short UoWs."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # A single guarded connection works for :memory: too.  File-backed
        # deployments still get WAL for crash-safe readers and writers.
        self.connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            if self.path != ":memory:":
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.executescript(DDL)
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _stamp(datetime.now(UTC))),
            )

    @contextmanager
    def uow(self) -> Iterator[sqlite3.Connection]:
        """A transaction for local state only; never await inside this block."""
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.connection.close()


class ConversationCoordinator:
    """A mailbox lock per stable conversation ID, allowing other contacts through."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def lock_for(self, conversation_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(conversation_id, asyncio.Lock())

    @contextmanager
    def _unused(self) -> Iterator[None]:  # keeps this class intentionally small
        yield

    async def run(self, conversation_id: str, callback: Any) -> Any:
        lock = await self.lock_for(conversation_id)
        async with lock:
            return callback()


class HubService:
    """Application service shared by adapters, WebUI and MCP.

    No method in this class performs model calls. ``run_send`` is the one
    exception that invokes an adapter, and it intentionally commits intent in
    one transaction then calls the adapter outside every transaction.
    """

    def __init__(
        self,
        store: SQLiteHubStore,
        *,
        clock: ClockPort | None = None,
        quiet_window_seconds: float = 6,
    ) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self.quiet_window_seconds = quiet_window_seconds
        self.coordinator = ConversationCoordinator()

    def now(self) -> datetime:
        return _utc(self.clock.now())

    def _outbox(
        self,
        conn: sqlite3.Connection,
        *,
        dedupe_key: str,
        event_type: str,
        aggregate_id: str,
        payload: Any,
        available_at: datetime | None = None,
    ) -> None:
        now = self.now()
        conn.execute(
            """INSERT OR IGNORE INTO outbox(outbox_id,dedupe_key,event_type,aggregate_id,payload_json,available_at,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                str(uuid4()),
                dedupe_key,
                event_type,
                aggregate_id,
                _json(payload),
                _stamp(available_at or now),
                _stamp(now),
            ),
        )

    def _audit(
        self, conn: sqlite3.Connection, action: str, aggregate_id: str, payload: Any
    ) -> None:
        conn.execute(
            "INSERT INTO audit_entries VALUES(?,?,?,?,?)",
            (str(uuid4()), action, aggregate_id, _json(payload), _stamp(self.now())),
        )

    def ingest(
        self, message: InboundMessage, *, envelope: EventEnvelope[Any] | None = None
    ) -> IngestResult:
        """Persist an inbound event and invalidate stale automatic work atomically."""
        now = self.now()
        observed = _utc(message.observed_at)
        event_id = str(envelope.event_id if envelope else message.event_id)
        with self.store.uow() as conn:
            existing = conn.execute(
                "SELECT 1 FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            by_key = conn.execute(
                "SELECT conversation_id FROM messages WHERE account_id=? AND platform_message_key=?",
                (message.account_id, message.platform_message_key),
            ).fetchone()
            if existing or by_key:
                return IngestResult(
                    False,
                    True,
                    by_key["conversation_id"] if by_key else message.conversation_id,
                )

            if envelope is None:
                envelope = EventEnvelope(
                    event_id=message.event_id,
                    event_type="message.observed",
                    occurred_at=observed,
                    observed_at=observed,
                    aggregate_type="conversation",
                    aggregate_id=message.conversation_id,
                    payload=message.model_dump(mode="json"),
                    producer="hub.ingress",
                )
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(envelope.event_id),
                    envelope.event_type,
                    _stamp(envelope.occurred_at),
                    _stamp(envelope.observed_at),
                    envelope.aggregate_type,
                    envelope.aggregate_id,
                    str(envelope.causation_id) if envelope.causation_id else None,
                    str(envelope.correlation_id),
                    envelope.schema_version,
                    _json(envelope.payload),
                    envelope.producer,
                    envelope.trace_id,
                    _stamp(now),
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO accounts(account_id,platform,created_at) VALUES(?,?,?)",
                (message.account_id, message.platform.value, _stamp(now)),
            )
            conn.execute(
                "INSERT OR IGNORE INTO contacts(contact_id,created_at) VALUES(?,?)",
                (message.contact_id, _stamp(now)),
            )
            stable_after = observed.timestamp() + self.quiet_window_seconds
            stable_at = datetime.fromtimestamp(stable_after, UTC)
            conn.execute(
                """INSERT INTO conversations(conversation_id,account_id,contact_id,last_message_key,last_inbound_at,stable_after,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(conversation_id) DO UPDATE SET last_message_key=excluded.last_message_key,
                     last_inbound_at=excluded.last_inbound_at,stable_after=excluded.stable_after,
                     version=conversations.version+1,updated_at=excluded.updated_at""",
                (
                    message.conversation_id,
                    message.account_id,
                    message.contact_id,
                    message.platform_message_key,
                    _stamp(observed),
                    _stamp(stable_at),
                    _stamp(now),
                    _stamp(now),
                ),
            )
            conn.execute(
                """INSERT INTO messages(message_id,event_id,platform,account_id,conversation_id,contact_id,platform_message_key,observed_at,text,content_type,payload_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid4()),
                    event_id,
                    message.platform.value,
                    message.account_id,
                    message.conversation_id,
                    message.contact_id,
                    message.platform_message_key,
                    _stamp(observed),
                    message.text,
                    message.content_type.value,
                    _json(message),
                    _stamp(now),
                ),
            )
            draft_ids = tuple(
                row[0]
                for row in conn.execute(
                    "SELECT draft_id FROM drafts WHERE conversation_id=? AND status IN (?,?)",
                    (message.conversation_id, *ACTIVE_DRAFT_STATUSES),
                )
            )
            plan_ids = tuple(
                row[0]
                for row in conn.execute(
                    "SELECT pacing_plan_id FROM pacing_plans WHERE conversation_id=? AND status IN (?,?,?)",
                    (message.conversation_id, *ACTIVE_PLAN_STATUSES),
                )
            )
            if draft_ids:
                conn.execute(
                    "UPDATE drafts SET status=?,version=version+1,updated_at=? WHERE conversation_id=? AND status IN (?,?)",
                    (
                        DraftStatus.EXPIRED.value,
                        _stamp(now),
                        message.conversation_id,
                        *ACTIVE_DRAFT_STATUSES,
                    ),
                )
            if plan_ids:
                conn.execute(
                    "UPDATE pacing_plans SET status=?,version=version+1,updated_at=? WHERE conversation_id=? AND status IN (?,?,?)",
                    (
                        PlanStatus.CANCELLED.value,
                        _stamp(now),
                        message.conversation_id,
                        *ACTIVE_PLAN_STATUSES,
                    ),
                )
            # A previous quiet-window notification is now stale.  Cancelling it
            # in the same commit as the message prevents a burst from yielding
            # N planner jobs / N competing drafts after a restart.
            conn.execute(
                "UPDATE outbox SET status='cancelled',version=version+1 WHERE aggregate_id=? AND event_type='conversation.stable_window' AND status='pending'",
                (message.conversation_id,),
            )
            self._outbox(
                conn,
                dedupe_key=f"stable:{message.conversation_id}:{message.platform_message_key}",
                event_type="conversation.stable_window",
                aggregate_id=message.conversation_id,
                payload={
                    "conversation_id": message.conversation_id,
                    "last_message_key": message.platform_message_key,
                },
                available_at=stable_at,
            )
            self._audit(
                conn,
                "message.ingested",
                message.conversation_id,
                {
                    "event_id": event_id,
                    "invalidated_drafts": draft_ids,
                    "invalidated_plans": plan_ids,
                },
            )
        return IngestResult(True, False, message.conversation_id, draft_ids, plan_ids)

    async def ingest_async(
        self, message: InboundMessage, *, envelope: EventEnvelope[Any] | None = None
    ) -> IngestResult:
        return await self.coordinator.run(
            message.conversation_id, lambda: self.ingest(message, envelope=envelope)
        )

    def record_consumer(self, consumer_name: str, event_id: UUID | str) -> bool:
        with self.store.uow() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO consumer_offsets VALUES(?,?,?)",
                (consumer_name, str(event_id), _stamp(self.now())),
            )
            return cursor.rowcount == 1

    def create_draft(self, draft: Draft) -> Draft:
        """Persist an immutable candidate; supersedes one older active candidate."""
        now = self.now()
        with self.store.uow() as conn:
            conversation = conn.execute(
                "SELECT contact_id FROM conversations WHERE conversation_id=?",
                (draft.conversation_id,),
            ).fetchone()
            if conversation is None or conversation["contact_id"] != draft.contact_id:
                raise DomainError(
                    ErrorCode.IDENTITY_AMBIGUOUS,
                    "draft does not match a known conversation/contact",
                )
            existing = conn.execute(
                "SELECT draft_id FROM drafts WHERE draft_id=?", (str(draft.draft_id),)
            ).fetchone()
            if existing:
                return draft
            conn.execute(
                "UPDATE drafts SET status=?,version=version+1,updated_at=? WHERE conversation_id=? AND status IN (?,?)",
                (
                    DraftStatus.EXPIRED.value,
                    _stamp(now),
                    draft.conversation_id,
                    *ACTIVE_DRAFT_STATUSES,
                ),
            )
            conn.execute(
                "INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(draft.draft_id),
                    draft.conversation_id,
                    draft.contact_id,
                    draft.text,
                    _json(list(draft.source_message_keys)),
                    draft.rule_version,
                    draft.text_hash,
                    draft.status.value,
                    1,
                    _stamp(now),
                    _stamp(now),
                ),
            )
            self._outbox(
                conn,
                dedupe_key=f"draft:{draft.draft_id}:created",
                event_type="draft.created",
                aggregate_id=str(draft.draft_id),
                payload=draft,
            )
            self._audit(
                conn,
                "draft.created",
                draft.conversation_id,
                {"draft_id": str(draft.draft_id)},
            )
        return draft

    def schedule_plan(self, plan: PacingPlan) -> PacingPlan:
        now = self.now()
        with self.store.uow() as conn:
            conn.execute(
                "UPDATE pacing_plans SET status=?,version=version+1,updated_at=? WHERE conversation_id=? AND status IN (?,?,?)",
                (
                    PlanStatus.CANCELLED.value,
                    _stamp(now),
                    plan.conversation_id,
                    *ACTIVE_PLAN_STATUSES,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO pacing_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(plan.pacing_plan_id),
                    plan.conversation_id,
                    _json(list(plan.source_message_keys)),
                    _stamp(plan.quiet_until),
                    _stamp(plan.earliest_send_at),
                    _stamp(plan.expires_at),
                    plan.pacing_rule_version,
                    plan.status.value,
                    _json(plan),
                    1,
                    _stamp(now),
                    _stamp(now),
                ),
            )
            self._outbox(
                conn,
                dedupe_key=f"pacing:{plan.pacing_plan_id}",
                event_type="pacing.due",
                aggregate_id=plan.conversation_id,
                payload={"pacing_plan_id": str(plan.pacing_plan_id)},
                available_at=plan.earliest_send_at,
            )
        return plan

    def invalidate_for_rule_change(self, rule_version: str) -> int:
        now = self.now()
        with self.store.uow() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM pacing_plans WHERE status IN (?,?,?) AND pacing_rule_version<>?",
                (*ACTIVE_PLAN_STATUSES, rule_version),
            ).fetchone()
            conn.execute(
                "UPDATE pacing_plans SET status=?,version=version+1,updated_at=? WHERE status IN (?,?,?) AND pacing_rule_version<>?",
                (
                    PlanStatus.CANCELLED.value,
                    _stamp(now),
                    *ACTIVE_PLAN_STATUSES,
                    rule_version,
                ),
            )
            conn.execute(
                "UPDATE drafts SET status=?,version=version+1,updated_at=? WHERE status IN (?,?) AND rule_version<>?",
                (
                    DraftStatus.EXPIRED.value,
                    _stamp(now),
                    *ACTIVE_DRAFT_STATUSES,
                    rule_version,
                ),
            )
            return int(row[0])

    def invalidate_conversation(
        self, conversation_id: str, *, reason: str
    ) -> Mapping[str, tuple[str, ...]]:
        """Cancel unsent automation after user takeover, pause, or health change.

        Drafts are retained as expired audit evidence; this method never deletes
        text or tries to compensate for an already committed send.
        """
        now = self.now()
        with self.store.uow() as conn:
            drafts = tuple(
                row[0]
                for row in conn.execute(
                    "SELECT draft_id FROM drafts WHERE conversation_id=? AND status IN (?,?)",
                    (conversation_id, *ACTIVE_DRAFT_STATUSES),
                )
            )
            plans = tuple(
                row[0]
                for row in conn.execute(
                    "SELECT pacing_plan_id FROM pacing_plans WHERE conversation_id=? AND status IN (?,?,?)",
                    (conversation_id, *ACTIVE_PLAN_STATUSES),
                )
            )
            conn.execute(
                "UPDATE drafts SET status=?,version=version+1,updated_at=? WHERE conversation_id=? AND status IN (?,?)",
                (
                    DraftStatus.EXPIRED.value,
                    _stamp(now),
                    conversation_id,
                    *ACTIVE_DRAFT_STATUSES,
                ),
            )
            conn.execute(
                "UPDATE pacing_plans SET status=?,version=version+1,updated_at=? WHERE conversation_id=? AND status IN (?,?,?)",
                (
                    PlanStatus.CANCELLED.value,
                    _stamp(now),
                    conversation_id,
                    *ACTIVE_PLAN_STATUSES,
                ),
            )
            conn.execute(
                "UPDATE outbox SET status='cancelled',version=version+1 WHERE aggregate_id=? AND status='pending' AND event_type IN ('conversation.stable_window','pacing.due')",
                (conversation_id,),
            )
            self._audit(
                conn,
                f"automation.invalidated.{reason}",
                conversation_id,
                {"draft_ids": drafts, "plan_ids": plans},
            )
            return {"draft_ids": drafts, "plan_ids": plans}

    def record_directional_message(self, *, event_id: UUID, conversation_id: str,
                                   contact_id: str, platform_message_key: str,
                                   direction: str, text: str, observed_at: datetime,
                                   operation_id: UUID | None = None) -> bool:
        if direction not in {"human_outbound", "bot_outbound"}:
            raise ValueError("directional history accepts outbound directions only")
        with self.store.uow() as conn:
            return bool(conn.execute(
                """INSERT OR IGNORE INTO directional_history(
                   event_id,conversation_id,contact_id,platform_message_key,direction,
                   operation_id,text,observed_at) VALUES(?,?,?,?,?,?,?,?)""",
                (str(event_id), conversation_id, contact_id, platform_message_key, direction,
                 str(operation_id) if operation_id else None, text, _stamp(observed_at)),
            ).rowcount)

    def stable_window_is_current(
        self, conversation_id: str, last_message_key: str
    ) -> bool:
        """Planner consumers call this after claiming an outbox task.

        It closes the small race where an older job had already been claimed
        just before a new inbound message arrived.
        """
        row = self.store.connection.execute(
            "SELECT last_message_key,stable_after FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return bool(
            row
            and row["last_message_key"] == last_message_key
            and _parse(row["stable_after"]) <= self.now()
        )

    def claim_outbox(self, *, limit: int = 100) -> list[OutboxItem]:
        now = self.now()
        with self.store.uow() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE status='pending' AND available_at<=? ORDER BY available_at,outbox_id LIMIT ?",
                (_stamp(now), limit),
            ).fetchall()
            result: list[OutboxItem] = []
            for row in rows:
                changed = conn.execute(
                    "UPDATE outbox SET status='dispatching',attempt_count=attempt_count+1,claimed_at=?,version=version+1 WHERE outbox_id=? AND status='pending'",
                    (_stamp(now), row["outbox_id"]),
                ).rowcount
                if changed:
                    result.append(
                        OutboxItem(
                            row["outbox_id"],
                            row["event_type"],
                            row["aggregate_id"],
                            json.loads(row["payload_json"]),
                            row["attempt_count"] + 1,
                        )
                    )
            return result

    def complete_outbox(
        self, outbox_id: str, *, delivered: bool, retry_at: datetime | None = None
    ) -> None:
        now = self.now()
        with self.store.uow() as conn:
            if delivered:
                conn.execute(
                    "UPDATE outbox SET status='delivered',delivered_at=?,version=version+1 WHERE outbox_id=? AND status='dispatching'",
                    (_stamp(now), outbox_id),
                )
            else:
                conn.execute(
                    "UPDATE outbox SET status='pending',available_at=?,version=version+1 WHERE outbox_id=? AND status='dispatching'",
                    (_stamp(retry_at or now), outbox_id),
                )

    def create_send_operation(self, command: AuthorizedSendCommand) -> SendOperation:
        now = self.now()
        with self.store.uow() as conn:
            row = conn.execute(
                "SELECT operation_id,draft_id,status,error_code FROM send_operations WHERE idempotency_key=?",
                (command.idempotency_key,),
            ).fetchone()
            if row:
                return SendOperation(
                    operation_id=UUID(row["operation_id"]),
                    idempotency_key=command.idempotency_key,
                    draft_id=UUID(row["draft_id"]),
                    status=SendStatus(row["status"]),
                    error_code=row["error_code"],
                )
            draft = conn.execute(
                "SELECT * FROM drafts WHERE draft_id=?", (str(command.draft_id),)
            ).fetchone()
            current = conn.execute(
                "SELECT last_message_key FROM conversations WHERE conversation_id=?",
                (command.conversation_id,),
            ).fetchone()
            authorization = conn.execute(
                "SELECT * FROM authorizations WHERE authorization_id=?",
                (str(command.authorization_id),),
            ).fetchone()
            if (
                not draft
                or not current
                or not authorization
                or draft["status"] != DraftStatus.AUTHORIZED.value
                or draft["text_hash"] != command.text_hash
                or current["last_message_key"] != command.expected_last_message_key
            ):
                raise DomainError(
                    ErrorCode.STALE_CONTEXT, "draft, text, or conversation changed"
                )
            if (
                authorization["consumed"]
                or authorization["draft_id"] != str(command.draft_id)
                or authorization["conversation_id"] != command.conversation_id
                or authorization["idempotency_key"] != command.idempotency_key
                or authorization["text_hash"] != command.text_hash
                or authorization["expected_last_message_key"]
                != command.expected_last_message_key
            ):
                raise DomainError(
                    ErrorCode.STALE_CONTEXT,
                    "authorization is not valid for this command",
                )
            if _parse(_stamp(command.expires_at)) <= now:
                raise DomainError(ErrorCode.STALE_CONTEXT, "authorization expired")
            op = SendOperation(
                idempotency_key=command.idempotency_key, draft_id=command.draft_id
            )
            conn.execute(
                "INSERT INTO send_operations(operation_id,idempotency_key,draft_id,authorization_id,status,error_code,commit_intent,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    str(op.operation_id),
                    op.idempotency_key,
                    str(op.draft_id),
                    str(command.authorization_id),
                    op.status.value,
                    None,
                    0,
                    _stamp(now),
                    _stamp(now),
                ),
            )
            conn.execute(
                "UPDATE authorizations SET consumed=1,version=version+1 WHERE authorization_id=? AND consumed=0",
                (str(command.authorization_id),),
            )
            self._outbox(
                conn,
                dedupe_key=f"send:{op.operation_id}:requested",
                event_type="send.requested",
                aggregate_id=str(op.operation_id),
                payload={"operation_id": str(op.operation_id)},
            )
            return op

    def persist_authorization(self, authorization: Authorization) -> Authorization:
        with self.store.uow() as conn:
            existing = conn.execute(
                "SELECT authorization_id FROM authorizations WHERE authorization_id=?",
                (str(authorization.authorization_id),),
            ).fetchone()
            if existing:
                return authorization
            draft = conn.execute(
                "SELECT * FROM drafts WHERE draft_id=?", (str(authorization.draft_id),)
            ).fetchone()
            if (
                not draft
                or draft["conversation_id"] != authorization.conversation_id
                or draft["text_hash"] != authorization.text_hash
                or draft["status"] != DraftStatus.CREATED.value
            ):
                raise DomainError(
                    ErrorCode.STALE_CONTEXT,
                    "authorization does not freeze the current draft",
                )
            conn.execute(
                "UPDATE drafts SET status=?,version=version+1,updated_at=? WHERE draft_id=? AND status=?",
                (
                    DraftStatus.AUTHORIZED.value,
                    _stamp(self.now()),
                    str(authorization.draft_id),
                    DraftStatus.CREATED.value,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO authorizations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(authorization.authorization_id),
                    str(authorization.draft_id),
                    authorization.conversation_id,
                    authorization.expected_last_message_key,
                    authorization.text_hash,
                    authorization.idempotency_key,
                    authorization.authorization_type.value,
                    authorization.policy_version,
                    _stamp(authorization.expires_at),
                    int(authorization.consumed),
                    1,
                    _stamp(self.now()),
                ),
            )
        return authorization

    def _operation(self, operation_id: UUID | str) -> SendOperation:
        row = self.store.connection.execute(
            "SELECT * FROM send_operations WHERE operation_id=?", (str(operation_id),)
        ).fetchone()
        if not row:
            raise KeyError(f"unknown send operation {operation_id}")
        return SendOperation(
            operation_id=UUID(row["operation_id"]),
            idempotency_key=row["idempotency_key"],
            draft_id=UUID(row["draft_id"]),
            status=SendStatus(row["status"]),
            error_code=row["error_code"],
        )

    def _command_for_operation(self, operation_id: UUID | str) -> AuthorizedSendCommand:
        row = self.store.connection.execute(
            """SELECT a.* FROM send_operations s
               JOIN authorizations a ON a.authorization_id=s.authorization_id
               WHERE s.operation_id=?""",
            (str(operation_id),),
        ).fetchone()
        if row is None:
            raise DomainError(ErrorCode.STALE_CONTEXT, "send operation has no M9 authorization")
        return AuthorizedSendCommand(
            draft_id=UUID(row["draft_id"]),
            conversation_id=row["conversation_id"],
            expected_last_message_key=row["expected_last_message_key"],
            text_hash=row["text_hash"],
            idempotency_key=row["idempotency_key"],
            authorization_type=AuthorizationType(row["authorization_type"]),
            authorization_id=UUID(row["authorization_id"]),
            policy_version=row["policy_version"],
            expires_at=_parse(row["expires_at"]),
        )

    def _persist_operation(
        self, op: SendOperation, *, commit_intent: bool | None = None
    ) -> SendOperation:
        with self.store.uow() as conn:
            updates = "status=?,error_code=?,updated_at=?,version=version+1"
            values: list[Any] = [op.status.value, op.error_code, _stamp(self.now())]
            if commit_intent is not None:
                updates += ",commit_intent=?"
                values.append(int(commit_intent))
            values.append(str(op.operation_id))
            conn.execute(
                f"UPDATE send_operations SET {updates} WHERE operation_id=?", values
            )
        return op

    def _fail_closed_phase_result(
        self,
        op: SendOperation,
        reported: SendStatus,
        *,
        phase: str,
        commit_intent: bool,
    ) -> SendOperation | None:
        """Reject a result status the phase cannot legitimately produce.

        Returns ``None`` when ``reported`` is phase-valid.  Otherwise the
        operation is forced to a conservative terminal state and persisted:
        a driver claiming the non-idempotent send already crossed the boundary
        (``COMMITTED``/``VERIFIED`` in the wrong phase) becomes
        ``send_uncertain``; every other off-phase status is an explicit
        ``FAILED_SAFE``.
        """
        if reported in _PHASE_RESULT_STATUSES[phase]:
            return None
        if commit_intent or reported in {SendStatus.COMMITTED, SendStatus.VERIFIED}:
            op.status = SendStatus.UNCERTAIN
            op.error_code = ErrorCode.SEND_UNCERTAIN.value
        else:
            op.status = SendStatus.FAILED
            op.error_code = ErrorCode.FAILED_SAFE.value
        return self._persist_operation(op, commit_intent=commit_intent)

    @staticmethod
    def _operation_identity(op: SendOperation) -> tuple[str, str, str]:
        """Return the immutable identity an adapter phase must preserve."""

        return (str(op.operation_id), op.idempotency_key, str(op.draft_id))

    def _fail_closed_identity_result(
        self,
        operation_id: UUID | str,
        *,
        expected: tuple[str, str, str],
        reported: SendOperation,
        commit_intent: bool,
    ) -> SendOperation | None:
        """Quarantine the original operation if a phase swaps its identity."""

        if self._operation_identity(reported) == expected:
            return None
        original = self._operation(operation_id)
        original.status = SendStatus.UNCERTAIN
        original.error_code = ErrorCode.SEND_UNCERTAIN.value
        return self._persist_operation(original, commit_intent=commit_intent)

    async def run_send(
        self,
        operation_id: UUID | str,
        adapter: Any,
        *,
        segment_ref: str = "legacy:0",
        binding_revision: int = 1,
        conversation_revision: int | None = None,
    ) -> SendOperation:
        """Execute a two-phase send without automatic retry from uncertainty."""
        op = self._operation(operation_id)
        if op.status in (
            SendStatus.VERIFIED,
            SendStatus.UNCERTAIN,
            SendStatus.FAILED,
            SendStatus.CANCELLED,
        ):
            return op
        if op.status == SendStatus.PENDING:
            op = await self.prepare_send(
                operation_id, adapter, segment_ref=segment_ref,
                binding_revision=binding_revision,
                conversation_revision=conversation_revision,
            )
            if op.status != SendStatus.PREPARED:
                return op
        return await self.commit_prepared(operation_id, adapter)

    async def prepare_send(
        self, operation_id: UUID | str, adapter: Any, *, segment_ref: str,
        binding_revision: int, conversation_revision: int | None = None,
    ) -> SendOperation:
        """Prepare only; runtime must revalidate and consume M9 before commit."""
        op = self._operation(operation_id)
        if op.status is not SendStatus.PENDING:
            return op
        if op.status == SendStatus.PENDING:
            command = self._command_for_operation(operation_id)
            expected_identity = self._operation_identity(op)
            if conversation_revision is None:
                row = self.store.connection.execute(
                    "SELECT version FROM conversations WHERE conversation_id=?",
                    (command.conversation_id,),
                ).fetchone()
                conversation_revision = int(row["version"]) if row else 0
            # Old test adapters used ``prepare_send(operation)``. Keep that
            # explicit compatibility seam while all production drivers receive
            # the immutable M9 command and the Hub-owned operation id.
            parameter = next(iter(inspect.signature(adapter.prepare_send).parameters.values()), None)
            if parameter is not None and parameter.name == "operation":
                prepared = await adapter.prepare_send(op)
            else:
                prepared = await adapter.prepare_send(
                    command,
                    operation_id=op.operation_id,
                    segment_ref=segment_ref,
                    binding_revision=binding_revision,
                    conversation_revision=conversation_revision,
                )
            if self._operation_identity(prepared) != expected_identity:
                original = self._operation(operation_id)
                original.status = SendStatus.FAILED
                original.error_code = ErrorCode.FAILED_SAFE.value
                return self._persist_operation(original)
            closed = self._fail_closed_phase_result(
                op, prepared.status, phase="prepare", commit_intent=False
            )
            if closed is not None:
                return closed
            op = self._persist_operation(prepared)
        return op

    async def commit_prepared(self, operation_id: UUID | str, adapter: Any) -> SendOperation:
        """Persist non-idempotent intent, then commit and verify exactly once."""
        op = self._operation(operation_id)
        if op.status is not SendStatus.PREPARED:
            return op
        # Persist before the non-idempotent commit: a crash after this point is
        # quarantined by recovery rather than guessing whether a message left.
        self._persist_operation(op, commit_intent=True)
        expected_identity = self._operation_identity(op)
        try:
            committed = await adapter.commit_send(op)
        except Exception:  # noqa: BLE001 - unknown adapter failure is send uncertainty
            op.status = SendStatus.UNCERTAIN
            op.error_code = ErrorCode.SEND_UNCERTAIN.value
            return self._persist_operation(op, commit_intent=True)
        closed = self._fail_closed_identity_result(
            operation_id,
            expected=expected_identity,
            reported=committed,
            commit_intent=True,
        )
        if closed is not None:
            return closed
        closed = self._fail_closed_phase_result(
            op, committed.status, phase="commit", commit_intent=True
        )
        if closed is not None:
            return closed
        op = self._persist_operation(committed, commit_intent=True)
        if op.status != SendStatus.COMMITTED:
            return op
        expected_identity = self._operation_identity(op)
        try:
            verified = await adapter.verify_send(op)
        except Exception:  # noqa: BLE001 - unknown adapter failure is send uncertainty
            op.status = SendStatus.UNCERTAIN
            op.error_code = ErrorCode.SEND_UNCERTAIN.value
            return self._persist_operation(op, commit_intent=True)
        closed = self._fail_closed_identity_result(
            operation_id,
            expected=expected_identity,
            reported=verified,
            commit_intent=True,
        )
        if closed is not None:
            return closed
        closed = self._fail_closed_phase_result(
            op, verified.status, phase="verify", commit_intent=True
        )
        if closed is not None:
            return closed
        return self._persist_operation(verified, commit_intent=True)

    def recover(self) -> dict[str, int]:
        """Reopen recoverable work, expire stale work, quarantine ambiguous sends."""
        now = self.now()
        with self.store.uow() as conn:
            expired_drafts = conn.execute(
                "UPDATE drafts SET status=?,version=version+1,updated_at=? WHERE status IN (?,?) AND draft_id IN (SELECT draft_id FROM authorizations WHERE expires_at<=?)",
                (
                    DraftStatus.EXPIRED.value,
                    _stamp(now),
                    *ACTIVE_DRAFT_STATUSES,
                    _stamp(now),
                ),
            ).rowcount
            expired_plans = conn.execute(
                "UPDATE pacing_plans SET status=?,version=version+1,updated_at=? WHERE status IN (?,?,?) AND expires_at<=?",
                (
                    PlanStatus.EXPIRED.value,
                    _stamp(now),
                    *ACTIVE_PLAN_STATUSES,
                    _stamp(now),
                ),
            ).rowcount
            reset_prepared = conn.execute(
                "UPDATE send_operations SET status=?,version=version+1,updated_at=? WHERE status=? AND commit_intent=0",
                (SendStatus.PENDING.value, _stamp(now), SendStatus.PREPARED.value),
            ).rowcount
            uncertain = conn.execute(
                "UPDATE send_operations SET status=?,error_code=?,version=version+1,updated_at=? WHERE (status=? AND commit_intent=1) OR status=?",
                (
                    SendStatus.UNCERTAIN.value,
                    ErrorCode.SEND_UNCERTAIN.value,
                    _stamp(now),
                    SendStatus.PREPARED.value,
                    SendStatus.COMMITTED.value,
                ),
            ).rowcount
            returned_outbox = conn.execute(
                "UPDATE outbox SET status='pending',version=version+1 WHERE status='dispatching'",
                (),
            ).rowcount
            return {
                "expired_drafts": expired_drafts,
                "expired_plans": expired_plans,
                "prepared_reset": reset_prepared,
                "send_uncertain": uncertain,
                "outbox_returned": returned_outbox,
            }

    def conversation_projection(self, conversation_id: str) -> Mapping[str, Any]:
        """A read-only, JSON-safe projection for M11/M12; no infrastructure leaks."""
        conn = self.store.connection
        conversation = conn.execute(
            "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        if not conversation:
            raise KeyError(conversation_id)
        messages = conn.execute(
            "SELECT platform_message_key,observed_at,text,content_type FROM messages WHERE conversation_id=? ORDER BY observed_at,platform_message_key",
            (conversation_id,),
        ).fetchall()
        drafts = conn.execute(
            "SELECT draft_id,text,rule_version,status,version,updated_at FROM drafts WHERE conversation_id=? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        plans = conn.execute(
            "SELECT pacing_plan_id,earliest_send_at,expires_at,status,pacing_rule_version FROM pacing_plans WHERE conversation_id=? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        return {
            "conversation": dict(conversation),
            "messages": [dict(row) for row in messages],
            "drafts": [dict(row) for row in drafts],
            "pacing_plans": [dict(row) for row in plans],
        }
