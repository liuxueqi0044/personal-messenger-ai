from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))

from messenger_ai.llm import ReplyAction, ReplyPlan, ReplyPlanResult
from messenger_ai.runtime.reevaluation import (
    inspect_operator_reevaluation,
    prepare_operator_reevaluation,
)
from test_v5_daemon_acceptance import _build


class PlanProvider:
    def __init__(self, plan: ReplyPlan) -> None:
        self.plan = plan

    async def plan_reply(self, request):
        return ReplyPlanResult(
            request_id=request.request_id,
            rule_version=request.rules.rule_version,
            context_fingerprint=request.context_fingerprint,
            plan=self.plan,
            model="audit-model",
            latency_ms=19,
        )


async def _plan_one(app, harness, text: str) -> None:
    for _ in range(3):
        await app.tick()
    harness.append_inbound(0, text, key="audit-inbound")
    for _ in range(10):
        await app.tick()


def test_review_exit_retains_plan_policy_reason_and_real_inbound(tmp_path):
    app, harness, _ = _build(tmp_path, 3)
    app.planning.planner.provider = PlanProvider(ReplyPlan(
        action=ReplyAction.DRAFT,
        reply_text="可以具体说说吗",
        reply_segments=["可以具体说说吗"],
        confidence=1,
    ))

    asyncio.run(_plan_one(app, harness, "这个价格是多少"))

    row = app.state.connection.execute(
        "SELECT * FROM runtime_planner_evaluations WHERE conversation_id='hub-0'"
    ).fetchone()
    assert (row["action"], row["outcome"], row["decision_code"]) == (
        "draft", "review_required", "review"
    )
    assert json.loads(row["plan_json"])["reply_text"] == "可以具体说说吗"
    assert json.loads(row["provider_request_json"])["inbound"][-1]["text"] == "这个价格是多少"
    policy_request = json.loads(row["policy_requests_json"])[0]
    assert policy_request["inbound_text"] == "这个价格是多少"
    assert "SENSITIVE_TOPIC" in json.loads(row["policy_reason_codes_json"])
    assert "money" in json.loads(row["policy_sensitive_categories_json"])
    job = app.state.connection.execute(
        "SELECT status,error_code FROM runtime_planning_jobs WHERE conversation_id='hub-0'"
    ).fetchone()
    assert tuple(job) == ("failed", "review")


def test_provider_prohibited_rule_mapping_is_audited_as_policy_block(tmp_path):
    app, harness, _ = _build(tmp_path, 3)
    app.planning.planner.provider = PlanProvider(ReplyPlan(
        action=ReplyAction.DRAFT,
        reply_text="普通回复",
        reply_segments=["普通回复"],
        confidence=1,
        prohibited_rule_results=("provider-rule-7",),
    ))

    asyncio.run(_plan_one(app, harness, "你好"))

    row = app.state.connection.execute(
        "SELECT * FROM runtime_planner_evaluations WHERE conversation_id='hub-0'"
    ).fetchone()
    assert (row["outcome"], row["decision_code"]) == ("policy_blocked", "blocked")
    assert "PROHIBITED_RULE_HIT" in json.loads(row["policy_reason_codes_json"])
    assert json.loads(row["policy_rule_ids_json"]) == ["provider-rule-7"]
    assert app.state.connection.execute(
        "SELECT COUNT(*) FROM runtime_plan_artifacts WHERE conversation_id='hub-0'"
    ).fetchone()[0] == 0


def test_disabled_content_policy_check_is_effective_and_audited(tmp_path):
    app, harness, _ = _build(
        tmp_path, 3, content_policy_checks_enabled=False
    )
    app.planning.planner.provider = PlanProvider(ReplyPlan(
        action=ReplyAction.DRAFT,
        reply_text="普通回复",
        reply_segments=["普通回复"],
        confidence=1,
        prohibited_rule_results=("provider-description-misclassified-as-rule",),
    ))

    asyncio.run(_plan_one(app, harness, "你好"))

    row = app.state.connection.execute(
        "SELECT outcome,content_policy_checks_enabled "
        "FROM runtime_planner_evaluations WHERE conversation_id='hub-0'"
    ).fetchone()
    assert tuple(row) == ("scheduled", 0)


def test_lost_final_version_cas_cancels_cross_store_pacing_plan(tmp_path):
    app, harness, _ = _build(tmp_path, 3)
    app.planning.planner.provider = PlanProvider(ReplyPlan(
        action=ReplyAction.DRAFT,
        reply_text="收到",
        reply_segments=["收到"],
        confidence=1,
    ))
    schedule = app.pacing.schedule

    def pause_after_schedule(request):
        outcome = schedule(request)
        revision, _paused, _reason = app.state.global_control()
        assert app.state.set_global_pause(
            paused=True,
            expected_revision=revision,
            reason="cas-test",
        )
        return outcome

    app.pacing.schedule = pause_after_schedule
    asyncio.run(_plan_one(app, harness, "你好"))

    evaluation = app.state.connection.execute(
        "SELECT outcome FROM runtime_planner_evaluations WHERE conversation_id='hub-0'"
    ).fetchone()
    assert evaluation["outcome"] == "stale"
    assert app.state.connection.execute(
        "SELECT COUNT(*) FROM runtime_plan_artifacts WHERE conversation_id='hub-0'"
    ).fetchone()[0] == 0
    pacing = app.pacing.connection.execute(
        "SELECT status,cancel_reason FROM m10_plans WHERE conversation_id='hub-0'"
    ).fetchone()
    assert tuple(pacing) == ("cancelled", "superseded")


def test_explicit_reevaluation_uses_fresh_pacing_anchor_for_old_inbound(tmp_path):
    app, harness, _ = _build(tmp_path, 3)
    app.planning.planner.provider = PlanProvider(ReplyPlan(
        action=ReplyAction.DRAFT,
        reply_text="你好呀 很高兴认识你",
        reply_segments=["你好呀 很高兴认识你"],
        confidence=0.72,
        prohibited_rule_results=("旧内容规则说明",),
    ))
    asyncio.run(_plan_one(app, harness, "你好呀"))
    original = app.state.connection.execute(
        "SELECT request_id FROM runtime_planner_evaluations WHERE conversation_id='hub-0'"
    ).fetchone()
    assert original is not None
    original_observed_at = app.hub.store.connection.execute(
        "SELECT observed_at FROM messages WHERE conversation_id='hub-0'"
    ).fetchone()[0]

    harness.clock.advance(600)
    bridge = sqlite3.connect(":memory:")
    bridge.row_factory = sqlite3.Row
    bridge.execute(
        "CREATE TABLE qq_vm_ops(operation_id TEXT,conversation_id TEXT,status TEXT)"
    )
    inspection = inspect_operator_reevaluation(
        runtime=app.state.connection,
        hub=app.hub.store.connection,
        bridge=bridge,
        original_request_id=original["request_id"],
    )
    prepare_operator_reevaluation(
        state=app.state,
        hub=app.hub.store.connection,
        bridge=bridge,
        reevaluation_id=str(uuid4()),
        original_request_id=original["request_id"],
        expected_conversation_id="hub-0",
        expected_binding_revision=1,
        expected_conversation_revision=2,
        expected_current_global_revision=inspection["current_global_revision"],
        expected_source_keys_sha256=inspection["source_keys_sha256"],
        operator_id="operator",
        reason_code="content_policy_disabled_by_user",
    )
    app.planning.policy.content_policy_checks_enabled = False
    asyncio.run(app.run_until_idle(max_ticks=20))

    plan = app.pacing.connection.execute(
        "SELECT status,created_at,expires_at FROM m10_plans WHERE conversation_id='hub-0'"
    ).fetchone()
    assert plan is not None and plan["status"] == "waiting"
    assert datetime.fromisoformat(plan["created_at"]) == harness.clock.now()
    assert datetime.fromisoformat(plan["expires_at"]) > harness.clock.now()
    assert app.hub.store.connection.execute(
        "SELECT observed_at FROM messages WHERE conversation_id='hub-0'"
    ).fetchone()[0] == original_observed_at
    assert app.hub.store.connection.execute(
        "SELECT COUNT(*) FROM send_operations"
    ).fetchone()[0] == 0
