from __future__ import annotations

import json
import sqlite3

import pytest

from messenger_ai.observability import BackupError, BackupManager, RestoreApproval


class FakeRecovery:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def pause_all(self, reason_code):
        self.calls.append(("pause", reason_code))

    def invalidate_all_authorizations(self, reason_code):
        self.calls.append(("invalidate", reason_code))
        return 3

    def cancel_expired_pacing(self, now):
        self.calls.append(("cancel_expired", now))
        return 2

    def mark_due_pacing_for_review(self, now):
        self.calls.append(("review_due", now))
        return 4


def make_database(path, value="private message content"):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO messages(body) VALUES (?)", (value,))


def approval(clock):
    return RestoreApproval(
        approver_id="local-user",
        reason="Explicitly restore the verified snapshot",
        approved_at=clock.now(),
    )


def test_protected_snapshot_manifest_has_no_database_content_and_restore_is_guarded(
    tmp_path, memory_secrets, clock
):
    source = tmp_path / "source.sqlite"
    destination = tmp_path / "restored.sqlite"
    make_database(source)
    manager = BackupManager(memory_secrets, clock.now)
    manifest = manager.create_snapshot(
        source, tmp_path / "manifests", database_schema_version="m1-v1"
    )
    assert "private message content" not in manifest.read_text(encoding="utf-8")

    recovery = FakeRecovery()
    report = manager.restore(
        manifest,
        destination,
        approval=approval(clock),
        recovery=recovery,
    )
    with sqlite3.connect(destination) as connection:
        restored = connection.execute("SELECT body FROM messages").fetchone()[0]
    assert restored == "private message content"
    assert report.authorizations_invalidated == 3
    assert report.expired_pacing_cancelled == 2
    assert report.due_pacing_marked_for_review == 4
    assert report.system_paused and not report.bulk_dispatch_allowed
    assert [item[0] for item in recovery.calls] == [
        "pause",
        "invalidate",
        "cancel_expired",
        "review_due",
    ]


def test_manifest_tampering_fails_before_restore(tmp_path, memory_secrets, clock):
    source = tmp_path / "source.sqlite"
    make_database(source)
    manager = BackupManager(memory_secrets, clock.now)
    manifest = manager.create_snapshot(source, tmp_path, database_schema_version="v1")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["size_bytes"] += 1
    manifest.write_text(json.dumps(document), encoding="utf-8")
    recovery = FakeRecovery()
    with pytest.raises(BackupError, match="authentication"):
        manager.restore(
            manifest,
            tmp_path / "restore.sqlite",
            approval=approval(clock),
            recovery=recovery,
        )
    assert recovery.calls == []


def test_snapshot_tampering_fails_integrity_check(tmp_path, memory_secrets, clock):
    source = tmp_path / "source.sqlite"
    make_database(source)
    manager = BackupManager(memory_secrets, clock.now)
    manifest_file = manager.create_snapshot(
        source, tmp_path, database_schema_version="v1"
    )
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    memory_secrets.values[manifest["storage_ref"]] += b"tamper"
    recovery = FakeRecovery()
    with pytest.raises(BackupError, match="integrity"):
        manager.restore(
            manifest_file,
            tmp_path / "restore.sqlite",
            approval=approval(clock),
            recovery=recovery,
        )
    assert recovery.calls == []
