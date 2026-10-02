from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import sqlite3
from uuid import uuid4

import pytest

from messenger_ai.domain.models import ReplyPlan
from messenger_ai.pacing import PacingScheduler
from messenger_ai.pacing.models import DraftSnapshot, ScheduleRequest
from messenger_ai.testing import FakeClock


NOW = datetime(2026, 10, 2, tzinfo=UTC)


def fixture(path=":memory:", *, one_shot=None, recoverable=True):
    clock = FakeClock(NOW)
    scheduler = PacingScheduler(path, clock=clock)
    body = "FirstSecond"
    plan = scheduler.schedule(ScheduleRequest(
        draft=DraftSnapshot(conversation_id="conversation", contact_id="contact", text=body,
                            text_hash=sha256(body.encode()).hexdigest(), expected_last_message_key="key-2",
                            rule_version="rules-v1", eligibility_id="eligibility"),
        reply_plan=ReplyPlan(action="draft", reply_text=body, reply_segments=["First", "Second"]),
        source_message_keys=("key-1", "key-2"), first_inbound_at=NOW, last_inbound_at=NOW,
        one_shot_attempt_id=one_shot,
    )).plan
    clock.advance(30)
    scheduler.due_for_revalidation(pacing_plan_id=plan.pacing_plan_id, one_shot_attempt_id=one_shot)
    claim = scheduler.claim_due_outbox_with_tokens(
        pacing_plan_id=plan.pacing_plan_id, segment_index=0, one_shot_attempt_id=one_shot,
        recoverable=recoverable,
    )[0]
    return scheduler, clock, claim


def test_compatible_migration_preserves_all_historical_columns_and_operation_facts(tmp_path):
    path = tmp_path / "pacing.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE m10_due_outbox(
        outbox_id INTEGER PRIMARY KEY, pacing_plan_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
        payload_json TEXT NOT NULL,status TEXT NOT NULL,operation_id TEXT,claimed_at TEXT,
        delivered_at TEXT,created_at TEXT NOT NULL,one_shot_attempt_id TEXT,
        UNIQUE(pacing_plan_id,segment_index))""")
    db.execute("INSERT INTO m10_due_outbox VALUES(1,'historical-plan',0,'{}','delivered','operation','claimed','delivered','created',NULL)")
    db.commit()
    before = db.execute("SELECT * FROM m10_due_outbox").fetchall()
    columns = [row[1] for row in db.execute("PRAGMA table_info(m10_due_outbox)")]
    scheduler = PacingScheduler(path, clock=FakeClock(NOW))
    assert db.execute("SELECT " + ",".join(columns) + " FROM m10_due_outbox").fetchall() == before
    assert db.execute("SELECT not_before,claim_token,defer_reason FROM m10_due_outbox").fetchall() == [(None, None, None)]
    scheduler.connection.close()
    db.close()


@pytest.mark.parametrize("recoverable,one_shot", [(True, None), (False, uuid4())])
def test_deferral_keeps_same_segment_payload_source_keys_and_expiry(recoverable, one_shot):
    scheduler, clock, claim = fixture(one_shot=one_shot, recoverable=recoverable)
    original = tuple(scheduler.connection.execute(
        "SELECT payload_json,expires_at,status FROM m10_plans").fetchone())
    rate_events = scheduler.connection.execute("SELECT COUNT(*) FROM m10_rate_events").fetchone()[0]
    assert scheduler.defer_due_outbox(claim, not_before=clock.now(), reason="navigation_timeout")
    assert scheduler.claim_due_outbox_with_tokens(
        pacing_plan_id=claim.due.pacing_plan_id, segment_index=0, one_shot_attempt_id=one_shot) == []
    clock.advance(10)
    restored = scheduler.claim_due_outbox_with_tokens(
        pacing_plan_id=claim.due.pacing_plan_id, segment_index=0, one_shot_attempt_id=one_shot)[0]
    assert restored.due == claim.due
    assert restored.claim_token != claim.claim_token
    assert tuple(scheduler.connection.execute("SELECT payload_json,expires_at,status FROM m10_plans").fetchone()) == original
    assert scheduler.connection.execute("SELECT COUNT(*) FROM m10_rate_events").fetchone()[0] == rate_events
    assert scheduler.get_plan(claim.due.pacing_plan_id).source_message_keys == ("key-1", "key-2")
    assert scheduler.due_claim_state(restored).eligible


def test_stale_nonce_after_recover_and_reclaim_cannot_defer_another_owner():
    scheduler, clock, old = fixture()
    assert scheduler.recover_due_outbox() == 1
    new = scheduler.claim_due_outbox_with_tokens()[0]
    assert new.claim_token != old.claim_token
    assert not scheduler.defer_due_outbox(old, not_before=None, reason="navigation_timeout")
    assert scheduler.due_claim_state(new).eligible
    assert scheduler.defer_due_outbox(new, not_before=clock.now() + timedelta(seconds=60), reason="navigation_timeout")


def test_deferral_rejects_provenance_mismatch_and_any_operation_already_bound():
    scheduler, clock, claim = fixture()
    different = replace(claim, due=claim.due.model_copy(update={"one_shot_attempt_id": uuid4()}))
    assert not scheduler.defer_due_outbox(different, not_before=None, reason="navigation_timeout")
    scheduler.connection.execute("UPDATE m10_due_outbox SET operation_id='existing-operation'")
    assert not scheduler.defer_due_outbox(claim, not_before=None, reason="navigation_timeout")
    assert scheduler.due_claim_state(claim).error_code == "due_operation_exists"


def test_restart_preserves_deferred_not_before_and_navigation_attention(tmp_path):
    path = tmp_path / "pacing.db"
    scheduler, clock, claim = fixture(path)
    assert scheduler.defer_due_outbox(claim, not_before=clock.now() + timedelta(seconds=20), reason="navigation_timeout")
    scheduler.connection.close()
    scheduler = PacingScheduler(path, clock=clock)
    assert scheduler.recover_due_outbox() == 0
    scheduler.recover()
    assert scheduler.claim_due_outbox() == []
    clock.advance(20)
    claim = scheduler.claim_due_outbox_with_tokens()[0]
    assert scheduler.defer_due_outbox(claim, not_before=None, reason="identity_profile_mismatch", needs_attention=True)
    scheduler.connection.close()
    scheduler = PacingScheduler(path, clock=clock)
    assert scheduler.recover_due_outbox() == 0
    scheduler.recover()
    clock.advance(10)
    assert scheduler.claim_due_outbox() == []
    row = scheduler.connection.execute("SELECT status,claim_token FROM m10_due_outbox").fetchone()
    assert tuple(row) == ("navigation_attention", claim.claim_token)
    assert not scheduler.release_navigation_hold(claim.outbox_id, expected_claim_token="stale-token")
    assert scheduler.release_navigation_hold(claim.outbox_id, expected_claim_token=claim.claim_token)
    assert scheduler.claim_due_outbox() == []
    clock.advance(10)
    assert scheduler.claim_due_outbox_with_tokens()[0].due == claim.due


def test_attention_release_cannot_renew_expired_or_cancelled_plan():
    scheduler, clock, claim = fixture()
    assert scheduler.defer_due_outbox(claim, not_before=None, reason="navigation_model_unable", needs_attention=True)
    expires = scheduler.get_plan(claim.due.pacing_plan_id).expires_at
    clock.set(expires)
    assert not scheduler.release_navigation_hold(claim.outbox_id, expected_claim_token=claim.claim_token)
    assert scheduler.get_plan(claim.due.pacing_plan_id).expires_at == expires


def test_operation_hold_remains_blocked_after_startup_recovery(tmp_path):
    path = tmp_path / "pacing.db"
    scheduler, clock, claim = fixture(path)
    operation_id = uuid4()
    assert scheduler.hold_due_outbox_for_recovery(claim, operation_id=operation_id)
    assert not scheduler.defer_due_outbox(claim, not_before=None, reason="navigation_timeout")
    scheduler.connection.close()
    scheduler = PacingScheduler(path, clock=clock)
    assert scheduler.recover_due_outbox() == 0
    scheduler.recover()
    assert scheduler.claim_due_outbox() == []
    assert tuple(scheduler.connection.execute("SELECT status,operation_id FROM m10_due_outbox").fetchone()) == (
        "operation_recovery_hold", str(operation_id))


@pytest.mark.parametrize("verified", [False, True])
def test_atomic_completion_nonce_rejects_stale_owner_without_receipt_or_plan_change(verified):
    scheduler, clock, old = fixture()
    scheduler.recover_due_outbox()
    new = scheduler.claim_due_outbox_with_tokens()[0]
    before = tuple(scheduler.connection.execute("SELECT * FROM m10_plans").fetchone())
    assert scheduler.record_revalidation_result_and_complete_due_outbox(
        old.outbox_id, old.due.pacing_plan_id, segment_sent_and_verified=verified,
        segment_index=old.due.segment_index, operation_id=uuid4(),
        expected_claim_token=old.claim_token,
    ) is None
    assert tuple(scheduler.connection.execute("SELECT * FROM m10_plans").fetchone()) == before
    assert scheduler.connection.execute("SELECT COUNT(*) FROM m10_segment_receipts").fetchone()[0] == 0
    assert scheduler.is_due_claim_current(new)
    assert not scheduler.is_due_claim_current(old)


def test_plan_sql_expiry_cannot_be_extended_without_immutable_payload_match():
    scheduler, clock, claim = fixture()
    original_expiry = scheduler.get_plan(claim.due.pacing_plan_id).expires_at
    scheduler.connection.execute("UPDATE m10_plans SET expires_at=?",
                                 ((original_expiry + timedelta(days=1)).isoformat(),))
    assert scheduler.due_claim_state(claim).error_code == "due_plan_expiry_mismatch"
    assert scheduler.get_plan(claim.due.pacing_plan_id).expires_at == original_expiry
