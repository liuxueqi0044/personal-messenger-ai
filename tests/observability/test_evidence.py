from __future__ import annotations

import sqlite3

import pytest

from messenger_ai.observability import EvidenceExpired, EvidenceNotFound, EvidenceVault


def test_evidence_content_is_separate_from_metadata_and_access_is_audited(
    tmp_path, memory_secrets, clock
):
    database = tmp_path / "evidence.sqlite"
    vault = EvidenceVault(database, memory_secrets, clock.now, default_ttl_seconds=30)
    content = b"private-pixel-content"
    reference = vault.put("conversation-alice", content, media_type="image/png")

    assert vault.get(reference.evidence_ref) == content
    assert content not in database.read_bytes()
    with sqlite3.connect(database) as connection:
        conversation_value = connection.execute(
            "SELECT conversation_hash FROM evidence_metadata"
        ).fetchone()[0]
    assert conversation_value != "conversation-alice"
    assert [item.action for item in vault.audit_entries(reference.evidence_ref)] == [
        "create",
        "read",
    ]


def test_expired_evidence_is_deleted_and_never_returned(
    tmp_path, memory_secrets, clock
):
    vault = EvidenceVault(tmp_path / "evidence.sqlite", memory_secrets, clock.now)
    reference = vault.put("conversation-1", b"pixels", ttl_seconds=2)
    secret_name = f"evidence.{reference.evidence_ref}"
    clock.advance(2)
    with pytest.raises(EvidenceExpired):
        vault.get(reference.evidence_ref)
    assert secret_name not in memory_secrets.values
    with pytest.raises(EvidenceNotFound):
        vault.get(reference.evidence_ref)


def test_clear_by_conversation_and_clear_all_are_scoped(
    tmp_path, memory_secrets, clock
):
    vault = EvidenceVault(tmp_path / "evidence.sqlite", memory_secrets, clock.now)
    first = vault.put("conversation-1", b"first")
    second = vault.put("conversation-2", b"second")
    assert vault.clear_conversation("conversation-1") == 1
    with pytest.raises(EvidenceNotFound):
        vault.get(first.evidence_ref)
    assert vault.get(second.evidence_ref) == b"second"
    assert vault.clear_all() == 1
    with pytest.raises(EvidenceNotFound):
        vault.get(second.evidence_ref)


def test_cleanup_expired_and_ttl_ceiling(tmp_path, memory_secrets, clock):
    vault = EvidenceVault(tmp_path / "evidence.sqlite", memory_secrets, clock.now)
    vault.put("conversation-1", b"short", ttl_seconds=1)
    vault.put("conversation-2", b"long", ttl_seconds=10)
    clock.advance(2)
    assert vault.cleanup_expired() == 1
    with pytest.raises(ValueError):
        vault.put("conversation-3", b"bad", ttl_seconds=86_401)
