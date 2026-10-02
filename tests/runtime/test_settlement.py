from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import pytest

from messenger_ai.adapters.qq.vm_driver.bridge import QQVMDriverBridge
from messenger_ai.domain.models import Draft
from messenger_ai.hub.service import SQLiteHubStore
from messenger_ai.pacing.scheduler import PacingScheduler
from messenger_ai.runtime import settlement as settlement_module
from messenger_ai.runtime.settlement import (
    SettlementRefused,
    TerminalSettlementTarget,
    settle_terminal_send,
    verify_terminal_settlement,
)
from messenger_ai.runtime.state import RuntimeState

NOW = "2026-09-13T00:00:00+00:00"
CONVERSATION_ID = "conversation-terminal"
BINDING_ID = "contact-terminal"
PLAN_ID = "00000000-0000-0000-0000-000000000101"
DRAFT_ID = "00000000-0000-0000-0000-000000000102"
AUTHORIZATION_ID = "00000000-0000-0000-0000-000000000103"
OPERATION_ID = "00000000-0000-0000-0000-000000000104"
SEND_OUTBOX_ID = "00000000-0000-0000-0000-000000000105"
STABLE_OUTBOX_ID = "00000000-0000-0000-0000-000000000106"
DRAFT_OUTBOX_ID = "00000000-0000-0000-0000-000000000107"
REVIEW_ID = "00000000-0000-0000-0000-0000000001a5"
DECISION_ID = "00000000-0000-0000-0000-0000000001a6"
SIBLING_DRAFT_ID = "00000000-0000-0000-0000-0000000001a7"
RELATED_OUTBOX_ID = "00000000-0000-0000-0000-0000000001a1"
DUPLICATE_SEND_OUTBOX_ID = "00000000-0000-0000-0000-0000000001a2"
NEWER_STABLE_OUTBOX_ID = "00000000-0000-0000-0000-0000000001a3"
UNRELATED_CONVERSATION_ID = "conversation-unrelated"
UNRELATED_OUTBOX_ID = "00000000-0000-0000-0000-0000000001a4"
TEXT = "terminal send must never be replayed"
BODY_HASH = hashlib.sha256(TEXT.encode()).hexdigest()


def _draft_payload(
    *,
    draft_id: str = DRAFT_ID,
    conversation_id: str = CONVERSATION_ID,
    contact_id: str = BINDING_ID,
    text: str = TEXT,
    rule_version: str = "rules-v1",
    source_message_keys: tuple[str, ...] = ("inbound-1",),
) -> dict[str, object]:
    """The exact JSON shape ``HubService`` emits for ``draft.created``."""

    draft = Draft(
        draft_id=UUID(draft_id),
        conversation_id=conversation_id,
        contact_id=contact_id,
        text=text,
        source_message_keys=source_message_keys,
        rule_version=rule_version,
    )
    return draft.model_dump(mode="json")


@dataclass(frozen=True)
class EvidenceFixture:
    root: Path
    target: TerminalSettlementTarget


class NoopWorker:
    def stop(self) -> None:
        pass


def _target() -> TerminalSettlementTarget:
    return TerminalSettlementTarget(
        settlement_id="settlement-terminal-1",
        operator_id="codex-test",
        reason_code="FAILED_SAFE_NO_COMMIT",
        conversation_id=CONVERSATION_ID,
        pacing_plan_id=PLAN_ID,
        segment_index=0,
        operation_id=OPERATION_ID,
        authorization_id=AUTHORIZATION_ID,
        draft_id=DRAFT_ID,
        hub_send_outbox_id=SEND_OUTBOX_ID,
        hub_stable_outbox_id=STABLE_OUTBOX_ID,
        hub_draft_outbox_id=DRAFT_OUTBOX_ID,
        binding_id=BINDING_ID,
        binding_revision=1,
        conversation_revision=3,
        body_hash=BODY_HASH,
    )


def _build_evidence(root: Path) -> EvidenceFixture:
    target = _target()

    state = RuntimeState(root / "runtime.sqlite3")
    state.register(
        account_id="account-terminal",
        contact_id=BINDING_ID,
        conversation_id=CONVERSATION_ID,
        binding_revision=1,
    )
    state.save_plan_artifact(
        pacing_plan_id=UUID(PLAN_ID),
        conversation_id=CONVERSATION_ID,
        conversation_revision=3,
        binding_revision=1,
        global_revision=7,
        eligibility_json="[]",
        planner_json=json.dumps({"reply_segments": [TEXT]}),
        rule_version="rules-v1",
        account_id="account-terminal",
        contact_id=BINDING_ID,
        source_keys=("inbound-1",),
        segment_draft_ids=(DRAFT_ID,),
    )
    assert state.create_segment_execution(
        pacing_plan_id=UUID(PLAN_ID),
        segment_index=0,
        conversation_id=CONVERSATION_ID,
        body_hash=BODY_HASH,
        binding_revision=1,
        conversation_revision=3,
    )
    assert state.bind_segment_operation(
        pacing_plan_id=UUID(PLAN_ID),
        segment_index=0,
        authorization_id=AUTHORIZATION_ID,
        operation_id=UUID(OPERATION_ID),
    )
    assert state.set_global_pause(
        paused=True,
        expected_revision=1,
        reason="test_terminal_settlement",
    )
    state.close()

    hub = SQLiteHubStore(root / "hub.sqlite3")
    with hub.uow() as db:
        db.execute(
            """INSERT INTO conversations(
                   conversation_id,account_id,contact_id,last_message_key,
                   last_inbound_at,stable_after,version,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                CONVERSATION_ID,
                "account-terminal",
                BINDING_ID,
                "inbound-1",
                NOW,
                NOW,
                1,
                NOW,
                NOW,
            ),
        )
        db.execute(
            """INSERT INTO drafts(
                   draft_id,conversation_id,contact_id,text,
                   source_message_keys_json,rule_version,text_hash,status,
                   created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                DRAFT_ID,
                CONVERSATION_ID,
                BINDING_ID,
                TEXT,
                "[]",
                "rules-v1",
                BODY_HASH,
                "authorized",
                NOW,
                NOW,
            ),
        )
        db.execute(
            """INSERT INTO authorizations(
                   authorization_id,draft_id,conversation_id,
                   expected_last_message_key,text_hash,idempotency_key,
                   authorization_type,policy_version,expires_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                AUTHORIZATION_ID,
                DRAFT_ID,
                CONVERSATION_ID,
                "inbound-1",
                BODY_HASH,
                "authorization-idempotency-1",
                "policy",
                "rules-v1",
                "2026-09-13T01:00:00+00:00",
                NOW,
            ),
        )
        db.execute(
            """INSERT INTO send_operations(
                   operation_id,idempotency_key,draft_id,authorization_id,
                   status,error_code,commit_intent,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                OPERATION_ID,
                "send-idempotency-1",
                DRAFT_ID,
                AUTHORIZATION_ID,
                "failed",
                "FAILED_SAFE",
                0,
                NOW,
                NOW,
            ),
        )
        db.executemany(
            """INSERT INTO outbox(
                   outbox_id,dedupe_key,event_type,aggregate_id,payload_json,
                   status,attempt_count,available_at,version,created_at)
               VALUES(?,?,?,?,?,'pending',0,?,1,?)""",
            (
                (
                    SEND_OUTBOX_ID,
                    "send-requested-terminal",
                    "send.requested",
                    OPERATION_ID,
                    json.dumps({"operation_id": OPERATION_ID}),
                    NOW,
                    NOW,
                ),
                (
                    STABLE_OUTBOX_ID,
                    "stable-window-terminal",
                    "conversation.stable_window",
                    CONVERSATION_ID,
                    json.dumps(
                        {
                            "conversation_id": CONVERSATION_ID,
                            "last_message_key": "inbound-1",
                        }
                    ),
                    NOW,
                    NOW,
                ),
                (
                    DRAFT_OUTBOX_ID,
                    f"draft:{DRAFT_ID}:created",
                    "draft.created",
                    DRAFT_ID,
                    json.dumps(_draft_payload()),
                    NOW,
                    NOW,
                ),
            ),
        )
    hub.close()

    bridge = QQVMDriverBridge(
        worker=NoopWorker(),  # type: ignore[arg-type]
        bindings=(),
        text_provider=lambda _command: TEXT,
        sqlite_path=root / "qq-vm-bridge.sqlite3",
        recover_persistent_state=False,
    )
    bridge.close()
    with sqlite3.connect(root / "qq-vm-bridge.sqlite3") as db:
        db.execute(
            """INSERT INTO qq_vm_ops(
                   operation_id,idempotency_key,draft_id,conversation_id,
                   binding_id,segment_ref,binding_revision,
                   conversation_revision,text_hash,status,commit_intent,error_code)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                OPERATION_ID,
                "bridge-idempotency-1",
                DRAFT_ID,
                CONVERSATION_ID,
                BINDING_ID,
                f"{PLAN_ID}:0",
                1,
                3,
                BODY_HASH,
                "failed",
                0,
                "FAILED_SAFE",
            ),
        )

    pacing = PacingScheduler(root / "pacing.sqlite3")
    plan_payload = {
        "pacing_plan_id": PLAN_ID,
        "conversation_id": CONVERSATION_ID,
        "contact_id": BINDING_ID,
        "segments": [TEXT],
        "segment_draft_ids": [DRAFT_ID],
    }
    due_payload = {
        "pacing_plan_id": PLAN_ID,
        "conversation_id": CONVERSATION_ID,
        "segment_index": 0,
        "draft_id": DRAFT_ID,
        "body_hash": BODY_HASH,
    }
    pacing.connection.execute(
        """INSERT INTO m10_plans(
               pacing_plan_id,conversation_id,contact_id,status,
               earliest_send_at,expires_at,payload_json,cancel_reason,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            PLAN_ID,
            CONVERSATION_ID,
            BINDING_ID,
            "rejected",
            NOW,
            "2026-09-13T01:00:00+00:00",
            json.dumps(plan_payload),
            "revalidation_rejected",
            NOW,
            NOW,
        ),
    )
    pacing.connection.execute(
        """INSERT INTO m10_due_outbox(
               pacing_plan_id,segment_index,payload_json,status,
               operation_id,delivered_at,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (PLAN_ID, 0, json.dumps(due_payload), "delivered", None, NOW, NOW),
    )
    pacing.connection.execute(
        """INSERT INTO m10_segment_receipts(
               pacing_plan_id,segment_index,operation_id,verified,created_at)
           VALUES(?,?,?,?,?)""",
        (PLAN_ID, 0, OPERATION_ID, 0, NOW),
    )
    pacing.close()

    return EvidenceFixture(root=root, target=target)


def _execute(path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    with sqlite3.connect(path) as db:
        db.execute(sql, params)


def _runtime_statuses(root: Path) -> tuple[str, str]:
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        segment = db.execute(
            "SELECT status FROM runtime_segment_executions"
        ).fetchone()[0]
        artifact = db.execute(
            "SELECT status FROM runtime_plan_artifacts"
        ).fetchone()[0]
    return str(segment), str(artifact)


def _audit_table_exists(root: Path) -> bool:
    """Whether the trusted, WITHOUT ROWID v2 runtime audit table exists."""

    with sqlite3.connect(root / "runtime.sqlite3") as db:
        row = db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='runtime_send_settlement_audit_v2'"""
        ).fetchone()
    return row is not None


def _legacy_audit_table_exists(root: Path) -> bool:
    """Whether the retired, untrusted runtime audit table is still present."""

    with sqlite3.connect(root / "runtime.sqlite3") as db:
        row = db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='runtime_send_settlement_audit'"""
        ).fetchone()
    return row is not None


def test_dry_run_is_read_only_and_accepts_exact_legacy_due_evidence(tmp_path: Path) -> None:
    fixture = _build_evidence(tmp_path)

    result = settle_terminal_send(fixture.root, fixture.target)

    assert result.applied is False
    assert result.idempotent is False
    assert result.terminal_status == "failed"
    assert result.artifact_status == "rejected"
    assert result.legacy_due_operation_id_missing is True
    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    assert _audit_table_exists(fixture.root) is False
    assert _legacy_audit_table_exists(fixture.root) is False
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT status FROM outbox ORDER BY outbox_id"
        ).fetchall() == [("pending",), ("pending",), ("pending",)]
        assert db.execute(
            """SELECT COUNT(*) FROM sqlite_master WHERE type='table'
               AND name='terminal_send_outbox_cancel_audit_v2'"""
        ).fetchone()[0] == 0


def test_unpaused_runtime_is_refused(tmp_path: Path) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(
        fixture.root / "runtime.sqlite3",
        "UPDATE runtime_global_control SET paused=0,reason=NULL",
    )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_RUNTIME_NOT_PAUSED",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    assert _audit_table_exists(fixture.root) is False
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT status FROM outbox ORDER BY outbox_id"
        ).fetchall() == [("pending",), ("pending",), ("pending",)]


def test_apply_atomically_settles_runtime_and_writes_append_only_audit(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)

    result = settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert result.applied is True
    assert result.idempotent is False
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        db.row_factory = sqlite3.Row
        audit = db.execute(
            "SELECT * FROM runtime_send_settlement_audit_v2"
        ).fetchone()
        assert audit is not None
        assert audit["settlement_id"] == fixture.target.settlement_id
        assert audit["operation_id"] == OPERATION_ID
        assert audit["terminal_status"] == "failed"
        assert audit["artifact_status"] == "rejected"
        assert audit["evidence_sha256"] == result.evidence_sha256
        evidence = json.loads(audit["evidence_json"])
        assert evidence["schema"] == "pmai-terminal-send-settlement-evidence-v2"
        assert evidence["legacy_due_operation_id_missing"] is True
        assert evidence["other_active_work"] == 0
        assert TEXT not in audit["evidence_json"]

        with pytest.raises(
            sqlite3.IntegrityError,
            match="runtime_send_settlement_audit_append_only",
        ):
            db.execute(
                "UPDATE runtime_send_settlement_audit_v2 SET reason_code='ALTERED'"
            )
        db.rollback()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="runtime_send_settlement_audit_append_only",
        ):
            db.execute("DELETE FROM runtime_send_settlement_audit_v2")
        db.rollback()

    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT status FROM outbox ORDER BY outbox_id"
        ).fetchall() == [("cancelled",), ("cancelled",), ("cancelled",)]
        assert db.execute(
            "SELECT COUNT(*) FROM terminal_send_outbox_cancel_audit_v2"
        ).fetchone()[0] == 3
        with pytest.raises(
            sqlite3.IntegrityError,
            match="terminal_send_outbox_cancel_audit_append_only",
        ):
            db.execute("DELETE FROM terminal_send_outbox_cancel_audit_v2")
        db.rollback()

    assert verify_terminal_settlement(
        fixture.root,
        conversation_id=CONVERSATION_ID,
        pacing_plan_id=PLAN_ID,
        segment_index=0,
        operation_id=OPERATION_ID,
        terminal_status="failed",
    )


def test_apply_rolls_back_all_databases_when_runtime_cas_fails(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        db.execute(
            """CREATE TRIGGER refuse_artifact_settlement
               BEFORE UPDATE OF status ON runtime_plan_artifacts
               BEGIN SELECT RAISE(ABORT, 'injected_artifact_failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected_artifact_failure"):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    _assert_settlement_rolled_back(fixture)
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        db.execute("DROP TRIGGER refuse_artifact_settlement")

    retry = settle_terminal_send(fixture.root, fixture.target, apply=True)
    assert retry.applied is True
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    assert _verify_failed_settlement(fixture.root)


def test_apply_rolls_back_all_databases_when_runtime_cas_loses_its_row(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        db.execute(
            """CREATE TRIGGER make_artifact_cas_lose
               BEFORE UPDATE OF status ON runtime_plan_artifacts
               BEGIN SELECT RAISE(IGNORE); END"""
        )

    with pytest.raises(
        SettlementRefused, match="SEND_SETTLEMENT_RUNTIME_CAS_LOST"
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    _assert_settlement_rolled_back(fixture)
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        db.execute("DROP TRIGGER make_artifact_cas_lose")

    retry = settle_terminal_send(fixture.root, fixture.target, apply=True)
    assert retry.applied is True
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    assert _verify_failed_settlement(fixture.root)


def test_repeated_apply_is_idempotent_and_does_not_duplicate_certificate(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    first = settle_terminal_send(fixture.root, fixture.target, apply=True)

    replay = settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert replay.applied is False
    assert replay.idempotent is True
    assert replay.evidence_sha256 == first.evidence_sha256
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM runtime_send_settlement_audit_v2"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("database", "sql", "params", "error_code"),
    [
        (
            "hub.sqlite3",
            "UPDATE send_operations SET commit_intent=1 WHERE operation_id=?",
            (OPERATION_ID,),
            "SEND_SETTLEMENT_HUB_EVIDENCE_MISMATCH",
        ),
        (
            "qq-vm-bridge.sqlite3",
            "UPDATE qq_vm_ops SET commit_intent=1 WHERE operation_id=?",
            (OPERATION_ID,),
            "SEND_SETTLEMENT_BRIDGE_EVIDENCE_MISMATCH",
        ),
        (
            "hub.sqlite3",
            "UPDATE send_operations SET status='send_uncertain' WHERE operation_id=?",
            (OPERATION_ID,),
            "SEND_SETTLEMENT_HUB_EVIDENCE_MISMATCH",
        ),
        (
            "qq-vm-bridge.sqlite3",
            "UPDATE qq_vm_ops SET status='send_uncertain' WHERE operation_id=?",
            (OPERATION_ID,),
            "SEND_SETTLEMENT_BRIDGE_EVIDENCE_MISMATCH",
        ),
        (
            "qq-vm-bridge.sqlite3",
            """INSERT INTO qq_vm_receipts(
                   operation_id,conversation_id,receipt_fingerprint,local_key)
               VALUES(?,?,?,?)""",
            (OPERATION_ID, CONVERSATION_ID, "receipt-fingerprint", "outbound-1"),
            "SEND_SETTLEMENT_BRIDGE_RECEIPT_PRESENT",
        ),
    ],
)
def test_commit_intent_receipt_and_uncertain_authorities_are_refused(
    tmp_path: Path,
    database: str,
    sql: str,
    params: tuple[object, ...],
    error_code: str,
) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(fixture.root / database, sql, params)

    with pytest.raises(SettlementRefused, match=error_code):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT status FROM outbox ORDER BY outbox_id"
        ).fetchall() == [("pending",), ("pending",), ("pending",)]


@pytest.mark.parametrize(
    "target",
    [
        replace(_target(), operation_id="00000000-0000-0000-0000-000000000999"),
        replace(_target(), body_hash="f" * 64),
    ],
)
def test_target_identifier_or_hash_mismatch_is_refused(
    tmp_path: Path, target: TerminalSettlementTarget
) -> None:
    fixture = _build_evidence(tmp_path)

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_RUNTIME_SEGMENT_MISMATCH",
    ):
        settle_terminal_send(fixture.root, target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")


@pytest.mark.parametrize(
    "target",
    [
        replace(_target(), hub_draft_outbox_id=SEND_OUTBOX_ID),
        replace(_target(), hub_draft_outbox_id=STABLE_OUTBOX_ID),
        replace(_target(), hub_stable_outbox_id=DRAFT_OUTBOX_ID),
        replace(_target(), hub_send_outbox_id=DRAFT_OUTBOX_ID),
    ],
)
def test_colliding_hub_outbox_identifiers_are_refused(
    tmp_path: Path, target: TerminalSettlementTarget
) -> None:
    fixture = _build_evidence(tmp_path)

    with pytest.raises(ValueError, match="must be distinct"):
        settle_terminal_send(fixture.root, target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    statuses = _hub_outbox_statuses(fixture.root)
    assert statuses[SEND_OUTBOX_ID] == "pending"
    assert statuses[STABLE_OUTBOX_ID] == "pending"
    assert statuses[DRAFT_OUTBOX_ID] == "pending"


def _activate_lane(fixture: EvidenceFixture, lane: str) -> None:
    if lane == "runtime":
        _execute(
            fixture.root / "runtime.sqlite3",
            """INSERT INTO runtime_event_outbox(
                   dedupe_key,event_type,aggregate_id,payload_json,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("active-runtime", "new_message", CONVERSATION_ID, "{}", "pending", NOW),
        )
    elif lane == "hub":
        _execute(
            fixture.root / "hub.sqlite3",
            """INSERT INTO outbox(
                   outbox_id,dedupe_key,event_type,aggregate_id,payload_json,
                   status,available_at,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                "active-hub",
                "active-hub",
                "send.requested",
                OPERATION_ID,
                "{}",
                "dispatching",
                NOW,
                NOW,
            ),
        )
    elif lane == "bridge":
        _execute(
            fixture.root / "qq-vm-bridge.sqlite3",
            """INSERT INTO qq_vm_ops(
                   operation_id,idempotency_key,draft_id,conversation_id,binding_id,
                   segment_ref,binding_revision,conversation_revision,text_hash,
                   status,commit_intent,error_code)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "00000000-0000-0000-0000-000000000204",
                "active-bridge",
                DRAFT_ID,
                CONVERSATION_ID,
                BINDING_ID,
                "other-plan:0",
                1,
                3,
                BODY_HASH,
                "pending",
                0,
                None,
            ),
        )
    elif lane == "pacing":
        _execute(
            fixture.root / "pacing.sqlite3",
            """INSERT INTO m10_plans(
                   pacing_plan_id,conversation_id,contact_id,status,
                   earliest_send_at,expires_at,payload_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "00000000-0000-0000-0000-000000000201",
                CONVERSATION_ID,
                BINDING_ID,
                "waiting",
                NOW,
                "2026-09-13T01:00:00+00:00",
                "{}",
                NOW,
                NOW,
            ),
        )
    else:  # pragma: no cover - protects the test helper itself
        raise AssertionError(f"unknown lane: {lane}")


@pytest.mark.parametrize("lane", ["runtime", "hub", "bridge", "pacing"])
def test_other_active_lane_is_refused(tmp_path: Path, lane: str) -> None:
    fixture = _build_evidence(tmp_path)
    _activate_lane(fixture, lane)

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_OTHER_WORK_ACTIVE",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "UPDATE m10_segment_receipts SET operation_id=?",
            ("00000000-0000-0000-0000-000000000999",),
        ),
        ("UPDATE m10_segment_receipts SET verified=1", ()),
        ("DELETE FROM m10_segment_receipts", ()),
    ],
)
def test_legacy_null_due_operation_requires_one_exact_unverified_receipt(
    tmp_path: Path, sql: str, params: tuple[object, ...]
) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(fixture.root / "pacing.sqlite3", sql, params)

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_PACING_RECEIPT_MISMATCH",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")


def test_legacy_null_due_operation_requires_a_unique_delivered_row(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    with sqlite3.connect(fixture.root / "pacing.sqlite3") as db:
        db.executescript(
            """
            ALTER TABLE m10_due_outbox RENAME TO m10_due_outbox_original;
            CREATE TABLE m10_due_outbox (
              outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
              pacing_plan_id TEXT NOT NULL,
              segment_index INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              operation_id TEXT,
              claimed_at TEXT,
              delivered_at TEXT,
              created_at TEXT NOT NULL,
              one_shot_attempt_id TEXT
            );
            INSERT INTO m10_due_outbox(
              outbox_id,pacing_plan_id,segment_index,payload_json,status,
              operation_id,claimed_at,delivered_at,created_at,one_shot_attempt_id)
            SELECT outbox_id,pacing_plan_id,segment_index,payload_json,status,
              operation_id,claimed_at,delivered_at,created_at,one_shot_attempt_id
            FROM m10_due_outbox_original;
            DROP TABLE m10_due_outbox_original;
            """
        )
        payload = db.execute(
            "SELECT payload_json FROM m10_due_outbox"
        ).fetchone()[0]
        db.execute(
            """INSERT INTO m10_due_outbox(
                   pacing_plan_id,segment_index,payload_json,status,
                   operation_id,delivered_at,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (PLAN_ID, 0, payload, "delivered", None, NOW, NOW),
        )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_DUE_OUTBOX_NOT_TERMINAL",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")


def test_legacy_null_due_operation_requires_delivered_status(tmp_path: Path) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(
        fixture.root / "pacing.sqlite3",
        "UPDATE m10_due_outbox SET status='pending'",
    )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_DUE_OUTBOX_NOT_TERMINAL",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")


def test_non_null_due_operation_must_match_exact_operation(tmp_path: Path) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(
        fixture.root / "pacing.sqlite3",
        "UPDATE m10_due_outbox SET operation_id=?",
        ("00000000-0000-0000-0000-000000000999",),
    )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_DUE_OPERATION_MISMATCH",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")


def test_exact_nonlegacy_due_operation_is_accepted(tmp_path: Path) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(
        fixture.root / "pacing.sqlite3",
        "UPDATE m10_due_outbox SET operation_id=?",
        (OPERATION_ID,),
    )

    result = settle_terminal_send(fixture.root, fixture.target)

    assert result.legacy_due_operation_id_missing is False
    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")


def _insert_hub_outbox(
    root: Path,
    *,
    outbox_id: str,
    event_type: str,
    aggregate_id: str,
    payload: dict[str, object],
    status: str = "pending",
) -> None:
    _execute(
        root / "hub.sqlite3",
        """INSERT INTO outbox(
               outbox_id,dedupe_key,event_type,aggregate_id,payload_json,
               status,available_at,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            outbox_id,
            outbox_id,
            event_type,
            aggregate_id,
            json.dumps(payload),
            status,
            NOW,
            NOW,
        ),
    )


def _hub_outbox_statuses(root: Path) -> dict[str, str]:
    with sqlite3.connect(root / "hub.sqlite3") as db:
        return {
            str(outbox_id): str(status)
            for outbox_id, status in db.execute(
                "SELECT outbox_id,status FROM outbox"
            )
        }


def _rewrite_hub_payload(
    root: Path, outbox_id: str, payload: dict[str, object]
) -> None:
    _execute(
        root / "hub.sqlite3",
        "UPDATE outbox SET payload_json=? WHERE outbox_id=?",
        (json.dumps(payload), outbox_id),
    )


def _stage_related_outbox(root: Path, case: str) -> None:
    if case == "draft_created":
        _insert_hub_outbox(
            root,
            outbox_id=RELATED_OUTBOX_ID,
            event_type="draft.created",
            aggregate_id=DRAFT_ID,
            payload={"draft_id": DRAFT_ID},
        )
    elif case == "review":
        _execute(
            root / "hub.sqlite3",
            """INSERT INTO review_requests(review_id,draft_id,status,created_at)
               VALUES(?,?,?,?)""",
            (REVIEW_ID, DRAFT_ID, "pending", NOW),
        )
        _insert_hub_outbox(
            root,
            outbox_id=RELATED_OUTBOX_ID,
            event_type="review.requested",
            aggregate_id=REVIEW_ID,
            payload={"review_id": REVIEW_ID},
        )
    elif case == "policy":
        _execute(
            root / "hub.sqlite3",
            """INSERT INTO policy_decisions(
                   decision_id,draft_id,decision_json,created_at)
               VALUES(?,?,?,?)""",
            (DECISION_ID, DRAFT_ID, "{}", NOW),
        )
        _insert_hub_outbox(
            root,
            outbox_id=RELATED_OUTBOX_ID,
            event_type="policy.decided",
            aggregate_id=DECISION_ID,
            payload={"decision_id": DECISION_ID},
        )
    elif case == "pacing":
        _insert_hub_outbox(
            root,
            outbox_id=RELATED_OUTBOX_ID,
            event_type="pacing.due",
            aggregate_id=CONVERSATION_ID,
            payload={"pacing_plan_id": PLAN_ID},
        )
    elif case == "duplicate_send_requested":
        _insert_hub_outbox(
            root,
            outbox_id=DUPLICATE_SEND_OUTBOX_ID,
            event_type="send.requested",
            aggregate_id=OPERATION_ID,
            payload={"operation_id": OPERATION_ID},
            status="dispatching",
        )
    else:  # pragma: no cover - protects the test helper itself
        raise AssertionError(f"unknown case: {case}")


@pytest.mark.parametrize(
    "case",
    ["draft_created", "review", "policy", "pacing", "duplicate_send_requested"],
)
def test_pending_related_hub_outbox_blocks_settlement(
    tmp_path: Path, case: str
) -> None:
    fixture = _build_evidence(tmp_path)
    _stage_related_outbox(fixture.root, case)

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_OTHER_WORK_ACTIVE",
    ):
        settle_terminal_send(fixture.root, fixture.target)
    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_OTHER_WORK_ACTIVE",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    assert _audit_table_exists(fixture.root) is False
    statuses = _hub_outbox_statuses(fixture.root)
    assert statuses[SEND_OUTBOX_ID] == "pending"
    assert statuses[STABLE_OUTBOX_ID] == "pending"
    assert statuses[DRAFT_OUTBOX_ID] == "pending"
    if case == "duplicate_send_requested":
        assert statuses[DUPLICATE_SEND_OUTBOX_ID] == "dispatching"
    else:
        assert statuses[RELATED_OUTBOX_ID] == "pending"


def test_exact_named_draft_outbox_is_cancelled_not_the_extra_one(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)

    result = settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert result.applied is True
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        db.row_factory = sqlite3.Row
        audit = {
            str(row["outbox_id"]): str(row["role"])
            for row in db.execute(
                """SELECT outbox_id,role
                   FROM terminal_send_outbox_cancel_audit_v2"""
            )
        }
    assert audit == {
        SEND_OUTBOX_ID: "send_requested",
        STABLE_OUTBOX_ID: "stable_window",
        DRAFT_OUTBOX_ID: "draft_created",
    }
    statuses = _hub_outbox_statuses(fixture.root)
    assert statuses[DRAFT_OUTBOX_ID] == "cancelled"
    assert set(statuses) == {SEND_OUTBOX_ID, STABLE_OUTBOX_ID, DRAFT_OUTBOX_ID}


def test_fourth_draft_created_outbox_keeps_the_lane_blocked(tmp_path: Path) -> None:
    fixture = _build_evidence(tmp_path)
    _stage_related_outbox(fixture.root, "draft_created")

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_OTHER_WORK_ACTIVE",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    statuses = _hub_outbox_statuses(fixture.root)
    assert statuses[DRAFT_OUTBOX_ID] == "pending"
    assert statuses[RELATED_OUTBOX_ID] == "pending"


def test_newer_pending_stable_window_is_refused(tmp_path: Path) -> None:
    fixture = _build_evidence(tmp_path)
    _insert_hub_outbox(
        fixture.root,
        outbox_id=NEWER_STABLE_OUTBOX_ID,
        event_type="conversation.stable_window",
        aggregate_id=CONVERSATION_ID,
        payload={"conversation_id": CONVERSATION_ID, "last_message_key": "inbound-2"},
    )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_OTHER_WORK_ACTIVE",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    statuses = _hub_outbox_statuses(fixture.root)
    assert statuses[STABLE_OUTBOX_ID] == "pending"
    assert statuses[NEWER_STABLE_OUTBOX_ID] == "pending"


def test_pending_sibling_draft_outbox_blocks_settlement(tmp_path: Path) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(
        fixture.root / "hub.sqlite3",
        """INSERT INTO drafts(
               draft_id,conversation_id,contact_id,text,
               source_message_keys_json,rule_version,text_hash,status,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            SIBLING_DRAFT_ID,
            CONVERSATION_ID,
            BINDING_ID,
            "sibling draft",
            "[]",
            "rules-v1",
            hashlib.sha256(b"sibling draft").hexdigest(),
            "created",
            NOW,
            NOW,
        ),
    )
    _insert_hub_outbox(
        fixture.root,
        outbox_id=RELATED_OUTBOX_ID,
        event_type="draft.created",
        aggregate_id=SIBLING_DRAFT_ID,
        payload={"draft_id": SIBLING_DRAFT_ID},
    )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_OTHER_WORK_ACTIVE",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    statuses = _hub_outbox_statuses(fixture.root)
    assert statuses[SEND_OUTBOX_ID] == "pending"
    assert statuses[STABLE_OUTBOX_ID] == "pending"
    assert statuses[DRAFT_OUTBOX_ID] == "pending"
    assert statuses[RELATED_OUTBOX_ID] == "pending"


def test_stable_window_payload_key_must_equal_authorization(tmp_path: Path) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(
        fixture.root / "hub.sqlite3",
        "UPDATE outbox SET payload_json=? WHERE outbox_id=?",
        (
            json.dumps(
                {"conversation_id": CONVERSATION_ID, "last_message_key": "inbound-2"}
            ),
            STABLE_OUTBOX_ID,
        ),
    )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_HUB_STABLE_WINDOW_MISMATCH",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("draft_id", "00000000-0000-0000-0000-000000000999"),
        ("conversation_id", "conversation-other"),
        ("contact_id", "contact-other"),
        ("rule_version", ""),
        ("status", "authorized"),
    ],
)
def test_draft_outbox_payload_identity_tamper_is_refused(
    tmp_path: Path, field: str, value: object
) -> None:
    fixture = _build_evidence(tmp_path)
    payload = _draft_payload()
    payload[field] = value
    _rewrite_hub_payload(fixture.root, DRAFT_OUTBOX_ID, payload)

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_HUB_OUTBOX_PAYLOAD_MISMATCH",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    assert _hub_outbox_statuses(fixture.root)[DRAFT_OUTBOX_ID] == "pending"


@pytest.mark.parametrize(
    "mutation", ["text", "text_hash", "missing_key", "extra_key"]
)
def test_draft_outbox_body_hash_tamper_is_refused(
    tmp_path: Path, mutation: str
) -> None:
    fixture = _build_evidence(tmp_path)
    payload = _draft_payload()
    if mutation == "text":
        payload["text"] = TEXT + " (edited)"
    elif mutation == "text_hash":
        payload["text_hash"] = "f" * 64
    elif mutation == "missing_key":
        del payload["text_hash"]
    else:
        payload["unexpected"] = "extra"
    _rewrite_hub_payload(fixture.root, DRAFT_OUTBOX_ID, payload)

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_HUB_OUTBOX_PAYLOAD_MISMATCH",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    assert _hub_outbox_statuses(fixture.root)[DRAFT_OUTBOX_ID] == "pending"


def test_conversation_key_advanced_past_authorization_is_refused(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(
        fixture.root / "hub.sqlite3",
        "UPDATE conversations SET last_message_key=?,stable_after=?,version=version+1"
        " WHERE conversation_id=?",
        ("inbound-2", NOW, CONVERSATION_ID),
    )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_HUB_CONVERSATION_KEY_MISMATCH",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")


def test_unrelated_conversation_outbox_does_not_block_or_get_cancelled(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    _insert_hub_outbox(
        fixture.root,
        outbox_id=UNRELATED_OUTBOX_ID,
        event_type="conversation.stable_window",
        aggregate_id=UNRELATED_CONVERSATION_ID,
        payload={
            "conversation_id": UNRELATED_CONVERSATION_ID,
            "last_message_key": "unrelated-1",
        },
    )

    result = settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert result.applied is True
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    statuses = _hub_outbox_statuses(fixture.root)
    assert statuses[SEND_OUTBOX_ID] == "cancelled"
    assert statuses[STABLE_OUTBOX_ID] == "cancelled"
    assert statuses[DRAFT_OUTBOX_ID] == "cancelled"
    assert statuses[UNRELATED_OUTBOX_ID] == "pending"
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        cancelled = db.execute(
            """SELECT outbox_id FROM terminal_send_outbox_cancel_audit_v2
               ORDER BY outbox_id"""
        ).fetchall()
    assert [row[0] for row in cancelled] == sorted(
        (SEND_OUTBOX_ID, STABLE_OUTBOX_ID, DRAFT_OUTBOX_ID)
    )


def test_hub_outbox_evidence_records_authorization_key_and_lineage(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)

    result = settle_terminal_send(fixture.root, fixture.target, apply=True)

    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        evidence = json.loads(
            db.execute(
                "SELECT evidence_json FROM runtime_send_settlement_audit_v2"
            ).fetchone()[0]
        )
    hub_evidence = evidence["hub_outboxes"]
    expected = "inbound-1"
    assert evidence["schema"] == "pmai-terminal-send-settlement-evidence-v2"
    assert [item["role"] for item in hub_evidence["outboxes"]] == [
        "send_requested",
        "stable_window",
        "draft_created",
    ]
    assert sorted(
        item["outbox_id"] for item in hub_evidence["outboxes"]
    ) == sorted((SEND_OUTBOX_ID, STABLE_OUTBOX_ID, DRAFT_OUTBOX_ID))
    assert hub_evidence["authorization_expected_last_message_key_sha256"] == hashlib.sha256(
        expected.encode()
    ).hexdigest()
    assert hub_evidence["stable_window_last_message_key_sha256"] == hashlib.sha256(
        expected.encode()
    ).hexdigest()
    assert hub_evidence["conversation_last_message_key_sha256"] == hashlib.sha256(
        expected.encode()
    ).hexdigest()
    assert hub_evidence["other_related_active"] == 0
    assert hub_evidence["lineage_aggregate_ids_sha256"]
    assert expected not in json.dumps(evidence)
    assert TEXT not in json.dumps(evidence)
    assert verify_terminal_settlement(
        fixture.root,
        conversation_id=CONVERSATION_ID,
        pacing_plan_id=PLAN_ID,
        segment_index=0,
        operation_id=OPERATION_ID,
        terminal_status="failed",
    )
    assert result.applied is True


def test_cancelled_outbox_reopened_to_pending_invalidates_certificate(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)
    _execute(
        fixture.root / "hub.sqlite3",
        "UPDATE outbox SET status='pending' WHERE outbox_id=?",
        (DRAFT_OUTBOX_ID,),
    )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_HUB_OUTBOX_AUDIT_MISMATCH",
    ):
        settle_terminal_send(fixture.root, fixture.target)
    assert not verify_terminal_settlement(
        fixture.root,
        conversation_id=CONVERSATION_ID,
        pacing_plan_id=PLAN_ID,
        segment_index=0,
        operation_id=OPERATION_ID,
        terminal_status="failed",
    )


_PENDING_HUB_OUTBOXES = {
    SEND_OUTBOX_ID: "pending",
    STABLE_OUTBOX_ID: "pending",
    DRAFT_OUTBOX_ID: "pending",
}


def _verify_failed_settlement(root: Path) -> bool:
    return verify_terminal_settlement(
        root,
        conversation_id=CONVERSATION_ID,
        pacing_plan_id=PLAN_ID,
        segment_index=0,
        operation_id=OPERATION_ID,
        terminal_status="failed",
    )


def _sqlite_master_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master")}


def _assert_settlement_rolled_back(fixture: EvidenceFixture) -> None:
    """Every cross-database mutation of a failed apply must be undone.

    Both append-only audit tables are created inside the same attached
    transaction as the Hub cancellations, so a rolled-back apply must leave no
    audit schema, no audit row, no bumped Hub version, and no certificate.
    """

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    runtime_objects = _sqlite_master_names(fixture.root / "runtime.sqlite3")
    assert "runtime_send_settlement_audit_v2" not in runtime_objects
    assert "runtime_send_settlement_audit_v2_no_replace" not in runtime_objects
    hub_objects = _sqlite_master_names(fixture.root / "hub.sqlite3")
    assert "terminal_send_outbox_cancel_audit_v2" not in hub_objects
    assert "terminal_send_outbox_cancel_audit_v2_no_replace" not in hub_objects
    assert _hub_outbox_statuses(fixture.root) == _PENDING_HUB_OUTBOXES
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT version FROM outbox ORDER BY outbox_id"
        ).fetchall() == [(1,), (1,), (1,)]
    assert _verify_failed_settlement(fixture.root) is False


def _runtime_audit_payload(root: Path) -> dict[str, object]:
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM runtime_send_settlement_audit_v2").fetchone()
    assert row is not None
    return dict(row)


def _insert_runtime_audit_replace(
    root: Path, payload: dict[str, object]
) -> None:
    columns = list(payload)
    placeholders = ",".join("?" for _ in columns)
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.execute(
            f"INSERT OR REPLACE INTO runtime_send_settlement_audit_v2"
            f"({','.join(columns)}) VALUES({placeholders})",
            tuple(payload[column] for column in columns),
        )


def _insert_hub_cancel_audit_replace(root: Path, outbox_id: str) -> None:
    with sqlite3.connect(root / "hub.sqlite3") as db:
        db.execute(
            """INSERT OR REPLACE INTO terminal_send_outbox_cancel_audit_v2(
                   outbox_id,settlement_id,role,original_version,operator_id,
                   reason_code,identity_sha256,cancelled_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                outbox_id,
                "settlement-forged-replacement",
                "send_requested",
                1,
                "codex-test",
                "FORGED_REPLACEMENT",
                "0" * 64,
                NOW,
            ),
        )


@pytest.mark.parametrize("conflict", ["settlement_id", "composite_unique_key"])
def test_runtime_audit_insert_or_replace_is_append_only(
    tmp_path: Path, conflict: str
) -> None:
    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)

    payload = _runtime_audit_payload(fixture.root)
    payload["reason_code"] = "FORGED_REPLACEMENT"
    if conflict == "settlement_id":
        # Keep the primary key but move the composite key off the live row.
        payload["operation_id"] = "00000000-0000-0000-0000-000000000999"
    else:
        # Keep the composite key but replace the primary key.
        payload["settlement_id"] = "settlement-forged-replacement"

    with pytest.raises(
        sqlite3.IntegrityError,
        match="runtime_send_settlement_audit_append_only",
    ):
        _insert_runtime_audit_replace(fixture.root, payload)

    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        rows = db.execute(
            "SELECT settlement_id,reason_code FROM runtime_send_settlement_audit_v2"
        ).fetchall()
    assert rows == [(fixture.target.settlement_id, fixture.target.reason_code)]
    assert _verify_failed_settlement(fixture.root)


@pytest.mark.parametrize(
    "outbox_id", [SEND_OUTBOX_ID, STABLE_OUTBOX_ID, DRAFT_OUTBOX_ID]
)
def test_hub_cancel_audit_insert_or_replace_is_append_only(
    tmp_path: Path, outbox_id: str
) -> None:
    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)

    with pytest.raises(
        sqlite3.IntegrityError,
        match="terminal_send_outbox_cancel_audit_append_only",
    ):
        _insert_hub_cancel_audit_replace(fixture.root, outbox_id)

    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            """SELECT settlement_id,role,reason_code
               FROM terminal_send_outbox_cancel_audit_v2 WHERE outbox_id=?""",
            (outbox_id,),
        ).fetchone() == (
            fixture.target.settlement_id,
            "send_requested"
            if outbox_id == SEND_OUTBOX_ID
            else "stable_window"
            if outbox_id == STABLE_OUTBOX_ID
            else "draft_created",
            fixture.target.reason_code,
        )
    assert _verify_failed_settlement(fixture.root)


_ROWID_ALIASES = ("rowid", "_rowid_", "oid")


def _hub_cancel_audit_rows(root: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(root / "hub.sqlite3") as db:
        return db.execute(
            """SELECT outbox_id,settlement_id,role,original_version,
                      operator_id,reason_code,identity_sha256,cancelled_at
               FROM terminal_send_outbox_cancel_audit_v2 ORDER BY outbox_id"""
        ).fetchall()


@pytest.mark.parametrize("alias", _ROWID_ALIASES)
def test_runtime_v2_audit_has_no_hidden_rowid_alias_column(
    tmp_path: Path, alias: str
) -> None:
    """A WITHOUT ROWID audit table cannot be addressed through a rowid alias.

    ``rowid``, ``_rowid_`` and ``oid`` only exist on rowid tables, so on
    ``runtime_send_settlement_audit_v2`` reading, deleting or inserting through
    any of them is refused before the append-only triggers even run.  The
    business primary key and composite unique key remain the only addresses,
    and those are append-only by trigger.
    """

    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)

    live = _runtime_audit_payload(fixture.root)
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            db.execute(
                f"SELECT {alias} FROM runtime_send_settlement_audit_v2"
            ).fetchall()
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            db.execute(
                f"DELETE FROM runtime_send_settlement_audit_v2 WHERE {alias}=1"
            )

    forged = dict(live)
    forged[alias] = 1
    forged["reason_code"] = "FORGED_ROWID_REPLACEMENT"

    with pytest.raises(
        sqlite3.OperationalError,
        match="has no column named",
    ):
        _insert_runtime_audit_replace(fixture.root, forged)

    assert _runtime_audit_payload(fixture.root) == live
    assert _verify_failed_settlement(fixture.root)


@pytest.mark.parametrize("alias", _ROWID_ALIASES)
def test_hub_v2_cancel_audit_has_no_hidden_rowid_alias_column(
    tmp_path: Path, alias: str
) -> None:
    """The Hub cancellation audit has no rowid alias either."""

    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)
    live = _hub_cancel_audit_rows(fixture.root)

    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            db.execute(
                f"SELECT {alias} FROM terminal_send_outbox_cancel_audit_v2"
            ).fetchall()
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            db.execute(
                f"DELETE FROM terminal_send_outbox_cancel_audit_v2 WHERE {alias}=1"
            )
        with pytest.raises(sqlite3.OperationalError, match="has no column named"):
            db.execute(
                f"""INSERT OR REPLACE INTO terminal_send_outbox_cancel_audit_v2(
                       {alias},outbox_id,settlement_id,role,original_version,
                       operator_id,reason_code,identity_sha256,cancelled_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    1,
                    "00000000-0000-0000-0000-0000000002c1",
                    "settlement-forged-rowid-replacement",
                    "send_requested",
                    1,
                    "codex-test",
                    "FORGED_ROWID_REPLACEMENT",
                    "0" * 64,
                    NOW,
                ),
            )

    assert _hub_cancel_audit_rows(fixture.root) == live
    assert _verify_failed_settlement(fixture.root)


def test_two_stage_runtime_audit_overwrite_is_refused_at_the_first_step(
    tmp_path: Path,
) -> None:
    """Deleting the certificate and re-inserting it under the same keys must fail.

    The first step of the two-stage overwrite is already refused, so the second
    (a REPLACE that pins the freed business keys) can never run.
    """

    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)
    live = _runtime_audit_payload(fixture.root)

    forged = dict(live)
    forged["operator_id"] = "codex-forged-replacement"
    forged["reason_code"] = "FORGED_TWO_STAGE_REPLACEMENT"

    with pytest.raises(
        sqlite3.IntegrityError,
        match="runtime_send_settlement_audit_append_only",
    ):
        _execute(
            fixture.root / "runtime.sqlite3",
            "DELETE FROM runtime_send_settlement_audit_v2 WHERE settlement_id=?",
            (fixture.target.settlement_id,),
        )
    assert _runtime_audit_payload(fixture.root) == live

    with pytest.raises(
        sqlite3.IntegrityError,
        match="runtime_send_settlement_audit_append_only",
    ):
        _insert_runtime_audit_replace(fixture.root, forged)

    assert _runtime_audit_payload(fixture.root) == live
    assert _verify_failed_settlement(fixture.root)


def test_two_stage_hub_cancel_audit_overwrite_is_refused_at_the_first_step(
    tmp_path: Path,
) -> None:
    """The Hub cancel audit refuses the delete and the re-insert alike."""

    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)
    live = _hub_cancel_audit_rows(fixture.root)

    with pytest.raises(
        sqlite3.IntegrityError,
        match="terminal_send_outbox_cancel_audit_append_only",
    ):
        _execute(
            fixture.root / "hub.sqlite3",
            "DELETE FROM terminal_send_outbox_cancel_audit_v2 WHERE outbox_id=?",
            (SEND_OUTBOX_ID,),
        )
    assert _hub_cancel_audit_rows(fixture.root) == live

    with pytest.raises(
        sqlite3.IntegrityError,
        match="terminal_send_outbox_cancel_audit_append_only",
    ):
        _insert_hub_cancel_audit_replace(fixture.root, SEND_OUTBOX_ID)

    assert _hub_cancel_audit_rows(fixture.root) == live
    assert _verify_failed_settlement(fixture.root)


def _set_runtime_terminal(fixture: EvidenceFixture) -> None:
    _execute(
        fixture.root / "runtime.sqlite3",
        "UPDATE runtime_segment_executions SET status='failed'",
    )
    _execute(
        fixture.root / "runtime.sqlite3",
        "UPDATE runtime_plan_artifacts SET status='rejected'",
    )


def test_terminal_runtime_without_certificate_dry_run_is_read_only(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    _set_runtime_terminal(fixture)

    result = settle_terminal_send(fixture.root, fixture.target)

    assert result.applied is False
    assert result.idempotent is False
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    assert _audit_table_exists(fixture.root) is False
    assert _hub_outbox_statuses(fixture.root) == _PENDING_HUB_OUTBOXES
    assert "terminal_send_outbox_cancel_audit_v2" not in _sqlite_master_names(
        fixture.root / "hub.sqlite3"
    )


def test_terminal_runtime_without_certificate_apply_cancels_and_certifies(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    _set_runtime_terminal(fixture)

    result = settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert result.applied is True
    assert result.idempotent is False
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    assert _hub_outbox_statuses(fixture.root) == {
        SEND_OUTBOX_ID: "cancelled",
        STABLE_OUTBOX_ID: "cancelled",
        DRAFT_OUTBOX_ID: "cancelled",
    }
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM runtime_send_settlement_audit_v2"
        ).fetchone()[0] == 1
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM terminal_send_outbox_cancel_audit_v2"
        ).fetchone()[0] == 3
    assert _verify_failed_settlement(fixture.root)


def test_terminal_runtime_without_certificate_repeated_apply_is_idempotent(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    _set_runtime_terminal(fixture)

    first = settle_terminal_send(fixture.root, fixture.target, apply=True)
    replay = settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert first.applied is True
    assert replay.applied is False
    assert replay.idempotent is True
    assert replay.evidence_sha256 == first.evidence_sha256
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM runtime_send_settlement_audit_v2"
        ).fetchone()[0] == 1
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM terminal_send_outbox_cancel_audit_v2"
        ).fetchone()[0] == 3


@pytest.mark.parametrize("apply", [False, True])
def test_mixed_runtime_state_is_refused_without_modification(
    tmp_path: Path, apply: bool
) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(
        fixture.root / "runtime.sqlite3",
        "UPDATE runtime_segment_executions SET status='failed'",
    )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_RUNTIME_CAS_STATE_MISMATCH",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=apply)

    assert _runtime_statuses(fixture.root) == ("failed", "waiting")
    assert _audit_table_exists(fixture.root) is False
    assert _hub_outbox_statuses(fixture.root) == _PENDING_HUB_OUTBOXES
    hub_objects = _sqlite_master_names(fixture.root / "hub.sqlite3")
    assert "terminal_send_outbox_cancel_audit_v2" not in hub_objects


@pytest.mark.parametrize("apply", [False, True])
def test_hub_draft_text_tamper_with_matching_hash_is_refused(
    tmp_path: Path, apply: bool
) -> None:
    fixture = _build_evidence(tmp_path)
    _execute(
        fixture.root / "hub.sqlite3",
        "UPDATE drafts SET text=? WHERE draft_id=?",
        (TEXT + " (edited)", DRAFT_ID),
    )
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT text_hash FROM drafts WHERE draft_id=?", (DRAFT_ID,)
        ).fetchone() == (BODY_HASH,)

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_HUB_EVIDENCE_MISMATCH",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=apply)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    assert _audit_table_exists(fixture.root) is False
    assert _hub_outbox_statuses(fixture.root) == _PENDING_HUB_OUTBOXES


def _journal_mode(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()


def _switch_to_wal(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(
            connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        ).lower()
    finally:
        connection.close()


def test_wal_databases_are_checkpointed_and_settled_atomically(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    filenames = (
        "runtime.sqlite3",
        "hub.sqlite3",
        "qq-vm-bridge.sqlite3",
        "pacing.sqlite3",
    )
    for filename in filenames:
        assert _switch_to_wal(fixture.root / filename) == "wal"
    for filename in filenames:
        assert _journal_mode(fixture.root / filename) == "wal"

    result = settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert result.applied is True
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    # The atomic cross-database commit requires DELETE journals, so the apply
    # must have checkpointed and switched every attached database back.
    for filename in filenames:
        assert _journal_mode(fixture.root / filename) == "delete"
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM terminal_send_outbox_cancel_audit_v2"
        ).fetchone()[0] == 3
    assert _verify_failed_settlement(fixture.root)


def test_external_wal_race_is_locked_out_while_the_journal_assertion_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outside writer cannot flip a database to WAL mid-apply.

    ``_assert_atomic_journal_modes`` runs only after ``BEGIN IMMEDIATE`` has
    pinned all four attached databases, so a ``timeout=0`` external connection
    asking for ``journal_mode=WAL`` must fail busy.  The assertion (and the
    settlement) then completes against the still-deleted journals.
    """

    fixture = _build_evidence(tmp_path)
    observed: dict[str, object] = {}
    original = settlement_module._assert_atomic_journal_modes

    def guarded(connection: sqlite3.Connection) -> None:
        observed["in_transaction"] = connection.in_transaction
        probe = sqlite3.connect(fixture.root / "hub.sqlite3", timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError) as failure:
                probe.execute("PRAGMA journal_mode=WAL").fetchone()
        finally:
            probe.close()
        observed["probe_error"] = str(failure.value)
        original(connection)
        observed["asserted"] = True

    monkeypatch.setattr(settlement_module, "_assert_atomic_journal_modes", guarded)

    result = settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert observed["in_transaction"] is True
    assert observed["asserted"] is True
    message = str(observed["probe_error"]).lower()
    assert "locked" in message or "busy" in message
    assert result.applied is True
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    for filename in (
        "runtime.sqlite3",
        "hub.sqlite3",
        "qq-vm-bridge.sqlite3",
        "pacing.sqlite3",
    ):
        assert _journal_mode(fixture.root / filename) == "delete"
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM terminal_send_outbox_cancel_audit_v2"
        ).fetchone()[0] == 3
    assert _verify_failed_settlement(fixture.root)


_LEGACY_RUNTIME_AUDIT_DDL = """
CREATE TABLE runtime_send_settlement_audit(
  settlement_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  pacing_plan_id TEXT NOT NULL,
  segment_index INTEGER NOT NULL,
  operation_id TEXT NOT NULL,
  authorization_id TEXT NOT NULL,
  draft_id TEXT NOT NULL,
  hub_send_outbox_id TEXT NOT NULL,
  hub_stable_outbox_id TEXT NOT NULL,
  binding_id TEXT NOT NULL,
  binding_revision INTEGER NOT NULL,
  conversation_revision INTEGER NOT NULL,
  body_hash TEXT NOT NULL,
  terminal_status TEXT NOT NULL,
  artifact_status TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  settled_at TEXT NOT NULL,
  UNIQUE(pacing_plan_id,segment_index,operation_id)
)
"""

_RUNTIME_AUDIT_GUARD_TRIGGER = """
CREATE TRIGGER runtime_send_settlement_audit_no_replace
BEFORE INSERT ON runtime_send_settlement_audit
WHEN EXISTS(
  SELECT 1 FROM runtime_send_settlement_audit
  WHERE settlement_id=NEW.settlement_id
     OR (pacing_plan_id=NEW.pacing_plan_id
         AND segment_index=NEW.segment_index
         AND operation_id=NEW.operation_id)
)
BEGIN
  SELECT RAISE(ABORT, 'runtime_send_settlement_audit_append_only');
END
"""


def _create_legacy_runtime_audit_table(
    root: Path, *, with_insert_guard: bool
) -> None:
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.execute(_LEGACY_RUNTIME_AUDIT_DDL)
        if with_insert_guard:
            db.execute(_RUNTIME_AUDIT_GUARD_TRIGGER)


def _insert_legacy_two_event_certificate(
    root: Path, target: TerminalSettlementTarget
) -> None:
    """A pre-refactor certificate that only names the send and stable outbox."""

    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.execute(
            """INSERT INTO runtime_send_settlement_audit(
                   settlement_id,conversation_id,pacing_plan_id,segment_index,
                   operation_id,authorization_id,draft_id,hub_send_outbox_id,
                   hub_stable_outbox_id,binding_id,binding_revision,
                   conversation_revision,body_hash,terminal_status,
                   artifact_status,operator_id,reason_code,evidence_sha256,
                   evidence_json,settled_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                target.settlement_id,
                target.conversation_id,
                target.pacing_plan_id,
                target.segment_index,
                target.operation_id,
                target.authorization_id,
                target.draft_id,
                target.hub_send_outbox_id,
                target.hub_stable_outbox_id,
                target.binding_id,
                target.binding_revision,
                target.conversation_revision,
                target.body_hash,
                target.terminal_status,
                "rejected",
                target.operator_id,
                target.reason_code,
                "0" * 64,
                "{}",
                NOW,
            ),
        )


def _legacy_runtime_audit_rows(root: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        return db.execute(
            "SELECT * FROM runtime_send_settlement_audit ORDER BY settlement_id"
        ).fetchall()


def test_legacy_certificate_is_kept_but_not_trusted(
    tmp_path: Path,
) -> None:
    """The retired table survives verbatim and never certifies a settlement.

    The old unsuffixed table is neither migrated nor trusted: a row in it is
    not a v2 certificate, so verification fails and a later apply writes an
    independent v2 row instead of rewriting the legacy table.
    """

    fixture = _build_evidence(tmp_path)
    _create_legacy_runtime_audit_table(fixture.root, with_insert_guard=True)
    _insert_legacy_two_event_certificate(fixture.root, fixture.target)
    legacy_rows = _legacy_runtime_audit_rows(fixture.root)
    assert len(legacy_rows) == 1
    assert _legacy_audit_table_exists(fixture.root) is True
    assert _audit_table_exists(fixture.root) is False

    assert _verify_failed_settlement(fixture.root) is False
    dry_run = settle_terminal_send(fixture.root, fixture.target)
    assert dry_run.applied is False
    assert dry_run.idempotent is False

    result = settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert result.applied is True
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    # The legacy table is retained exactly as it was written: the apply neither
    # migrated nor extended it.
    assert _legacy_runtime_audit_rows(fixture.root) == legacy_rows
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        legacy_columns = {
            str(row[1])
            for row in db.execute(
                "PRAGMA table_info(runtime_send_settlement_audit)"
            )
        }
        assert "hub_draft_outbox_id" not in legacy_columns
        assert db.execute(
            """SELECT settlement_id,hub_draft_outbox_id
               FROM runtime_send_settlement_audit_v2"""
        ).fetchall() == [(fixture.target.settlement_id, DRAFT_OUTBOX_ID)]
    assert _verify_failed_settlement(fixture.root) is True


def test_certificate_without_insert_guard_trigger_is_unverifiable(
    tmp_path: Path,
) -> None:
    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)
    _execute(
        fixture.root / "runtime.sqlite3",
        "DROP TRIGGER runtime_send_settlement_audit_v2_no_replace",
    )

    assert _verify_failed_settlement(fixture.root) is False
    for apply in (False, True):
        with pytest.raises(
            SettlementRefused,
            match="SEND_SETTLEMENT_CERTIFICATE_SCHEMA_UNSAFE",
        ):
            settle_terminal_send(fixture.root, fixture.target, apply=apply)


def test_legacy_empty_audit_table_is_not_migrated_before_apply(
    tmp_path: Path,
) -> None:
    """Apply creates an independent v2 table and leaves the legacy table alone."""

    fixture = _build_evidence(tmp_path)
    _create_legacy_runtime_audit_table(fixture.root, with_insert_guard=False)
    assert _legacy_audit_table_exists(fixture.root) is True
    assert _audit_table_exists(fixture.root) is False

    result = settle_terminal_send(fixture.root, fixture.target, apply=True)

    assert result.applied is True
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        v2_columns = {
            str(row[1])
            for row in db.execute(
                "PRAGMA table_info(runtime_send_settlement_audit_v2)"
            )
        }
        assert "hub_draft_outbox_id" in v2_columns
        assert db.execute(
            "SELECT COUNT(*) FROM runtime_send_settlement_audit_v2"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT hub_draft_outbox_id FROM runtime_send_settlement_audit_v2"
        ).fetchone()[0] == DRAFT_OUTBOX_ID
        # The legacy table is untouched: same schema, still empty.
        legacy_columns = {
            str(row[1])
            for row in db.execute(
                "PRAGMA table_info(runtime_send_settlement_audit)"
            )
        }
        assert "hub_draft_outbox_id" not in legacy_columns
        assert db.execute(
            "SELECT COUNT(*) FROM runtime_send_settlement_audit"
        ).fetchone()[0] == 0
    runtime_objects = _sqlite_master_names(fixture.root / "runtime.sqlite3")
    assert "runtime_send_settlement_audit_v2_no_replace" in runtime_objects
    assert "runtime_send_settlement_audit_no_replace" not in runtime_objects
    hub_objects = _sqlite_master_names(fixture.root / "hub.sqlite3")
    assert "terminal_send_outbox_cancel_audit_v2_no_replace" in hub_objects
    assert _verify_failed_settlement(fixture.root)


_CANCELLED_HUB_OUTBOXES = {
    SEND_OUTBOX_ID: "cancelled",
    STABLE_OUTBOX_ID: "cancelled",
    DRAFT_OUTBOX_ID: "cancelled",
}


def _reopen_the_three_hub_outboxes(root: Path) -> None:
    _execute(
        root / "hub.sqlite3",
        """UPDATE outbox SET status='pending'
           WHERE outbox_id IN (?,?,?)""",
        (SEND_OUTBOX_ID, STABLE_OUTBOX_ID, DRAFT_OUTBOX_ID),
    )


def test_all_hub_outboxes_reopened_to_pending_invalidate_certificate(
    tmp_path: Path,
) -> None:
    """Reopening all three cancelled Hub rows invalidates the certificate.

    The v2 cancellation audit refuses the delete (append-only trigger), so this
    test leaves the audit rows in place and only puts the outboxes back to
    pending.  The live Hub rows then contradict both the recorded cancellation
    and the certificate, and verification must fail closed.
    """

    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="terminal_send_outbox_cancel_audit_append_only",
        ):
            db.execute("DELETE FROM terminal_send_outbox_cancel_audit_v2")
        db.rollback()

    _reopen_the_three_hub_outboxes(fixture.root)

    assert _hub_outbox_statuses(fixture.root) == _PENDING_HUB_OUTBOXES
    for apply in (False, True):
        with pytest.raises(
            SettlementRefused,
            match="SEND_SETTLEMENT_HUB_OUTBOX_AUDIT_MISMATCH",
        ):
            settle_terminal_send(fixture.root, fixture.target, apply=apply)

    assert _verify_failed_settlement(fixture.root) is False
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM terminal_send_outbox_cancel_audit_v2"
        ).fetchone()[0] == 3


def test_reopened_hub_outboxes_that_lost_the_cancel_audit_are_certificate_mismatch(
    tmp_path: Path,
) -> None:
    """A certificate whose Hub cancellations are gone must stop verifying.

    The v2 audit rows cannot be deleted, so the damage a partial restore leaves
    behind is simulated by dropping the whole Hub cancellation audit table and
    reopening the outboxes.  Evidence collection succeeds again, and it is the
    certificate validation that must refuse with the Hub state mismatch.
    """

    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        db.execute("DROP TABLE terminal_send_outbox_cancel_audit_v2")

    _reopen_the_three_hub_outboxes(fixture.root)

    assert _hub_outbox_statuses(fixture.root) == _PENDING_HUB_OUTBOXES
    for apply in (False, True):
        with pytest.raises(
            SettlementRefused,
            match="SEND_SETTLEMENT_CERTIFICATE_HUB_STATE_MISMATCH",
        ):
            settle_terminal_send(fixture.root, fixture.target, apply=apply)

    assert _verify_failed_settlement(fixture.root) is False
    # The runtime certificate itself stays append-only through the attempt.
    with sqlite3.connect(fixture.root / "runtime.sqlite3") as db:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="runtime_send_settlement_audit_append_only",
        ):
            db.execute("DELETE FROM runtime_send_settlement_audit_v2")
        db.rollback()
        assert db.execute(
            "SELECT COUNT(*) FROM runtime_send_settlement_audit_v2"
        ).fetchone()[0] == 1


_V2_AUDIT_GUARDS = (
    (
        "runtime.sqlite3",
        "runtime_send_settlement_audit_v2_no_update",
        "SEND_SETTLEMENT_CERTIFICATE_SCHEMA_UNSAFE",
    ),
    (
        "runtime.sqlite3",
        "runtime_send_settlement_audit_v2_no_delete",
        "SEND_SETTLEMENT_CERTIFICATE_SCHEMA_UNSAFE",
    ),
    (
        "runtime.sqlite3",
        "runtime_send_settlement_audit_v2_no_replace",
        "SEND_SETTLEMENT_CERTIFICATE_SCHEMA_UNSAFE",
    ),
    (
        "hub.sqlite3",
        "terminal_send_outbox_cancel_audit_v2_no_update",
        "SEND_SETTLEMENT_HUB_OUTBOX_AUDIT_SCHEMA_UNSAFE",
    ),
    (
        "hub.sqlite3",
        "terminal_send_outbox_cancel_audit_v2_no_delete",
        "SEND_SETTLEMENT_HUB_OUTBOX_AUDIT_SCHEMA_UNSAFE",
    ),
    (
        "hub.sqlite3",
        "terminal_send_outbox_cancel_audit_v2_no_replace",
        "SEND_SETTLEMENT_HUB_OUTBOX_AUDIT_SCHEMA_UNSAFE",
    ),
)


@pytest.mark.parametrize(
    ("database", "trigger", "error_code"),
    _V2_AUDIT_GUARDS,
    ids=[trigger for _, trigger, _ in _V2_AUDIT_GUARDS],
)
def test_dropping_any_v2_audit_guard_invalidates_the_certificate(
    tmp_path: Path, database: str, trigger: str, error_code: str
) -> None:
    """All six append-only guards are required to re-verify a certificate."""

    fixture = _build_evidence(tmp_path)
    settle_terminal_send(fixture.root, fixture.target, apply=True)
    _execute(fixture.root / database, f"DROP TRIGGER {trigger}")

    assert trigger not in _sqlite_master_names(fixture.root / database)
    for apply in (False, True):
        with pytest.raises(SettlementRefused, match=error_code):
            settle_terminal_send(fixture.root, fixture.target, apply=apply)

    assert _verify_failed_settlement(fixture.root) is False
    assert _runtime_statuses(fixture.root) == ("failed", "rejected")
    assert _hub_outbox_statuses(fixture.root) == _CANCELLED_HUB_OUTBOXES


@pytest.mark.parametrize("apply", [False, True])
def test_pending_review_request_without_outbox_blocks_settlement(
    tmp_path: Path, apply: bool
) -> None:
    """A pending Hub review for the target draft is work even without an outbox."""

    fixture = _build_evidence(tmp_path)
    _execute(
        fixture.root / "hub.sqlite3",
        """INSERT INTO review_requests(review_id,draft_id,status,created_at)
           VALUES(?,?,?,?)""",
        (REVIEW_ID, DRAFT_ID, "pending", NOW),
    )

    with pytest.raises(
        SettlementRefused,
        match="SEND_SETTLEMENT_OTHER_WORK_ACTIVE",
    ):
        settle_terminal_send(fixture.root, fixture.target, apply=apply)

    assert _runtime_statuses(fixture.root) == ("authorized", "waiting")
    assert _hub_outbox_statuses(fixture.root) == _PENDING_HUB_OUTBOXES
    assert _audit_table_exists(fixture.root) is False
    with sqlite3.connect(fixture.root / "hub.sqlite3") as db:
        assert db.execute(
            "SELECT status FROM review_requests WHERE review_id=?",
            (REVIEW_ID,),
        ).fetchone() == ("pending",)
