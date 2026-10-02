"""HMAC-signed, short-lived, one-time authorizations with SQLite atomic consume."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

from .engine import PolicyEngine
from .models import (
    AuthorizationBinding,
    AuthorizationConsumeResult,
    AuthorizationEnvelope,
    AuthorizationKind,
    InvalidationEvent,
    InvalidationKind,
    PolicyDecision,
    PolicyOutcome,
    PolicyReason,
    PolicyRequest,
)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class SQLiteAuthorizationStore:
    """Persistence boundary; every consume/revoke is a single IMMEDIATE transaction."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        if str(database) == ":memory:":
            self._database = f"file:m9-auth-{uuid4()}?mode=memory&cache=shared"
            self._uri = True
        else:
            self._database = str(database)
            self._uri = self._database.startswith("file:")
        self._keeper = self._connect()
        self._initialize(self._keeper)
        self._closed = False
        self._lifecycle_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database,
            uri=self._uri,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_authorizations (
                authorization_id TEXT PRIMARY KEY,
                token_digest TEXT NOT NULL,
                binding_json TEXT NOT NULL,
                binding_hash TEXT NOT NULL,
                authorization_kind TEXT NOT NULL,
                platform TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                contact_id TEXT NOT NULL,
                draft_id TEXT NOT NULL,
                rulepack_version TEXT NOT NULL,
                pacing_rule_version TEXT NOT NULL,
                capability_snapshot_hash TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                invalidated_at TEXT,
                invalidation_reason TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_policy_auth_active "
            "ON policy_authorizations(conversation_id, consumed_at, invalidated_at)"
        )

    def insert(
        self,
        envelope: AuthorizationEnvelope,
        *,
        token_digest: str,
    ) -> None:
        binding = envelope.binding
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO policy_authorizations (
                    authorization_id, token_digest, binding_json, binding_hash,
                    authorization_kind, platform, conversation_id, contact_id,
                    draft_id, rulepack_version, pacing_rule_version,
                    capability_snapshot_hash, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.authorization_id,
                    token_digest,
                    binding.model_dump_json(),
                    binding.binding_hash,
                    binding.authorization_kind.value,
                    binding.platform,
                    binding.conversation_id,
                    binding.contact_id,
                    binding.draft_id,
                    binding.rulepack_version,
                    binding.pacing_rule_version,
                    binding.capability_snapshot_hash,
                    _iso(envelope.issued_at),
                    _iso(envelope.expires_at),
                ),
            )
            connection.commit()

    def load(self, authorization_id: str) -> sqlite3.Row | None:
        # Shared-cache in-memory SQLite reports SQLITE_LOCKED (not SQLITE_BUSY)
        # when a read overlaps BEGIN IMMEDIATE, so coordinate all local access.
        # The database transaction still provides the cross-process guarantee for
        # file-backed stores; this lock only prevents Python threads leaking an
        # operational error instead of receiving a stable replay result.
        with self._write_lock, self._connect() as connection:
            return connection.execute(
                "SELECT * FROM policy_authorizations WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()

    def consume_once(
        self, authorization_id: str, token_digest: str, now: datetime
    ) -> PolicyReason:
        with self._write_lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM policy_authorizations WHERE authorization_id = ?",
                    (authorization_id,),
                ).fetchone()
                if row is None or not hmac.compare_digest(
                    row["token_digest"], token_digest
                ):
                    connection.rollback()
                    return PolicyReason.AUTHORIZATION_INVALID
                if row["invalidated_at"] is not None:
                    connection.rollback()
                    return PolicyReason.AUTHORIZATION_REVOKED
                if row["consumed_at"] is not None:
                    connection.rollback()
                    return PolicyReason.AUTHORIZATION_REPLAYED
                if now > _parse_time(row["expires_at"]):
                    connection.rollback()
                    return PolicyReason.AUTHORIZATION_EXPIRED
                changed = connection.execute(
                    """
                UPDATE policy_authorizations
                SET consumed_at = ?
                WHERE authorization_id = ?
                  AND token_digest = ?
                  AND consumed_at IS NULL
                  AND invalidated_at IS NULL
                """,
                    (_iso(now), authorization_id, token_digest),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    return PolicyReason.AUTHORIZATION_REPLAYED
                connection.commit()
                return PolicyReason.AUTHORIZATION_CONSUMED
            finally:
                connection.close()

    def invalidate(self, event: InvalidationEvent, now: datetime) -> int:
        clauses = ["consumed_at IS NULL", "invalidated_at IS NULL"]
        parameters: list[str] = []

        if event.kind is InvalidationKind.PLATFORM_PAUSE:
            if event.platform is None:
                raise ValueError("platform_pause requires platform")
            clauses.append("platform = ?")
            parameters.append(event.platform)
        elif event.kind is InvalidationKind.CONTACT_PAUSE:
            if event.contact_id is None:
                raise ValueError("contact_pause requires contact_id")
            clauses.append("contact_id = ?")
            parameters.append(event.contact_id)
        elif event.kind is InvalidationKind.AUTO_MODE_DISABLED:
            clauses.append("authorization_kind = ?")
            parameters.append(AuthorizationKind.POLICY.value)
            if event.platform is not None:
                clauses.append("platform = ?")
                parameters.append(event.platform)
        elif event.kind is InvalidationKind.DRAFT_CHANGED:
            if event.draft_id is None:
                raise ValueError("draft_changed requires draft_id")
            clauses.append("draft_id = ?")
            parameters.append(event.draft_id)
        elif event.kind is InvalidationKind.NEW_MESSAGE:
            if event.conversation_id is None:
                raise ValueError("new_message requires conversation_id")
            clauses.append("conversation_id = ?")
            parameters.append(event.conversation_id)
        elif event.kind is InvalidationKind.RULE_CHANGED:
            if event.rulepack_version is None:
                raise ValueError("rule_changed requires the new rulepack_version")
            clauses.append("rulepack_version <> ?")
            parameters.append(event.rulepack_version)
        elif event.kind is InvalidationKind.PACING_CHANGED:
            if event.pacing_rule_version is None:
                raise ValueError("pacing_changed requires the new pacing_rule_version")
            clauses.append("pacing_rule_version <> ?")
            parameters.append(event.pacing_rule_version)
        elif event.kind is InvalidationKind.CAPABILITY_CHANGED:
            if event.capability_snapshot_hash is None:
                raise ValueError(
                    "capability_changed requires the new capability_snapshot_hash"
                )
            clauses.append("capability_snapshot_hash <> ?")
            parameters.append(event.capability_snapshot_hash)
        elif event.kind is not InvalidationKind.GLOBAL_PAUSE:
            raise ValueError(f"unsupported invalidation kind: {event.kind}")

        parameters.extend((_iso(now), event.reason))
        sql = (
            "UPDATE policy_authorizations SET invalidated_at = ?, invalidation_reason = ? WHERE "
            + " AND ".join(clauses)
        )
        # The predicate values precede SET values in our list, so construct exact SQL-order args.
        predicate_count = len(parameters) - 2
        args = [
            parameters[predicate_count],
            parameters[predicate_count + 1],
            *parameters[:predicate_count],
        ]
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(sql, args).rowcount
            connection.commit()
            return count

    def active_count(self) -> int:
        with self._write_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM policy_authorizations "
                "WHERE consumed_at IS NULL AND invalidated_at IS NULL"
            ).fetchone()
            return int(row["count"])

    def close(self) -> None:
        with self._lifecycle_lock:
            if not self._closed:
                self._keeper.close()
                self._closed = True


class AuthorizationService:
    """Only public path from a due policy decision to a frozen authorization."""

    def __init__(
        self,
        engine: PolicyEngine,
        store: SQLiteAuthorizationStore,
        signing_key: bytes,
        clock: Callable[[], datetime],
        *,
        ttl_seconds: float = 10,
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("authorization signing key must contain at least 32 bytes")
        if not 0 < ttl_seconds <= 30:
            raise ValueError("authorization TTL must be in (0, 30] seconds")
        self._engine = engine
        self._store = store
        self._key = signing_key
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)

    def revalidate_due_passthrough(self, eligibility: PolicyDecision, request: PolicyRequest) -> PolicyDecision:
        """Read-only preparation check; creates or consumes no authorization."""
        return self._engine.revalidate_due(eligibility, request)

    def authorize_due(
        self,
        eligibility: PolicyDecision,
        request: PolicyRequest,
    ) -> tuple[PolicyDecision, AuthorizationEnvelope | None]:
        revalidation = self._engine.revalidate_due(eligibility, request)
        if not revalidation.may_authorize or revalidation.authorization_kind is None:
            return revalidation, None

        now = self._clock()
        approval_expiry = (
            request.approval.expires_at if request.approval else request.plan_expires_at
        )
        expires_at = min(
            now + self._ttl,
            revalidation.valid_until,
            request.draft.expires_at,
            request.plan_expires_at,
            approval_expiry,
        )
        if expires_at <= now:
            return revalidation.model_copy(
                update={
                    "outcome": PolicyOutcome.BLOCKED,
                    "authorization_kind": None,
                    "reason_codes": (PolicyReason.AUTHORIZATION_EXPIRED,),
                }
            ), None

        binding = AuthorizationBinding(
            eligibility_id=eligibility.decision_id,
            authorization_kind=revalidation.authorization_kind,
            draft_id=request.draft.draft_id,
            platform=request.draft.platform,
            account_id=request.draft.account_id,
            conversation_id=request.draft.conversation_id,
            contact_id=request.draft.contact_id,
            body_hash=request.draft.body_hash,
            expected_last_message_key=request.draft.expected_last_message_key,
            rulepack_version=request.draft.rulepack_version,
            pacing_plan_id=request.draft.pacing_plan_id,
            pacing_rule_version=request.draft.pacing_rule_version,
            capability_snapshot_hash=request.draft.capability_snapshot_hash,
            policy_state_version=request.draft.policy_state_version,
            execution_mode=request.state.capability.execution_mode,
            binding_revision=request.state.binding_revision,
            conversation_revision=request.state.conversation_revision,
            approval_id=revalidation.approval_id,
        )
        authorization_id = str(uuid4())
        claims = {
            "authorization_id": authorization_id,
            "binding_hash": binding.binding_hash,
            "issued_at": _iso(now),
            "expires_at": _iso(expires_at),
            "nonce": token_urlsafe(18),
            "version": 1,
        }
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        encoded_payload = _b64encode(payload)
        signature = _b64encode(
            hmac.digest(self._key, encoded_payload.encode("ascii"), "sha256")
        )
        token = f"m9v1.{encoded_payload}.{signature}"
        envelope = AuthorizationEnvelope(
            authorization_id=authorization_id,
            token=token,
            binding=binding,
            issued_at=now,
            expires_at=expires_at,
        )
        self._store.insert(
            envelope, token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest()
        )
        return revalidation, envelope

    def consume(
        self,
        token: str,
        *,
        expected_binding: AuthorizationBinding,
        live_request: PolicyRequest,
    ) -> AuthorizationConsumeResult:
        now = self._clock()
        claims, token_reason = self._verify_token(token)
        if claims is None:
            return self._consume_result(False, None, token_reason, None)
        authorization_id = str(claims["authorization_id"])
        if now > _parse_time(str(claims["expires_at"])):
            return self._consume_result(
                False, authorization_id, PolicyReason.AUTHORIZATION_EXPIRED, None
            )
        if not hmac.compare_digest(
            str(claims["binding_hash"]), expected_binding.binding_hash
        ):
            return self._consume_result(
                False, authorization_id, PolicyReason.EXECUTION_BINDING_MISMATCH, None
            )

        row = self._store.load(authorization_id)
        if row is None:
            return self._consume_result(
                False, authorization_id, PolicyReason.AUTHORIZATION_INVALID, None
            )
        stored_binding = AuthorizationBinding.model_validate_json(row["binding_json"])
        if stored_binding != expected_binding or not self._request_matches_binding(
            live_request, stored_binding
        ):
            return self._consume_result(
                False, authorization_id, PolicyReason.EXECUTION_BINDING_MISMATCH, None
            )

        failures = self._engine.validate_execution_state(live_request)
        if failures:
            return self._consume_result(False, authorization_id, failures[0], None)

        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        outcome = self._store.consume_once(authorization_id, token_digest, now)
        accepted = outcome is PolicyReason.AUTHORIZATION_CONSUMED
        return self._consume_result(
            accepted, authorization_id, outcome, now if accepted else None
        )

    def invalidate(self, event: InvalidationEvent) -> int:
        return self._store.invalidate(event, self._clock())

    def _verify_token(
        self, token: str
    ) -> tuple[dict[str, object] | None, PolicyReason]:
        try:
            prefix, payload, signature = token.split(".")
            if prefix != "m9v1":
                return None, PolicyReason.AUTHORIZATION_INVALID
            expected = _b64encode(
                hmac.digest(self._key, payload.encode("ascii"), "sha256")
            )
            if not hmac.compare_digest(signature, expected):
                return None, PolicyReason.AUTHORIZATION_TAMPERED
            claims = json.loads(_b64decode(payload))
            required = {
                "authorization_id",
                "binding_hash",
                "issued_at",
                "expires_at",
                "nonce",
                "version",
            }
            if set(claims) != required or claims["version"] != 1:
                return None, PolicyReason.AUTHORIZATION_INVALID
            _parse_time(str(claims["issued_at"]))
            _parse_time(str(claims["expires_at"]))
            return claims, PolicyReason.AUTHORIZATION_CONSUMED
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None, PolicyReason.AUTHORIZATION_INVALID

    @staticmethod
    def _request_matches_binding(
        request: PolicyRequest, binding: AuthorizationBinding
    ) -> bool:
        draft = request.draft
        state = request.state
        return all(
            (
                draft.draft_id == binding.draft_id,
                draft.platform == binding.platform,
                draft.account_id == binding.account_id,
                draft.conversation_id == binding.conversation_id,
                draft.contact_id == binding.contact_id,
                draft.body_hash == binding.body_hash,
                draft.expected_last_message_key == binding.expected_last_message_key,
                draft.rulepack_version == binding.rulepack_version,
                state.last_message_key == binding.expected_last_message_key,
                state.active_rulepack_version == binding.rulepack_version,
                draft.pacing_plan_id == binding.pacing_plan_id,
                draft.pacing_rule_version == binding.pacing_rule_version,
                state.active_pacing_rule_version == binding.pacing_rule_version,
                draft.capability_snapshot_hash == binding.capability_snapshot_hash,
                state.capability.snapshot_hash == binding.capability_snapshot_hash,
                state.capability.execution_mode == binding.execution_mode,
                draft.binding_revision == binding.binding_revision,
                state.binding_revision == binding.binding_revision,
                draft.conversation_revision == binding.conversation_revision,
                state.conversation_revision == binding.conversation_revision,
                draft.policy_state_version == binding.policy_state_version,
                state.policy_state_version == binding.policy_state_version,
                (request.approval.approval_id if request.approval else None)
                == binding.approval_id,
            )
        )

    @staticmethod
    def _consume_result(
        accepted: bool,
        authorization_id: str | None,
        reason: PolicyReason,
        consumed_at: datetime | None,
    ) -> AuthorizationConsumeResult:
        return AuthorizationConsumeResult(
            accepted=accepted,
            authorization_id=authorization_id,
            reason=reason,
            consumed_at=consumed_at,
            audit_summary=(
                f"authorization={authorization_id or 'unknown'};accepted={str(accepted).lower()};reason={reason.value}"
            ),
        )
