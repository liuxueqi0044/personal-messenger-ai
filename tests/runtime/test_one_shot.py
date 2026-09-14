from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import messenger_ai.runtime.one_shot as one_shot_module
import pytest
from messenger_ai.adapters.qq.models import QQIdentityBinding
from messenger_ai.domain import SendOperation, SendStatus
from messenger_ai.runtime.due_dispatch import DueCoordinator
from messenger_ai.runtime.one_shot import (
    OneShotAttemptLedger,
    _handoff_proves_fresh_verify,
    run_one_shot_reply,
)


def binding() -> QQIdentityBinding:
    return QQIdentityBinding(
        hub_conversation_id="conversation",
        contact_id="contact",
        account_id="account",
        platform_conversation_id="qq-conversation",
        participant_signature="proof",
        binding_id="session-contact-1",
        conversation_type="direct",
        authorization_scope="all_direct_including_temporary",
    )


def handoff(operation_id: UUID) -> dict[str, object]:
    return {
        "operation_id": str(operation_id),
        "commit_worker_epoch": "00000000-0000-4000-8000-000000000001",
        "verify_health_worker_epoch": "00000000-0000-4000-8000-000000000002",
        "verify_worker_epoch": "00000000-0000-4000-8000-000000000002",
        "commit_worker_process_id": 101,
        "verify_worker_process_id": 202,
        "first_worker_retired": True,
        "verify_status": "ok",
    }


def test_ledger_blocks_new_attempt_id_for_same_binding_source(tmp_path) -> None:
    ledger = OneShotAttemptLedger(tmp_path / "one-shot.sqlite3")
    first = uuid4()
    assert ledger.claim(
        attempt_id=first,
        binding_id="session-contact-1",
        source_keys=("source-1",),
    )
    ledger.update(attempt_id=first, state="uncertain")
    assert not ledger.claim(
        attempt_id=uuid4(),
        binding_id="session-contact-1",
        source_keys=("source-1",),
    )
    assert ledger.claim(
        attempt_id=uuid4(),
        binding_id="session-contact-2",
        source_keys=("source-1",),
    )


def test_ledger_releases_only_an_explicit_terminal_pre_provider_batch(
    tmp_path,
) -> None:
    ledger = OneShotAttemptLedger(tmp_path / "reclaim.sqlite3")
    first, second = uuid4(), uuid4()
    sources = ("source-1", "source-2")
    assert ledger.claim(
        attempt_id=first,
        binding_id="session-contact-1",
        source_keys=sources,
    )
    ledger.update(attempt_id=first, state="pre_provider_timeout")

    assert ledger.claim(
        attempt_id=second,
        binding_id="session-contact-1",
        source_keys=sources,
    )
    released = ledger.connection.execute(
        "SELECT state,evidence_json FROM one_shot_attempts WHERE attempt_id=?",
        (str(first),),
    ).fetchone()
    assert released["state"] == "released_safe"
    assert json.loads(released["evidence_json"])["replaced_by_attempt_id"] == str(
        second
    )
    owners = ledger.connection.execute(
        "SELECT DISTINCT attempt_id FROM one_shot_sources"
    ).fetchall()
    assert [row["attempt_id"] for row in owners] == [str(second)]


def test_ledger_never_releases_an_active_attempt_without_a_death_witness(
    tmp_path,
) -> None:
    sources = ("source-1", "source-2")
    for state in ("claimed", "events_dispatched", "planning_claimed"):
        ledger = OneShotAttemptLedger(tmp_path / f"active-{state}.sqlite3")
        assert ledger.claim(
            attempt_id=uuid4(),
            binding_id="session-contact-1",
            source_keys=sources,
        )
        if state != "claimed":
            owner = ledger.connection.execute(
                "SELECT attempt_id FROM one_shot_attempts"
            ).fetchone()
            ledger.update(attempt_id=UUID(owner["attempt_id"]), state=state)

        evidence = ledger.connection.execute(
            "SELECT evidence_json FROM one_shot_attempts"
        ).fetchone()
        assert json.loads(evidence["evidence_json"])["provider_started"] is False

        assert not ledger.claim(
            attempt_id=uuid4(),
            binding_id="session-contact-1",
            source_keys=sources,
        )


def test_ledger_does_not_release_a_partial_pre_provider_batch(tmp_path) -> None:
    ledger = OneShotAttemptLedger(tmp_path / "partial.sqlite3")
    first = uuid4()
    assert ledger.claim(
        attempt_id=first,
        binding_id="session-contact-1",
        source_keys=("source-1", "source-2"),
    )
    ledger.update(attempt_id=first, state="planning_claimed")

    assert not ledger.claim(
        attempt_id=uuid4(),
        binding_id="session-contact-1",
        source_keys=("source-1",),
    )


def test_handoff_requires_distinct_process_and_epoch() -> None:
    operation_id = uuid4()
    assert _handoff_proves_fresh_verify(
        handoff(operation_id), operation_id=operation_id
    )
    same_process = handoff(operation_id)
    same_process["verify_worker_process_id"] = 101
    assert not _handoff_proves_fresh_verify(
        same_process, operation_id=operation_id
    )


def test_cancel_plan_requires_a_changed_row() -> None:
    calls: list[object] = []
    app = SimpleNamespace(
        pacing=SimpleNamespace(
            cancel_plan=lambda *args: calls.append(args) or 0,
        )
    )

    assert not one_shot_module._cancel_plan(app, uuid4())
    assert len(calls) == 1


def test_one_shot_no_new_inbound_never_calls_provider_or_due(tmp_path) -> None:
    class State:
        def execution_state(self, _conversation):
            return 1, 1, False, 1, False

    class Coordinator:
        async def observe_driver(self, _driver, _conversation):
            return ()

    app = SimpleNamespace(
        state=State(),
        coordinator=Coordinator(),
        driver=object(),
        planning=SimpleNamespace(run_claimed=lambda *_a, **_k: None),
        due=SimpleNamespace(dispatch_exact=lambda **_k: None),
    )
    ledger = OneShotAttemptLedger(tmp_path / "no-inbound.sqlite3")

    result = asyncio.run(run_one_shot_reply(
        app=app,
        ledger=ledger,
        binding=binding(),
        attempt_id=uuid4(),
        max_wait_seconds=1,
    ))

    assert result.state == "no_new_inbound"
    assert result.provider_called is False
    assert result.action_attempted is False


def test_one_shot_incomplete_observation_is_not_reported_as_clean_noop(tmp_path) -> None:
    class State:
        calls = 0

        def execution_state(self, _conversation):
            self.calls += 1
            return 1, 1, self.calls > 1, 1, False

    class Coordinator:
        async def observe_driver(self, _driver, _conversation):
            return ()

    app = SimpleNamespace(
        state=State(),
        coordinator=Coordinator(),
        driver=object(),
    )
    result = asyncio.run(
        run_one_shot_reply(
            app=app,
            ledger=OneShotAttemptLedger(tmp_path / "incomplete.sqlite3"),
            binding=binding(),
            attempt_id=uuid4(),
            max_wait_seconds=1,
        )
    )

    assert result.state == "failed"
    assert result.error_code == "ONE_SHOT_OBSERVATION_INCOMPLETE"
    assert result.provider_called is False


def test_one_shot_may_observe_a_temporary_pause_but_requires_it_to_heal(
    tmp_path,
) -> None:
    class State:
        def one_shot_observation_execution_state(self, _conversation):
            return 1, 1, False, 1, False

        def execution_state(self, _conversation):
            return 1, 1, False, 1, False

    class Coordinator:
        async def observe_driver(self, _driver, _conversation):
            return ()

    app = SimpleNamespace(
        state=State(), coordinator=Coordinator(), driver=object()
    )
    result = asyncio.run(
        run_one_shot_reply(
            app=app,
            ledger=OneShotAttemptLedger(tmp_path / "temporary-pause.sqlite3"),
            binding=binding(),
            attempt_id=uuid4(),
            max_wait_seconds=1,
        )
    )

    assert result.state == "no_new_inbound"
    assert result.provider_called is False


def test_one_shot_does_not_observe_a_non_temporary_pause(tmp_path) -> None:
    observed = False

    class State:
        def one_shot_observation_execution_state(self, _conversation):
            return 1, 1, True, 1, False

    class Coordinator:
        async def observe_driver(self, _driver, _conversation):
            nonlocal observed
            observed = True
            return ()

    app = SimpleNamespace(
        state=State(), coordinator=Coordinator(), driver=object()
    )
    result = asyncio.run(
        run_one_shot_reply(
            app=app,
            ledger=OneShotAttemptLedger(tmp_path / "durable-pause.sqlite3"),
            binding=binding(),
            attempt_id=uuid4(),
            max_wait_seconds=1,
        )
    )

    assert result.error_code == "ONE_SHOT_TARGET_NOT_ACTIVE_DIRECT"
    assert observed is False


def test_one_shot_total_deadline_times_out_observation_before_provider(
    tmp_path,
) -> None:
    calls: list[str] = []

    class State:
        def execution_state(self, _conversation):
            return 1, 1, False, 1, False

    class Coordinator:
        async def observe_driver(self, _driver, _conversation):
            calls.append("observe")
            await asyncio.Event().wait()

    app = SimpleNamespace(
        state=State(),
        coordinator=Coordinator(),
        driver=object(),
        planning=SimpleNamespace(run_claimed=lambda *_a, **_k: calls.append("plan")),
        due=SimpleNamespace(dispatch_exact=lambda **_k: calls.append("due")),
    )

    result = asyncio.run(
        run_one_shot_reply(
            app=app,
            ledger=OneShotAttemptLedger(tmp_path / "observe-timeout.sqlite3"),
            binding=binding(),
            attempt_id=uuid4(),
            max_wait_seconds=0.05,
        )
    )

    assert result.state == "failed"
    assert result.error_code == "ONE_SHOT_OBSERVE_TIMEOUT"
    assert result.provider_called is False
    assert result.action_attempted is False
    assert calls == ["observe"]


def test_one_shot_provider_timeout_never_dispatches_or_releases_source(tmp_path) -> None:
    calls: list[str] = []
    plan_id = uuid4()

    class State:
        connection = None

        def execution_state(self, _conversation):
            return 1, 1, False, 1, False

        def revisions(self, _conversation):
            return 1, 2

        def pending_planning_job_for(self, *_args, **_kwargs):
            return {"binding_revision": 1, "source_keys_json": '["source-1"]'}

        def claim_planning_job_for(self, *_args, **_kwargs):
            return {"binding_revision": 1, "source_keys_json": '["source-1"]'}

        def plan_artifact_for_revision(self, *_args, **_kwargs):
            # A provider can finish its durable planning write immediately
            # before cancellation reaches an async boundary.  Timeout cleanup
            # must still cancel that exact, attempt-owned plan before return.
            return {
                "pacing_plan_id": str(plan_id),
                "planner_json": '{"reply_segments":["hidden"]}',
            }

    class Coordinator:
        async def observe_driver(self, _driver, _conversation):
            return ("new_message",)

        def dispatch_events(self, **_kwargs):
            calls.append("events")

    class Planning:
        async def run_claimed(self, *_args, **_kwargs):
            calls.append("provider")
            await asyncio.Event().wait()

    class Due:
        async def dispatch_exact(self, **_kwargs):
            calls.append("due")
            raise AssertionError("deadline must stop before due dispatch")

    ledger = OneShotAttemptLedger(tmp_path / "provider-timeout.sqlite3")
    result = asyncio.run(
        run_one_shot_reply(
            app=SimpleNamespace(
                state=State(),
                coordinator=Coordinator(),
                planning=Planning(),
                due=Due(),
                driver=object(),
                pacing=SimpleNamespace(
                    cancel_plan=lambda *_a: calls.append("cancel") or 1
                ),
            ),
            ledger=ledger,
            binding=binding(),
            attempt_id=uuid4(),
            max_wait_seconds=0.05,
        )
    )

    assert result.state == "uncertain"
    assert result.error_code == "ONE_SHOT_PROVIDER_TIMEOUT"
    assert result.provider_called is True
    assert result.action_attempted is False
    assert result.pacing_plan_id == plan_id
    assert calls == ["events", "provider", "cancel"]
    state = ledger.connection.execute(
        "SELECT state FROM one_shot_attempts"
    ).fetchone()
    assert state["state"] == "provider_timeout_ambiguous"
    assert not ledger.claim(
        attempt_id=uuid4(),
        binding_id="session-contact-1",
        source_keys=("source-1",),
    )


@pytest.mark.parametrize(
    ("cancel_result", "expected_state", "expected_error"),
    (
        (1, "deferred_cancelled", "ONE_SHOT_WAIT_TIMEOUT"),
        (0, "uncertain", "ONE_SHOT_TIMEOUT_CLEANUP_FAILED"),
    ),
)
def test_one_shot_expiry_after_planning_requires_confirmed_cancellation(
    monkeypatch, tmp_path, cancel_result, expected_state, expected_error
) -> None:
    class Clock:
        value = 0.0

        @classmethod
        def monotonic(cls) -> float:
            return cls.value

    monkeypatch.setattr(one_shot_module.time, "monotonic", Clock.monotonic)
    calls: list[str] = []
    plan_id = uuid4()

    class State:
        connection = None

        def execution_state(self, _conversation):
            return 1, 1, False, 1, False

        def revisions(self, _conversation):
            return 1, 2

        def pending_planning_job_for(self, *_args, **_kwargs):
            return {"binding_revision": 1, "source_keys_json": '["source-1"]'}

        def claim_planning_job_for(self, *_args, **_kwargs):
            return {"binding_revision": 1, "source_keys_json": '["source-1"]'}

        def plan_artifact_for_revision(self, *_args, **_kwargs):
            return {
                "pacing_plan_id": str(plan_id),
                "planner_json": '{"reply_segments":["hidden"]}',
            }

    class Coordinator:
        async def observe_driver(self, _driver, _conversation):
            return ("new_message",)

        def dispatch_events(self, **_kwargs):
            calls.append("events")

    class Planning:
        async def run_claimed(self, *_args, **_kwargs):
            Clock.value = 2.0
            return "scheduled"

    class Due:
        async def dispatch_exact(self, **_kwargs):
            calls.append("due")
            raise AssertionError("expired one-shot must not dispatch")

    result = asyncio.run(
        run_one_shot_reply(
            app=SimpleNamespace(
                state=State(),
                coordinator=Coordinator(),
                planning=Planning(),
                due=Due(),
                driver=object(),
                pacing=SimpleNamespace(
                    cancel_plan=lambda *_a: calls.append("cancel") or cancel_result
                ),
            ),
            ledger=OneShotAttemptLedger(tmp_path / "post-plan-timeout.sqlite3"),
            binding=binding(),
            attempt_id=uuid4(),
            max_wait_seconds=1,
        )
    )

    assert result.state == expected_state
    assert result.error_code == expected_error
    assert calls == ["events", "cancel"]


def test_one_shot_dispatch_timeout_is_quarantined_and_never_reclaimable(
    tmp_path,
) -> None:
    calls: list[str] = []
    plan_id = uuid4()

    class State:
        connection = None

        def execution_state(self, _conversation):
            return 1, 1, False, 1, False

        def revisions(self, _conversation):
            return 1, 2

        def pending_planning_job_for(self, *_args, **_kwargs):
            return {"binding_revision": 1, "source_keys_json": '["source-1"]'}

        def claim_planning_job_for(self, *_args, **_kwargs):
            return {"binding_revision": 1, "source_keys_json": '["source-1"]'}

        def plan_artifact_for_revision(self, *_args, **_kwargs):
            return {
                "pacing_plan_id": str(plan_id),
                "planner_json": '{"reply_segments":["hidden"]}',
            }

    class Coordinator:
        async def observe_driver(self, _driver, _conversation):
            return ("new_message",)

        def dispatch_events(self, **_kwargs):
            calls.append("events")

    class Planning:
        async def run_claimed(self, *_args, **_kwargs):
            return "scheduled"

    class Due:
        async def dispatch_exact(self, **_kwargs):
            calls.append("due")
            await asyncio.Event().wait()

    ledger = OneShotAttemptLedger(tmp_path / "dispatch-timeout.sqlite3")
    result = asyncio.run(
        run_one_shot_reply(
            app=SimpleNamespace(
                state=State(),
                coordinator=Coordinator(),
                planning=Planning(),
                due=Due(),
                driver=object(),
                pacing=SimpleNamespace(
                    cancel_plan=lambda *_a: calls.append("cancel") or 1
                ),
            ),
            ledger=ledger,
            binding=binding(),
            attempt_id=uuid4(),
            max_wait_seconds=0.05,
        )
    )

    assert result.state == "uncertain"
    assert result.error_code == "ONE_SHOT_DISPATCH_TIMEOUT"
    assert result.action_attempted is True
    assert calls == ["events", "due", "cancel"]
    assert not ledger.claim(
        attempt_id=uuid4(),
        binding_id="session-contact-1",
        source_keys=("source-1",),
    )


def test_one_shot_dispatches_only_exact_plan_once_and_requires_fresh_verify(
    tmp_path,
) -> None:
    operation_id = uuid4()
    plan_id = uuid4()
    calls: list[object] = []

    class State:
        connection = None

        def execution_state(self, conversation):
            calls.append(("execution_state", conversation))
            return 1, 1, False, 1, False

        def revisions(self, conversation):
            calls.append(("revisions", conversation))
            return 1, 2

        def pending_planning_job_for(
            self, conversation, *, expected_revision, one_shot_attempt_id
        ):
            calls.append(
                (
                    "pending",
                    conversation,
                    expected_revision,
                    one_shot_attempt_id,
                )
            )
            return {
                "binding_revision": 1,
                "source_keys_json": json.dumps(["source-1"]),
            }

        def claim_planning_job_for(
            self, conversation, *, expected_revision, one_shot_attempt_id
        ):
            calls.append(
                ("claim", conversation, expected_revision, one_shot_attempt_id)
            )
            return {
                "binding_revision": 1,
                "source_keys_json": json.dumps(["source-1"]),
            }

        def plan_artifact_for_revision(
            self, conversation, revision, *, one_shot_attempt_id
        ):
            calls.append(
                ("artifact", conversation, revision, one_shot_attempt_id)
            )
            return {
                "pacing_plan_id": str(plan_id),
                "planner_json": json.dumps({"reply_segments": ["hidden"]}),
            }

    class Coordinator:
        async def observe_driver(self, _driver, conversation):
            calls.append(("observe", conversation))
            return ("new_message",)

        def dispatch_events(self, *, conversation_id, one_shot_attempt_id):
            calls.append(("events", conversation_id, one_shot_attempt_id))
            return 1

    class Planning:
        async def run_claimed(self, _job, *, max_reply_segments):
            calls.append(("planning", max_reply_segments))
            return "scheduled"

    class Due:
        async def dispatch_exact(self, **kwargs):
            calls.append(("due", kwargs))
            return True, SendOperation(
                operation_id=operation_id,
                idempotency_key="one-shot",
                draft_id=uuid4(),
                status=SendStatus.VERIFIED,
            )

    class Driver:
        def last_send_handoff(self):
            return handoff(operation_id)

    app = SimpleNamespace(
        state=State(),
        coordinator=Coordinator(),
        planning=Planning(),
        due=Due(),
        driver=Driver(),
        pacing=SimpleNamespace(cancel_plan=lambda *_a: calls.append("cancel")),
    )
    class Ledger(OneShotAttemptLedger):
        def claim(self, **kwargs):
            calls.append(("ledger", kwargs["attempt_id"]))
            return super().claim(**kwargs)

    ledger = Ledger(tmp_path / "verified.sqlite3")
    attempt_id = uuid4()

    result = asyncio.run(run_one_shot_reply(
        app=app,
        ledger=ledger,
        binding=binding(),
        attempt_id=attempt_id,
        max_wait_seconds=1,
    ))

    assert result.state == "verified"
    assert result.provider_called is True
    assert result.action_attempted is True
    assert result.send_action_attempted is True
    assert result.selection_action_attempted is None
    assert result.operation_id == operation_id
    assert calls.index(("ledger", attempt_id)) < calls.index(
        ("events", "conversation", attempt_id)
    )
    assert calls.index(("events", "conversation", attempt_id)) < calls.index(
        ("claim", "conversation", 2, attempt_id)
    )
    due_calls = [item for item in calls if isinstance(item, tuple) and item[0] == "due"]
    assert len(due_calls) == 1
    assert due_calls[0][1] == {
        "pacing_plan_id": plan_id,
        "conversation_id": "conversation",
        "segment_index": 0,
        "one_shot_attempt_id": attempt_id,
    }
    assert "cancel" not in calls


def test_due_coordinator_exact_claim_is_scoped_to_plan_and_segment() -> None:
    plan_id = uuid4()
    due = SimpleNamespace(conversation_id="conversation")
    operation = object()
    calls: list[object] = []

    class State:
        def plan_artifact(self, requested_plan_id):
            calls.append(("artifact", requested_plan_id))
            return {
                "conversation_id": "conversation",
                "one_shot_attempt_id": None,
            }

    class Pacing:
        def due_for_revalidation(self, **kwargs):
            calls.append(("advance", kwargs))

        def claim_due_outbox(self, **kwargs):
            calls.append(("claim", kwargs))
            return [(17, due)]

    coordinator = DueCoordinator(
        state=State(),
        hub=None,
        pacing=Pacing(),
        rules=None,
        authorization=None,
        dispatcher=None,
        capability_provider=None,
    )

    async def dispatch_claimed(outbox_id, claimed_due, *, artifact=None):
        calls.append(("dispatch", outbox_id, claimed_due, artifact))
        return operation

    coordinator._dispatch_claimed = dispatch_claimed
    consumed, actual = asyncio.run(
        coordinator.dispatch_exact(
            pacing_plan_id=plan_id,
            conversation_id="conversation",
            segment_index=0,
        )
    )

    assert consumed is True
    assert actual is operation
    assert (
        "advance",
        {"pacing_plan_id": plan_id, "one_shot_attempt_id": None},
    ) in calls
    assert (
        "claim",
        {
            "limit": 1,
            "pacing_plan_id": plan_id,
            "segment_index": 0,
            "one_shot_attempt_id": None,
            "recoverable": False,
        },
    ) in calls


def test_due_coordinator_exact_rejects_wrong_conversation_before_advancing() -> None:
    class State:
        def plan_artifact(self, _requested_plan_id):
            return {
                "conversation_id": "actual-conversation",
                "one_shot_attempt_id": None,
            }

    class Pacing:
        def due_for_revalidation(self, **_kwargs):
            raise AssertionError("must not advance a mismatched plan")

    coordinator = DueCoordinator(
        state=State(),
        hub=None,
        pacing=Pacing(),
        rules=None,
        authorization=None,
        dispatcher=None,
        capability_provider=None,
    )

    try:
        asyncio.run(
            coordinator.dispatch_exact(
                pacing_plan_id=uuid4(),
                conversation_id="wrong-conversation",
                segment_index=0,
            )
        )
    except ValueError as exc:
        assert "does not own" in str(exc)
    else:
        raise AssertionError("mismatched conversation must be rejected")
