"""Atomic SQLite rule-pack activation, rollback, audit, and invalidation."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from .compiler import RulePackCompiler
from .models import (
    ActivationEvent,
    HumanApproval,
    ImmutableRulePack,
    RuleContext,
    RuleEvalReport,
    RulePackDraft,
    RulePackReport,
    RuleSource,
)


class RulePackError(RuntimeError):
    pass


class ActivationInvalidationPort(Protocol):
    def invalidate(self, event: ActivationEvent) -> None: ...


class RulePackService(Protocol):
    def ingest(self, source: RuleSource) -> RulePackDraft: ...
    def validate(self, draft_id: str) -> RulePackReport: ...
    def evaluate_examples(self, draft_id: str) -> RuleEvalReport: ...
    def activate(self, draft_id: str, approval: HumanApproval) -> ImmutableRulePack: ...
    def resolve(self, contact_id: str, at: datetime | None = None) -> RuleContext: ...
    def rollback(self, version: str, approval: HumanApproval) -> ImmutableRulePack: ...


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


class AtomicRulePackStore:
    """One SQLite transaction owns version switch, audit and activation outbox."""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        invalidation_port: ActivationInvalidationPort | None = None,
        transaction_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.connection = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.invalidation_port = invalidation_port
        self.transaction_hook = transaction_hook
        self.compiler = RulePackCompiler()
        with self._lock:
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS m7_rulepack_sources (
                    draft_id TEXT PRIMARY KEY,
                    rulepack_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_format TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    source_blob BLOB NOT NULL,
                    payload_json TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(rulepack_id, version)
                );
                CREATE TABLE IF NOT EXISTS m7_rulepack_audit (
                    audit_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    rulepack_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS m7_rulepack_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                """
            )

    @contextmanager
    def _uow(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def ingest(self, source: RuleSource) -> RulePackDraft:
        return self.save_draft(self.compiler.ingest(source), source_blob=source.content)

    def save_draft(
        self, draft: RulePackDraft, *, source_blob: bytes = b""
    ) -> RulePackDraft:
        with self._uow() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO m7_rulepack_sources(draft_id,rulepack_id,version,source_name,source_format,source_hash,source_blob,payload_json,report_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    draft.draft_id,
                    draft.rulepack_id,
                    draft.version,
                    draft.source_name,
                    draft.source_format.value,
                    draft.source_hash,
                    source_blob,
                    json.dumps(
                        draft.normalized.model_dump(mode="json"),
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        draft.report.model_dump(mode="json"),
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                    "draft",
                    _stamp(draft.created_at),
                ),
            )
        return draft

    def draft(self, draft_id: str) -> RulePackDraft:
        row = self.connection.execute(
            "SELECT * FROM m7_rulepack_sources WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise RulePackError(f"unknown rule-pack draft {draft_id}")
        from .models import NormalizedRuleSource, RulePackReport, SourceFormat

        return RulePackDraft(
            draft_id=row["draft_id"],
            rulepack_id=row["rulepack_id"],
            version=row["version"],
            source_name=row["source_name"],
            source_format=SourceFormat(row["source_format"]),
            source_hash=row["source_hash"],
            normalized=NormalizedRuleSource.model_validate(
                json.loads(row["payload_json"])
            ),
            report=RulePackReport.model_validate(json.loads(row["report_json"])),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def validate(self, draft_id: str) -> RulePackReport:
        return self.draft(draft_id).report

    def evaluate_examples(self, draft_id: str) -> RuleEvalReport:
        draft = self.draft(draft_id)
        test_ids = tuple(item.test_id for item in draft.report.test_cases)
        return RuleEvalReport(
            total=len(test_ids),
            passed=0,
            failed_test_ids=test_ids,
            rulepack_version=draft.version,
        )

    def activate(self, draft_id: str, approval: HumanApproval) -> ImmutableRulePack:
        draft = self.draft(draft_id)
        if not draft.report.valid:
            raise RulePackError("conflicted or ambiguous rule pack cannot be activated")
        now = datetime.now(UTC)
        previous: str | None = None
        with self._uow() as conn:
            active = conn.execute(
                "SELECT rulepack_id,version FROM m7_rulepack_sources WHERE rulepack_id=? AND status='active'",
                (draft.rulepack_id,),
            ).fetchone()
            if active:
                previous = active["version"]
                conn.execute(
                    "UPDATE m7_rulepack_sources SET status='retired' WHERE rulepack_id=? AND status='active'",
                    (draft.rulepack_id,),
                )
            changed = conn.execute(
                "UPDATE m7_rulepack_sources SET status='active' WHERE draft_id=? AND status='draft'",
                (draft_id,),
            ).rowcount
            if changed != 1:
                raise RulePackError("draft is already activated or missing")
            event = ActivationEvent(
                event_type="rulepack.activated",
                rulepack_id=draft.rulepack_id,
                new_version=draft.version,
                previous_version=previous,
                source_hash=draft.source_hash,
            )
            self._write_event(conn, event, approval, now)
            if self.transaction_hook:
                self.transaction_hook("before_commit")
        result = self._immutable(draft, now)
        self._dispatch_pending()
        return result

    def rollback(self, version: str, approval: HumanApproval) -> ImmutableRulePack:
        row = self.connection.execute(
            "SELECT * FROM m7_rulepack_sources WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise RulePackError(f"unknown rule-pack version {version}")
        if row["status"] == "active":
            return self._immutable(self.draft(row["draft_id"]), datetime.now(UTC))
        draft = self.draft(row["draft_id"])
        now = datetime.now(UTC)
        with self._uow() as conn:
            active = conn.execute(
                "SELECT rulepack_id,version FROM m7_rulepack_sources WHERE rulepack_id=? AND status='active'",
                (draft.rulepack_id,),
            ).fetchone()
            previous = active["version"] if active else None
            conn.execute(
                "UPDATE m7_rulepack_sources SET status='retired' WHERE rulepack_id=? AND status='active'",
                (draft.rulepack_id,),
            )
            conn.execute(
                "UPDATE m7_rulepack_sources SET status='active' WHERE draft_id=?",
                (draft.draft_id,),
            )
            event = ActivationEvent(
                event_type="rulepack.rolled_back",
                rulepack_id=draft.rulepack_id,
                new_version=draft.version,
                previous_version=previous,
                source_hash=draft.source_hash,
            )
            self._write_event(conn, event, approval, now)
        result = self._immutable(draft, now)
        self._dispatch_pending()
        return result

    def resolve(self, contact_id: str, at: datetime | None = None) -> RuleContext:
        if not contact_id or any(char.isspace() for char in contact_id):
            raise RulePackError("resolve requires stable contact_id")
        row = self.connection.execute(
            "SELECT * FROM m7_rulepack_sources WHERE status='active' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RulePackError("no active rule pack")
        draft = self.draft(row["draft_id"])
        pack = self._immutable(draft, datetime.fromisoformat(row["created_at"]))
        override = next(
            (
                item
                for item in draft.normalized.contacts
                if item.contact_id == contact_id
            ),
            None,
        )
        required = list(draft.normalized.required_behaviors)
        prohibited = list(draft.normalized.prohibited_behaviors)
        if override:
            required.extend(override.required_behaviors)
            # A contact override may add hard prohibitions, never remove/replace global ones.
            prohibited.extend(override.prohibited_behaviors)
        return RuleContext(
            rulepack=pack,
            contact_override=override,
            effective_required=tuple(required),
            effective_prohibited=tuple(prohibited),
        )

    def pending_outbox(self) -> list[ActivationEvent]:
        rows = self.connection.execute(
            "SELECT payload_json FROM m7_rulepack_outbox WHERE status='pending' ORDER BY created_at"
        ).fetchall()
        return [
            ActivationEvent.model_validate(json.loads(row["payload_json"]))
            for row in rows
        ]

    def _write_event(
        self,
        conn: sqlite3.Connection,
        event: ActivationEvent,
        approval: HumanApproval,
        now: datetime,
    ) -> None:
        payload = event.model_dump(mode="json")
        conn.execute(
            "INSERT INTO m7_rulepack_outbox VALUES(?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                f"{event.event_type}:{event.rulepack_id}:{event.previous_version}:{event.new_version}",
                event.event_type,
                json.dumps(payload, sort_keys=True),
                "pending",
                _stamp(now),
                None,
            ),
        )
        conn.execute(
            "INSERT INTO m7_rulepack_audit VALUES(?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                event.event_type,
                event.rulepack_id,
                event.new_version,
                approval.approver_id,
                json.dumps(
                    {"reason": approval.reason, "event": payload}, sort_keys=True
                ),
                _stamp(now),
            ),
        )

    def _dispatch_pending(self) -> None:
        if self.invalidation_port is None:
            return
        rows = self.connection.execute(
            "SELECT outbox_id,payload_json FROM m7_rulepack_outbox WHERE status='pending' ORDER BY created_at"
        ).fetchall()
        for row in rows:
            event = ActivationEvent.model_validate(json.loads(row["payload_json"]))
            try:
                self.invalidation_port.invalidate(event)
            except RuntimeError:
                continue
            with self._uow() as conn:
                conn.execute(
                    "UPDATE m7_rulepack_outbox SET status='delivered',delivered_at=? WHERE outbox_id=? AND status='pending'",
                    (_stamp(datetime.now(UTC)), row["outbox_id"]),
                )

    @staticmethod
    def _immutable(draft: RulePackDraft, activated_at: datetime) -> ImmutableRulePack:
        return ImmutableRulePack(
            rulepack_id=draft.rulepack_id,
            version=draft.version,
            source_hash=draft.source_hash,
            normalized=draft.normalized,
            compiler_version=draft.compiler_version,
            activated_at=activated_at,
        )


AtomicActivator = AtomicRulePackStore
