from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from messenger_ai.domain import DomainError, ErrorCode, EventEnvelope, InboundMessage
from messenger_ai.domain.clock import ClockPort, SystemClock

from .models import (
    Contact,
    ContactContext,
    ContactFact,
    ContactPreference,
    ContactStatus,
    ConversationSummary,
    DeletionReceipt,
    FactStatus,
    IdentityBinding,
    MemoryMessage,
    MemoryMessageDirection,
    RelationshipState,
    SourceKind,
)
from .store import SQLiteMemoryStore, encode, parse, stamp


class MemoryService:
    consumer_name = "memory-v1"

    def __init__(
        self, store: SQLiteMemoryStore, *, clock: ClockPort | None = None
    ) -> None:
        self.store = store
        self.clock = clock or SystemClock()

    def create_contact(self, contact: Contact) -> Contact:
        with self.store.uow() as db:
            row = db.execute(
                "SELECT payload_json FROM memory_contacts WHERE contact_id=?",
                (contact.contact_id,),
            ).fetchone()
            if row is not None:
                existing = Contact.model_validate_json(row["payload_json"])
                if existing != contact:
                    raise DomainError(ErrorCode.DUPLICATE, "contact id already exists")
                return existing
            db.execute(
                "INSERT INTO memory_contacts(contact_id,payload_json,status,created_at) VALUES (?,?,?,?)",
                (
                    contact.contact_id,
                    encode(contact),
                    contact.status.value,
                    stamp(contact.created_at),
                ),
            )
            self._audit(
                db, "contact.created", contact.contact_id, {"status": contact.status}
            )
        return contact

    def bind_identity(self, binding: IdentityBinding) -> IdentityBinding:
        """Only an already-validated HumanApproval can create/change a binding."""
        with self.store.uow() as db:
            contact = self._active_contact(db, binding.contact_id)
            del contact
            existing = db.execute(
                "SELECT payload_json,evidence_hash,contact_id FROM memory_bindings "
                "WHERE platform=? AND account_id=? AND conversation_id=?",
                (binding.platform.value, binding.account_id, binding.conversation_id),
            ).fetchone()
            if existing is not None and existing["contact_id"] != binding.contact_id:
                raise DomainError(
                    ErrorCode.IDENTITY_AMBIGUOUS, "conversation already bound"
                )
            if (
                existing is not None
                and existing["evidence_hash"] == binding.platform_evidence_hash
            ):
                return IdentityBinding.model_validate_json(existing["payload_json"])
            if existing is None:
                db.execute(
                    "INSERT INTO memory_bindings(binding_id,contact_id,platform,account_id,conversation_id,evidence_hash,payload_json,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        str(binding.binding_id),
                        binding.contact_id,
                        binding.platform.value,
                        binding.account_id,
                        binding.conversation_id,
                        binding.platform_evidence_hash,
                        encode(binding),
                        stamp(binding.verified_at),
                    ),
                )
                action = "identity.bound"
            else:
                db.execute(
                    "UPDATE memory_bindings SET binding_id=?,evidence_hash=?,payload_json=?,updated_at=? "
                    "WHERE platform=? AND account_id=? AND conversation_id=?",
                    (
                        str(binding.binding_id),
                        binding.platform_evidence_hash,
                        encode(binding),
                        stamp(binding.verified_at),
                        binding.platform.value,
                        binding.account_id,
                        binding.conversation_id,
                    ),
                )
                action = "identity.evidence_changed"
            self._audit(
                db,
                action,
                binding.contact_id,
                {
                    "binding_id": str(binding.binding_id),
                    "verified_by": binding.verified_by,
                },
            )
        return binding

    def consume_inbound(self, event: EventEnvelope[InboundMessage]) -> bool:
        message = event.payload
        with self.store.uow() as db:
            prior = db.execute(
                "SELECT event_id FROM memory_consumed_events WHERE consumer_name=? AND event_id=?",
                (self.consumer_name, str(event.event_id)),
            ).fetchone()
            if prior is not None:
                return False
            binding = db.execute(
                "SELECT contact_id,payload_json FROM memory_bindings WHERE platform=? AND account_id=? AND conversation_id=?",
                (message.platform.value, message.account_id, message.conversation_id),
            ).fetchone()
            if binding is None or binding["contact_id"] != message.contact_id:
                raise DomainError(
                    ErrorCode.IDENTITY_AMBIGUOUS, "message has no approved binding"
                )
            self._active_contact(db, message.contact_id)
            expires = message.observed_at + timedelta(days=30)
            memory_message = MemoryMessage(
                contact_id=message.contact_id,
                conversation_id=message.conversation_id,
                source_event_id=event.event_id,
                platform_message_key=message.platform_message_key,
                text=message.text,
                observed_at=message.observed_at,
                expires_at=expires,
                direction=MemoryMessageDirection.INBOUND,
            )
            self._insert_message(db, memory_message, event_id=event.event_id)
            db.execute(
                "INSERT INTO memory_consumed_events(consumer_name,event_id,consumed_at) VALUES (?,?,?)",
                (self.consumer_name, str(event.event_id), stamp(self.now())),
            )
        return True

    def record_message(self, message: MemoryMessage) -> MemoryMessage:
        """Persist an already-authorized direction-aware history item."""
        with self.store.uow() as db:
            self._assert_context(db, message.contact_id, message.conversation_id)
            self._insert_message(db, message, event_id=message.source_event_id)
        return message

    @staticmethod
    def _insert_message(db, message: MemoryMessage, *, event_id: UUID) -> None:
        try:
            db.execute(
                "INSERT INTO memory_messages(message_id,contact_id,conversation_id,event_id,message_key,observed_at,expires_at,direction,payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(message.message_id), message.contact_id, message.conversation_id,
                 str(event_id), message.platform_message_key, stamp(message.observed_at),
                 stamp(message.expires_at), message.direction.value, encode(message)),
            )
        except Exception as exc:
            if "UNIQUE" not in str(exc).upper():
                raise

    def add_summary(self, summary: ConversationSummary) -> ConversationSummary:
        with self.store.uow() as db:
            self._assert_context(db, summary.contact_id, summary.conversation_id)
            for message_id in summary.source_message_ids:
                if not self._message_belongs(
                    db, summary.contact_id, summary.conversation_id, message_id
                ):
                    raise DomainError(
                        ErrorCode.IDENTITY_AMBIGUOUS, "summary source leaks context"
                    )
            db.execute(
                "INSERT OR IGNORE INTO memory_summaries(summary_id,contact_id,conversation_id,created_at,payload_json) VALUES (?,?,?,?,?)",
                (
                    str(summary.summary_id),
                    summary.contact_id,
                    summary.conversation_id,
                    stamp(summary.created_at),
                    encode(summary),
                ),
            )
        return summary

    def record_fact(self, fact: ContactFact) -> ContactFact:
        with self.store.uow() as db:
            self._assert_context(db, fact.contact_id, fact.conversation_id)
            self._validate_evidence(
                db,
                fact.contact_id,
                fact.conversation_id,
                fact.evidence.kind,
                fact.evidence.source_message_id,
            )
            db.execute(
                "INSERT OR IGNORE INTO memory_facts(fact_id,contact_id,conversation_id,status,hard_rule,created_at,payload_json) VALUES (?,?,?,?,?,?,?)",
                (
                    str(fact.fact_id),
                    fact.contact_id,
                    fact.conversation_id,
                    fact.status.value,
                    int(fact.hard_rule),
                    stamp(fact.created_at),
                    encode(fact),
                ),
            )
        return fact

    def set_preference(self, preference: ContactPreference) -> ContactPreference:
        with self.store.uow() as db:
            self._assert_context(db, preference.contact_id, preference.conversation_id)
            db.execute(
                "INSERT OR REPLACE INTO memory_preferences(preference_id,contact_id,conversation_id,hard_rule,verified_at,payload_json) VALUES (?,?,?,?,?,?)",
                (
                    str(preference.preference_id),
                    preference.contact_id,
                    preference.conversation_id,
                    int(preference.hard_rule),
                    stamp(preference.approval.verified_at),
                    encode(preference),
                ),
            )
        return preference

    def set_relationship(self, state: RelationshipState) -> RelationshipState:
        with self.store.uow() as db:
            self._assert_context(db, state.contact_id, state.conversation_id)
            self._validate_evidence(
                db,
                state.contact_id,
                state.conversation_id,
                state.evidence.kind,
                state.evidence.source_message_id,
            )
            db.execute(
                "INSERT OR REPLACE INTO memory_relationships(state_id,contact_id,conversation_id,verified_at,payload_json) VALUES (?,?,?,?,?)",
                (
                    str(state.state_id),
                    state.contact_id,
                    state.conversation_id,
                    stamp(state.approval.verified_at),
                    encode(state),
                ),
            )
        return state

    def context(
        self, contact_id: str, conversation_id: str, *, budget_chars: int
    ) -> ContactContext:
        if budget_chars <= 0:
            raise ValueError("budget_chars must be positive")
        now = self.now()
        with self.store._lock:
            db = self.store.connection
            self._assert_context(db, contact_id, conversation_id)
            facts = self._fetch_models(
                db,
                "memory_facts",
                ContactFact,
                contact_id,
                conversation_id,
                "created_at DESC",
            )
            preferences = self._fetch_models(
                db,
                "memory_preferences",
                ContactPreference,
                contact_id,
                conversation_id,
                "verified_at DESC",
            )
            relationships = self._fetch_models(
                db,
                "memory_relationships",
                RelationshipState,
                contact_id,
                conversation_id,
                "verified_at DESC",
            )
            summaries = self._fetch_models(
                db,
                "memory_summaries",
                ConversationSummary,
                contact_id,
                conversation_id,
                "created_at DESC",
            )
            messages = self._fetch_messages(db, contact_id, conversation_id, now)
        hard_rules = tuple(
            [fact for fact in facts if fact.hard_rule]
            + [preference for preference in preferences if preference.hard_rule]
        )
        confirmed_preferences = tuple(
            preference for preference in preferences if not preference.hard_rule
        )
        confirmed_facts = tuple(
            fact
            for fact in facts
            if fact.status is FactStatus.CONFIRMED and not fact.hard_rule
        )
        kept: list[object] = []
        used = 0
        for item in (*hard_rules, *confirmed_preferences):
            used += self._size(item)
            kept.append(item)
        # Protected items are intentionally retained even when a caller supplied too-small budget.
        for group in (confirmed_facts, relationships, summaries, messages):
            for item in group:
                size = self._size(item)
                if used + size > budget_chars:
                    continue
                used += size
                kept.append(item)
        retained_ids = {item.message_id for item in messages if item in kept}
        for item in (*hard_rules, *confirmed_facts, *relationships, *summaries):
            if item not in kept:
                continue
            if isinstance(item, ConversationSummary):
                retained_ids.update(item.source_message_ids)
            elif (
                isinstance(item, (ContactFact, RelationshipState))
                and item.evidence.source_message_id is not None
            ):
                retained_ids.add(item.evidence.source_message_id)
        return ContactContext(
            contact_id=contact_id,
            conversation_id=conversation_id,
            hard_rules=hard_rules,
            confirmed_preferences=confirmed_preferences,
            confirmed_facts=tuple(item for item in confirmed_facts if item in kept),
            relationship_states=tuple(item for item in relationships if item in kept),
            summaries=tuple(item for item in summaries if item in kept),
            recent_messages=tuple(item for item in messages if item in kept),
            source_message_ids=tuple(sorted(retained_ids, key=str)),
            budget_chars=budget_chars,
            used_chars=used,
            truncated=len(kept)
            < len(hard_rules)
            + len(confirmed_preferences)
            + len(confirmed_facts)
            + len(relationships)
            + len(summaries)
            + len(messages),
        )

    def trace_fact(
        self, contact_id: str, conversation_id: str, fact_id: str
    ) -> ContactFact:
        with self.store._lock:
            row = self.store.connection.execute(
                "SELECT payload_json FROM memory_facts WHERE contact_id=? AND conversation_id=? AND fact_id=?",
                (contact_id, conversation_id, fact_id),
            ).fetchone()
        if row is None:
            raise KeyError(fact_id)
        return ContactFact.model_validate_json(row["payload_json"])

    def purge_expired_messages(self) -> int:
        with self.store.uow() as db:
            cursor = db.execute(
                "DELETE FROM memory_messages WHERE expires_at<=?", (stamp(self.now()),)
            )
            return cursor.rowcount

    def delete_contact(
        self, contact_id: str, *, deleted_by: str, recovery_days: int = 30
    ) -> DeletionReceipt:
        if not deleted_by or recovery_days < 1:
            raise ValueError("deletion needs actor and a positive recovery window")
        now = self.now()
        receipt = DeletionReceipt(
            contact_id=contact_id,
            deleted_by=deleted_by,
            deleted_at=now,
            recoverable_until=now + timedelta(days=recovery_days),
        )
        with self.store.uow() as db:
            contact = self._active_contact(db, contact_id)
            snapshot = self._snapshot(db, contact_id)
            deleted_contact = contact.model_copy(
                update={"status": ContactStatus.DELETED}
            )
            db.execute(
                "UPDATE memory_contacts SET payload_json=?,status=? WHERE contact_id=?",
                (encode(deleted_contact), ContactStatus.DELETED.value, contact_id),
            )
            for table in (
                "memory_bindings",
                "memory_messages",
                "memory_summaries",
                "memory_facts",
                "memory_preferences",
                "memory_relationships",
            ):
                db.execute(f"DELETE FROM {table} WHERE contact_id=?", (contact_id,))
            db.execute(
                "INSERT INTO memory_deletions(deletion_id,contact_id,recoverable_until,snapshot_json,receipt_json) VALUES (?,?,?,?,?)",
                (
                    str(receipt.deletion_id),
                    contact_id,
                    stamp(receipt.recoverable_until),
                    encode(snapshot),
                    encode(receipt),
                ),
            )
            self._audit(
                db,
                "contact.deleted",
                contact_id,
                {
                    "deleted_by": deleted_by,
                    "external_cleanup": receipt.external_cleanup_scopes,
                },
            )
        return receipt

    def restore_contact(self, deletion_id: UUID | str, *, restored_by: str) -> Contact:
        with self.store.uow() as db:
            row = db.execute(
                "SELECT contact_id,recoverable_until,snapshot_json,restored_at FROM memory_deletions WHERE deletion_id=?",
                (str(deletion_id),),
            ).fetchone()
            if (
                row is None
                or row["restored_at"] is not None
                or parse(row["recoverable_until"]) < self.now()
            ):
                raise DomainError(ErrorCode.FAILED_SAFE, "contact is not recoverable")
            snapshot = json.loads(row["snapshot_json"])
            contact = Contact.model_validate(snapshot["contact"])
            active = contact.model_copy(update={"status": ContactStatus.ACTIVE})
            db.execute(
                "UPDATE memory_contacts SET payload_json=?,status=? WHERE contact_id=?",
                (encode(active), ContactStatus.ACTIVE.value, active.contact_id),
            )
            self._restore_rows(db, snapshot)
            db.execute(
                "UPDATE memory_deletions SET restored_at=? WHERE deletion_id=?",
                (stamp(self.now()), str(deletion_id)),
            )
            self._audit(
                db, "contact.restored", active.contact_id, {"restored_by": restored_by}
            )
        return active

    def now(self) -> datetime:
        value = self.clock.now()
        if value.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _size(item: object) -> int:
        if hasattr(item, "model_dump_json"):
            return len(item.model_dump_json())
        return len(str(item))

    def _active_contact(self, db, contact_id: str) -> Contact:
        row = db.execute(
            "SELECT payload_json,status FROM memory_contacts WHERE contact_id=?",
            (contact_id,),
        ).fetchone()
        if row is None or row["status"] != ContactStatus.ACTIVE.value:
            raise DomainError(
                ErrorCode.IDENTITY_AMBIGUOUS, "contact is absent or deleted"
            )
        return Contact.model_validate_json(row["payload_json"])

    def _assert_context(self, db, contact_id: str, conversation_id: str) -> None:
        self._active_contact(db, contact_id)
        row = db.execute(
            "SELECT contact_id FROM memory_bindings WHERE contact_id=? AND conversation_id=?",
            (contact_id, conversation_id),
        ).fetchone()
        if row is None:
            raise DomainError(
                ErrorCode.IDENTITY_AMBIGUOUS, "unapproved contact/conversation context"
            )

    @staticmethod
    def _message_belongs(
        db, contact_id: str, conversation_id: str, message_id: UUID
    ) -> bool:
        return (
            db.execute(
                "SELECT message_id FROM memory_messages WHERE contact_id=? AND conversation_id=? AND message_id=?",
                (contact_id, conversation_id, str(message_id)),
            ).fetchone()
            is not None
        )

    def _validate_evidence(
        self, db, contact_id, conversation_id, kind, source_message_id
    ) -> None:
        if kind is SourceKind.MESSAGE and not self._message_belongs(
            db, contact_id, conversation_id, source_message_id
        ):
            raise DomainError(
                ErrorCode.IDENTITY_AMBIGUOUS, "evidence source is outside context"
            )

    @staticmethod
    def _fetch_models(db, table, model, contact_id, conversation_id, order):
        rows = db.execute(
            f"SELECT payload_json FROM {table} WHERE contact_id=? AND conversation_id=? ORDER BY {order}",
            (contact_id, conversation_id),
        ).fetchall()
        return tuple(model.model_validate_json(row["payload_json"]) for row in rows)

    @staticmethod
    def _fetch_messages(db, contact_id, conversation_id, now):
        rows = db.execute(
            "SELECT payload_json FROM memory_messages WHERE contact_id=? AND conversation_id=? AND expires_at>? ORDER BY observed_at DESC",
            (contact_id, conversation_id, stamp(now)),
        ).fetchall()
        return tuple(
            MemoryMessage.model_validate_json(row["payload_json"]) for row in rows
        )

    def _audit(self, db, action, contact_id, payload) -> None:
        db.execute(
            "INSERT INTO memory_audit(audit_id,action,contact_id,payload_json,created_at) VALUES (?,?,?,?,?)",
            (
                str(uuid4()),
                action,
                contact_id,
                encode(payload),
                stamp(self.now()),
            ),
        )

    @staticmethod
    def _snapshot(db, contact_id: str) -> dict:
        snapshot = {
            "contact": json.loads(
                db.execute(
                    "SELECT payload_json FROM memory_contacts WHERE contact_id=?",
                    (contact_id,),
                ).fetchone()["payload_json"]
            )
        }
        for table in (
            "memory_bindings",
            "memory_messages",
            "memory_summaries",
            "memory_facts",
            "memory_preferences",
            "memory_relationships",
        ):
            rows = db.execute(
                f"SELECT payload_json FROM {table} WHERE contact_id=?", (contact_id,)
            ).fetchall()
            snapshot[table] = [json.loads(row["payload_json"]) for row in rows]
        return snapshot

    @staticmethod
    def _restore_rows(db, snapshot: dict) -> None:
        # Recovery restores the preserved immutable records using their original identifiers.
        for binding in snapshot["memory_bindings"]:
            item = IdentityBinding.model_validate(binding)
            db.execute(
                "INSERT INTO memory_bindings(binding_id,contact_id,platform,account_id,conversation_id,evidence_hash,payload_json,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(item.binding_id),
                    item.contact_id,
                    item.platform.value,
                    item.account_id,
                    item.conversation_id,
                    item.platform_evidence_hash,
                    encode(item),
                    stamp(item.verified_at),
                ),
            )
        mappings = (
            (
                "memory_messages",
                MemoryMessage,
                "message_id",
                "event_id,message_key,observed_at,expires_at",
            ),
            ("memory_summaries", ConversationSummary, "summary_id", "created_at"),
            ("memory_facts", ContactFact, "fact_id", "status,hard_rule,created_at"),
            (
                "memory_preferences",
                ContactPreference,
                "preference_id",
                "hard_rule,verified_at",
            ),
            ("memory_relationships", RelationshipState, "state_id", "verified_at"),
        )
        for table, model, id_name, columns in mappings:
            for raw in snapshot[table]:
                item = model.model_validate(raw)
                values = [
                    str(getattr(item, id_name)),
                    item.contact_id,
                    item.conversation_id,
                ]
                if table == "memory_messages":
                    values += [
                        str(item.source_event_id),
                        item.platform_message_key,
                        stamp(item.observed_at),
                        stamp(item.expires_at),
                    ]
                elif table == "memory_summaries":
                    values += [stamp(item.created_at)]
                elif table == "memory_facts":
                    values += [
                        item.status.value,
                        int(item.hard_rule),
                        stamp(item.created_at),
                    ]
                elif table == "memory_preferences":
                    values += [int(item.hard_rule), stamp(item.approval.verified_at)]
                else:
                    values += [stamp(item.approval.verified_at)]
                columns_sql = (
                    f"{id_name},contact_id,conversation_id,{columns},payload_json"
                )
                placeholders = ",".join("?" for _ in range(len(values) + 1))
                db.execute(
                    f"INSERT INTO {table}({columns_sql}) VALUES ({placeholders})",
                    (*values, encode(item)),
                )
