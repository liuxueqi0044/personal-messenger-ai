import hashlib
import json
import sqlite3
from datetime import datetime

import pytest

from messenger_ai.adapters.qq.vm_driver import message_cursor
from messenger_ai.adapters.qq.vm_driver.message_cursor import (
    CURRENT_IDENTITY_SCHEMA,
    IDENTITY_SCHEMA_VERSION,
    MessageCursorStore,
)


def row(direction: str, text: str) -> dict[str, object]:
    return {"direction": direction, "text": text, "message_key": text,
            "conversation_internal_id": "current"}


def legacy_identity(item: dict[str, object]) -> str:
    payload = {
        key: item.get(key)
        for key in (
            "direction",
            "text",
            "message_key",
            "conversation_internal_id",
        )
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def create_legacy_cursor(path, *, next_seq: int, snapshot: list[str]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE cursor_state(
                   conversation_id TEXT PRIMARY KEY,
                   next_seq INTEGER NOT NULL,
                   snapshot_json TEXT NOT NULL
               )"""
        )
        connection.execute(
            "INSERT INTO cursor_state VALUES(?,?,?)",
            ("conversation", next_seq, json.dumps(snapshot)),
        )


def audit_trigger_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='trigger' AND tbl_name='cursor_reanchor_audit'"""
        )
    }


def create_audit_table_without_triggers(path) -> None:
    """Simulate a pre-existing database created before append-only hardening."""
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE cursor_reanchor_audit(
                   audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   conversation_id TEXT NOT NULL, operator_id TEXT NOT NULL,
                   reason_code TEXT NOT NULL, created_at TEXT NOT NULL,
                   expected_snapshot_sha256 TEXT NOT NULL,
                   observed_snapshot_sha256 TEXT NOT NULL,
                   replacement_snapshot_sha256 TEXT NOT NULL, next_seq INTEGER NOT NULL,
                   outcome TEXT NOT NULL CHECK(outcome IN ('applied','idempotent','cas_mismatch','outbox_not_settled'))
               )"""
        )
        connection.execute(
            """INSERT INTO cursor_reanchor_audit(
                   conversation_id,operator_id,reason_code,created_at,
                   expected_snapshot_sha256,observed_snapshot_sha256,
                   replacement_snapshot_sha256,next_seq,outcome
               ) VALUES('conversation','operator-1','QQ_RESTART_REANCHOR',
                        '2026-01-01T00:00:00+00:00',? ,? ,? ,1,'applied')""",
            ("a" * 64, "b" * 64, "c" * 64),
        )


def test_bootstrap_emits_only_final_inbound_and_is_idempotent() -> None:
    store = MessageCursorStore(":memory:")
    snapshot = [row("inbound", "old"), row("outbound", "reply"), row("inbound", "approved")]
    assert store.bootstrap_last_inbound_once("conversation", snapshot) == ("1",)
    claimed = store.claim("conversation")
    assert len(claimed) == 1 and '"approved"' in claimed[0]["payload_json"]
    assert store.connection.execute(
        "SELECT identity_schema FROM cursor_state WHERE conversation_id=?",
        ("conversation",),
    ).fetchone()[0] == CURRENT_IDENTITY_SCHEMA
    assert f"-v{IDENTITY_SCHEMA_VERSION}:" in CURRENT_IDENTITY_SCHEMA
    identity_schema_column = next(
        item
        for item in store.connection.execute("PRAGMA table_info(cursor_state)")
        if item["name"] == "identity_schema"
    )
    assert identity_schema_column["notnull"] == 0
    assert store.bootstrap_last_inbound_once(
        "conversation", snapshot + [row("outbound", "sent-after-bootstrap")]) == ()


def test_bootstrap_rejects_non_inbound_final_row_without_creating_state() -> None:
    store = MessageCursorStore(":memory:")
    with pytest.raises(ValueError, match="not_inbound"):
        store.bootstrap_last_inbound_once("conversation", [row("outbound", "last")])
    assert store.connection.execute("SELECT 1 FROM cursor_state").fetchone() is None


def test_cursor_alignment_ignores_recreated_qq_runtime_locator() -> None:
    store = MessageCursorStore(":memory:")
    first = [row("inbound", "anchor")]
    assert store.ingest_snapshot("conversation", first) == ()
    assert store.connection.execute(
        "SELECT identity_schema FROM cursor_state WHERE conversation_id=?",
        ("conversation",),
    ).fetchone()[0] == CURRENT_IDENTITY_SCHEMA
    after_restart = [
        {**first[0], "conversation_internal_id": "recreated-runtime-locator"},
        {**row("inbound", "new"), "conversation_internal_id": "recreated-runtime-locator"},
    ]
    assert store.ingest_snapshot("conversation", after_restart) == ("1",)


def test_reanchor_is_cas_protected_audited_and_idempotent() -> None:
    store = MessageCursorStore(":memory:")
    assert store.ingest_snapshot("conversation", [row("inbound", "old")]) == ()
    original = store.snapshot_token("conversation")
    assert original is not None
    result = store.reanchor_snapshot(
        "conversation", [row("inbound", "replacement")], expected_snapshot_sha256=original,
        operator_id="operator-1", reason_code="QQ_RESTART_REANCHOR",
    )
    assert result.applied is True and result.idempotent is False and result.next_seq == 1
    assert store.ingest_snapshot("conversation", [row("inbound", "replacement")]) == ()
    repeated = store.reanchor_snapshot(
        "conversation", [row("inbound", "replacement")], expected_snapshot_sha256=original,
        operator_id="operator-1", reason_code="QQ_RESTART_REANCHOR",
    )
    assert repeated.applied is False and repeated.idempotent is True and repeated.next_seq == 1
    audit = store.connection.execute("SELECT * FROM cursor_reanchor_audit ORDER BY audit_id").fetchall()
    assert [item["outcome"] for item in audit] == ["applied", "idempotent"]
    assert all(item["operator_id"] == "operator-1" and item["reason_code"] == "QQ_RESTART_REANCHOR" for item in audit)
    assert all(datetime.fromisoformat(item["created_at"]).tzinfo is not None for item in audit)
    assert all("replacement" not in "|".join(str(value) for value in item) for item in audit)
    with pytest.raises(RuntimeError, match="cas_mismatch"):
        store.reanchor_snapshot(
            "conversation", [row("inbound", "different")], expected_snapshot_sha256=original,
            operator_id="operator-1", reason_code="QQ_RESTART_REANCHOR",
        )
    assert store.connection.execute(
        "SELECT outcome FROM cursor_reanchor_audit ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()[0] == "cas_mismatch"


@pytest.mark.parametrize("claimed", [False, True])
def test_reanchor_refuses_pending_or_dispatching_outbox(claimed: bool) -> None:
    store = MessageCursorStore(":memory:")
    assert store.ingest_snapshot("conversation", [row("inbound", "anchor")]) == ()
    assert store.ingest_snapshot("conversation", [row("inbound", "anchor"), row("inbound", "pending")]) == ("1",)
    if claimed:
        assert len(store.claim("conversation")) == 1
    token = store.snapshot_token("conversation")
    assert token is not None
    with pytest.raises(RuntimeError, match="outbox_not_settled"):
        store.reanchor_snapshot("conversation", [row("inbound", "fresh")], expected_snapshot_sha256=token,
                                operator_id="operator-1", reason_code="QQ_RESTART_REANCHOR")
    audit = store.connection.execute("SELECT outcome,operator_id,reason_code,created_at FROM cursor_reanchor_audit").fetchone()
    assert audit[0:3] == ("outbox_not_settled", "operator-1", "QQ_RESTART_REANCHOR")
    assert datetime.fromisoformat(audit[3]).tzinfo is not None


@pytest.mark.parametrize("operator_id,reason_code", [("OP", "QQ_RESTART_REANCHOR"), ("operator-1", "bad-reason")])
def test_reanchor_rejects_noncanonical_audit_source(operator_id: str, reason_code: str) -> None:
    store = MessageCursorStore(":memory:")
    assert store.ingest_snapshot("conversation", [row("inbound", "anchor")]) == ()
    token = store.snapshot_token("conversation")
    with pytest.raises(ValueError, match="reanchor_(operator|reason)_invalid"):
        store.reanchor_snapshot("conversation", [row("inbound", "fresh")], expected_snapshot_sha256=token,
                                operator_id=operator_id, reason_code=reason_code)


def test_legacy_nonempty_cursor_requires_reanchor_then_restarts_without_replay(
    tmp_path, monkeypatch,
) -> None:
    path = tmp_path / "legacy-cursor.sqlite3"
    old_anchor = {
        **row("inbound", "anchor"),
        "conversation_internal_id": "legacy-runtime-locator",
    }
    create_legacy_cursor(path, next_seq=7, snapshot=[legacy_identity(old_anchor)])

    store = MessageCursorStore(path)
    current = [
        {
            **old_anchor,
            "conversation_internal_id": "recreated-runtime-locator",
        }
    ]
    before = tuple(
        store.connection.execute(
            """SELECT next_seq,snapshot_json,identity_schema FROM cursor_state
               WHERE conversation_id=?""",
            ("conversation",),
        ).fetchone()
    )
    alignment_attempted = False

    def unexpected_alignment(_before, _after):
        nonlocal alignment_attempted
        alignment_attempted = True
        raise AssertionError("legacy cursor reached current-schema alignment")

    monkeypatch.setattr(message_cursor, "unique_suffix_start", unexpected_alignment)

    with pytest.raises(
        ValueError, match="^message_cursor_schema_migration_required$"
    ):
        store.ingest_snapshot("conversation", current)

    assert alignment_attempted is False
    after_failure = tuple(
        store.connection.execute(
            """SELECT next_seq,snapshot_json,identity_schema FROM cursor_state
               WHERE conversation_id=?""",
            ("conversation",),
        ).fetchone()
    )
    assert after_failure == before
    assert store.connection.execute(
        "SELECT COUNT(*) FROM observation_outbox WHERE conversation_id=?",
        ("conversation",),
    ).fetchone()[0] == 0
    monkeypatch.undo()

    legacy_token = store.snapshot_token("conversation")
    assert legacy_token is not None
    migrated = store.reanchor_snapshot(
        "conversation",
        current,
        expected_snapshot_sha256=legacy_token,
        operator_id="operator-1",
        reason_code="IDENTITY_SCHEMA_MIGRATION",
    )
    assert migrated.applied is True
    assert migrated.next_seq == 7
    assert store.connection.execute(
        "SELECT identity_schema FROM cursor_state WHERE conversation_id=?",
        ("conversation",),
    ).fetchone()[0] == CURRENT_IDENTITY_SCHEMA
    assert store.ingest_snapshot("conversation", current) == ()
    assert store.connection.execute(
        "SELECT COUNT(*) FROM observation_outbox WHERE conversation_id=?",
        ("conversation",),
    ).fetchone()[0] == 0

    repeated = store.reanchor_snapshot(
        "conversation",
        current,
        expected_snapshot_sha256=legacy_token,
        operator_id="operator-1",
        reason_code="IDENTITY_SCHEMA_MIGRATION",
    )
    assert repeated.applied is False
    assert repeated.idempotent is True
    store.close()

    restarted = MessageCursorStore(path)
    assert restarted.ingest_snapshot("conversation", current) == ()
    fresh = [*current, row("inbound", "fresh")]
    assert restarted.ingest_snapshot("conversation", fresh) == ("7",)
    claimed = restarted.claim("conversation")
    assert [item["local_key"] for item in claimed] == ["7"]
    assert [json.loads(item["payload_json"])["text"] for item in claimed] == ["fresh"]


def test_snapshot_token_binds_schema_even_when_identities_are_unchanged() -> None:
    store = MessageCursorStore(":memory:")
    snapshot = [row("inbound", "anchor")]
    assert store.ingest_snapshot("conversation", snapshot) == ()
    current_token = store.snapshot_token("conversation")
    store.connection.execute(
        "UPDATE cursor_state SET identity_schema=NULL WHERE conversation_id=?",
        ("conversation",),
    )
    legacy_token = store.snapshot_token("conversation")
    assert legacy_token != current_token

    result = store.reanchor_snapshot(
        "conversation",
        snapshot,
        expected_snapshot_sha256=legacy_token,
        operator_id="operator-1",
        reason_code="IDENTITY_SCHEMA_MIGRATION",
    )
    assert result.applied is True
    assert result.snapshot_sha256 == current_token


def test_legacy_empty_cursor_adopts_only_from_an_empty_complete_snapshot(
    tmp_path,
) -> None:
    safe_path = tmp_path / "legacy-empty-safe.sqlite3"
    create_legacy_cursor(safe_path, next_seq=1, snapshot=[])
    safe = MessageCursorStore(safe_path)
    assert safe.ingest_snapshot("conversation", []) == ()
    assert safe.connection.execute(
        "SELECT identity_schema FROM cursor_state WHERE conversation_id=?",
        ("conversation",),
    ).fetchone()[0] == CURRENT_IDENTITY_SCHEMA

    unsafe_path = tmp_path / "legacy-empty-unsafe.sqlite3"
    create_legacy_cursor(unsafe_path, next_seq=1, snapshot=[])
    unsafe = MessageCursorStore(unsafe_path)
    with pytest.raises(
        ValueError, match="^message_cursor_schema_migration_required$"
    ):
        unsafe.ingest_snapshot("conversation", [row("inbound", "unanchored")])
    assert unsafe.connection.execute(
        "SELECT identity_schema FROM cursor_state WHERE conversation_id=?",
        ("conversation",),
    ).fetchone()[0] is None
    assert unsafe.connection.execute(
        "SELECT COUNT(*) FROM observation_outbox WHERE conversation_id=?",
        ("conversation",),
    ).fetchone()[0] == 0


def test_reanchor_audit_installs_append_only_triggers() -> None:
    store = MessageCursorStore(":memory:")
    assert audit_trigger_names(store.connection) == {
        "cursor_reanchor_audit_no_update",
        "cursor_reanchor_audit_no_delete",
    }


def test_reanchor_audit_rejects_update_and_delete_but_allows_insert() -> None:
    store = MessageCursorStore(":memory:")
    assert store.ingest_snapshot("conversation", [row("inbound", "old")]) == ()
    token = store.snapshot_token("conversation")
    assert token is not None
    assert store.reanchor_snapshot(
        "conversation", [row("inbound", "replacement")],
        expected_snapshot_sha256=token,
        operator_id="operator-1", reason_code="QQ_RESTART_REANCHOR",
    ).applied is True
    assert store.connection.execute(
        "SELECT COUNT(*) FROM cursor_reanchor_audit"
    ).fetchone()[0] == 1

    with pytest.raises(sqlite3.IntegrityError, match="cursor_reanchor_audit_append_only"):
        store.connection.execute(
            "UPDATE cursor_reanchor_audit SET outcome='idempotent' WHERE audit_id=1"
        )
    with pytest.raises(sqlite3.IntegrityError, match="cursor_reanchor_audit_append_only"):
        store.connection.execute("DELETE FROM cursor_reanchor_audit WHERE audit_id=1")

    # The row that mutation attempts failed to touch is intact, and a normal
    # append (the only sanctioned write) still succeeds.
    surviving = store.connection.execute(
        "SELECT audit_id,outcome FROM cursor_reanchor_audit ORDER BY audit_id"
    ).fetchall()
    assert [(item["audit_id"], item["outcome"]) for item in surviving] == [(1, "applied")]
    store.connection.execute(
        """INSERT INTO cursor_reanchor_audit(
               conversation_id,operator_id,reason_code,created_at,
               expected_snapshot_sha256,observed_snapshot_sha256,
               replacement_snapshot_sha256,next_seq,outcome
           ) VALUES('conversation','operator-1','QQ_RESTART_REANCHOR',
                    '2026-01-01T00:00:00+00:00',?,?,?,1,'applied')""",
        ("a" * 64, "b" * 64, "c" * 64),
    )
    assert store.connection.execute(
        "SELECT COUNT(*) FROM cursor_reanchor_audit"
    ).fetchone()[0] == 2


def test_reanchor_audit_triggers_migrated_onto_existing_database(tmp_path) -> None:
    path = tmp_path / "legacy-audit.sqlite3"
    create_audit_table_without_triggers(path)
    with sqlite3.connect(path) as connection:
        assert audit_trigger_names(connection) == set()
        before = connection.execute(
            "SELECT audit_id,operator_id,outcome FROM cursor_reanchor_audit"
        ).fetchall()

    store = MessageCursorStore(path)
    try:
        assert audit_trigger_names(store.connection) == {
            "cursor_reanchor_audit_no_update",
            "cursor_reanchor_audit_no_delete",
        }
        # The pre-existing append is preserved, not rewritten by the migration.
        assert [
            tuple(item)
            for item in store.connection.execute(
                "SELECT audit_id,operator_id,outcome FROM cursor_reanchor_audit"
            )
        ] == before
        with pytest.raises(sqlite3.IntegrityError, match="cursor_reanchor_audit_append_only"):
            store.connection.execute(
                "UPDATE cursor_reanchor_audit SET outcome='idempotent' WHERE audit_id=1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cursor_reanchor_audit_append_only"):
            store.connection.execute("DELETE FROM cursor_reanchor_audit WHERE audit_id=1")
    finally:
        store.close()


def test_reanchor_audit_trigger_installation_is_idempotent_on_reinit(tmp_path) -> None:
    path = tmp_path / "reinit-audit.sqlite3"
    first = MessageCursorStore(path)
    first.close()
    # Reopening an already hardened database must not raise (IF NOT EXISTS) and
    # must leave exactly one pair of enforcement triggers installed.
    second = MessageCursorStore(path)
    try:
        assert audit_trigger_names(second.connection) == {
            "cursor_reanchor_audit_no_update",
            "cursor_reanchor_audit_no_delete",
        }
        # Triggers are per-row, so seed a row before probing enforcement.
        second.connection.execute(
            """INSERT INTO cursor_reanchor_audit(
                   conversation_id,operator_id,reason_code,created_at,
                   expected_snapshot_sha256,observed_snapshot_sha256,
                   replacement_snapshot_sha256,next_seq,outcome
               ) VALUES('conversation','operator-1','QQ_RESTART_REANCHOR',
                        '2026-01-01T00:00:00+00:00',?,?,?,1,'applied')""",
            ("a" * 64, "b" * 64, "c" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError, match="cursor_reanchor_audit_append_only"):
            second.connection.execute("DELETE FROM cursor_reanchor_audit WHERE audit_id=1")
    finally:
        second.close()
