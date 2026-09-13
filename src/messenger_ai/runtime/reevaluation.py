"""Explicit, offline preparation of one previously blocked planner evaluation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .state import RuntimeState


@dataclass(frozen=True)
class ReevaluationPreparation:
    reevaluation_id: str
    original_request_id: str
    conversation_id: str
    status: str
    source_keys_sha256: str
    current_global_revision: int
    replayed: bool = False


def _uuid(value: str, field: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid") from exc


def _source_keys(provider_request_json: str) -> tuple[str, ...]:
    try:
        request = json.loads(provider_request_json)
        inbound = request["inbound"]
        keys = tuple(item["message_key"] for item in inbound)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("original_provider_request_invalid") from exc
    if (
        not keys
        or any(not isinstance(key, str) or not key for key in keys)
        or len(set(keys)) != len(keys)
    ):
        raise ValueError("original_source_keys_invalid")
    return keys


def _source_hash(source_keys: tuple[str, ...]) -> str:
    value = json.dumps(source_keys, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hub_preconditions(
    hub: sqlite3.Connection,
    *,
    conversation_id: str,
    source_keys: tuple[str, ...],
) -> None:
    placeholders = ",".join("?" for _ in source_keys)
    rows = hub.execute(
        f"""SELECT platform_message_key FROM messages
            WHERE conversation_id=? AND platform_message_key IN ({placeholders})""",
        (conversation_id, *source_keys),
    ).fetchall()
    if {str(row[0]) for row in rows} != set(source_keys):
        raise ValueError("source_messages_not_current_conversation")

    for draft in hub.execute(
        "SELECT draft_id,source_message_keys_json FROM drafts WHERE conversation_id=?",
        (conversation_id,),
    ).fetchall():
        try:
            draft_keys = tuple(json.loads(draft["source_message_keys_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("existing_draft_source_invalid") from exc
        if not set(draft_keys).intersection(source_keys):
            continue
        sent = hub.execute(
            "SELECT 1 FROM send_operations WHERE draft_id=? LIMIT 1",
            (draft["draft_id"],),
        ).fetchone()
        if sent is not None:
            raise ValueError("source_has_send_operation")


def _bridge_preconditions(
    bridge: sqlite3.Connection, *, conversation_id: str
) -> None:
    row = bridge.execute(
        """SELECT status FROM qq_vm_ops
           WHERE conversation_id=? AND status NOT IN ('failed','cancelled')
           LIMIT 1""",
        (conversation_id,),
    ).fetchone()
    if row is not None:
        raise ValueError("conversation_has_qq_send_operation")


def inspect_operator_reevaluation(
    *,
    runtime: sqlite3.Connection,
    hub: sqlite3.Connection,
    bridge: sqlite3.Connection,
    original_request_id: str,
) -> dict[str, Any]:
    """Return only CAS metadata for a possible reevaluation; never write."""

    original_request_id = _uuid(original_request_id, "original_request_id")
    original = runtime.execute(
        "SELECT * FROM runtime_planner_evaluations WHERE request_id=?",
        (original_request_id,),
    ).fetchone()
    if original is None:
        raise ValueError("original_evaluation_not_found")
    source_keys = _source_keys(str(original["provider_request_json"]))
    conversation_id = str(original["conversation_id"])
    conversation = runtime.execute(
        "SELECT * FROM runtime_conversations WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    control = runtime.execute(
        "SELECT revision,paused,reason FROM runtime_global_control WHERE singleton=1"
    ).fetchone()
    job = runtime.execute(
        "SELECT * FROM runtime_planning_jobs WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    latest = runtime.execute(
        """SELECT request_id FROM runtime_planner_evaluations
           WHERE conversation_id=? AND conversation_revision=?
           ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (conversation_id, int(original["conversation_revision"])),
    ).fetchone()

    source_messages_present = True
    source_has_send_operation = False
    hub_precondition_error: str | None = None
    try:
        _hub_preconditions(
            hub, conversation_id=conversation_id, source_keys=source_keys
        )
    except ValueError as exc:
        hub_precondition_error = str(exc)
        source_messages_present = str(exc) != "source_messages_not_current_conversation"
        source_has_send_operation = str(exc) == "source_has_send_operation"
    qq_send_operation_present = False
    try:
        _bridge_preconditions(bridge, conversation_id=conversation_id)
    except ValueError:
        qq_send_operation_present = True

    source_hash = _source_hash(source_keys)
    latest_match = bool(
        latest is not None and latest["request_id"] == original_request_id
    )
    current_revision_match = bool(
        conversation is not None
        and int(conversation["binding_revision"]) == int(original["binding_revision"])
        and int(conversation["conversation_revision"])
        == int(original["conversation_revision"])
    )
    try:
        job_source_keys = tuple(json.loads(job["source_keys_json"])) if job else ()
    except (TypeError, json.JSONDecodeError):
        job_source_keys = ()
    job_match = bool(
        job is not None
        and int(job["conversation_revision"])
        == int(original["conversation_revision"])
        and job_source_keys == source_keys
        and job["status"] == "failed"
    )
    source_has_plan_artifact = False
    for artifact in runtime.execute(
        """SELECT source_keys_json FROM runtime_plan_artifacts
           WHERE conversation_id=? AND conversation_revision=?""",
        (conversation_id, int(original["conversation_revision"])),
    ).fetchall():
        try:
            artifact_keys = tuple(json.loads(artifact["source_keys_json"]))
        except (TypeError, json.JSONDecodeError):
            source_has_plan_artifact = True
            break
        if artifact_keys == source_keys:
            source_has_plan_artifact = True
            break
    source_has_segment_execution = runtime.execute(
        """SELECT 1 FROM runtime_segment_executions
           WHERE conversation_id=? AND conversation_revision=? LIMIT 1""",
        (conversation_id, int(original["conversation_revision"])),
    ).fetchone() is not None
    return {
        "schema": "pmai-planning-reevaluation-inspection-v1",
        "original_request_id": original_request_id,
        "conversation_id": conversation_id,
        "binding_revision": int(original["binding_revision"]),
        "conversation_revision": int(original["conversation_revision"]),
        "original_global_revision": int(original["global_revision"]),
        "current_global_revision": int(control["revision"]) if control else None,
        "global_paused": bool(control["paused"]) if control else None,
        "source_keys_sha256": source_hash,
        "latest_evaluation_match": latest_match,
        "current_revision_match": current_revision_match,
        "failed_job_match": job_match,
        "contact_paused": bool(conversation["paused"]) if conversation else None,
        "source_messages_present": source_messages_present,
        "source_has_send_operation": source_has_send_operation,
        "qq_send_operation_present": qq_send_operation_present,
        "source_has_plan_artifact": source_has_plan_artifact,
        "source_has_segment_execution": source_has_segment_execution,
        "ready_for_prepare": bool(
            original["outcome"] in {"policy_blocked", "review_required"}
            and latest_match
            and current_revision_match
            and job_match
            and conversation is not None
            and not bool(conversation["paused"])
            and control is not None
            and hub_precondition_error is None
            and not source_has_send_operation
            and not qq_send_operation_present
            and not source_has_plan_artifact
            and not source_has_segment_execution
        ),
    }


def prepare_operator_reevaluation(
    *,
    state: RuntimeState,
    hub: sqlite3.Connection,
    bridge: sqlite3.Connection,
    reevaluation_id: str,
    original_request_id: str,
    expected_conversation_id: str,
    expected_binding_revision: int,
    expected_conversation_revision: int,
    expected_current_global_revision: int,
    expected_source_keys_sha256: str,
    operator_id: str,
    reason_code: str,
) -> ReevaluationPreparation:
    """CAS one completed blocked job back to pending without touching messages/cursors.

    The caller must hold the same process owner used by the live runtime for the
    full call.  That excludes concurrent observation, planning, and sends while
    the Hub read checks and RuntimeState transaction are performed.
    """

    reevaluation_id = _uuid(reevaluation_id, "reevaluation_id")
    original_request_id = _uuid(original_request_id, "original_request_id")
    if not operator_id or len(operator_id) > 128:
        raise ValueError("operator_id_invalid")
    if not reason_code or len(reason_code) > 128:
        raise ValueError("reason_code_invalid")
    if expected_binding_revision < 1 or expected_conversation_revision < 1:
        raise ValueError("expected_revision_invalid")
    if expected_current_global_revision < 1:
        raise ValueError("expected_current_global_revision_invalid")
    if (
        len(expected_source_keys_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_source_keys_sha256)
    ):
        raise ValueError("expected_source_keys_sha256_invalid")

    original = state.connection.execute(
        "SELECT * FROM runtime_planner_evaluations WHERE request_id=?",
        (original_request_id,),
    ).fetchone()
    if original is None:
        raise ValueError("original_evaluation_not_found")
    source_keys = _source_keys(str(original["provider_request_json"]))
    source_hash = _source_hash(source_keys)
    if source_hash != expected_source_keys_sha256:
        raise ValueError("source_keys_hash_mismatch")

    _hub_preconditions(
        hub, conversation_id=expected_conversation_id, source_keys=source_keys
    )
    _bridge_preconditions(bridge, conversation_id=expected_conversation_id)

    now = datetime.now(UTC).isoformat()
    with state.uow() as db:
        replay = db.execute(
            "SELECT * FROM runtime_operator_reevaluations WHERE reevaluation_id=?",
            (reevaluation_id,),
        ).fetchone()
        if replay is not None:
            expected = (
                original_request_id,
                expected_conversation_id,
                expected_binding_revision,
                expected_conversation_revision,
                expected_current_global_revision,
                expected_source_keys_sha256,
                operator_id,
                reason_code,
            )
            actual = (
                replay["original_request_id"],
                replay["conversation_id"],
                int(replay["binding_revision"]),
                int(replay["conversation_revision"]),
                int(replay["requested_current_global_revision"]),
                replay["source_keys_sha256"],
                replay["operator_id"],
                replay["reason_code"],
            )
            if actual != expected:
                raise ValueError("reevaluation_idempotency_conflict")
            return ReevaluationPreparation(
                reevaluation_id=reevaluation_id,
                original_request_id=original_request_id,
                conversation_id=expected_conversation_id,
                status=str(replay["status"]),
                source_keys_sha256=source_hash,
                current_global_revision=expected_current_global_revision,
                replayed=True,
            )

        if db.execute(
            "SELECT 1 FROM runtime_operator_reevaluations WHERE original_request_id=?",
            (original_request_id,),
        ).fetchone() is not None:
            raise ValueError("original_evaluation_already_reevaluated")
        original = db.execute(
            "SELECT * FROM runtime_planner_evaluations WHERE request_id=?",
            (original_request_id,),
        ).fetchone()
        if original is None:
            raise ValueError("original_evaluation_not_found")
        if original["outcome"] not in {"policy_blocked", "review_required"}:
            raise ValueError("original_evaluation_not_reviewable")
        latest = db.execute(
            """SELECT request_id FROM runtime_planner_evaluations
               WHERE conversation_id=? AND conversation_revision=?
               ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (expected_conversation_id, expected_conversation_revision),
        ).fetchone()
        if latest is None or latest["request_id"] != original_request_id:
            raise ValueError("original_evaluation_not_latest")
        if (
            original["conversation_id"] != expected_conversation_id
            or int(original["binding_revision"]) != expected_binding_revision
            or int(original["conversation_revision"])
            != expected_conversation_revision
        ):
            raise ValueError("original_evaluation_revision_mismatch")

        conversation = db.execute(
            "SELECT * FROM runtime_conversations WHERE conversation_id=?",
            (expected_conversation_id,),
        ).fetchone()
        if conversation is None:
            raise ValueError("conversation_not_found")
        if (
            int(conversation["binding_revision"]) != expected_binding_revision
            or int(conversation["conversation_revision"])
            != expected_conversation_revision
        ):
            raise ValueError("current_conversation_revision_mismatch")
        if bool(conversation["paused"]):
            raise ValueError("contact_paused")
        control = db.execute(
            "SELECT revision FROM runtime_global_control WHERE singleton=1"
        ).fetchone()
        if (
            control is None
            or int(control["revision"]) != expected_current_global_revision
        ):
            raise ValueError("current_global_revision_mismatch")

        job = db.execute(
            "SELECT * FROM runtime_planning_jobs WHERE conversation_id=?",
            (expected_conversation_id,),
        ).fetchone()
        if job is None or int(job["conversation_revision"]) != expected_conversation_revision:
            raise ValueError("planning_job_revision_mismatch")
        if job["status"] != "failed":
            raise ValueError("planning_job_not_failed")
        try:
            job_source_keys = tuple(json.loads(job["source_keys_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("planning_job_source_invalid") from exc
        if job_source_keys != source_keys:
            raise ValueError("planning_job_source_mismatch")

        artifacts = db.execute(
            """SELECT source_keys_json FROM runtime_plan_artifacts
               WHERE conversation_id=? AND conversation_revision=?""",
            (expected_conversation_id, expected_conversation_revision),
        ).fetchall()
        for artifact in artifacts:
            try:
                artifact_keys = tuple(json.loads(artifact["source_keys_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("plan_artifact_source_invalid") from exc
            if artifact_keys == source_keys:
                raise ValueError("source_has_plan_artifact")
        if db.execute(
            """SELECT 1 FROM runtime_segment_executions
               WHERE conversation_id=? AND conversation_revision=? LIMIT 1""",
            (expected_conversation_id, expected_conversation_revision),
        ).fetchone() is not None:
            raise ValueError("source_has_segment_execution")

        db.execute(
            """INSERT INTO runtime_operator_reevaluations(
               reevaluation_id,original_request_id,new_request_id,conversation_id,
               account_id,contact_id,binding_revision,conversation_revision,
               original_global_revision,requested_current_global_revision,
               source_keys_sha256,operator_id,reason_code,original_outcome,
               original_decision_code,status,cursor_unchanged,observation_unchanged,
               no_send_operations,created_at,completed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                reevaluation_id,
                original_request_id,
                None,
                expected_conversation_id,
                conversation["account_id"],
                conversation["contact_id"],
                expected_binding_revision,
                expected_conversation_revision,
                int(original["global_revision"]),
                expected_current_global_revision,
                source_hash,
                operator_id,
                reason_code,
                original["outcome"],
                original["decision_code"],
                "prepared",
                1,
                1,
                1,
                now,
                None,
            ),
        )
        changed = db.execute(
            """UPDATE runtime_planning_jobs
               SET status='pending',error_code=NULL,updated_at=?,reevaluation_id=?
               WHERE conversation_id=? AND conversation_revision=? AND status='failed'""",
            (
                now,
                reevaluation_id,
                expected_conversation_id,
                expected_conversation_revision,
            ),
        ).rowcount
        if changed != 1:
            raise ValueError("planning_job_cas_failed")

    return ReevaluationPreparation(
        reevaluation_id=reevaluation_id,
        original_request_id=original_request_id,
        conversation_id=expected_conversation_id,
        status="prepared",
        source_keys_sha256=source_hash,
        current_global_revision=expected_current_global_revision,
    )
