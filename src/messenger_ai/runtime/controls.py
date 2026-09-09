"""Transactional WebUI command controls over the durable runtime state."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from messenger_ai.domain import SendStatus

from .state import RuntimeState


class AtomicRuntimeControls:
    """Own command CAS/idempotency and runtime-side review records.

    Contact and global state changes are committed through the RuntimeState
    SQLite connection.  A command replay is stable; a reused key with changed
    name or payload is rejected.
    """

    def __init__(self, *, state: RuntimeState, hub: Any) -> None:
        self.state, self.hub = state, hub
        self.state.connection.executescript("""
        CREATE TABLE IF NOT EXISTS runtime_control_commands(
          idempotency_key TEXT PRIMARY KEY, name TEXT NOT NULL, payload_hash TEXT NOT NULL,
          result_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runtime_control_reviews(
          operation_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
          operation_revision INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        """)

    @staticmethod
    def _hash(name: str, payload: dict[str, Any]) -> str:
        clean = {k: v for k, v in payload.items() if k != "idempotency_key"}
        return hashlib.sha256(json.dumps([name, clean], sort_keys=True, default=str).encode()).hexdigest()

    def _existing(self, db: sqlite3.Connection, key: str, name: str, digest: str) -> dict[str, Any] | None:
        row = db.execute("SELECT name,payload_hash,result_json FROM runtime_control_commands WHERE idempotency_key=?", (key,)).fetchone()
        if row is None: return None
        if row["name"] != name or row["payload_hash"] != digest: raise ValueError("idempotency_conflict")
        return {**json.loads(row["result_json"]), "replayed": True}

    def _finish(self, db: sqlite3.Connection, key: str, name: str, digest: str, result: dict[str, Any]) -> dict[str, Any]:
        db.execute("INSERT INTO runtime_control_commands VALUES(?,?,?,?,?)", (key, name, digest, json.dumps(result, sort_keys=True), datetime.now(UTC).isoformat()))
        return result

    def _operations(self, conversation_id: str) -> list[sqlite3.Row]:
        conn = getattr(getattr(self.hub, "store", None), "connection", None)
        if conn is None: return []
        return conn.execute("""SELECT s.operation_id,s.version FROM send_operations s JOIN drafts d ON d.draft_id=s.draft_id WHERE d.conversation_id=? AND s.status=?""", (conversation_id, SendStatus.UNCERTAIN.value)).fetchall()

    def command(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("idempotency_key", ""))
        if not key: raise ValueError("idempotency_key_required")
        digest = self._hash(name, payload)
        with self.state.uow() as db:
            replay = self._existing(db, key, name, digest)
            if replay is not None: return replay
            expected = payload.get("expected_revision", payload.get("entity_version"))
            if expected is None: raise ValueError("expected_revision_required")
            expected = int(expected); entity = str(payload.get("entity_id", ""))
            if name in {"pause", "resume"} and entity == "global":
                control = db.execute("SELECT revision FROM runtime_global_control WHERE singleton=1").fetchone()
                if control is None or int(control["revision"]) != expected: raise ValueError("stale_version")
                changed = db.execute("UPDATE runtime_global_control SET paused=?,reason=?,revision=revision+1 WHERE singleton=1 AND revision=?", (int(name == "pause"), "manual_global_pause" if name == "pause" else None, expected)).rowcount
                if changed != 1: raise ValueError("stale_version")
                revision = expected + 1
                db.execute("INSERT INTO runtime_event_outbox(dedupe_key,event_type,aggregate_id,payload_json,created_at) VALUES(?,?,?,?,?)", (f"global_pause:{revision}", "global_pause" if name == "pause" else "global_resume", "global", json.dumps({"paused": name == "pause", "reason": "manual_global_pause", "revision": revision}), datetime.now(UTC).isoformat()))
                return self._finish(db, key, name, digest, {"accepted": True, "command": name, "command_id": key, "entity_id": entity, "replayed": False})
            rows = db.execute("SELECT * FROM runtime_conversations WHERE conversation_id=? OR contact_id=?", (entity, entity)).fetchall()
            if len(rows) != 1: raise ValueError("contact_not_found" if not rows else "contact_ambiguous")
            row = rows[0]; cid = row["conversation_id"]
            if name == "pause_contact":
                if int(row["conversation_revision"]) != expected: raise ValueError("stale_version")
                revision = expected + 1
                if db.execute("UPDATE runtime_conversations SET paused=1,pause_reason='manual_pause',conversation_revision=? WHERE conversation_id=? AND conversation_revision=?", (revision, cid, expected)).rowcount != 1: raise ValueError("stale_version")
                db.execute("INSERT INTO runtime_event_outbox(dedupe_key,event_type,aggregate_id,payload_json,created_at) VALUES(?,?,?,?,?)", (f"contact_pause:{cid}:{revision}", "contact_pause", cid, json.dumps({"reason": "manual_pause", "revision": revision}), datetime.now(UTC).isoformat()))
            elif name == "resume_contact":
                revision = expected + 1
                if db.execute("UPDATE runtime_conversations SET paused=0,pause_reason=NULL,conversation_revision=? WHERE conversation_id=? AND conversation_revision=? AND pause_reason='manual_pause'", (revision, cid, expected)).rowcount != 1: raise ValueError("resume_rejected")
                db.execute("INSERT INTO runtime_event_outbox(dedupe_key,event_type,aggregate_id,payload_json,created_at) VALUES(?,?,?,?,?)", (f"contact_resume:{cid}:{revision}", "contact_resume", cid, json.dumps({"revision": revision}), datetime.now(UTC).isoformat()))
            elif name == "ack_uncertain":
                if int(payload.get("expected_contact_revision", -1)) != int(row["conversation_revision"]): raise ValueError("stale_version")
                op = str(payload.get("operation_id", "")); oprev = int(payload.get("expected_operation_revision", -1))
                if not any(str(item["operation_id"]) == op and int(item["version"]) == oprev for item in self._operations(cid)): raise ValueError("uncertain_operation_not_found")
                db.execute("INSERT OR IGNORE INTO runtime_control_reviews VALUES(?,?,?,?)", (op, cid, oprev, datetime.now(UTC).isoformat()))
            else: raise ValueError("unknown_command")
            return self._finish(db, key, name, digest, {"accepted": True, "command": name, "command_id": key, "entity_id": entity, "replayed": False})
