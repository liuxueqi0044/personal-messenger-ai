import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from messenger_ai.runtime.contracts import Direction, ObservationBatch, ObservedMessage
from messenger_ai.runtime.state import RuntimeState, VerifiedSendStorePaths


def test_new_runtime_state_can_be_created_globally_paused(tmp_path):
    state = RuntimeState(
        tmp_path / "runtime.sqlite3",
        initially_paused=True,
        initial_pause_reason="isolated_identity_recovery",
    )
    try:
        assert state.global_control() == (1, True, "isolated_identity_recovery")
    finally:
        state.close()


def batch(*messages, revision=1, complete=True):
    return ObservationBatch(
        account_id="account", contact_id="contact", conversation_id="conversation",
        binding_revision=revision, conversation_revision=1, complete=complete,
        messages=messages,
    )


def message(key, direction, *, operation_id=None):
    return ObservedMessage(local_message_key=key, direction=direction, text="same",
                           observed_at=datetime.now(UTC), operation_id=operation_id)


def complete_scheduled_evaluation(
    state: RuntimeState,
    *,
    conversation_revision: int,
    global_revision: int,
) -> bool:
    return state.complete_planning_evaluation(
        "conversation",
        conversation_revision,
        binding_revision=1,
        global_revision=global_revision,
        rule_version="rules-v1",
        request_id=str(uuid4()),
        provider_request_json="{}",
        plan_json='{"action":"reply","reply_segments":["safe"]}',
        action="reply",
        model="test-model",
        latency_ms=1,
        usage_json="{}",
        audit_outcome="scheduled",
        job_outcome="completed",
        plan_artifact={
            "pacing_plan_id": str(uuid4()),
            "eligibility_json": "[]",
            "account_id": "account",
            "contact_id": "contact",
            "source_keys_json": '["qq-uia/conversation/inbound"]',
            "segment_draft_ids_json": "[]",
        },
    )


def test_repeated_text_keeps_distinct_local_keys_and_human_outbound_invalidates():
    state = RuntimeState()
    state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=1)
    events = state.apply_observation(batch(
        message("1", Direction.INBOUND), message("2", Direction.INBOUND),
        message("3", Direction.OUTBOUND),
    ))
    assert events == ("new_message", "new_message", "human_outbound")
    assert state.revisions("conversation") == (1, 4)


def test_one_shot_claims_only_requested_conversation_events_and_job():
    state = RuntimeState()
    attempt_id = uuid4()
    for number in (1, 2):
        state.register(
            account_id="account",
            contact_id=f"contact-{number}",
            conversation_id=f"conversation-{number}",
            binding_revision=1,
            conversation_type="direct",
        )
    global_revision, _, _ = state.global_control()
    reason = "runtime_control:graceful_stop:11111111-1111-4111-8111-111111111111"
    assert state.set_global_pause(
        paused=True, expected_revision=global_revision, reason=reason
    )
    state.connection.execute(
        "UPDATE runtime_event_outbox SET status='delivered' "
        "WHERE one_shot_attempt_id IS NULL"
    )

    with state.one_shot_stopped_runtime_scope(
        attempt_id=attempt_id, conversation_id="conversation-2"
    ):
        state.apply_observation(ObservationBatch(
            account_id="account",
            contact_id="contact-2",
            conversation_id="conversation-2",
            binding_revision=1,
            conversation_revision=1,
            complete=True,
            messages=(message("in-2", Direction.INBOUND),),
        ))
        assert state.claim_events(limit=10) == []
        assert state.claim_planning_jobs(limit=10) == []
        assert state.claim_events_for(
            "conversation-2",
            one_shot_attempt_id=uuid4(),
            limit=10,
        ) == []
        revision = state.revisions("conversation-2")[1]
        assert state.claim_planning_job_for(
            "conversation-2",
            expected_revision=revision,
            one_shot_attempt_id=uuid4(),
        ) is None

        claimed = state.claim_events_for(
            "conversation-2",
            one_shot_attempt_id=attempt_id,
            limit=10,
        )
        assert [row["aggregate_id"] for row in claimed] == ["conversation-2"]
        assert claimed[0]["one_shot_attempt_id"] == str(attempt_id)
        assert state.recover_events() == 1
        assert state.claim_events(limit=10) == []
        claimed = state.claim_events_for(
            "conversation-2",
            one_shot_attempt_id=attempt_id,
            limit=10,
        )
        state.complete_event(claimed[0]["event_id"], delivered=True)
        job = state.claim_planning_job_for(
            "conversation-2",
            expected_revision=revision,
            one_shot_attempt_id=attempt_id,
        )
        assert job is not None
        assert job["conversation_id"] == "conversation-2"
        assert job["one_shot_attempt_id"] == str(attempt_id)


def test_normal_runtime_claims_normal_events_and_jobs() -> None:
    state = RuntimeState()
    state.register(
        account_id="account",
        contact_id="contact",
        conversation_id="conversation",
        binding_revision=1,
        conversation_type="direct",
    )
    state.apply_observation(batch(message("inbound", Direction.INBOUND)))

    events = state.claim_events(limit=10)
    assert len(events) == 1
    assert events[0]["one_shot_attempt_id"] is None
    state.complete_event(events[0]["event_id"], delivered=True)
    jobs = state.claim_planning_jobs(limit=10)
    assert len(jobs) == 1
    assert jobs[0]["one_shot_attempt_id"] is None


@pytest.mark.parametrize("mutation", ["revision", "reason"])
def test_changed_graceful_stop_fence_blocks_one_shot_observation(mutation) -> None:
    state = RuntimeState()
    for conversation_id, contact_id in (
        ("conversation", "contact"),
        ("other-conversation", "other-contact"),
    ):
        state.register(
            account_id="account",
            contact_id=contact_id,
            conversation_id=conversation_id,
            binding_revision=1,
            conversation_type="direct",
        )
    attempt_id = uuid4()
    revision, _, _ = state.global_control()
    reason = "runtime_control:graceful_stop:11111111-1111-4111-8111-111111111111"
    assert state.set_global_pause(
        paused=True, expected_revision=revision, reason=reason
    )

    with state.one_shot_stopped_runtime_scope(
        attempt_id=attempt_id, conversation_id="conversation"
    ):
        assert state.execution_state("conversation")[4] is False
        assert state.execution_state("other-conversation")[4] is True
        if mutation == "revision":
            state.connection.execute(
                "UPDATE runtime_global_control SET revision=revision+1 "
                "WHERE singleton=1"
            )
        else:
            state.connection.execute(
                "UPDATE runtime_global_control SET reason=? WHERE singleton=1",
                (
                    (
                        "runtime_control:graceful_stop:"
                        "22222222-2222-4222-8222-222222222222"
                    ),
                ),
            )
        assert state.global_control()[1] is True
        assert state.execution_state("conversation")[4] is True
        with pytest.raises(
            RuntimeError, match="ONE_SHOT_GRACEFUL_STOP_FENCE_CHANGED"
        ):
            state.apply_observation(
                batch(message("inbound", Direction.INBOUND))
            )

    assert state.connection.execute(
        "SELECT COUNT(*) FROM runtime_observations"
    ).fetchone()[0] == 0
    assert state.connection.execute(
        "SELECT COUNT(*) FROM runtime_planning_jobs"
    ).fetchone()[0] == 0


def test_one_shot_planning_completion_inherits_exact_attempt() -> None:
    state = RuntimeState()
    state.register(
        account_id="account",
        contact_id="contact",
        conversation_id="conversation",
        binding_revision=1,
        conversation_type="direct",
    )
    attempt_id = uuid4()
    revision, _, _ = state.global_control()
    reason = "runtime_control:graceful_stop:11111111-1111-4111-8111-111111111111"
    assert state.set_global_pause(
        paused=True, expected_revision=revision, reason=reason
    )

    with state.one_shot_stopped_runtime_scope(
        attempt_id=attempt_id, conversation_id="conversation"
    ) as context:
        state.apply_observation(batch(message("inbound", Direction.INBOUND)))
        conversation_revision = state.revisions("conversation")[1]
        events = state.claim_events_for(
            "conversation", one_shot_attempt_id=attempt_id
        )
        assert len(events) == 1
        state.complete_event(events[0]["event_id"], delivered=True)
        job = state.claim_planning_job_for(
            "conversation",
            expected_revision=conversation_revision,
            one_shot_attempt_id=attempt_id,
        )
        assert job is not None
        assert complete_scheduled_evaluation(
            state,
            conversation_revision=conversation_revision,
            global_revision=context.global_revision,
        )
        artifact = state.plan_artifact_for_revision(
            "conversation",
            conversation_revision,
            one_shot_attempt_id=attempt_id,
        )
        assert artifact["one_shot_attempt_id"] == str(attempt_id)


@pytest.mark.parametrize("mutation", ["revision", "reason"])
def test_changed_graceful_stop_fence_blocks_planning_completion(mutation) -> None:
    state = RuntimeState()
    state.register(
        account_id="account",
        contact_id="contact",
        conversation_id="conversation",
        binding_revision=1,
        conversation_type="direct",
    )
    attempt_id = uuid4()
    revision, _, _ = state.global_control()
    reason = "runtime_control:graceful_stop:11111111-1111-4111-8111-111111111111"
    assert state.set_global_pause(
        paused=True, expected_revision=revision, reason=reason
    )

    with state.one_shot_stopped_runtime_scope(
        attempt_id=attempt_id, conversation_id="conversation"
    ) as context:
        state.apply_observation(batch(message("inbound", Direction.INBOUND)))
        conversation_revision = state.revisions("conversation")[1]
        events = state.claim_events_for(
            "conversation", one_shot_attempt_id=attempt_id
        )
        state.complete_event(events[0]["event_id"], delivered=True)
        assert state.claim_planning_job_for(
            "conversation",
            expected_revision=conversation_revision,
            one_shot_attempt_id=attempt_id,
        ) is not None
        if mutation == "revision":
            state.connection.execute(
                "UPDATE runtime_global_control SET revision=revision+1 "
                "WHERE singleton=1"
            )
        else:
            state.connection.execute(
                "UPDATE runtime_global_control SET reason=? WHERE singleton=1",
                (
                    (
                        "runtime_control:graceful_stop:"
                        "22222222-2222-4222-8222-222222222222"
                    ),
                ),
            )

        assert not complete_scheduled_evaluation(
            state,
            conversation_revision=conversation_revision,
            global_revision=context.global_revision,
        )

    assert state.connection.execute(
        "SELECT COUNT(*) FROM runtime_plan_artifacts"
    ).fetchone()[0] == 0
    job = state.connection.execute(
        "SELECT status,error_code FROM runtime_planning_jobs"
    ).fetchone()
    assert (job["status"], job["error_code"]) == ("stale", "stale")


def test_one_shot_plan_artifact_lookup_requires_unique_exact_revision():
    state = RuntimeState()
    plan_id = uuid4()
    state.save_plan_artifact(
        pacing_plan_id=plan_id,
        conversation_id="conversation",
        conversation_revision=4,
        binding_revision=2,
        global_revision=1,
        eligibility_json="[]",
        planner_json="{}",
        rule_version="rules-v1",
        account_id="account",
        contact_id="contact",
        source_keys=("source",),
    )

    assert state.plan_artifact_for_revision("conversation", 4)[
        "pacing_plan_id"
    ] == str(plan_id)
    with pytest.raises(KeyError):
        state.plan_artifact_for_revision("conversation", 5)


def _seed_verified_send_proof(
    tmp_path, *, operation_id, authorization_id, plan_id, conversation_id="conversation"
):
    hub = sqlite3.connect(tmp_path / "hub.sqlite3")
    hub.executescript(
        "CREATE TABLE send_operations(operation_id TEXT PRIMARY KEY,draft_id TEXT,"
        "authorization_id TEXT,status TEXT,commit_intent INTEGER);"
        "CREATE TABLE drafts(draft_id TEXT PRIMARY KEY,conversation_id TEXT);"
    )
    hub.execute(
        "INSERT INTO drafts VALUES(?,?)", ("draft", conversation_id)
    )
    hub.execute(
        "INSERT INTO send_operations VALUES(?,?,?,?,?)",
        (str(operation_id), "draft", str(authorization_id), "verified", 1),
    )
    hub.commit()
    hub.close()
    pacing = sqlite3.connect(tmp_path / "pacing.sqlite3")
    pacing.execute(
        "CREATE TABLE m10_segment_receipts(pacing_plan_id TEXT,segment_index "
        "INTEGER,operation_id TEXT,verified INTEGER)"
    )
    pacing.execute(
        "INSERT INTO m10_segment_receipts VALUES(?,?,?,1)",
        (str(plan_id), 0, str(operation_id)),
    )
    pacing.commit()
    pacing.close()


def test_bot_echo_requires_known_operation_and_does_not_advance_revision(tmp_path):
    state = RuntimeState(
        tmp_path / "runtime.sqlite3",
        verified_send_stores=VerifiedSendStorePaths(
            hub=tmp_path / "hub.sqlite3", pacing=tmp_path / "pacing.sqlite3"
        ),
    )
    state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=1)
    operation_id = uuid4()
    plan_id = uuid4()
    assert state.create_segment_execution(pacing_plan_id=plan_id, segment_index=0,
                                          conversation_id="conversation",
                                          body_hash="a" * 64, binding_revision=1,
                                          conversation_revision=1)
    authorization_id = uuid4()
    assert state.bind_segment_operation(
        pacing_plan_id=plan_id, segment_index=0, operation_id=operation_id,
        authorization_id=str(authorization_id),
    )
    assert state.bind_segment_operation(
        pacing_plan_id=plan_id, segment_index=0,
        operation_id=operation_id, authorization_id=authorization_id,
    )
    _seed_verified_send_proof(
        tmp_path,
        operation_id=operation_id,
        authorization_id=authorization_id,
        plan_id=plan_id,
    )
    assert state.settle_verified_segment(
        pacing_plan_id=plan_id,
        segment_index=0,
        operation_id=operation_id,
        authorization_id=authorization_id,
    )
    assert state.apply_observation(batch(message("echo", Direction.OUTBOUND, operation_id=operation_id))) == ("bot_observed",)
    assert state.revisions("conversation") == (1, 1)
    unknown = state.apply_observation(batch(message("spoof", Direction.OUTBOUND, operation_id=uuid4())))
    assert unknown == ("direction_unknown",)
    assert state.revisions("conversation") == (1, 2)


def test_verified_segment_recovery_requires_durable_stores_and_is_append_only(
    tmp_path,
):
    stores = VerifiedSendStorePaths(
        hub=tmp_path / "hub.sqlite3", pacing=tmp_path / "pacing.sqlite3"
    )
    state = RuntimeState(
        tmp_path / "runtime.sqlite3", verified_send_stores=stores
    )
    operation_id = uuid4()
    authorization_id = uuid4()
    plan_id = uuid4()
    assert state.create_segment_execution(
        pacing_plan_id=plan_id,
        segment_index=0,
        conversation_id="conversation",
        body_hash="a" * 64,
        binding_revision=1,
        conversation_revision=1,
    )
    assert state.bind_segment_operation(
        pacing_plan_id=plan_id,
        segment_index=0,
        operation_id=operation_id,
        authorization_id=str(authorization_id),
    )
    assert state.recover_verified_segments() == 0
    _seed_verified_send_proof(
        tmp_path,
        operation_id=operation_id,
        authorization_id=authorization_id,
        plan_id=plan_id,
    )
    assert state.recover_verified_segments() == 1
    assert state.recover_verified_segments() == 0
    assert _segment_status(state, plan_id) == "verified"
    assert state.connection.execute(
        "SELECT COUNT(*) FROM runtime_verified_send_proofs"
    ).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        state.connection.execute("DELETE FROM runtime_verified_send_proofs")


def test_legacy_verified_status_without_durable_proof_is_not_a_bot_echo():
    state = RuntimeState()
    state.register(
        account_id="account",
        contact_id="contact",
        conversation_id="conversation",
        binding_revision=1,
    )
    operation_id = uuid4()
    plan_id = uuid4()
    state.connection.execute(
        """INSERT INTO runtime_segment_executions(
               pacing_plan_id,segment_index,conversation_id,body_hash,
               authorization_id,operation_id,status,binding_revision,
               conversation_revision)
           VALUES(?,0,'conversation',?,'legacy-auth',?,'verified',1,1)""",
        (str(plan_id), "a" * 64, str(operation_id)),
    )
    assert state.apply_observation(
        batch(message("legacy-echo", Direction.OUTBOUND, operation_id=operation_id))
    ) == ("direction_unknown",)


def _pending_segment(state: RuntimeState) -> tuple:
    plan_id = uuid4()
    assert state.create_segment_execution(
        pacing_plan_id=plan_id, segment_index=0, conversation_id="conversation",
        body_hash="a" * 64, binding_revision=1, conversation_revision=1,
    )
    return plan_id


def _segment_status(state: RuntimeState, plan_id) -> str:
    return state.connection.execute(
        """SELECT status FROM runtime_segment_executions
           WHERE pacing_plan_id=? AND segment_index=0""",
        (str(plan_id),),
    ).fetchone()[0]


def test_bind_verified_operation_cannot_fabricate_verified_without_receipt():
    state = RuntimeState()
    state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=1)
    operation_id = uuid4()
    plan_id = _pending_segment(state)
    assert state.bind_verified_operation(
        pacing_plan_id=plan_id, segment_index=0,
        operation_id=operation_id, authorization_id="auth",
    )
    # Without proof the operation is merely bound; the segment is not verified.
    assert _segment_status(state, plan_id) == "authorized"
    with pytest.raises(ValueError, match="unsupported send operation status"):
        state.settle_segment_operation(
            pacing_plan_id=plan_id,
            segment_index=0,
            operation_id=operation_id,
            authorization_id="auth",
            operation_status="verified",
        )
    assert state.apply_observation(
        batch(message("echo", Direction.OUTBOUND, operation_id=operation_id))
    ) == ("direction_unknown",)


def test_bind_verified_operation_rejects_caller_supplied_receipt():
    state = RuntimeState()
    state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=1)
    operation_id = uuid4()
    plan_id = _pending_segment(state)
    assert not state.bind_verified_operation(
        pacing_plan_id=plan_id, segment_index=0,
        operation_id=operation_id, authorization_id="auth",
        receipt={
            "operation_id": str(operation_id),
            "conversation_id": "conversation",
            "bridge_receipt": "caller-asserted-fingerprint",
            "pacing_verified": True,
        },
    )
    assert _segment_status(state, plan_id) == "due"
def test_stale_binding_is_rejected_and_manual_pause_is_not_cleared_by_observe():
    state = RuntimeState()
    state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=2)
    assert state.apply_observation(batch(revision=1)) == ("binding_changed",)
    stale = state.connection.execute("SELECT paused,pause_reason FROM runtime_conversations").fetchone()
    assert (stale["paused"], stale["pause_reason"]) == (1, "binding_revision_changed")
    state.pause("conversation")
    revision = state.revisions("conversation")[1]
    state.apply_observation(batch(revision=2))
    row = state.connection.execute("SELECT paused,pause_reason,conversation_revision FROM runtime_conversations").fetchone()
    assert (row["paused"], row["pause_reason"], row["conversation_revision"]) == (1, "manual_pause", revision)


def test_complete_observation_recovers_only_temporary_driver_pause():
    state = RuntimeState()
    state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=1)
    state.apply_observation(batch(complete=False).model_copy(
        update={"gap_reason": "driver_temporary:qq_window_ambiguous"}
    ))
    paused = state.connection.execute(
        "SELECT paused,pause_reason FROM runtime_conversations"
    ).fetchone()
    assert (paused["paused"], paused["pause_reason"]) == (
        1,
        "driver_temporary:qq_window_ambiguous",
    )

    state.apply_observation(batch(complete=True))
    recovered = state.connection.execute(
        "SELECT paused,pause_reason FROM runtime_conversations"
    ).fetchone()
    assert (recovered["paused"], recovered["pause_reason"]) == (0, None)


def test_one_shot_scope_masks_only_canonical_graceful_stop_in_process():
    state = RuntimeState()
    state.register(
        account_id="account",
        contact_id="contact",
        conversation_id="conversation",
        binding_revision=1,
    )
    revision, _, _ = state.global_control()
    run_id = "11111111-1111-4111-8111-111111111111"
    reason = f"runtime_control:graceful_stop:{run_id}"
    assert state.set_global_pause(
        paused=True, expected_revision=revision, reason=reason
    )

    attempt_id = uuid4()
    with state.one_shot_stopped_runtime_scope(
        attempt_id=attempt_id, conversation_id="conversation"
    ) as context:
        assert context.attempt_id == attempt_id
        assert context.conversation_id == "conversation"
        assert context.global_revision == revision + 1
        assert context.graceful_stop_reason == reason
        assert state.global_control()[1:] == (False, reason)
        durable = state.connection.execute(
            "SELECT paused,reason FROM runtime_global_control WHERE singleton=1"
        ).fetchone()
        assert (durable["paused"], durable["reason"]) == (1, reason)

    assert state.global_control()[1:] == (True, reason)


@pytest.mark.parametrize(
    "reason",
    ["manual_global_pause", "runtime_control:graceful_stop:not-a-uuid"],
)
def test_one_shot_scope_rejects_non_graceful_global_pause(reason):
    state = RuntimeState()
    state.register(
        account_id="account",
        contact_id="contact",
        conversation_id="conversation",
        binding_revision=1,
    )
    revision, _, _ = state.global_control()
    assert state.set_global_pause(
        paused=True, expected_revision=revision, reason=reason
    )

    with (
        pytest.raises(
            RuntimeError, match="ONE_SHOT_GRACEFUL_STOP_SCOPE_REJECTED"
        ),
        state.one_shot_stopped_runtime_scope(
            attempt_id=uuid4(), conversation_id="conversation"
        ),
    ):
        pass


def test_one_shot_observation_entry_allows_only_temporary_contact_pause():
    state = RuntimeState()
    state.register(
        account_id="account",
        contact_id="contact",
        conversation_id="conversation",
        binding_revision=1,
        conversation_type="direct",
    )
    state.pause("conversation", reason="driver_temporary:worker_not_alive")
    assert state.one_shot_observation_execution_state("conversation")[2] is False

    state.pause("conversation", reason="message_anchor_gap")
    assert state.one_shot_observation_execution_state("conversation")[2] is True


def test_legacy_runtime_database_adds_evaluation_audit_without_data_loss(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    state = RuntimeState(path)
    state.register(
        account_id="account",
        contact_id="contact",
        conversation_id="conversation",
        binding_revision=1,
    )
    state.connection.execute(
        """INSERT INTO runtime_planner_decisions VALUES(
           'legacy-request','conversation',1,1,'ignore','legacy','model',1,
           '2026-01-01T00:00:00+00:00')"""
    )
    state.connection.execute("DROP TABLE runtime_planner_evaluations")
    state.close()

    migrated = RuntimeState(path)
    assert tuple(migrated.connection.execute(
        "SELECT account_id,contact_id FROM runtime_conversations WHERE conversation_id='conversation'"
    ).fetchone()) == ("account", "contact")
    columns = {
        row["name"] for row in migrated.connection.execute(
            "PRAGMA table_info(runtime_planner_evaluations)"
        )
    }
    assert {
        "provider_request_json",
        "plan_json",
        "action",
        "policy_decisions_json",
        "content_policy_checks_enabled",
    } <= columns
    assert migrated.connection.execute(
        "SELECT selection_reason FROM runtime_planner_decisions WHERE request_id='legacy-request'"
    ).fetchone()[0] == "legacy"
    migrated.close()


def test_legacy_runtime_database_adds_nullable_one_shot_provenance(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE runtime_event_outbox(
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          dedupe_key TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL,
          aggregate_id TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL
        );
        CREATE TABLE runtime_planning_jobs(
          conversation_id TEXT PRIMARY KEY,
          conversation_revision INTEGER NOT NULL,
          status TEXT NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          error_code TEXT,
          updated_at TEXT NOT NULL,
          source_keys_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE runtime_plan_artifacts(
          pacing_plan_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL,
          conversation_revision INTEGER NOT NULL,
          binding_revision INTEGER NOT NULL,
          global_revision INTEGER NOT NULL,
          eligibility_json TEXT NOT NULL,
          planner_json TEXT NOT NULL,
          rule_version TEXT NOT NULL,
          account_id TEXT NOT NULL,
          contact_id TEXT NOT NULL,
          source_keys_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'waiting'
        );
        INSERT INTO runtime_event_outbox(
          dedupe_key,event_type,aggregate_id,payload_json,created_at
        ) VALUES('legacy-event','new_message','conversation','{}','now');
        INSERT INTO runtime_planning_jobs(
          conversation_id,conversation_revision,status,updated_at
        ) VALUES('conversation',1,'pending','now');
        INSERT INTO runtime_plan_artifacts(
          pacing_plan_id,conversation_id,conversation_revision,binding_revision,
          global_revision,eligibility_json,planner_json,rule_version,account_id,
          contact_id,source_keys_json
        ) VALUES('legacy-plan','conversation',1,1,1,'[]','{}','rules','account',
                 'contact','[]');
        """
    )
    connection.close()

    state = RuntimeState(path)
    for table in (
        "runtime_event_outbox",
        "runtime_planning_jobs",
        "runtime_plan_artifacts",
    ):
        columns = {
            row["name"]
            for row in state.connection.execute(f"PRAGMA table_info({table})")
        }
        assert "one_shot_attempt_id" in columns
        assert state.connection.execute(
            f"SELECT one_shot_attempt_id FROM {table} LIMIT 1"
        ).fetchone()[0] is None
    state.close()


def test_complete_observation_does_not_recover_identity_guard_pause():
    state = RuntimeState()
    state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=1)
    state.apply_observation(batch(complete=False).model_copy(
        update={"gap_reason": "identity_guard:session_identity_process_restarted"}
    ))
    state.apply_observation(batch(complete=True))
    row = state.connection.execute(
        "SELECT paused,pause_reason FROM runtime_conversations"
    ).fetchone()
    assert (row["paused"], row["pause_reason"]) == (
        1,
        "identity_guard:session_identity_process_restarted",
    )


def test_repeated_untyped_registration_preserves_certified_type():
    state = RuntimeState()
    state.register(
        account_id="account", contact_id="contact", conversation_id="conversation",
        binding_revision=1, conversation_type="direct",
    )
    state.register(
        account_id="account", contact_id="contact", conversation_id="conversation",
        binding_revision=1,
    )
    row = state.connection.execute(
        "SELECT conversation_type FROM runtime_conversations"
    ).fetchone()
    assert row["conversation_type"] == "direct"


def test_new_untyped_binding_revision_invalidates_certified_type():
    state = RuntimeState()
    state.register(
        account_id="account", contact_id="contact", conversation_id="conversation",
        binding_revision=1, conversation_type="direct",
    )
    state.register(
        account_id="account", contact_id="contact", conversation_id="conversation",
        binding_revision=2,
    )
    row = state.connection.execute(
        "SELECT conversation_type FROM runtime_conversations"
    ).fetchone()
    assert row["conversation_type"] == "unknown"


def test_existing_database_migrates_conversation_type_to_unknown(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE runtime_conversations("
        "conversation_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,"
        "contact_id TEXT NOT NULL,binding_revision INTEGER NOT NULL,"
        "conversation_revision INTEGER NOT NULL DEFAULT 1,"
        "paused INTEGER NOT NULL DEFAULT 0,pause_reason TEXT,"
        "last_observed_at TEXT,UNIQUE(account_id,contact_id))"
    )
    connection.execute(
        "INSERT INTO runtime_conversations(conversation_id,account_id,contact_id,binding_revision) "
        "VALUES('conversation','account','contact',1)"
    )
    connection.commit()
    connection.close()

    state = RuntimeState(path)
    row = state.connection.execute(
        "SELECT conversation_type FROM runtime_conversations"
    ).fetchone()
    assert row["conversation_type"] == "unknown"
