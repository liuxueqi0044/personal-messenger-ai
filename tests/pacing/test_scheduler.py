from __future__ import annotations

from datetime import UTC, datetime, time
from hashlib import sha256

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
