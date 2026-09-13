from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time
from hashlib import sha256
from uuid import UUID, uuid4

from messenger_ai.domain.models import PacingProfile, ReplyPlan
from messenger_ai.pacing import PacingScheduler
from messenger_ai.pacing.models import (
    AllowedTimeWindow,
    CancellationReason,
    DraftSnapshot,
    PacingConfiguration,
    ScheduleRequest,
    SchedulerLimits,
    grapheme_count,
)
from messenger_ai.rules.models import PacingRules
from messenger_ai.testing.fakes import FakeClock

START = datetime(2025, 1, 1, tzinfo=UTC)


def request(
    clock: FakeClock,
    *,
    reply: str = "好呀",
    segments: list[str] | None = None,
    inbound: str = "在吗",
    conversation: str = "conv-1",
    contact: str = "contact-1",
    last_outbound_at: datetime | None = None,
    profile: PacingProfile | None = None,
    limits: SchedulerLimits | None = None,
    one_shot_attempt_id: UUID | None = None,
) -> ScheduleRequest:
    text = "".join(segments) if segments else reply
    draft = DraftSnapshot(
        conversation_id=conversation,
        contact_id=contact,
        text=text,
        text_hash=sha256(text.encode()).hexdigest(),
        expected_last_message_key="inbound-1",
        rule_version="rule-v1",
        eligibility_id="eligibility-1",
    )
    return ScheduleRequest(
        draft=draft,
        reply_plan=ReplyPlan(
            action="draft", reply_text=reply, reply_segments=segments or []
        ),
        source_message_keys=("inbound-1",),
        first_inbound_at=clock.now(),
        last_inbound_at=clock.now(),
        inbound_text=inbound,
        last_outbound_at=last_outbound_at,
        profile=profile or PacingProfile(),
        limits=limits or SchedulerLimits(),
        one_shot_attempt_id=one_shot_attempt_id,
    )


def test_ignore_creates_no_plan() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    value = request(clock).model_copy(update={"reply_plan": ReplyPlan(action="ignore")})
    assert scheduler.schedule(value).action == "ignored"
    assert scheduler.due_for_revalidation() == []


def test_activated_rule_pacing_is_translated_without_losing_new_constraints() -> None:
    config = PacingConfiguration.from_rule_pacing(
        PacingRules(
            hard_min_latency_seconds=9,
            long_reply_threshold_chars=60,
            long_reply_min_latency_seconds=30,
            auto_reply_max_segments=3,
        )
    )
    assert config.profile.hard_min_latency_seconds == 9
    assert config.profile.long_reply_min_latency_seconds == 30
    assert config.limits.auto_reply_max_segments == 3


def test_first_due_obeys_hard_quiet_and_long_reply_bounds() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    outcome = scheduler.schedule(request(clock, reply="好" * 60))
    assert outcome.action == "scheduled"
    assert outcome.plan is not None
    assert outcome.plan.earliest_send_at >= START.replace(second=30)
    assert outcome.plan.earliest_send_at >= outcome.plan.quiet_until
    clock.advance(29)
    assert scheduler.due_for_revalidation() == []
    clock.advance(1)
    due = scheduler.due_for_revalidation()
    assert len(due) == 1
    assert due[0].body_hash == sha256(("好" * 60).encode()).hexdigest()
    assert due[0].eligibility_id == "eligibility-1"
    assert due[0].pacing_rule_version == "rule-v1"


def test_new_plan_supersedes_and_explicit_cancellation_reasons_are_auditable() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    first = scheduler.schedule(request(clock)).plan
    assert first
    second = scheduler.schedule(request(clock, reply="后来一条"))
    assert second.plan
    assert first.pacing_plan_id != second.plan.pacing_plan_id
    assert scheduler.cancel("conv-1", CancellationReason.USER_TAKEOVER) == 1
    reasons = [row["reason_code"] for row in scheduler.audit_rows()]
    assert "superseded" in reasons
    assert "user_takeover" in reasons


def test_scheduler_never_splits_bubbles_and_advances_only_after_verified_segment() -> (
    None
):
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    plan = scheduler.schedule(
        request(clock, segments=["第一句", "第二句", "第三句"])
    ).plan
    assert plan and plan.segment_count == 3
    clock.advance(8)
    first = scheduler.due_for_revalidation()[0]
    assert first.body == "第一句"
    next_plan = scheduler.record_revalidation_result(
        first.pacing_plan_id, segment_sent_and_verified=True
    )
    assert next_plan and next_plan.segment_index == 1
    assert next_plan.earliest_send_at >= clock.now().replace(second=10)
    clock.advance(2)
    second = scheduler.due_for_revalidation()[0]
    assert second.body == "第二句"
    scheduler.record_revalidation_result(
        second.pacing_plan_id, segment_sent_and_verified=False
    )
    assert scheduler.due_for_revalidation() == []


def test_window_and_contact_gap_are_hard_lower_bounds() -> None:
    clock = FakeClock(START.replace(hour=6))
    scheduler = PacingScheduler(clock=clock)
    profile = PacingProfile(plan_ttl_seconds=14_400)
    limits = SchedulerLimits(
        allowed_time_windows=(AllowedTimeWindow(start=time(9), end=time(18)),)
    )
    outcome = scheduler.schedule(
        request(
            clock,
            last_outbound_at=START.replace(hour=5, minute=59, second=55),
            profile=profile,
            limits=limits,
        )
    )
    assert outcome.plan and outcome.plan.earliest_send_at == START.replace(hour=9)


def test_restart_preserves_plan_and_due_is_exactly_once(tmp_path) -> None:
    clock = FakeClock(START)
    path = tmp_path / "pacing.sqlite"
    first = PacingScheduler(path, clock=clock)
    plan = first.schedule(request(clock)).plan
    assert plan
    first.close()
    scheduler = PacingScheduler(path, clock=clock)
    clock.advance(8)
    assert len(scheduler.due_for_revalidation()) == 1
    assert scheduler.due_for_revalidation() == []


def test_due_outbox_survives_claim_crash_and_receipt_replay_is_idempotent() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    plan = scheduler.schedule(request(clock, segments=["一", "二"])).plan
    assert plan
    clock.advance(8)
    due = scheduler.due_for_revalidation()[0]
    first_claim = scheduler.claim_due_outbox()
    assert first_claim[0][1].event_id == due.event_id
    assert scheduler.recover_due_outbox() == 1
    outbox_id, replayed = scheduler.claim_due_outbox()[0]
    assert replayed.event_id == due.event_id
    assert scheduler.complete_due_outbox(outbox_id)
    operation_id = uuid4()
    advanced = scheduler.record_revalidation_result(
        plan.pacing_plan_id,
        segment_sent_and_verified=True,
        segment_index=0,
        operation_id=operation_id,
    )
    assert advanced and advanced.segment_index == 1
    replay = scheduler.record_revalidation_result(
        plan.pacing_plan_id,
        segment_sent_and_verified=True,
        segment_index=0,
        operation_id=operation_id,
    )
    assert replay and replay.segment_index == 1


def test_legacy_schema_adds_nullable_one_shot_provenance_columns(tmp_path) -> None:
    path = tmp_path / "legacy-pacing.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
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
        """
    )
    connection.close()

    clock = FakeClock(START)
    scheduler = PacingScheduler(path, clock=clock)
    attempt_id = uuid4()
    plan = scheduler.schedule(request(clock, one_shot_attempt_id=attempt_id)).plan
    assert plan is not None
    clock.advance(8)
    due = scheduler.due_for_revalidation(
        pacing_plan_id=plan.pacing_plan_id,
        one_shot_attempt_id=attempt_id,
    )

    for table in ("m10_plans", "m10_due_outbox"):
        columns = {
            row["name"]
            for row in scheduler.connection.execute(f"PRAGMA table_info({table})")
        }
        assert "one_shot_attempt_id" in columns
    assert due[0].one_shot_attempt_id == attempt_id
    assert scheduler.connection.execute(
        "SELECT one_shot_attempt_id FROM m10_due_outbox"
    ).fetchone()["one_shot_attempt_id"] == str(attempt_id)


def test_one_shot_due_requires_the_exact_attempt_and_skips_normal_scan() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    attempt_id = uuid4()
    plan = scheduler.schedule(request(clock, one_shot_attempt_id=attempt_id)).plan
    assert plan is not None and plan.one_shot_attempt_id == attempt_id
    clock.advance(8)

    assert scheduler.due_for_revalidation() == []
    assert scheduler.due_for_revalidation(pacing_plan_id=plan.pacing_plan_id) == []
    assert (
        scheduler.due_for_revalidation(
            pacing_plan_id=plan.pacing_plan_id,
            one_shot_attempt_id=uuid4(),
        )
        == []
    )
    assert (
        scheduler.connection.execute(
            "SELECT status FROM m10_plans WHERE pacing_plan_id=?",
            (str(plan.pacing_plan_id),),
        ).fetchone()["status"]
        == "waiting"
    )

    due = scheduler.due_for_revalidation(
        pacing_plan_id=plan.pacing_plan_id,
        one_shot_attempt_id=attempt_id,
    )

    assert len(due) == 1
    assert due[0].one_shot_attempt_id == attempt_id


def test_recovered_one_shot_claim_remains_quarantined_from_normal_claims() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    attempt_id = uuid4()
    plan = scheduler.schedule(request(clock, one_shot_attempt_id=attempt_id)).plan
    assert plan is not None
    clock.advance(8)
    assert scheduler.due_for_revalidation(
        pacing_plan_id=plan.pacing_plan_id,
        one_shot_attempt_id=attempt_id,
    )
    claimed = scheduler.claim_due_outbox(
        pacing_plan_id=plan.pacing_plan_id,
        segment_index=0,
        one_shot_attempt_id=attempt_id,
    )
    assert len(claimed) == 1

    assert scheduler.recover_due_outbox() == 1
    assert scheduler.claim_due_outbox() == []
    assert (
        scheduler.claim_due_outbox(
            pacing_plan_id=plan.pacing_plan_id,
            segment_index=0,
        )
        == []
    )
    assert (
        scheduler.claim_due_outbox(
            pacing_plan_id=plan.pacing_plan_id,
            segment_index=0,
            one_shot_attempt_id=uuid4(),
        )
        == []
    )
    assert (
        len(
            scheduler.claim_due_outbox(
                pacing_plan_id=plan.pacing_plan_id,
                segment_index=0,
                one_shot_attempt_id=attempt_id,
            )
        )
        == 1
    )


def test_clock_jump_scans_are_isolated_by_one_shot_provenance() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    attempt_id = uuid4()
    regular = scheduler.schedule(
        request(
            clock,
            conversation="regular",
            contact="regular-contact",
            profile=PacingProfile(plan_ttl_seconds=1_000),
        )
    ).plan
    one_shot = scheduler.schedule(
        request(
            clock,
            conversation="one-shot",
            contact="one-shot-contact",
            profile=PacingProfile(plan_ttl_seconds=1_000),
            one_shot_attempt_id=attempt_id,
        )
    ).plan
    assert regular is not None and one_shot is not None
    assert scheduler.due_for_revalidation() == []
    clock.advance(301)

    assert (
        scheduler.due_for_revalidation(
            pacing_plan_id=one_shot.pacing_plan_id,
            one_shot_attempt_id=uuid4(),
        )
        == []
    )
    assert (
        scheduler.connection.execute(
            "SELECT status FROM m10_plans WHERE pacing_plan_id=?",
            (str(one_shot.pacing_plan_id),),
        ).fetchone()["status"]
        == "waiting"
    )
    assert (
        scheduler.due_for_revalidation(
            pacing_plan_id=one_shot.pacing_plan_id,
            one_shot_attempt_id=attempt_id,
        )
        == []
    )
    assert scheduler.due_for_revalidation() == []

    statuses = {
        row["pacing_plan_id"]: row["status"]
        for row in scheduler.connection.execute(
            "SELECT pacing_plan_id,status FROM m10_plans"
        )
    }
    assert statuses[str(one_shot.pacing_plan_id)] == "hold_replan"
    assert statuses[str(regular.pacing_plan_id)] == "hold_replan"


def test_atomic_result_and_outbox_completion_roll_back_and_recover_safely() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    attempt_id = uuid4()
    operation_id = uuid4()
    plan = scheduler.schedule(request(clock, one_shot_attempt_id=attempt_id)).plan
    assert plan is not None
    clock.advance(8)
    assert scheduler.due_for_revalidation(
        pacing_plan_id=plan.pacing_plan_id,
        one_shot_attempt_id=attempt_id,
    )
    outbox_id, _due = scheduler.claim_due_outbox(
        pacing_plan_id=plan.pacing_plan_id,
        segment_index=0,
        one_shot_attempt_id=attempt_id,
    )[0]
    assert (
        scheduler.record_revalidation_result_and_complete_due_outbox(
            outbox_id,
            uuid4(),
            segment_sent_and_verified=True,
            segment_index=0,
            operation_id=operation_id,
            one_shot_attempt_id=attempt_id,
        )
        is None
    )
    assert (
        scheduler.record_revalidation_result_and_complete_due_outbox(
            outbox_id,
            plan.pacing_plan_id,
            segment_sent_and_verified=True,
            segment_index=1,
            operation_id=operation_id,
            one_shot_attempt_id=attempt_id,
        )
        is None
    )
    assert (
        scheduler.record_revalidation_result_and_complete_due_outbox(
            outbox_id,
            plan.pacing_plan_id,
            segment_sent_and_verified=True,
            segment_index=0,
            operation_id=operation_id,
            one_shot_attempt_id=uuid4(),
        )
        is None
    )
    scheduler.connection.execute(
        """CREATE TRIGGER fail_due_delivery
           BEFORE UPDATE OF status ON m10_due_outbox
           WHEN NEW.status='delivered'
           BEGIN SELECT RAISE(ABORT, 'simulated crash'); END"""
    )

    try:
        scheduler.record_revalidation_result_and_complete_due_outbox(
            outbox_id,
            plan.pacing_plan_id,
            segment_sent_and_verified=True,
            segment_index=0,
            operation_id=operation_id,
            one_shot_attempt_id=attempt_id,
        )
    except sqlite3.IntegrityError as error:
        assert "simulated crash" in str(error)
    else:
        raise AssertionError("delivery trigger should abort the pacing transaction")

    plan_row = scheduler.connection.execute(
        "SELECT status FROM m10_plans WHERE pacing_plan_id=?",
        (str(plan.pacing_plan_id),),
    ).fetchone()
    outbox_row = scheduler.connection.execute(
        "SELECT status,operation_id FROM m10_due_outbox WHERE outbox_id=?",
        (outbox_id,),
    ).fetchone()
    assert plan_row["status"] == "due_for_revalidation"
    assert tuple(outbox_row) == ("dispatching", None)
    assert (
        scheduler.connection.execute(
            "SELECT COUNT(*) FROM m10_segment_receipts"
        ).fetchone()[0]
        == 0
    )

    scheduler.connection.execute("DROP TRIGGER fail_due_delivery")
    assert scheduler.record_revalidation_result_and_complete_due_outbox(
        outbox_id,
        plan.pacing_plan_id,
        segment_sent_and_verified=True,
        segment_index=0,
        operation_id=operation_id,
        one_shot_attempt_id=attempt_id,
    )
    assert scheduler.recover_due_outbox() == 0
    delivered_row = scheduler.connection.execute(
        "SELECT status,operation_id FROM m10_due_outbox WHERE outbox_id=?",
        (outbox_id,),
    ).fetchone()
    assert tuple(delivered_row) == ("delivered", str(operation_id))
    assert (
        scheduler.record_revalidation_result_and_complete_due_outbox(
            outbox_id,
            plan.pacing_plan_id,
            segment_sent_and_verified=False,
            segment_index=0,
            operation_id=operation_id,
            one_shot_attempt_id=attempt_id,
        )
        is None
    )
    assert (
        scheduler.record_revalidation_result_and_complete_due_outbox(
            outbox_id,
            plan.pacing_plan_id,
            segment_sent_and_verified=True,
            segment_index=0,
            operation_id=uuid4(),
            one_shot_attempt_id=attempt_id,
        )
        is None
    )


def test_atomic_revalidation_rejection_needs_no_send_operation() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    attempt_id = uuid4()
    plan = scheduler.schedule(request(clock, one_shot_attempt_id=attempt_id)).plan
    assert plan is not None
    clock.advance(8)
    assert scheduler.due_for_revalidation(
        pacing_plan_id=plan.pacing_plan_id,
        one_shot_attempt_id=attempt_id,
    )
    outbox_id, _due = scheduler.claim_due_outbox(
        pacing_plan_id=plan.pacing_plan_id,
        segment_index=0,
        one_shot_attempt_id=attempt_id,
    )[0]

    assert scheduler.record_revalidation_result_and_complete_due_outbox(
        outbox_id,
        plan.pacing_plan_id,
        segment_sent_and_verified=False,
        segment_index=0,
        one_shot_attempt_id=attempt_id,
    )
    plan_status = scheduler.connection.execute(
        "SELECT status FROM m10_plans WHERE pacing_plan_id=?",
        (str(plan.pacing_plan_id),),
    ).fetchone()["status"]
    outbox_status = scheduler.connection.execute(
        "SELECT status,operation_id FROM m10_due_outbox WHERE outbox_id=?",
        (outbox_id,),
    ).fetchone()
    assert plan_status == "rejected"
    assert tuple(outbox_status) == ("delivered", None)
    assert scheduler.recover_due_outbox() == 0


def test_exact_due_only_advances_the_named_waiting_plan() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    selected = scheduler.schedule(
        request(clock, conversation="conv-selected", contact="contact-selected")
    ).plan
    other = scheduler.schedule(
        request(clock, conversation="conv-other", contact="contact-other")
    ).plan
    assert selected and other

    clock.advance(8)
    due = scheduler.due_for_revalidation(pacing_plan_id=selected.pacing_plan_id)

    assert [event.pacing_plan_id for event in due] == [selected.pacing_plan_id]
    rows = scheduler.connection.execute(
        "SELECT pacing_plan_id,status FROM m10_plans ORDER BY pacing_plan_id"
    ).fetchall()
    statuses = {row["pacing_plan_id"]: row["status"] for row in rows}
    assert statuses[str(selected.pacing_plan_id)] == "due_for_revalidation"
    assert statuses[str(other.pacing_plan_id)] == "waiting"
    outbox_plan_ids = [
        row["pacing_plan_id"]
        for row in scheduler.connection.execute(
            "SELECT pacing_plan_id FROM m10_due_outbox ORDER BY outbox_id"
        ).fetchall()
    ]
    assert outbox_plan_ids == [str(selected.pacing_plan_id)]


def test_exact_claim_only_dispatches_the_named_plan_segment() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    selected = scheduler.schedule(
        request(clock, conversation="conv-selected", contact="contact-selected")
    ).plan
    other = scheduler.schedule(
        request(clock, conversation="conv-other", contact="contact-other")
    ).plan
    assert selected and other
    clock.advance(8)
    assert scheduler.due_for_revalidation(pacing_plan_id=selected.pacing_plan_id)
    assert scheduler.due_for_revalidation(pacing_plan_id=other.pacing_plan_id)

    assert (
        scheduler.claim_due_outbox(
            pacing_plan_id=selected.pacing_plan_id, segment_index=1
        )
        == []
    )
    claimed = scheduler.claim_due_outbox(
        pacing_plan_id=selected.pacing_plan_id, segment_index=0
    )

    assert len(claimed) == 1
    assert claimed[0][1].pacing_plan_id == selected.pacing_plan_id
    rows = scheduler.connection.execute(
        "SELECT pacing_plan_id,status FROM m10_due_outbox ORDER BY outbox_id"
    ).fetchall()
    statuses = {row["pacing_plan_id"]: row["status"] for row in rows}
    assert statuses[str(selected.pacing_plan_id)] == "dispatching"
    assert statuses[str(other.pacing_plan_id)] == "pending"


def test_cancel_plan_only_cancels_the_named_plan() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    selected = scheduler.schedule(
        request(clock, conversation="conv-selected", contact="contact-selected")
    ).plan
    other = scheduler.schedule(
        request(clock, conversation="conv-other", contact="contact-other")
    ).plan
    assert selected and other

    assert (
        scheduler.cancel_plan(selected.pacing_plan_id, CancellationReason.USER_TAKEOVER)
        == 1
    )
    assert (
        scheduler.cancel_plan(selected.pacing_plan_id, CancellationReason.USER_TAKEOVER)
        == 0
    )
    rows = scheduler.connection.execute(
        "SELECT pacing_plan_id,status,cancel_reason FROM m10_plans ORDER BY pacing_plan_id"
    ).fetchall()
    states = {
        row["pacing_plan_id"]: (row["status"], row["cancel_reason"]) for row in rows
    }
    assert states[str(selected.pacing_plan_id)] == (
        "cancelled",
        "user_takeover",
    )
    assert states[str(other.pacing_plan_id)] == ("waiting", None)


def test_nonrecoverable_exact_claim_is_never_requeued_after_crash() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    selected = scheduler.schedule(request(clock)).plan
    assert selected
    clock.advance(8)
    assert scheduler.due_for_revalidation(pacing_plan_id=selected.pacing_plan_id)

    claimed = scheduler.claim_due_outbox(
        pacing_plan_id=selected.pacing_plan_id,
        segment_index=0,
        recoverable=False,
    )

    assert len(claimed) == 1
    assert scheduler.recover_due_outbox() == 0
    row = scheduler.connection.execute(
        "SELECT status FROM m10_due_outbox WHERE outbox_id=?", (claimed[0][0],)
    ).fetchone()
    assert row["status"] == "dispatching_nonrecoverable"
    assert (
        scheduler.claim_due_outbox(
            pacing_plan_id=selected.pacing_plan_id,
            segment_index=0,
            recoverable=False,
        )
        == []
    )


def test_clock_jump_holds_instead_of_batch_revalidating() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    assert scheduler.schedule(request(clock)).plan
    scheduler.recover()
    clock.advance(301)
    report = scheduler.recover()
    assert report.reason_code == "clock_jump"
    assert scheduler.due_for_revalidation() == []


def test_global_and_contact_revalidation_limits_are_enforced() -> None:
    clock = FakeClock(START)
    scheduler = PacingScheduler(clock=clock)
    limits = SchedulerLimits(
        max_global_revalidations=1, max_contact_revalidations=1, window_seconds=3600
    )
    assert scheduler.schedule(
        request(clock, conversation="conv-a", contact="contact-a", limits=limits)
    ).plan
    assert scheduler.schedule(
        request(clock, conversation="conv-b", contact="contact-b", limits=limits)
    ).plan
    clock.advance(8)
    due = scheduler.due_for_revalidation()
    assert len(due) == 1
    # The second due candidate is conservatively cancelled rather than emitted past its facts.
    assert scheduler.due_for_revalidation() == []


def test_10000_monotonic_delay_property() -> None:
    """10k deterministic cases: bigger texts cannot reduce raw reading/composition delay."""
    profile = PacingProfile()
    previous = -1.0
    for length in range(10_000):
        seconds = max(
            profile.min_reading_seconds,
            min(profile.max_reading_seconds, length / profile.reading_chars_per_second),
        ) + max(
            profile.min_composition_seconds,
            min(
                profile.max_composition_seconds,
                length / profile.composition_chars_per_second,
            ),
        )
        assert seconds >= previous
        previous = seconds


def test_1000_recovery_checks_never_emit_early(tmp_path) -> None:
    """A fast 1k restart/recovery simulation without wall-clock sleeps."""
    path = tmp_path / "restart.sqlite"
    clock = FakeClock(START)
    scheduler = PacingScheduler(path, clock=clock)
    assert scheduler.schedule(request(clock)).plan
    for _ in range(1_000):
        assert scheduler.recover().held_plan_ids == ()
        assert scheduler.due_for_revalidation() == []
    clock.advance(8)
    assert len(scheduler.due_for_revalidation()) == 1


def test_unicode_character_count_does_not_count_combining_marks_twice() -> None:
    assert grapheme_count("e\u0301") == 1
