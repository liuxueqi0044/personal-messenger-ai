"""Durable RuntimeState/Hub/M10 projection for the local WebUI."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from messenger_ai.domain import SendStatus

from .state import RuntimeState


class RuntimeWebUIProjection:
    def __init__(self, *, state: RuntimeState, hub: Any, pacing: Any | None = None, rules: Any | None = None, driver: Any | None = None) -> None:
        self.state, self.hub, self.pacing, self.rules, self.driver = state, hub, pacing, rules, driver
        self.state.connection.executescript("""
        CREATE TABLE IF NOT EXISTS runtime_webui_reviews(operation_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, expected_revision INTEGER NOT NULL, reviewed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runtime_webui_commands(idempotency_key TEXT PRIMARY KEY, command_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
        """)

    @property
    def _db(self) -> sqlite3.Connection:
        return self.state.connection

    def _rows(self) -> list[sqlite3.Row]:
        return list(self._db.execute("SELECT * FROM runtime_conversations ORDER BY conversation_id"))

    def _hub(self, conversation_id: str) -> dict[str, Any]:
        try:
            return dict(self.hub.conversation_projection(conversation_id))
        except (AttributeError, KeyError):
            return {"conversation": {}, "messages": [], "drafts": []}

    def _operations(self, conversation_id: str) -> list[dict[str, Any]]:
        conn = getattr(getattr(self.hub, "store", None), "connection", None)
        if conn is None:
            return []
        rows = conn.execute("""SELECT s.operation_id,s.status,s.error_code,s.version
            FROM send_operations s JOIN drafts d ON d.draft_id=s.draft_id
            WHERE d.conversation_id=? AND s.status=? ORDER BY s.updated_at""", (conversation_id, SendStatus.UNCERTAIN.value)).fetchall()
        reviewed = {r["operation_id"] for r in self._db.execute("SELECT operation_id FROM runtime_webui_reviews WHERE conversation_id=?", (conversation_id,))}
        return [{"operation_id": r["operation_id"], "status": r["status"], "error_code": r["error_code"], "expected_revision": int(r["version"]), "expected_operation_revision": int(r["version"]), "reviewed": r["operation_id"] in reviewed} for r in rows]

    def _m10_plan(self, conversation_id: str) -> dict[str, Any]:
        if self.pacing is None:
            return {}
        row = self.pacing.connection.execute("SELECT * FROM m10_plans WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone()
        return dict(row) if row else {}

    def _contact(self, row: sqlite3.Row) -> dict[str, Any]:
        cid = row["conversation_id"]
        hub = self._hub(cid)
        conversation = hub.get("conversation", {})
        reason = row["pause_reason"] or ""
        plan = self._m10_plan(cid)
        ops = self._operations(cid)
        return {
            "contact_id": row["contact_id"], "display_name": conversation.get("contact_id", row["contact_id"]),
            "platform": conversation.get("platform", "unknown"), "binding_status": "paused" if reason == "binding_revision_changed" else "bound",
            "binding_expires_at": "", "last_observed_at": row["last_observed_at"] or "", "plan_status": plan.get("status", "not_configured"),
            "pause_status": "paused" if row["paused"] else "running", "paused": bool(row["paused"]), "pause_reason": reason,
            "health": "paused" if row["paused"] else "unknown", "uncertain": bool(ops), "uncertain_operations": ops,
            "revision": int(row["conversation_revision"]), "binding_revision": int(row["binding_revision"]), "conversation_id": cid,
        }

    def webui_page(self, name: str, entity_id: str | None = None) -> dict[str, Any]:
        rows = self._rows(); contacts = [self._contact(r) for r in rows]; paused = bool(rows) and all(r["paused"] for r in rows)
        if name == "contacts": return {"contacts": contacts, "paused": paused, "revision": max((c["revision"] for c in contacts), default=1)}
        if name == "contact":
            item = next((c for c in contacts if c["contact_id"] == entity_id), None)
            if item is None: raise KeyError(entity_id)
            return {"contact": item, "paused": item["paused"]}
        if name == "inbox":
            values = []
            for r in rows:
                h = self._hub(r["conversation_id"]); c = dict(h.get("conversation", {})); c.update({"conversation_id": r["conversation_id"], "contact_id": r["contact_id"], "paused": bool(r["paused"]), "revision": int(r["conversation_revision"])}); c.setdefault("unread", 0); c.setdefault("last_message_at", r["last_observed_at"] or ""); values.append(c)
            return {"conversations": values, "contacts": contacts, "paused": paused, "revision": max((c["revision"] for c in contacts), default=1)}
        if name == "conversation":
            r = next((r for r in rows if r["conversation_id"] == entity_id), None)
            if r is None: raise KeyError(entity_id)
            h = self._hub(entity_id or ""); c = dict(h.get("conversation", {})); c.update({"conversation_id": entity_id, "contact_id": r["contact_id"], "version": int(r["conversation_revision"])}); c.setdefault("platform", "unknown")
            return {"conversation": c, "messages": h.get("messages", []), "drafts": h.get("drafts", []), "plan": self._m10_plan(entity_id or ""), "paused": bool(r["paused"])}
        if name == "reviews": return {"drafts": [d for r in rows for d in self._hub(r["conversation_id"]).get("drafts", [])], "paused": paused}
        if name == "pacing": return {"profile": {}, "plans": [dict(r) for r in self.pacing.connection.execute("SELECT * FROM m10_plans ORDER BY created_at")] if self.pacing is not None else [], "paused": paused}
        if name == "adapters":
            health = str(self.driver.health()) if self.driver is not None and callable(getattr(self.driver, "health", None)) else "unknown"
            return {"adapters": [{"platform": "qq", "status": health, "background_send": "unknown"}], "paused": paused}
        if name == "incidents": return {"incidents": [{"id": c["conversation_id"], "status": c["pause_reason"], "summary": c["pause_reason"]} for c in contacts if c["pause_reason"]], "paused": paused}
        if name == "audit": return {"entries": [], "paused": paused}
        if name == "rules":
            if self.rules is None: return {"rule_version": "", "status": "not_configured", "paused": paused}
            value = self.rules.webui_snapshot() if callable(getattr(self.rules, "webui_snapshot", None)) else {}
            return {**value, "paused": paused}
        if name == "settings": return {"automation": "runtime", "retention": "local", "paused": paused}
        raise KeyError(name)

    def _record(self, key: str, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        self._db.execute("INSERT INTO runtime_webui_commands VALUES(?,?,?,?)", (key, json.dumps(payload, sort_keys=True), json.dumps(result, sort_keys=True), datetime.now(UTC).isoformat())); return result

    def _replay(self, key: str, name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        row = self._db.execute("SELECT command_json,result_json FROM runtime_webui_commands WHERE idempotency_key=?", (key,)).fetchone()
        if not row: return None
        old = json.loads(row["command_json"])
        if old.get("name") != name or {k: v for k, v in old.items() if k not in {"name", "idempotency_key"}} != {k: v for k, v in payload.items() if k != "idempotency_key"}: raise ValueError("idempotency_conflict")
        return {**json.loads(row["result_json"]), "replayed": True}

    def webui_command(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("idempotency_key", ""));
        if not key: raise ValueError("idempotency_key_required")
        replay = self._replay(key, name, payload)
        if replay is not None: return replay
        payload = {**payload, "name": name}; entity = str(payload.get("entity_id", "")); expected = payload.get("expected_revision", payload.get("entity_version"))
        if expected is None: raise ValueError("expected_revision_required")
        expected = int(expected)
        if name in {"pause", "resume"} and entity == "global":
            method = getattr(self.state, "pause_global" if name == "pause" else "resume_global", None)
            if not callable(method): raise ValueError("global_pause_requires_runtime_control")
            if method(expected_revision=expected) is False: raise ValueError("stale_version")
            result = {"accepted": True, "command": name, "command_id": key, "entity_id": entity, "changed": [r["conversation_id"] for r in self._rows()], "replayed": False}; return self._record(key, payload, result)
        targets = [r for r in self._rows() if r["conversation_id"] == entity or r["contact_id"] == entity]
        if not targets: raise ValueError("contact_not_found")
        if name in {"pause_contact", "resume_contact", "ack_uncertain"} and len(targets) != 1: raise ValueError("contact_ambiguous")
        changed = []
        for r in targets:
            if name in {"pause_contact", "resume_contact"} and int(r["conversation_revision"]) != expected: raise ValueError("stale_version")
            if name == "pause_contact": self.state.pause(r["conversation_id"])
            elif name == "resume_contact":
                if not self.state.resume(r["conversation_id"], expected_revision=expected): raise ValueError("resume_rejected")
            elif name == "ack_uncertain":
                if int(payload.get("expected_contact_revision", payload.get("entity_version", -1))) != int(r["conversation_revision"]): raise ValueError("stale_version")
                op = str(payload.get("operation_id", "")); oprev = int(payload.get("expected_operation_revision", payload.get("expected_revision", -1)))
                if not any(x["operation_id"] == op and x["expected_operation_revision"] == oprev for x in self._operations(r["conversation_id"])): raise ValueError("uncertain_operation_not_found")
                self._db.execute("INSERT OR IGNORE INTO runtime_webui_reviews VALUES(?,?,?,?)", (op, r["conversation_id"], oprev, datetime.now(UTC).isoformat()))
            else: raise ValueError("unknown_command")
            changed.append(r["conversation_id"])
        return self._record(key, payload, {"accepted": True, "command": name, "command_id": key, "entity_id": entity, "changed": changed, "replayed": False})
