from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from messenger_ai.domain import (
    Authorization,
    AuthorizationType,
    AuthorizedSendCommand,
    Draft,
    InboundMessage,
    PacingPlan,
    PlanStatus,
    Platform,
    SendStatus,
)
from messenger_ai.hub import HubService, SQLiteHubStore


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def hub(tmp_path):
    clock = Clock()
    store = SQLiteHubStore(tmp_path / "hub.sqlite3")
    yield HubService(store, clock=clock), clock
    store.close()


def message(
    clock: Clock, key: str, *, event_id=None, conversation="conv-a"
) -> InboundMessage:
    return InboundMessage(
        event_id=event_id or uuid4(),
        platform=Platform.QQ,
        account_id="acct",
        conversation_id=conversation,
        contact_id=f"contact-{conversation}",
        platform_message_key=key,
        observed_at=clock.now(),
        text=key,
    )


def make_draft(hub: HubService, clock: Clock, source: str = "m1") -> Draft:
    draft = Draft(
        conversation_id="conv-a",
        contact_id="contact-conv-a",
        text="收到",
        source_message_keys=(source,),
        rule_version="r1",
    )
    hub.create_draft(draft)
    return draft


def test_concurrent_same_event_is_deduplicated_and_one_stable_event(hub):
    service, clock = hub
    event_id = uuid4()

    async def run():
        return await asyncio.gather(
            *[
                service.ingest_async(message(clock, "m1", event_id=event_id))
                for _ in range(40)
            ]
        )

    results = asyncio.run(run())
    assert sum(result.accepted for result in results) == 1
    assert sum(result.duplicate for result in results) == 39
    assert (
        service.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        == 1
    )
    clock.advance(7)
    due = service.claim_outbox()
    assert [(item.event_type, item.aggregate_id) for item in due] == [
        ("conversation.stable_window", "conv-a")
    ]


def test_inbound_event_and_outbox_are_atomic(hub):
    service, clock = hub
    result = service.ingest(message(clock, "m1"))
    assert result.accepted
    db = service.store.connection
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert (
        db.execute(
            "SELECT COUNT(*) FROM outbox WHERE event_type='conversation.stable_window'"
        ).fetchone()[0]
        == 1
    )


def test_burst_invalidates_old_draft_and_plan_in_one_transaction(hub):
    service, clock = hub
    service.ingest(message(clock, "m1"))
    first = make_draft(service, clock)
    plan = PacingPlan(
        conversation_id="conv-a",
        source_message_keys=("m1",),
        quiet_until=clock.now(),
        earliest_send_at=clock.now(),
        expires_at=clock.now() + timedelta(minutes=1),
        reading_delay_ms=0,
        composition_delay_ms=0,
        inter_message_gap_ms=0,
        pacing_rule_version="r1",
    )
    service.schedule_plan(plan)
    clock.advance(1)
    result = service.ingest(message(clock, "m2"))
    assert result.invalidated_draft_ids == (str(first.draft_id),)
    assert result.invalidated_plan_ids == (str(plan.pacing_plan_id),)
    assert (
        service.store.connection.execute(
            "SELECT status FROM drafts WHERE draft_id=?", (str(first.draft_id),)
        ).fetchone()[0]
        == "expired"
    )
    assert (
        service.store.connection.execute(
            "SELECT status FROM pacing_plans WHERE pacing_plan_id=?",
            (str(plan.pacing_plan_id),),
        ).fetchone()[0]
        == PlanStatus.CANCELLED.value
    )
    final = make_draft(service, clock, "m2")
    active = service.store.connection.execute(
        "SELECT draft_id FROM drafts WHERE conversation_id='conv-a' AND status IN ('created','authorized')"
    ).fetchall()
    assert [row[0] for row in active] == [str(final.draft_id)]


def test_burst_has_only_latest_stable_window_job(hub):
    service, clock = hub
    service.ingest(message(clock, "m1"))
    clock.advance(1)
    service.ingest(message(clock, "m2"))
    clock.advance(6)
    jobs = service.claim_outbox()
    assert [(job.event_type, job.payload["last_message_key"]) for job in jobs] == [
        ("conversation.stable_window", "m2")
    ]
    assert service.stable_window_is_current("conv-a", "m2")
    assert not service.stable_window_is_current("conv-a", "m1")


def test_restart_resets_prepare_without_commit_and_quarantines_commit(hub):
    service, clock = hub
    with service.store.uow() as conn:
        now = clock.now().isoformat()
        conn.execute(
            "INSERT INTO send_operations(operation_id,idempotency_key,draft_id,status,error_code,commit_intent,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                "prepared-safe",
                str(uuid4()),
                "prepared",
                None,
                0,
                now,
                now,
            ),
        )
        committed_id = str(uuid4())
        conn.execute(
            "INSERT INTO send_operations(operation_id,idempotency_key,draft_id,status,error_code,commit_intent,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                committed_id,
                "commit-ambiguous",
                str(uuid4()),
                "committed",
                None,
                1,
                now,
                now,
            ),
        )
    report = service.recover()
    statuses = dict(
        service.store.connection.execute(
            "SELECT idempotency_key,status FROM send_operations"
        )
    )
    assert report["prepared_reset"] == 1
    assert report["send_uncertain"] == 1
    assert statuses == {
        "prepared-safe": "pending",
        "commit-ambiguous": "send_uncertain",
    }


class UncertainAdapter:
    def __init__(self) -> None:
        self.commits = 0

    async def prepare_send(self, operation):
        operation.status = SendStatus.PREPARED
        return operation

    async def commit_send(self, operation):
        self.commits += 1
        operation.status = SendStatus.COMMITTED
        return operation

    async def verify_send(self, operation):
        operation.status = SendStatus.UNCERTAIN
        operation.error_code = "SEND_UNCERTAIN"
        return operation


def test_send_uncertain_is_never_recommitted_and_recovery_keeps_it_quarantined(hub):
    service, clock = hub
    service.ingest(message(clock, "m1"))
    draft = make_draft(service, clock)
    authorization = Authorization(
        draft_id=draft.draft_id,
        conversation_id="conv-a",
        expected_last_message_key="m1",
        text_hash=draft.text_hash,
        idempotency_key="send-1",
        authorization_type=AuthorizationType.HUMAN,
        policy_version="p1",
        expires_at=clock.now() + timedelta(minutes=1),
    )
    service.persist_authorization(authorization)
    command = AuthorizedSendCommand(
        draft_id=draft.draft_id,
        conversation_id="conv-a",
        expected_last_message_key="m1",
        text_hash=draft.text_hash,
        idempotency_key="send-1",
        authorization_type=AuthorizationType.HUMAN,
        authorization_id=authorization.authorization_id,
        policy_version="p1",
        expires_at=authorization.expires_at,
    )
    operation = service.create_send_operation(command)
    adapter = UncertainAdapter()

    async def run():
        first = await service.run_send(operation.operation_id, adapter)
        second = await service.run_send(operation.operation_id, adapter)
        return first, second

    first, second = asyncio.run(run())
    assert first.status == SendStatus.UNCERTAIN
    assert second.status == SendStatus.UNCERTAIN
    assert adapter.commits == 1
    assert service.recover()["send_uncertain"] == 0
