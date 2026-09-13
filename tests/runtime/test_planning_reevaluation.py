from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from messenger_ai.runtime.contracts import Direction, ObservationBatch, ObservedMessage
from messenger_ai.runtime.reevaluation import (
    inspect_operator_reevaluation,
    prepare_operator_reevaluation,
)
from messenger_ai.runtime.state import RuntimeState
from messenger_ai.hub.service import SQLiteHubStore


def _fixture(tmp_path: Path):
    state = RuntimeState(tmp_path / "runtime.sqlite3")
    state.register(
        account_id="account",
        contact_id="contact",
        conversation_id="conversation",
        binding_revision=1,
        conversation_type="direct",
    )
    source_key = "qq-uia/conversation/inbound-1"
    now = datetime.now(UTC).isoformat()
    request_id = str(uuid4())
    state.connection.execute(
        """INSERT INTO runtime_planning_jobs(
           conversation_id,conversation_revision,status,attempt_count,error_code,
           updated_at,source_keys_json,reevaluation_id)
           VALUES('conversation',1,'failed',3,'blocked',?,?,NULL)""",
        (now, json.dumps([source_key])),
    )
    state.connection.execute(
        """INSERT INTO runtime_planner_evaluations(
           request_id,conversation_id,conversation_revision,binding_revision,
           global_revision,rule_version,provider_request_json,plan_json,action,
           model,latency_ms,usage_json,outcome,decision_code,
           policy_decisions_json,policy_requests_json,policy_reason_codes_json,
           policy_rule_ids_json,policy_sensitive_categories_json,
           content_policy_checks_enabled,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            request_id,
            "conversation",
            1,
            1,
            1,
            "rules-1",
            json.dumps({"inbound": [{"message_key": source_key}]}),
            json.dumps({"action": "draft"}),
            "draft",
            "model",
            7,
            "{}",
            "policy_blocked",
            "blocked",
            "[]",
            "[]",
            '["PROHIBITED_RULE_HIT"]',
            '["bad-rule"]',
            "[]",
            1,
            now,
        ),
    )
    hub_store = SQLiteHubStore(tmp_path / "hub.sqlite3")
    hub_store.connection.execute(
        """INSERT INTO messages VALUES(
           ?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid4()),
            str(uuid4()),
            "qq",
            "account",
            "conversation",
            "contact",
            source_key,
            now,
            "hidden from operator output",
            "text",
            "{}",
            now,
        ),
    )
    bridge = sqlite3.connect(tmp_path / "qq-vm-bridge.sqlite3")
    bridge.row_factory = sqlite3.Row
    bridge.execute(
        """CREATE TABLE qq_vm_ops(
           operation_id TEXT,conversation_id TEXT,status TEXT)"""
    )
    return state, hub_store, bridge, request_id, source_key


def _prepare(state, hub_store, bridge, request_id, inspection):
    return prepare_operator_reevaluation(
        state=state,
        hub=hub_store.connection,
        bridge=bridge,
        reevaluation_id=str(uuid4()),
        original_request_id=request_id,
        expected_conversation_id="conversation",
        expected_binding_revision=1,
        expected_conversation_revision=1,
        expected_current_global_revision=inspection["current_global_revision"],
        expected_source_keys_sha256=inspection["source_keys_sha256"],
        operator_id="operator",
        reason_code="content_policy_disabled_by_user",
    )


def test_explicit_reevaluation_preserves_old_audit_and_attempt_count(tmp_path):
    state, hub_store, bridge, request_id, _ = _fixture(tmp_path)
    assert state.set_global_pause(paused=True, expected_revision=1, reason="operator_pause")
    inspection = inspect_operator_reevaluation(
        runtime=state.connection,
        hub=hub_store.connection,
        bridge=bridge,
        original_request_id=request_id,
    )
    assert inspection["ready_for_prepare"] is True
    prepared = _prepare(state, hub_store, bridge, request_id, inspection)
    job = state.connection.execute(
        "SELECT status,attempt_count,reevaluation_id FROM runtime_planning_jobs"
    ).fetchone()
    assert (job["status"], job["attempt_count"], job["reevaluation_id"]) == (
        "pending",
        3,
        prepared.reevaluation_id,
    )
    assert state.connection.execute(
        "SELECT COUNT(*) FROM runtime_planner_evaluations"
    ).fetchone()[0] == 1

    assert state.set_global_pause(paused=False, expected_revision=2, reason="operator_resume")
    claimed = state.claim_planning_jobs(limit=1)
    assert len(claimed) == 1
    new_request_id = str(uuid4())
    assert state.complete_planning_evaluation(
        "conversation",
        1,
        binding_revision=1,
        global_revision=3,
        rule_version="rules-1",
        request_id=new_request_id,
        provider_request_json='{"inbound":[]}',
        plan_json='{"action":"ignore"}',
        action="ignore",
        model="model",
        latency_ms=1,
        usage_json="{}",
        audit_outcome="ignore",
        job_outcome="ignored",
        content_policy_checks_enabled=False,
    )
    row = state.connection.execute(
        "SELECT status,new_request_id FROM runtime_operator_reevaluations"
    ).fetchone()
    assert tuple(row) == ("ignore", new_request_id)
    assert state.connection.execute(
        "SELECT COUNT(*) FROM runtime_planner_evaluations"
    ).fetchone()[0] == 2
    assert state.connection.execute(
        "SELECT attempt_count FROM runtime_planning_jobs"
    ).fetchone()[0] == 4


def test_new_observation_supersedes_prepared_reevaluation(tmp_path):
    state, hub_store, bridge, request_id, _ = _fixture(tmp_path)
    inspection = inspect_operator_reevaluation(
        runtime=state.connection,
        hub=hub_store.connection,
        bridge=bridge,
        original_request_id=request_id,
    )
    prepared = _prepare(state, hub_store, bridge, request_id, inspection)
    state.apply_observation(ObservationBatch(
        account_id="account",
        contact_id="contact",
        conversation_id="conversation",
        binding_revision=1,
        conversation_revision=1,
        complete=True,
        messages=(ObservedMessage(
            local_message_key="inbound-2",
            direction=Direction.INBOUND,
            observed_at=datetime.now(UTC),
            text="new",
        ),),
    ))
    audit = state.connection.execute(
        "SELECT status FROM runtime_operator_reevaluations WHERE reevaluation_id=?",
        (prepared.reevaluation_id,),
    ).fetchone()
    job = state.connection.execute(
        "SELECT conversation_revision,reevaluation_id FROM runtime_planning_jobs"
    ).fetchone()
    assert audit["status"] == "superseded"
    assert (job["conversation_revision"], job["reevaluation_id"]) == (2, None)


def test_any_overlapping_source_send_or_qq_operation_rejects(tmp_path):
    state, hub_store, bridge, request_id, source_key = _fixture(tmp_path)
    now = datetime.now(UTC).isoformat()
    hub_store.connection.execute(
        """INSERT INTO drafts VALUES(
           'draft','conversation','contact','body',?,'rules','hash','rejected',1,?,?)""",
        (json.dumps([source_key, "other"]), now, now),
    )
    hub_store.connection.execute(
        """INSERT INTO send_operations VALUES(
           'operation','key','draft',NULL,'failed',NULL,0,1,?,?)""",
        (now, now),
    )
    inspection = inspect_operator_reevaluation(
        runtime=state.connection,
        hub=hub_store.connection,
        bridge=bridge,
        original_request_id=request_id,
    )
    with pytest.raises(ValueError, match="source_has_send_operation"):
        _prepare(state, hub_store, bridge, request_id, inspection)
