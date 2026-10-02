import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from messenger_ai.domain import Authorization, AuthorizationType, AuthorizedSendCommand, Draft, InboundMessage, Platform, ReplyPlan, SendOperation, SendStatus
from messenger_ai.hub import HubService, SQLiteHubStore
from messenger_ai.pacing import PacingScheduler
from messenger_ai.pacing.models import DraftSnapshot as PacingDraft, ScheduleRequest
from messenger_ai.policy import AuthorizationService, CapabilitySnapshot, ConversationType, DraftSnapshot, LivePolicyState, PolicyEngine, PolicyRequest, SQLiteAuthorizationStore, SupportLevel
from messenger_ai.runtime.contracts import ObservationBatch, ObservedMessage
from messenger_ai.runtime.coordinator import RuntimeCoordinator
from messenger_ai.runtime.due_dispatch import DueCoordinator, DueNavigationPreflightResult
from messenger_ai.runtime.projections import policy_version
from messenger_ai.runtime.send_dispatcher import SendDispatcher
from messenger_ai.runtime.state import RuntimeState, VerifiedSendStorePaths
from messenger_ai.testing import FakeClock


NOW = datetime(2026, 10, 2, tzinfo=UTC)
KEYS = ("source-1", "source-2")


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


class CountingAuthorization:
    def __init__(self, service):
        self.service = service
        self.requests = []

    def authorize_due(self, eligibility, request):
        self.requests.append(request)
        return self.service.authorize_due(eligibility, request)

    def consume(self, *args, **kwargs):
        return self.service.consume(*args, **kwargs)


class Preflight:
    def __init__(self, results=None, action=None):
        self.results = list(results or [DueNavigationPreflightResult(status="ready")])
        self.action = action
        self.calls = []

    async def check(self, due, **revisions):
        self.calls.append((due, revisions))
        if self.action:
            self.action()
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class Harness:
    def __init__(self, tmp_path, preflight=None, recovery=None):
        self.clock = FakeClock(NOW)
        self.hub = HubService(SQLiteHubStore(tmp_path / "hub.sqlite"), clock=self.clock)
        self.state = RuntimeState(tmp_path / "runtime.sqlite", verified_send_stores=VerifiedSendStorePaths(
            hub=tmp_path / "hub.sqlite", pacing=tmp_path / "pacing.sqlite"))
        self.state.register(account_id="account", contact_id="contact", conversation_id="conversation", binding_revision=1)
        messages = tuple(ObservedMessage(local_message_key=key, direction="inbound", text=f"Message {index}",
                                         observed_at=NOW) for index, key in enumerate(KEYS))
        self.state.apply_observation(ObservationBatch(account_id="account", contact_id="contact",
            conversation_id="conversation", binding_revision=1, conversation_revision=1,
            complete=True, messages=messages))
        for message in messages:
            self.hub.ingest(InboundMessage(platform=Platform.QQ, account_id="account", conversation_id="conversation",
                contact_id="contact", platform_message_key=message.local_message_key, observed_at=NOW, text=message.text))
        self.pacing = PacingScheduler(tmp_path / "pacing.sqlite", clock=self.clock)
        self.policy_store = SQLiteAuthorizationStore(tmp_path / "auth.sqlite")
        self.engine = PolicyEngine(self.clock.now, content_policy_checks_enabled=False)
        self.authorization = CountingAuthorization(AuthorizationService(
            self.engine, self.policy_store, b"test-signing-key-is-not-a-real-key", self.clock.now))
        self.capability = CapabilitySnapshot(capability_version="test", environment_fingerprint="test",
            send_background=SupportLevel.SUPPORTED, verify_background=SupportLevel.SUPPORTED,
            healthy=True, client_version="test")
        self.rules = SimpleNamespace(version="rules-v1")
        self.rules.resolve = lambda *_args, **_kwargs: SimpleNamespace(rulepack=SimpleNamespace(version=self.rules.version))
        self.driver = Driver()
        self.dispatcher = SendDispatcher(state=self.state, hub=self.hub, pacing=self.pacing,
                                        authorization=self.authorization, driver=self.driver)
        self.coordinator = DueCoordinator(state=self.state, hub=self.hub, pacing=self.pacing, rules=self.rules,
            authorization=self.authorization, dispatcher=self.dispatcher, capability_provider=lambda: self.capability,
            navigation_preflight=preflight, due_operation_recovery=recovery)
        plan_id, draft_ids = uuid4(), (uuid4(), uuid4())
        binding, revision = self.state.revisions("conversation")
        version = policy_version(binding_revision=binding, conversation_revision=revision,
                                 global_revision=1, rule_version="rules-v1")
        self.requests = tuple(PolicyRequest(
            draft=DraftSnapshot(draft_id=str(draft_id), platform="qq", account_id="account", conversation_id="conversation",
                contact_id="contact", body=body, expected_last_message_key=KEYS[-1], source_message_keys=KEYS,
                rulepack_version="rules-v1", pacing_plan_id=str(plan_id), pacing_rule_version="rules-v1",
                capability_snapshot_hash=self.capability.snapshot_hash, policy_state_version=version,
                binding_revision=binding, conversation_revision=revision,
                created_at=NOW, expires_at=NOW + timedelta(minutes=10)),
            state=LivePolicyState(observed_at=NOW, last_message_key=KEYS[-1], active_rulepack_version="rules-v1",
                active_pacing_rule_version="rules-v1", capability=self.capability, policy_state_version=version,
                binding_revision=binding, conversation_revision=revision, conversation_type=ConversationType.DIRECT,
                contact_whitelisted=True, automation_enabled=True),
            scheduled_due_at=NOW, plan_expires_at=NOW + timedelta(minutes=10),
        ) for draft_id, body in zip(draft_ids, ("First", "Second")))
        self.decisions = tuple(self.engine.evaluate_eligibility(request) for request in self.requests)
        assert all(decision.may_schedule for decision in self.decisions)
        self.plan = self.pacing.schedule(ScheduleRequest(
            draft=PacingDraft(draft_id=draft_ids[0], conversation_id="conversation", contact_id="contact",
                text="FirstSecond", text_hash=sha256(b"FirstSecond").hexdigest(), expected_last_message_key=KEYS[-1],
                rule_version="rules-v1", eligibility_id=self.decisions[0].decision_id),
            reply_plan=ReplyPlan(action="draft", reply_text="FirstSecond", reply_segments=["First", "Second"]),
            source_message_keys=KEYS, first_inbound_at=NOW, last_inbound_at=NOW,
            reserved_pacing_plan_id=plan_id, segment_draft_ids=draft_ids,
            segment_eligibility_ids=tuple(decision.decision_id for decision in self.decisions),
        )).plan
        self.state.save_plan_artifact(pacing_plan_id=plan_id, conversation_id="conversation",
            conversation_revision=revision, binding_revision=binding, global_revision=1,
            eligibility_json=json.dumps([{"eligibility": decision.model_dump(mode="json"),
                                          "request": request.model_dump(mode="json")}
                                         for decision, request in zip(self.decisions, self.requests)]),
            planner_json=json.dumps({"reply_segments": ["First", "Second"]}), rule_version="rules-v1",
            account_id="account", contact_id="contact", source_keys=KEYS,
            segment_draft_ids=tuple(map(str, draft_ids)))
        self.clock.advance(30)

    def row(self):
        return self.pacing.connection.execute("SELECT * FROM m10_due_outbox ORDER BY outbox_id LIMIT 1").fetchone()

    def no_new_authority(self):
        assert self.hub.store.connection.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0] == 0
        assert self.hub.store.connection.execute("SELECT COUNT(*) FROM send_operations").fetchone()[0] == 0
        assert self.policy_store._keeper.execute("SELECT COUNT(*) FROM policy_authorizations").fetchone()[0] == 0

    def create_existing_operation(self, *, commit_intent=False, due=None):
        if due is None:
            self.pacing.due_for_revalidation()
            due = self.pacing.claim_due_outbox_with_tokens()[0].due
            self.pacing.recover_due_outbox()
        request = self.requests[0].model_copy(update={"scheduled_due_at": due.due_at})
        decision, envelope = self.authorization.authorize_due(self.decisions[0], request)
        assert envelope is not None
        self.hub.create_draft(Draft(draft_id=due.draft_id, conversation_id=due.conversation_id,
            contact_id=due.contact_id, text=due.body, source_message_keys=KEYS, rule_version="rules-v1"))
        authorization = Authorization(authorization_id=UUID(envelope.authorization_id), draft_id=due.draft_id,
            conversation_id=due.conversation_id, expected_last_message_key=due.expected_last_message_key,
            text_hash=due.body_hash, idempotency_key=f"m10:{due.pacing_plan_id}:0",
            authorization_type=AuthorizationType.POLICY, policy_version=request.draft.policy_state_version,
            expires_at=envelope.expires_at)
        self.hub.persist_authorization(authorization)
        operation = self.hub.create_send_operation(AuthorizedSendCommand(**authorization.model_dump(exclude={"consumed"})))
        if commit_intent:
            operation.status = SendStatus.UNCERTAIN
            self.hub._persist_operation(operation, commit_intent=True)
        binding, revision = self.state.revisions(due.conversation_id)
        assert self.state.create_segment_execution(pacing_plan_id=due.pacing_plan_id, segment_index=0,
            conversation_id=due.conversation_id, body_hash=due.body_hash,
            binding_revision=binding, conversation_revision=revision)
        assert self.state.bind_segment_operation(pacing_plan_id=due.pacing_plan_id, segment_index=0,
            authorization_id=authorization.authorization_id, operation_id=operation.operation_id)
        return operation


@pytest.mark.asyncio
async def test_failed_navigation_preserves_pending_segment_and_keys_then_success_uses_same_due(tmp_path):
    preflight = Preflight([DueNavigationPreflightResult(status="retry_wait", error_code="navigation_timeout"),
                          DueNavigationPreflightResult(status="ready")])
    harness = Harness(tmp_path, preflight)
    observations = list(map(tuple, harness.state.connection.execute("SELECT * FROM runtime_observations")))
    assert await harness.coordinator.dispatch_one()
    harness.no_new_authority()
    assert harness.authorization.requests == []
    assert harness.driver.prepares == []
    assert harness.row()["status"] == "pending"
    saved_payload = harness.row()["payload_json"]
    expires = harness.plan.expires_at
    assert not await harness.coordinator.dispatch_one()
    harness.clock.advance(10)
    assert await harness.coordinator.dispatch_one()
    assert len(preflight.calls) == 2
    assert preflight.calls[0][0] == preflight.calls[1][0]
    assert preflight.calls[0][0].body == "First"
    assert harness.row()["payload_json"] == saved_payload
    assert harness.row()["status"] == "delivered"
    assert len(harness.driver.prepares) == harness.driver.commit_calls == 1
    assert tuple(json.loads(harness.state.plan_artifact(harness.plan.pacing_plan_id)["source_keys_json"])) == KEYS
    assert harness.pacing.get_plan(harness.plan.pacing_plan_id).expires_at == expires
    assert list(map(tuple, harness.state.connection.execute("SELECT * FROM runtime_observations"))) == observations


@pytest.mark.asyncio
async def test_expired_due_skips_navigation_and_cannot_issue_or_renew_authorization(tmp_path):
    preflight = Preflight()
    harness = Harness(tmp_path, preflight)
    harness.pacing.due_for_revalidation()
    expires = harness.plan.expires_at
    harness.clock.set(max(expires, harness.requests[0].plan_expires_at) + timedelta(seconds=1))
    assert await harness.coordinator.dispatch_one()
    assert preflight.calls == harness.authorization.requests == harness.driver.prepares == []
    harness.no_new_authority()
    assert harness.row()["status"] == "delivered"
    assert harness.pacing.get_plan(harness.plan.pacing_plan_id).expires_at == expires


@pytest.mark.asyncio
async def test_needs_attention_stays_held_across_startup_recovery_and_never_blindly_retries(tmp_path):
    preflight = Preflight([DueNavigationPreflightResult(status="needs_attention", error_code="identity_profile_mismatch")])
    harness = Harness(tmp_path, preflight)
    assert await harness.coordinator.dispatch_one()
    RuntimeCoordinator(state=harness.state, hub=harness.hub, pacing=harness.pacing).recover()
    harness.pacing.recover()
    harness.clock.advance(30)
    assert not await harness.coordinator.dispatch_one()
    assert len(preflight.calls) == 1
    assert harness.row()["status"] == "navigation_attention"
    assert harness.state.plan_artifact(harness.plan.pacing_plan_id)["status"] == "waiting"
    assert harness.state.connection.execute("SELECT COUNT(*) FROM runtime_segment_executions").fetchone()[0] == 0
    harness.no_new_authority()


@pytest.mark.asyncio
async def test_ready_navigation_re_reads_control_and_does_not_authorize_paused_contact(tmp_path):
    harness = Harness(tmp_path, Preflight())
    harness.coordinator.navigation_preflight.action = lambda: harness.state.pause("conversation")
    assert await harness.coordinator.dispatch_one()
    assert harness.authorization.requests[-1].state.contact_paused
    assert harness.driver.prepares == []
    harness.no_new_authority()


@pytest.mark.asyncio
async def test_ready_navigation_re_reads_rule_and_plan_eligibility(tmp_path):
    harness = Harness(tmp_path, Preflight())
    harness.coordinator.navigation_preflight.action = lambda: setattr(harness.rules, "version", "rules-v2")
    assert await harness.coordinator.dispatch_one()
    assert harness.authorization.requests[-1].state.active_rulepack_version == "rules-v2"
    harness.no_new_authority()
    # A plan invalidated during the await is held before even asking M9.
    other = tmp_path / "other"
    other.mkdir()
    harness = Harness(other, Preflight())
    harness.coordinator.navigation_preflight.action = lambda: harness.pacing.connection.execute(
        "UPDATE m10_plans SET status='cancelled' WHERE pacing_plan_id=?", (str(harness.plan.pacing_plan_id),))
    assert await harness.coordinator.dispatch_one()
    assert harness.authorization.requests == []
    assert harness.row()["status"] == "navigation_attention"
    assert harness.row()["defer_reason"] == "due_plan_not_eligible"
    harness.no_new_authority()


@pytest.mark.asyncio
async def test_existing_operation_without_recovery_callback_holds_without_navigation_or_prepare(tmp_path):
    preflight = Preflight()
    harness = Harness(tmp_path, preflight)
    operation = harness.create_existing_operation(commit_intent=True)
    issued = len(harness.authorization.requests)
    assert await harness.coordinator.dispatch_one()
    assert preflight.calls == harness.driver.prepares == []
    assert len(harness.authorization.requests) == issued
    assert harness.row()["status"] == "operation_recovery_hold"
    assert harness.row()["operation_id"] == str(operation.operation_id)
    RuntimeCoordinator(state=harness.state, hub=harness.hub, pacing=harness.pacing).recover()
    assert not await harness.coordinator.dispatch_one()
    assert harness.row()["status"] == "operation_recovery_hold"


@pytest.mark.asyncio
async def test_existing_operation_uses_explicit_recovery_callback_and_never_navigation_or_new_prepare(tmp_path):
    preflight = Preflight()
    calls = []
    harness = Harness(tmp_path, preflight)
    operation = harness.create_existing_operation()
    issued = len(harness.authorization.requests)

    async def recover(due, existing):
        calls.append((due, existing.operation_id))
        existing.status = SendStatus.CANCELLED
        return harness.hub._persist_operation(existing)

    harness.coordinator.due_operation_recovery = recover
    assert await harness.coordinator.dispatch_one()
    assert calls[0][1] == operation.operation_id
    assert preflight.calls == harness.driver.prepares == []
    assert len(harness.authorization.requests) == issued
    assert harness.row()["status"] == "delivered"
    assert harness.state.connection.execute("SELECT status FROM runtime_segment_executions").fetchone()[0] == "cancelled"


@pytest.mark.asyncio
async def test_legacy_none_hook_uses_original_send_path(tmp_path):
    harness = Harness(tmp_path)
    assert await harness.coordinator.dispatch_one()
    assert len(harness.driver.prepares) == harness.driver.commit_calls == 1
    assert harness.row()["status"] == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["global_pause", "binding_revision"])
async def test_ready_navigation_rereads_global_and_identity_control(change, tmp_path):
    harness = Harness(tmp_path, Preflight())
    if change == "global_pause":
        action = lambda: harness.state.set_global_pause(paused=True, expected_revision=1)
    else:
        action = lambda: harness.state.register(account_id="account", contact_id="contact",
            conversation_id="conversation", binding_revision=2)
    harness.coordinator.navigation_preflight.action = action
    assert await harness.coordinator.dispatch_one()
    request = harness.authorization.requests[-1]
    assert request.state.global_paused if change == "global_pause" else request.state.binding_revision == 2
    assert harness.driver.prepares == []
    harness.no_new_authority()


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_during_wait", [False, True])
async def test_stale_navigation_ready_cannot_reject_or_settle_new_owner(expires_during_wait, tmp_path):
    harness = Harness(tmp_path, Preflight())
    reclaimed = []

    def reclaim():
        assert harness.pacing.recover_due_outbox() == 1
        reclaimed.append(harness.pacing.claim_due_outbox_with_tokens()[0])
        if expires_during_wait:
            harness.clock.set(max(harness.plan.expires_at, harness.requests[0].plan_expires_at)
                              + timedelta(seconds=1))

    harness.coordinator.navigation_preflight.action = reclaim
    with pytest.raises(RuntimeError, match="lost exact claim"):
        await harness.coordinator.dispatch_one()
    assert harness.pacing.is_due_claim_current(reclaimed[0])
    assert harness.row()["status"] == "dispatching"
    assert harness.pacing.connection.execute("SELECT status FROM m10_plans").fetchone()[0] == "due_for_revalidation"
    assert harness.state.connection.execute("SELECT COUNT(*) FROM runtime_segment_executions").fetchone()[0] == 0
    assert harness.authorization.requests == harness.driver.prepares == []
    harness.no_new_authority()


@pytest.mark.asyncio
async def test_stale_recovery_callback_cannot_complete_new_owner_or_runtime_segment(tmp_path):
    harness = Harness(tmp_path, Preflight())
    harness.create_existing_operation()
    reclaimed = []

    async def recover(_due, existing):
        harness.pacing.recover_due_outbox()
        reclaimed.append(harness.pacing.claim_due_outbox_with_tokens()[0])
        existing.status = SendStatus.CANCELLED
        return harness.hub._persist_operation(existing)

    harness.coordinator.due_operation_recovery = recover
    with pytest.raises(RuntimeError, match="lost exact claim"):
        await harness.coordinator.dispatch_one()
    assert harness.pacing.is_due_claim_current(reclaimed[0])
    assert harness.row()["status"] == "dispatching"
    assert harness.pacing.connection.execute("SELECT COUNT(*) FROM m10_segment_receipts").fetchone()[0] == 0
    assert harness.state.connection.execute("SELECT status FROM runtime_segment_executions").fetchone()[0] == "authorized"
    assert harness.driver.prepares == []


@pytest.mark.asyncio
async def test_ready_navigation_cannot_launder_extended_original_expiry(tmp_path):
    harness = Harness(tmp_path, Preflight())

    def extend_original():
        artifact = harness.state.plan_artifact(harness.plan.pacing_plan_id)
        snapshots = json.loads(artifact["eligibility_json"])
        snapshots[0]["request"]["plan_expires_at"] = (NOW + timedelta(days=1)).isoformat()
        snapshots[0]["request"]["draft"]["expires_at"] = (NOW + timedelta(days=1)).isoformat()
        harness.state.connection.execute("UPDATE runtime_plan_artifacts SET eligibility_json=?",
                                         (json.dumps(snapshots),))

    harness.coordinator.navigation_preflight.action = extend_original
    assert await harness.coordinator.dispatch_one()
    assert harness.row()["status"] == "navigation_attention"
    assert harness.row()["defer_reason"] == "due_artifact_changed"
    assert harness.authorization.requests == harness.driver.prepares == []
    harness.no_new_authority()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unpersisted_verified", "changed_identity", "verified_without_intent", "failed_with_intent", "runtime_unbound"])
async def test_recovery_requires_original_persisted_identity_and_durable_obligations(failure, tmp_path):
    harness = Harness(tmp_path, Preflight())
    operation = harness.create_existing_operation(commit_intent=failure == "failed_with_intent")
    issued = len(harness.authorization.requests)

    async def recover(_due, existing):
        if failure == "unpersisted_verified":
            return existing.model_copy(update={"status": SendStatus.VERIFIED})
        if failure == "changed_identity":
            return existing.model_copy(update={"draft_id": uuid4(), "status": SendStatus.CANCELLED})
        if failure == "runtime_unbound":
            harness.state.connection.execute("DELETE FROM runtime_segment_executions")
        existing.status = SendStatus.VERIFIED if failure == "verified_without_intent" else SendStatus.FAILED
        return harness.hub._persist_operation(existing)

    harness.coordinator.due_operation_recovery = recover
    assert await harness.coordinator.dispatch_one()
    assert harness.row()["status"] == "operation_recovery_hold"
    assert harness.row()["operation_id"] == str(operation.operation_id)
    assert harness.pacing.connection.execute("SELECT COUNT(*) FROM m10_segment_receipts").fetchone()[0] == 0
    assert len(harness.authorization.requests) == issued
    assert harness.driver.prepares == harness.coordinator.navigation_preflight.calls == []


@pytest.mark.asyncio
async def test_operation_appearing_during_navigation_is_recovery_held_even_when_expired(tmp_path):
    harness = Harness(tmp_path, Preflight())
    operations = []

    def existing_operation_appears():
        operations.append(harness.create_existing_operation(
            commit_intent=True, due=harness.coordinator.navigation_preflight.calls[-1][0]))
        harness.pacing.connection.execute("UPDATE m10_due_outbox SET operation_id=?",
                                          (str(operations[-1].operation_id),))
        harness.clock.set(max(harness.plan.expires_at, harness.requests[0].plan_expires_at)
                          + timedelta(seconds=1))

    harness.coordinator.navigation_preflight.action = existing_operation_appears
    assert await harness.coordinator.dispatch_one()
    assert harness.row()["status"] == "operation_recovery_hold"
    assert harness.row()["operation_id"] == str(operations[0].operation_id)
    assert len(harness.authorization.requests) == 1  # original operation setup only
    assert harness.driver.prepares == []
    assert harness.state.connection.execute("SELECT status FROM runtime_segment_executions").fetchone()[0] == "authorized"
