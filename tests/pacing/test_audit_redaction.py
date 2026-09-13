"""P1 privacy: m10_audit payloads never persist plaintext."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from messenger_ai.domain.models import PacingProfile, ReplyPlan
from messenger_ai.pacing import PacingScheduler
from messenger_ai.pacing.models import (
    CancellationReason,
    DraftSnapshot,
    ScheduleRequest,
    SchedulerLimits,
    audit_digest,
    sanitize_audit_payload,
)
from messenger_ai.testing.fakes import FakeClock

START = datetime(2025, 1, 1, tzinfo=UTC)


def _marker(label: str) -> str:
    """Unique, unpredictable marker so absence checks cannot pass by accident."""
    return f"P1-SENSITIVE-{label}-{uuid4().hex}"


def _request(
    clock: FakeClock,
    *,
    segments: list[str],
) -> ScheduleRequest:
    text = "".join(segments)
    draft = DraftSnapshot(
        conversation_id="conv-audit",
        contact_id="contact-audit",
        text=text,
        text_hash=sha256(text.encode()).hexdigest(),
        expected_last_message_key="inbound-audit",
        rule_version="rule-audit-v1",
        eligibility_id="eligibility-audit",
    )
    return ScheduleRequest(
        draft=draft,
        reply_plan=ReplyPlan(action="draft", reply_text=text, reply_segments=segments),
        source_message_keys=("inbound-audit",),
        first_inbound_at=clock.now(),
        last_inbound_at=clock.now(),
        inbound_text=segments[0],
        profile=PacingProfile(),
        limits=SchedulerLimits(),
    )


def _audit_entries(scheduler: PacingScheduler) -> list[tuple[str, object]]:
    return [
        (row["action"], json.loads(row["payload_json"]))
        for row in scheduler.audit_rows()
    ]


def _audit_blob(scheduler: PacingScheduler) -> str:
    return "\u0000".join(row["payload_json"] for row in scheduler.audit_rows())


def _business_payloads(scheduler: PacingScheduler) -> list[str]:
    rows = scheduler.connection.execute(
        "SELECT payload_json FROM m10_plans "
        "UNION ALL SELECT payload_json FROM m10_due_outbox"
    ).fetchall()
    return [row["payload_json"] for row in rows]


def test_sanitizer_is_recursive_deterministic_and_idempotent() -> None:
    secret = _marker("nested")
    payload = {
        "conversation_id": "conv-1",
        "status": "waiting",
        "segment_count": 2,
        "text_hash": "deadbeef",
        "body": secret,
        "segments": [secret, "second"],
        "detail": {"reply_text": secret, "message_body": secret},
        "items": [{"content": secret}, {"segment_count": 1}],
        "untrusted": {"value": secret, "api_key": secret, "custom_key": secret},
        "misc": secret,
    }
    sanitized = sanitize_audit_payload(payload)
    assert sanitize_audit_payload(sanitized) == sanitized
    blob = json.dumps(sanitized)
    assert (secret in blob) is False
    assert sanitized["conversation_id"] == "conv-1"
    assert sanitized["status"] == "waiting"
    assert sanitized["segment_count"] == 2
    assert sanitized["text_hash"] == "deadbeef"
    assert sanitized["body"] == audit_digest(secret)
    assert sanitized["segments"] == [audit_digest(secret), audit_digest("second")]
    assert sanitized["detail"]["reply_text"] == audit_digest(secret)
    assert sanitized["detail"]["message_body"] == audit_digest(secret)
    assert sanitized["items"][0]["content"] == audit_digest(secret)
    assert sanitized["items"][1]["segment_count"] == 1
    assert sanitized["untrusted"] == {
        "value": "[redacted]",
        "api_key": "[redacted]",
        "custom_key": "[redacted]",
    }
    assert sanitized["misc"] == "[redacted]"


def test_live_writes_never_store_plaintext_but_business_rows_stay_intact(
    tmp_path,
) -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(tmp_path / "pacing.sqlite", clock=clock)
    inbound_marker = _marker("inbound")
    first_segment = _marker("segment-one")
    second_segment = _marker("segment-two")
    segments = [first_segment, second_segment]
    draft_text = "".join(segments)

    plan = scheduler.schedule(_request(clock, segments=segments)).plan
    assert plan is not None
    clock.advance(30)
    due = scheduler.due_for_revalidation()
    assert len(due) == 1
    advanced = scheduler.record_revalidation_result(
        plan.pacing_plan_id,
        segment_sent_and_verified=True,
        segment_index=0,
        operation_id=uuid4(),
    )
    assert advanced is not None and advanced.segment_index == 1
    clock.advance(10)
    assert len(scheduler.due_for_revalidation()) == 1
    cancelled = scheduler.cancel_plan(
        plan.pacing_plan_id, CancellationReason.USER_TAKEOVER
    )
    assert cancelled == 1

    markers = [inbound_marker, first_segment, second_segment]
    audit_blob = _audit_blob(scheduler)
    assert any(marker in audit_blob for marker in markers) is False

    # Business rows keep the real text so M10 stays functionally intact.
    business_blob = "\u0000".join(_business_payloads(scheduler))
    assert all(marker in business_blob for marker in segments) is True
    stored_plan = scheduler.get_plan(plan.pacing_plan_id)
    assert stored_plan is not None
    assert stored_plan.segments == tuple(segments)
    assert due[0].body == first_segment

    # Audit keeps IDs, status, counts and deterministic hashes.
    entries = _audit_entries(scheduler)
    created = [payload for action, payload in entries if action == "plan_created"]
    assert len(created) == 1
    created_payload = created[0]
    assert created_payload["conversation_id"] == "conv-audit"
    assert created_payload["status"] == "waiting"
    assert created_payload["segment_count"] == 2
    assert created_payload["text_hash"] == sha256(draft_text.encode()).hexdigest()
    assert created_payload["segments"] == [audit_digest(item) for item in segments]

    due_payloads = [
        payload for action, payload in entries if action == "due_for_revalidation"
    ]
    assert len(due_payloads) == 2
    assert due_payloads[0]["pacing_plan_id"] == str(plan.pacing_plan_id)
    assert due_payloads[0]["body"] == audit_digest(first_segment)
    assert due_payloads[0]["body_hash"] == sha256(first_segment.encode()).hexdigest()
    assert due_payloads[1]["body_hash"] == sha256(second_segment.encode()).hexdigest()


LEGACY_SCHEMA = """
CREATE TABLE m10_plans (
  pacing_plan_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
  contact_id TEXT NOT NULL, status TEXT NOT NULL,
  earliest_send_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  payload_json TEXT NOT NULL, due_emitted_at TEXT, cancel_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE m10_due_outbox (
  outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
  pacing_plan_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
  payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  operation_id TEXT, claimed_at TEXT, delivered_at TEXT,
  created_at TEXT NOT NULL, UNIQUE(pacing_plan_id, segment_index)
);
CREATE TABLE m10_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT,
  action TEXT NOT NULL, reason_code TEXT NOT NULL,
  payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL
);
"""


def test_migration_redacts_preexisting_audit_rows_in_place_and_is_idempotent(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-pacing.sqlite"
    plan_marker = _marker("legacy-plan")
    due_marker = _marker("legacy-due")
    audit_segment = _marker("legacy-audit-segment")
    audit_body = _marker("legacy-audit-body")
    audit_nested = _marker("legacy-audit-nested")
    audit_bare = _marker("legacy-audit-bare")
    audit_corrupt = _marker("legacy-audit-corrupt")
    markers = [
        plan_marker,
        due_marker,
        audit_segment,
        audit_body,
        audit_nested,
        audit_bare,
        audit_corrupt,
    ]

    business_plan = json.dumps({"segments": [plan_marker], "text": plan_marker})
    business_due = json.dumps({"body": due_marker, "segment_index": 0})
    audit_structured = json.dumps(
        {
            "segments": [audit_segment],
            "body": audit_body,
            "nested": {"reply_text": audit_nested},
            "conversation_id": "conv-legacy",
            "status": "waiting",
            "segment_count": 1,
            "text_hash": "deadbeef",
        }
    )
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.execute(
        "INSERT INTO m10_plans(pacing_plan_id,conversation_id,contact_id,status,"
        "earliest_send_at,expires_at,payload_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "plan-legacy",
            "conv-legacy",
            "contact-legacy",
            "waiting",
            "2025-01-01T00:00:08+00:00",
            "2025-01-01T00:05:00+00:00",
            business_plan,
            "2025-01-01T00:00:00+00:00",
            "2025-01-01T00:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO m10_due_outbox(pacing_plan_id,segment_index,payload_json,"
        "created_at) VALUES(?,?,?,?)",
        ("plan-legacy", 0, business_due, "2025-01-01T00:00:08+00:00"),
    )
    connection.execute(
        "INSERT INTO m10_audit(plan_id,action,reason_code,payload_json,"
        "occurred_at) VALUES(?,?,?,?,?)",
        (
            "plan-legacy",
            "plan_created",
            "PACED",
            audit_structured,
            "2025-01-01T00:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO m10_audit(plan_id,action,reason_code,payload_json,"
        "occurred_at) VALUES(?,?,?,?,?)",
        (
            "plan-legacy",
            "plan_created",
            "PACED",
            json.dumps("bare " + audit_bare),
            "2025-01-01T00:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO m10_audit(plan_id,action,reason_code,payload_json,"
        "occurred_at) VALUES(?,?,?,?,?)",
        (
            "plan-legacy",
            "plan_created",
            "PACED",
            "raw not json " + audit_corrupt,
            "2025-01-01T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    clock = FakeClock(START)
    scheduler = PacingScheduler(path, clock=clock)
    audit_rows = scheduler.audit_rows()
    assert len(audit_rows) == 3
    audit_blob = _audit_blob(scheduler)
    assert any(marker in audit_blob for marker in markers) is False

    sanitized_structured = json.loads(audit_rows[0]["payload_json"])
    assert sanitized_structured["conversation_id"] == "conv-legacy"
    assert sanitized_structured["status"] == "waiting"
    assert sanitized_structured["segment_count"] == 1
    assert sanitized_structured["text_hash"] == "deadbeef"
    assert sanitized_structured["segments"] == [audit_digest(audit_segment)]
    assert sanitized_structured["body"] == audit_digest(audit_body)
    assert sanitized_structured["nested"]["reply_text"] == audit_digest(audit_nested)
    assert audit_rows[1]["payload_json"] == '"[redacted]"'
    assert audit_rows[2]["payload_json"] == '"[redacted]"'

    # Business payloads are untouched, byte for byte.
    stored_plan = scheduler.connection.execute(
        "SELECT payload_json FROM m10_plans WHERE pacing_plan_id='plan-legacy'"
    ).fetchone()["payload_json"]
    stored_due = scheduler.connection.execute(
        "SELECT payload_json FROM m10_due_outbox WHERE pacing_plan_id='plan-legacy'"
    ).fetchone()["payload_json"]
    assert stored_plan == business_plan
    assert stored_due == business_due

    # A second open is a no-op: the audit rows and business rows are unchanged.
    migrated_audit = [row["payload_json"] for row in audit_rows]
    scheduler.close()
    reopened = PacingScheduler(path, clock=clock)
    assert [row["payload_json"] for row in reopened.audit_rows()] == migrated_audit
    assert (
        reopened.connection.execute(
            "SELECT payload_json FROM m10_plans WHERE pacing_plan_id='plan-legacy'"
        ).fetchone()["payload_json"]
        == business_plan
    )
    assert (
        reopened.connection.execute(
            "SELECT payload_json FROM m10_due_outbox "
            "WHERE pacing_plan_id='plan-legacy'"
        ).fetchone()["payload_json"]
        == business_due
    )
    marker_row = reopened.connection.execute(
        "SELECT value FROM m10_metadata WHERE key='m10_audit_payload_redaction'"
    ).fetchone()
    assert marker_row is not None
    reopened.close()


def test_startup_fails_closed_on_unknown_audit_migration_marker(tmp_path) -> None:
    path = tmp_path / "unknown-marker.sqlite"
    scheduler = PacingScheduler(path)
    scheduler.connection.execute(
        "UPDATE m10_metadata SET value='unexpected-version' WHERE key=?",
        ("m10_audit_payload_redaction",),
    )
    scheduler.close()

    with pytest.raises(
        RuntimeError, match="PACING_AUDIT_MIGRATION_VERSION_MISMATCH"
    ):
        PacingScheduler(path)
