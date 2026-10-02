"""Navigation gates exercise the real assembled authorities, without live UI."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from messenger_ai.domain import (
    Authorization, AuthorizationType, AuthorizedSendCommand, Draft, InboundMessage,
    Platform, ReplyPlan, SendOperation, SendStatus,
)
from messenger_ai.pacing.models import DraftSnapshot as PacingDraft, ScheduleRequest
from messenger_ai.policy import (
    CapabilitySnapshot, ConversationType, DraftSnapshot, LivePolicyState,
    PolicyRequest, SupportLevel,
)
from messenger_ai.rules.models import HumanApproval, RuleSource
from messenger_ai.runtime.assembly import assemble_runtime
from messenger_ai.runtime.contracts import ObservationBatch, ObservedMessage
from messenger_ai.runtime.due_dispatch import DueNavigationPreflightResult
from messenger_ai.runtime.projections import policy_version
from messenger_ai.testing import FakeClock


NOW = datetime(2026, 10, 2, tzinfo=UTC)
KEY = "assembly-source-key"
RULES = b"""schema_version: 1
rulepack_id: navigation-assembly
persona:
  identity: test persona
  language: zh-CN
  tone: [warm]
  preferred_length: concise
required_behaviors: [answer naturally]
prohibited_behaviors: []
escalation_rules: []
pacing: {}
contacts: {}
examples: {}
"""


class Planner:
    async def plan_reply(self, request):
        raise AssertionError("due tests must not create another plan")


class Driver:
    def __init__(self):
        self.prepares = []
        self.commit_calls = 0

    async def prepare_send(self, command, *, operation_id, **kwargs):
        self.prepares.append(command)
        return SendOperation(operation_id=operation_id, idempotency_key=command.idempotency_key,
                             draft_id=command.draft_id, status=SendStatus.PREPARED)

    async def commit_send(self, operation):
        self.commit_calls += 1
        return operation.model_copy(update={"status": SendStatus.COMMITTED})

    async def verify_send(self, operation):
        return operation.model_copy(update={"status": SendStatus.VERIFIED})


class Gate:
    def __init__(self, status):
        self.status = status
        self.calls = []
        self.app = None

    async def check(self, due, **revisions):
        # This is observed inside the actual assembled DueCoordinator call.
        assert self.app.hub.store.connection.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] == 0
        assert self.app.hub.store.connection.execute("SELECT COUNT(*) FROM send_operations").fetchone()[0] == 0
        assert self.app.due.authorization._store._keeper.execute("SELECT COUNT(*) FROM policy_authorizations").fetchone()[0] == 0
        self.calls.append((due, revisions))
        return DueNavigationPreflightResult(status=self.status, error_code="assembly_navigation_blocked")


def build(tmp_path, **optional):
    clock, driver = FakeClock(NOW), Driver()
    capability = CapabilitySnapshot(capability_version="assembly-test", environment_fingerprint="test",
        send_background=SupportLevel.SUPPORTED, verify_background=SupportLevel.SUPPORTED,
        healthy=True, client_version="test")
    app = assemble_runtime(data_dir=tmp_path, planner_provider=Planner(), driver=driver,
        capability=capability, authorization_signing_key=b"assembly-test-signing-key-not-real", clock=clock,
        content_policy_checks_enabled=False, **optional)
    gate = optional.get("navigation_preflight")
    if isinstance(gate, Gate):
        gate.app = app
    app.state.register(account_id="account", contact_id="contact", conversation_id="conversation",
                       binding_revision=1, conversation_type="direct")
    app.state.apply_observation(ObservationBatch(account_id="account", contact_id="contact",
        conversation_id="conversation", binding_revision=1, conversation_revision=1, complete=True,
        messages=(ObservedMessage(local_message_key=KEY, direction="inbound", text="Hello", observed_at=NOW),)))
    app.hub.ingest(InboundMessage(platform=Platform.QQ, account_id="account", conversation_id="conversation",
        contact_id="contact", platform_message_key=KEY, text="Hello", observed_at=NOW))
    rule_draft = app.rules.ingest(RuleSource(name="navigation-assembly.yaml", content=RULES))
    app.rules.activate(rule_draft.draft_id, HumanApproval(approver_id="fixture", reason="offline test"))
    rule_version = app.rules.resolve("contact").rulepack.version
    binding, revision = app.state.revisions("conversation")
    version = policy_version(binding_revision=binding, conversation_revision=revision,
                             global_revision=1, rule_version=rule_version)
    draft_id, plan_id = uuid4(), uuid4()
    request = PolicyRequest(draft=DraftSnapshot(draft_id=str(draft_id), platform="qq", account_id="account",
        conversation_id="conversation", contact_id="contact", body="Reply", expected_last_message_key=KEY,
        source_message_keys=(KEY,), rulepack_version=rule_version, pacing_plan_id=str(plan_id),
        pacing_rule_version=rule_version, capability_snapshot_hash=capability.snapshot_hash,
        policy_state_version=version, binding_revision=binding, conversation_revision=revision,
        created_at=NOW, expires_at=NOW + timedelta(minutes=10)),
        state=LivePolicyState(observed_at=NOW, last_message_key=KEY, active_rulepack_version=rule_version,
            active_pacing_rule_version=rule_version, capability=capability, policy_state_version=version,
            binding_revision=binding, conversation_revision=revision, conversation_type=ConversationType.DIRECT,
            contact_whitelisted=True, automation_enabled=True),
        scheduled_due_at=NOW, plan_expires_at=NOW + timedelta(minutes=10))
    eligibility = app.due.authorization._engine.evaluate_eligibility(request)
    assert eligibility.may_schedule
    plan = app.pacing.schedule(ScheduleRequest(
        draft=PacingDraft(draft_id=draft_id, conversation_id="conversation", contact_id="contact", text="Reply",
            text_hash=sha256(b"Reply").hexdigest(), expected_last_message_key=KEY, rule_version=rule_version,
            eligibility_id=eligibility.decision_id),
        reply_plan=ReplyPlan(action="draft", reply_text="Reply", reply_segments=["Reply"]),
        source_message_keys=(KEY,), first_inbound_at=NOW, last_inbound_at=NOW,
        reserved_pacing_plan_id=plan_id, segment_draft_ids=(draft_id,),
        segment_eligibility_ids=(eligibility.decision_id,),
    )).plan
    app.state.save_plan_artifact(pacing_plan_id=plan_id, conversation_id="conversation",
        conversation_revision=revision, binding_revision=binding, global_revision=1,
        eligibility_json=json.dumps([{"eligibility": eligibility.model_dump(mode="json"),
                                      "request": request.model_dump(mode="json")}]),
        planner_json=json.dumps({"reply_segments": ["Reply"]}), rule_version=rule_version,
        account_id="account", contact_id="contact", source_keys=(KEY,), segment_draft_ids=(str(draft_id),))
    clock.advance(30)
    return app, driver, clock, plan, request, eligibility


@pytest.mark.asyncio
@pytest.mark.parametrize("status,outbox_status", [("retry_wait", "pending"), ("needs_attention", "navigation_attention")])
async def test_assembled_navigation_failure_runs_before_any_authorization_and_consumes_nothing(tmp_path, status, outbox_status):
    gate = Gate(status)
    app, driver, _clock, plan, _request, _eligibility = build(tmp_path, navigation_preflight=gate)
    try:
        assert app.due.navigation_preflight is gate
        assert await app.due.dispatch_one()
        assert gate.calls[0][0].pacing_plan_id == plan.pacing_plan_id
        assert gate.calls[0][1] == {"binding_revision": 1, "conversation_revision": 2, "global_revision": 1}
        assert driver.prepares == [] and driver.commit_calls == 0
        assert app.hub.store.connection.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] == 0
        assert app.due.authorization._store._keeper.execute("SELECT COUNT(*) FROM policy_authorizations").fetchone()[0] == 0
        assert app.state.connection.execute("SELECT COUNT(*) FROM runtime_segment_executions").fetchone()[0] == 0
        row = app.pacing.connection.execute("SELECT status,payload_json FROM m10_due_outbox").fetchone()
        assert row["status"] == outbox_status
        assert json.loads(row["payload_json"])["body"] == "Reply"
        assert app.pacing.get_plan(plan.pacing_plan_id).source_message_keys == (KEY,)
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_default_assembled_runtime_keeps_original_due_send_path(tmp_path):
    app, driver, _clock, _plan, _request, _eligibility = build(tmp_path)
    try:
        assert app.due.navigation_preflight is app.due.due_operation_recovery is None
        assert await app.due.dispatch_one()
        assert len(driver.prepares) == driver.commit_calls == 1
        assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == "delivered"
        assert app.hub.store.connection.execute("SELECT status,commit_intent FROM send_operations").fetchone()[0] == "verified"
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_explicit_recovery_is_passed_through_and_existing_operation_bypasses_navigation(tmp_path):
    calls = []

    async def recover(due, operation):
        calls.append((due, operation.operation_id))
        operation.status = SendStatus.CANCELLED
        return app.hub._persist_operation(operation)

    gate = Gate("needs_attention")
    app, driver, _clock, plan, request, eligibility = build(
        tmp_path, navigation_preflight=gate, due_operation_recovery=recover)
    try:
        assert app.due.due_operation_recovery is recover
        app.pacing.due_for_revalidation()
        due = app.pacing.claim_due_outbox_with_tokens()[0].due
        app.pacing.recover_due_outbox()
        _decision, envelope = app.due.authorization.authorize_due(
            eligibility, request.model_copy(update={"scheduled_due_at": due.due_at}))
        assert envelope is not None
        app.hub.create_draft(Draft(draft_id=due.draft_id, conversation_id=due.conversation_id,
            contact_id=due.contact_id, text=due.body, source_message_keys=(KEY,), rule_version=due.rule_version))
        authorization = Authorization(authorization_id=UUID(envelope.authorization_id), draft_id=due.draft_id,
            conversation_id=due.conversation_id, expected_last_message_key=KEY, text_hash=due.body_hash,
            idempotency_key=f"m10:{plan.pacing_plan_id}:0", authorization_type=AuthorizationType.POLICY,
            policy_version=request.draft.policy_state_version, expires_at=envelope.expires_at)
        app.hub.persist_authorization(authorization)
        operation = app.hub.create_send_operation(AuthorizedSendCommand(**authorization.model_dump(exclude={"consumed"})))
        binding, revision = app.state.revisions(due.conversation_id)
        assert app.state.create_segment_execution(pacing_plan_id=due.pacing_plan_id, segment_index=0,
            conversation_id=due.conversation_id, body_hash=due.body_hash,
            binding_revision=binding, conversation_revision=revision)
        assert app.state.bind_segment_operation(pacing_plan_id=due.pacing_plan_id, segment_index=0,
            authorization_id=authorization.authorization_id, operation_id=operation.operation_id)
        assert await app.due.dispatch_one()
        assert calls == [(due, operation.operation_id)]
        assert gate.calls == driver.prepares == []
        assert app.pacing.connection.execute("SELECT status FROM m10_due_outbox").fetchone()[0] == "delivered"
        assert app.state.connection.execute("SELECT status FROM runtime_segment_executions").fetchone()[0] == "cancelled"
        assert app.hub.store.connection.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] == 1
        assert app.due.authorization._store._keeper.execute("SELECT COUNT(*) FROM policy_authorizations").fetchone()[0] == 1
    finally:
        await app.aclose()
