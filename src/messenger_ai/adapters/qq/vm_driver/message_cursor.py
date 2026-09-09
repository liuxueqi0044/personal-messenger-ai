from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def _identity(row: dict[str, object]) -> str:
    payload = {key: row.get(key) for key in ("direction", "text", "message_key", "conversation_internal_id")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class MessageCursorStore:
    """Transactional per-conversation snapshot, sequence and delivery outbox."""

    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(str(path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS cursor_state(
          conversation_id TEXT PRIMARY KEY, next_seq INTEGER NOT NULL, snapshot_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observation_outbox(
          outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
          local_key TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          UNIQUE(conversation_id,local_key)
        );
        """)

    def ingest_snapshot(self, conversation_id: str, bubbles: list[dict[str, object]]) -> tuple[str, ...]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT * FROM cursor_state WHERE conversation_id=?", (conversation_id,)).fetchone()
            identities = [_identity(item) for item in bubbles]
            if row is None:
                self.connection.execute("INSERT INTO cursor_state VALUES(?,?,?)", (conversation_id, 1, json.dumps(identities)))
                self.connection.execute("COMMIT")
                return ()  # first observation establishes a baseline
            before = json.loads(row["snapshot_json"])
            if not before:
                longest = 0
            else:
                matches = [overlap for overlap in range(1, min(len(before), len(identities)) + 1)
                           if before[-overlap:] == identities[:overlap]]
                if not matches:
                    raise ValueError("message_anchor_gap")
                longest = max(matches)
                if len(matches) > 1 and len(set(before[-longest:])) == 1:
                    raise ValueError("message_anchor_ambiguous")
            next_seq = int(row["next_seq"])
            keys: list[str] = []
            for bubble in bubbles[longest:]:
                key = str(next_seq); next_seq += 1
                self.connection.execute("INSERT INTO observation_outbox(conversation_id,local_key,payload_json) VALUES(?,?,?)",
                                        (conversation_id, key, json.dumps(bubble, sort_keys=True, ensure_ascii=False)))
                keys.append(key)
            self.connection.execute("UPDATE cursor_state SET next_seq=?,snapshot_json=? WHERE conversation_id=?",
                                    (next_seq, json.dumps(identities), conversation_id))
            self.connection.execute("COMMIT")
            return tuple(keys)
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def claim(self, conversation_id: str) -> list[sqlite3.Row]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute("SELECT * FROM observation_outbox WHERE conversation_id=? AND status='pending' ORDER BY outbox_id", (conversation_id,)).fetchall()
            self.connection.executemany("UPDATE observation_outbox SET status='dispatching' WHERE outbox_id=?", [(r["outbox_id"],) for r in rows])
            self.connection.execute("COMMIT")
            return rows
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def acknowledge(self, outbox_id: int) -> bool:
        return bool(self.connection.execute("UPDATE observation_outbox SET status='delivered' WHERE outbox_id=? AND status='dispatching'", (outbox_id,)).rowcount)

    def acknowledge_keys(self, conversation_id: str, local_keys: tuple[str, ...]) -> int:
        if not local_keys:
            return 0
        placeholders = ",".join("?" for _ in local_keys)
        return self.connection.execute(
            f"UPDATE observation_outbox SET status='delivered' WHERE conversation_id=? AND local_key IN ({placeholders}) AND status='dispatching'",
            (conversation_id, *local_keys),
        ).rowcount

    def recover(self) -> int:
        return self.connection.execute("UPDATE observation_outbox SET status='pending' WHERE status='dispatching'").rowcount
