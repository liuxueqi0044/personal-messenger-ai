"""Short-lived local evidence references with protected content and access audit."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .secrets import SecretNotFoundError, SecretStore


class EvidenceError(RuntimeError):
    pass


class EvidenceNotFound(EvidenceError):
    pass


class EvidenceExpired(EvidenceError):
    pass


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceReference(EvidenceModel):
    evidence_ref: str = Field(pattern=r"^ev_[0-9a-f]{32}$")
    media_type: str = Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
    size_bytes: int = Field(ge=1)
    created_at: datetime
    expires_at: datetime


class EvidenceAuditEntry(EvidenceModel):
    sequence: int
    evidence_ref: str
    action: str
    outcome: str
    occurred_at: datetime


_REF = re.compile(r"^ev_[0-9a-f]{32}$")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware timestamp")
    return value.astimezone(UTC)


class EvidenceVault:
    """Metadata lives in SQLite; evidence bytes live only in SecretStore."""

    def __init__(
        self,
        metadata_database: str | Path,
        secret_store: SecretStore,
        clock: Callable[[], datetime],
        *,
        default_ttl_seconds: float = 900,
        max_ttl_seconds: float = 86_400,
        max_content_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        if not 0 < default_ttl_seconds <= max_ttl_seconds <= 86_400:
            raise ValueError("evidence TTL configuration is invalid")
        self._database = str(metadata_database)
        self._secret_store = secret_store
        self._clock = clock
        self._default_ttl = default_ttl_seconds
        self._max_ttl = max_ttl_seconds
        self._max_content = max_content_bytes
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_metadata (
                    evidence_ref TEXT PRIMARY KEY,
                    conversation_hash TEXT NOT NULL,
                    secret_name TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    content_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    deleted_at TEXT,
                    delete_reason TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_access_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_ref TEXT NOT NULL,
                    action TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )

    def put(
        self,
        conversation_id: str,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
        ttl_seconds: float | None = None,
    ) -> EvidenceReference:
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not isinstance(content, bytes) or not 0 < len(content) <= self._max_content:
            raise ValueError("evidence content size is invalid")
        ttl = self._default_ttl if ttl_seconds is None else ttl_seconds
        if not 0 < ttl <= self._max_ttl:
            raise ValueError("evidence TTL is outside the allowed range")
        if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", media_type):
            raise ValueError("media type is invalid")

        now = _utc(self._clock())
        expires_at = now + timedelta(seconds=ttl)
        evidence_ref = f"ev_{uuid4().hex}"
        secret_name = f"evidence.{evidence_ref}"
        self._secret_store.set_secret(secret_name, content)
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO evidence_metadata (
                        evidence_ref, conversation_hash, secret_name, media_type,
                        size_bytes, content_digest, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_ref,
                        self._conversation_hash(conversation_id),
                        secret_name,
                        media_type,
                        len(content),
                        hashlib.sha256(content).hexdigest(),
                        now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
                self._audit(connection, evidence_ref, "create", "stored", now)
        except Exception:
            self._secret_store.delete_secret(secret_name)
            raise
        return EvidenceReference(
            evidence_ref=evidence_ref,
            media_type=media_type,
            size_bytes=len(content),
            created_at=now,
            expires_at=expires_at,
        )

    def get(self, evidence_ref: str) -> bytes:
        self._validate_ref(evidence_ref)
        now = _utc(self._clock())
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_metadata WHERE evidence_ref = ?",
                (evidence_ref,),
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                self._audit(connection, evidence_ref, "read", "not_found", now)
                connection.commit()
                raise EvidenceNotFound("evidence is unavailable")
            if now >= datetime.fromisoformat(row["expires_at"]):
                self._delete_row(connection, row, now, "expired")
                self._audit(connection, evidence_ref, "read", "expired", now)
                connection.commit()
                raise EvidenceExpired("evidence has expired")
            try:
                content = self._secret_store.get_secret(row["secret_name"])
            except SecretNotFoundError:
                self._audit(connection, evidence_ref, "read", "missing_content", now)
                connection.commit()
                raise EvidenceNotFound("evidence content is unavailable") from None
            if hashlib.sha256(content).hexdigest() != row["content_digest"]:
                self._audit(connection, evidence_ref, "read", "integrity_failed", now)
                connection.commit()
                raise EvidenceError("evidence integrity check failed")
            self._audit(connection, evidence_ref, "read", "allowed", now)
            return content

    def clear_conversation(self, conversation_id: str) -> int:
        return self._clear(
            "conversation_hash = ?",
            (self._conversation_hash(conversation_id),),
            reason="conversation_clear",
        )

    def clear_all(self) -> int:
        return self._clear("1 = 1", (), reason="clear_all")

    def cleanup_expired(self) -> int:
        now = _utc(self._clock())
        return self._clear("expires_at <= ?", (now.isoformat(),), reason="expired")

    def audit_entries(
        self, evidence_ref: str | None = None
    ) -> tuple[EvidenceAuditEntry, ...]:
        with self._lock, self._connect() as connection:
            if evidence_ref is None:
                rows = connection.execute(
                    "SELECT * FROM evidence_access_audit ORDER BY sequence"
                ).fetchall()
            else:
                self._validate_ref(evidence_ref)
                rows = connection.execute(
                    "SELECT * FROM evidence_access_audit WHERE evidence_ref = ? ORDER BY sequence",
                    (evidence_ref,),
                ).fetchall()
        return tuple(
            EvidenceAuditEntry(
                sequence=row["sequence"],
                evidence_ref=row["evidence_ref"],
                action=row["action"],
                outcome=row["outcome"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
            )
            for row in rows
        )

    def _clear(
        self, predicate: str, parameters: tuple[str, ...], *, reason: str
    ) -> int:
        now = _utc(self._clock())
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM evidence_metadata WHERE deleted_at IS NULL AND {predicate}",
                parameters,
            ).fetchall()
            for row in rows:
                self._delete_row(connection, row, now, reason)
                self._audit(connection, row["evidence_ref"], "delete", reason, now)
            return len(rows)

    def _delete_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        reason: str,
    ) -> None:
        self._secret_store.delete_secret(row["secret_name"])
        connection.execute(
            "UPDATE evidence_metadata SET deleted_at = ?, delete_reason = ? WHERE evidence_ref = ?",
            (now.isoformat(), reason, row["evidence_ref"]),
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        evidence_ref: str,
        action: str,
        outcome: str,
        now: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO evidence_access_audit (evidence_ref, action, outcome, occurred_at) VALUES (?, ?, ?, ?)",
            (evidence_ref, action, outcome, now.isoformat()),
        )

    @staticmethod
    def _conversation_hash(conversation_id: str) -> str:
        return hashlib.sha256(f"conversation:{conversation_id}".encode()).hexdigest()

    @staticmethod
    def _validate_ref(evidence_ref: str) -> None:
        if not _REF.fullmatch(evidence_ref):
            raise EvidenceNotFound("evidence reference is invalid")
