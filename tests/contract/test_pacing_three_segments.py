from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from messenger_ai.domain import PacingPlan


def _plan(segment_count: int) -> PacingPlan:
    now = datetime.now(UTC)
    return PacingPlan(
        conversation_id="conversation-1",
        source_message_keys=("message-1",),
        quiet_until=now + timedelta(seconds=6),
        earliest_send_at=now + timedelta(seconds=30),
        expires_at=now + timedelta(minutes=5),
        reading_delay_ms=2_000,
        composition_delay_ms=4_000,
        inter_message_gap_ms=15_000,
        segment_count=segment_count,
        pacing_rule_version="personal-default",
    )


def test_pacing_plan_accepts_three_natural_segments() -> None:
    assert _plan(3).segment_count == 3


def test_pacing_plan_rejects_more_than_three_segments() -> None:
    with pytest.raises(ValidationError):
        _plan(4)
