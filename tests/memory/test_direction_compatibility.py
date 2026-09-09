from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from messenger_ai.domain import Platform
from messenger_ai.llm import ContactProjection, InboundItem, ReplyPlanRequest, RuleProjection, build_projection
from messenger_ai.memory import Contact, IdentityBinding, MemoryMessage, MemoryMessageDirection, MemoryService, SQLiteMemoryStore


NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


def test_old_memory_messages_table_migrates_and_defaults_inbound(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE memory_messages (
        message_id TEXT PRIMARY KEY, contact_id TEXT, conversation_id TEXT,
        event_id TEXT, message_key TEXT, observed_at TEXT, expires_at TEXT,
        payload_json TEXT
      );
    """)
    db.commit(); db.close()
    store = SQLiteMemoryStore(path)
    columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(memory_messages)")}
    assert "direction" in columns
    assert MemoryMessage(contact_id="c", conversation_id="v", source_event_id=uuid4(), platform_message_key="m", text="x", observed_at=NOW, expires_at=NOW + timedelta(days=1)).direction is MemoryMessageDirection.INBOUND
    store.close()


def test_outbound_direction_survives_memory_context_and_prompt(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    service = MemoryService(store)
    service.create_contact(Contact(contact_id="c", created_at=NOW))
    service.bind_identity(IdentityBinding(contact_id="c", platform=Platform.QQ, account_id="a", conversation_id="v", platform_evidence_hash=hashlib.sha256(b"proof").hexdigest(), approval={"verified_by": "owner", "verified_at": NOW, "reason": "test"}))
    service.record_message(MemoryMessage(contact_id="c", conversation_id="v", source_event_id=uuid4(), platform_message_key="out-1", text="我先忙", observed_at=NOW, expires_at=NOW + timedelta(days=1), direction=MemoryMessageDirection.HUMAN_OUTBOUND))
    context = service.context("c", "v", budget_chars=1000)
    assert context.recent_messages[0].direction is MemoryMessageDirection.HUMAN_OUTBOUND
    request = ReplyPlanRequest(request_id="r", account_id="a", contact=ContactProjection(contact_id="c", conversation_id="v", recent_messages=(InboundItem(message_key="out-1", text="我先忙", direction="human_outbound"),)), rules=RuleProjection(rulepack_id="rules", rule_version="v1", source_hash="hash"), inbound=(InboundItem(message_key="in-1", text="好"),), context_fingerprint="fp", created_at=NOW)
    contact_data = build_projection(request).contact_context
    assert '"direction":"human_outbound"' in contact_data
    store.close()
