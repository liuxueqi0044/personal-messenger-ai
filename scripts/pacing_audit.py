"""Run deterministic M10 pacing acceptance checks without a messenger client."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from datetime import UTC, datetime
from hashlib import sha256

from messenger_ai.domain.models import ReplyPlan
from messenger_ai.pacing import PacingScheduler
from messenger_ai.pacing.models import DraftSnapshot, ScheduleRequest
from messenger_ai.testing.fakes import FakeClock


def main() -> int:
    clock = FakeClock(datetime(2025, 1, 1, tzinfo=UTC))
    text = "这是一条必须经过可解释等待的回复"
    request = ScheduleRequest(
        draft=DraftSnapshot(
            conversation_id="audit-conversation",
            contact_id="contact-audit",
            text=text,
            text_hash=sha256(text.encode()).hexdigest(),
            expected_last_message_key="audit-inbound",
            rule_version="audit-rule-v1",
            eligibility_id="audit-eligibility",
        ),
        reply_plan=ReplyPlan(reply_text=text),
        source_message_keys=("audit-inbound",),
        first_inbound_at=clock.now(),
        last_inbound_at=clock.now(),
        inbound_text="在吗",
    )
    scheduler = PacingScheduler(clock=clock)
    outcome = scheduler.schedule(request)
    assert outcome.plan and outcome.plan.earliest_send_at >= clock.now().replace(
        second=8
    )
    clock.advance(8)
    due = scheduler.due_for_revalidation()
    assert len(due) == 1 and due[0].body_hash == request.draft.text_hash
    assert not hasattr(scheduler, "send") and not hasattr(scheduler, "authorize")
    print("M10 pacing audit: PASS (deterministic due-for-revalidation only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
