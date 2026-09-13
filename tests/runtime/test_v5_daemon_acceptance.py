"""Black-box daemon acceptance checks using the assembled shared authorities.

These tests intentionally pass ``clock`` to ``assemble_runtime``.  Until that
optional parameter is present in production, collection reaches a concrete
TypeError instead of silently substituting a second fake runtime.
"""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta

import pytest

from messenger_ai.execution_guard import SupportLevel
from messenger_ai.llm import ReplyAction, ReplyPlan, ReplyPlanResult
from messenger_ai.memory import Contact, IdentityBinding
from messenger_ai.policy import CapabilitySnapshot, ExecutionMode
from messenger_ai.domain import SendStatus
from messenger_ai.rules.models import HumanApproval, RuleSource
from messenger_ai.runtime.assembly import assemble_runtime
_HARNESS_SPEC = importlib.util.spec_from_file_location(
    "v5_harness", __file__.replace("test_v5_daemon_acceptance.py", "v5_harness.py")
)
assert _HARNESS_SPEC and _HARNESS_SPEC.loader
_HARNESS_MODULE = importlib.util.module_from_spec(_HARNESS_SPEC)
_HARNESS_SPEC.loader.exec_module(_HARNESS_MODULE)
V5VMHarness = _HARNESS_MODULE.V5VMHarness


RULES = """
schema_version: 1
rulepack_id: daemon-acceptance
persona:
  identity: test persona
  language: zh-CN
  tone: [warm]
  preferred_length: concise
required_behaviors: [\"先确认对方问题\"]
prohibited_behaviors:
  - text: \"禁止透露隐私\"
    enforcement: [POLICY_GUARD]
escalation_rules: []
pacing: {}
contacts: {}
examples: {}
"""


class RecordingProvider:
    def __init__(self) -> None:
        self.requests = []

    async def plan_reply(self, request):
        self.requests.append(request)
        return ReplyPlanResult(
            request_id=request.request_id,
            rule_version=request.rules.rule_version,
            context_fingerprint=request.context_fingerprint,
            plan=ReplyPlan(action=ReplyAction.DRAFT, reply_text="收到啦", reply_segments=["收到啦"], confidence=1),
            model="fixture-model",
            latency_ms=0,
        )


def _build(
    tmp_path,
    contacts: int,
    *,
    conversation_type: str = "direct",
    content_policy_checks_enabled: bool = True,
):
    harness = V5VMHarness(tmp_path / "ports", contacts=contacts)
    capability = CapabilitySnapshot(
        capability_version="fixture-v1", environment_fingerprint="fixture",
        send_background=SupportLevel.UNSUPPORTED, verify_background=SupportLevel.UNSUPPORTED,
        send_guest_foreground=SupportLevel.SUPPORTED, verify_guest_foreground=SupportLevel.SUPPORTED,
        execution_mode=ExecutionMode.GUEST_FOREGROUND, healthy=True, client_version="fixture",
    )
    provider = RecordingProvider()
    # Clock injection is part of the acceptance contract; current production
    # builds may fail here until Sol's clock plumbing lands.
    app = assemble_runtime(
        data_dir=tmp_path / "app", planner_provider=provider, driver=harness.bridge,
        capability=capability, authorization_signing_key=b"x" * 32,
        model_concurrency=2, clock=harness.clock,
        content_policy_checks_enabled=content_policy_checks_enabled,
    )
    draft = app.rules.ingest(RuleSource(name="daemon.yaml", content=RULES.encode()))
    app.rules.activate(draft.draft_id, HumanApproval(approver_id="fixture", reason="acceptance"))
    for binding in harness.bindings:
        app.memory.create_contact(Contact(contact_id=binding.contact_id, created_at=harness.clock.now()))
        app.memory.bind_identity(IdentityBinding(
            contact_id=binding.contact_id, platform="qq", account_id=binding.account_id,
            conversation_id=binding.hub_conversation_id,
            platform_evidence_hash=__import__("hashlib").sha256(binding.participant_signature.encode()).hexdigest(),
            verified_by="fixture", verified_at=harness.clock.now()))
        app.state.register(account_id=binding.account_id, contact_id=binding.contact_id,
                           conversation_id=binding.hub_conversation_id, binding_revision=1,
                           conversation_type=conversation_type)
    return app, harness, provider


@pytest.mark.parametrize(
    ("conversation_type", "error_code"),
    (("group", "group_conversation_unsupported"),
     ("unknown", "conversation_type_unknown")),
)
def test_uncertified_or_group_conversation_stops_before_model(
    tmp_path, conversation_type, error_code
):
    app, harness, provider = _build(
        tmp_path, 3, conversation_type=conversation_type
    )

    async def run():
        for _ in range(3):
            await app.tick()
        harness.append_inbound(0, "guarded", key="guarded")
        for _ in range(6):
            await app.tick()
        await app.run_until_idle(max_ticks=20)

    asyncio.run(run())
    assert provider.requests == []
    row = app.state.connection.execute(
        "SELECT status,error_code FROM runtime_planning_jobs "
        "WHERE conversation_id='hub-0'"
    ).fetchone()
    assert (row["status"], row["error_code"]) == ("failed", error_code)


@pytest.mark.parametrize("contacts", [3, 5])
def test_real_assembled_daemon_one_round_and_direction_history(tmp_path, contacts):
    app, harness, provider = _build(tmp_path, contacts)

    async def run():
        # Establish the bridge cursor baseline before introducing new inbound.
        await app.tick()
        harness.append_inbound(0, "第一轮来信", key="in-1")
        for _ in range(contacts):
            await app.tick()
        await app.run_until_idle(max_ticks=20)
        assert provider.requests
        first = provider.requests[-1]
        assert [item.message_key for item in first.inbound] == ["qq-uia/hub-0/1"]
        assert [item.message_key for item in first.contact.recent_messages] == ["qq-uia/hub-0/1"]

        plan_row = app.pacing.connection.execute(
            "SELECT earliest_send_at FROM m10_plans WHERE conversation_id='hub-0'"
        ).fetchone()
        assert plan_row is not None
        harness.clock.set(datetime.fromisoformat(plan_row["earliest_send_at"]) + timedelta(seconds=1))
        harness.register_send_text("收到啦")
        for _ in range(contacts * 2):
            await app.tick()
        await app.run_until_idle(max_ticks=20)
        operation = app.hub.store.connection.execute(
            "SELECT status FROM send_operations WHERE idempotency_key LIKE 'm10:%'"
        ).fetchone()
        assert operation is not None and operation["status"] == SendStatus.VERIFIED.value
        assert sum(item.kind.value == "commit" for item in harness.port.requests) == 1

        # Let the real bridge observation publish the verified bot echo before
        # the next inbound arrives; bot echo must become directional history.
        for _ in range(contacts):
            await app.tick()
        await app.run_until_idle(max_ticks=20)

        harness.append_inbound(0, "第二轮来信", key="in-2")
        for _ in range(contacts):
            await app.tick()
        await app.run_until_idle(max_ticks=20)
        second = [item for item in provider.requests if item.contact.conversation_id == "hub-0"][-1]
        assert [item.message_key for item in second.inbound] == ["qq-uia/hub-0/3"]
        assert any(item.direction.value == "bot_outbound" for item in second.contact.recent_messages)

    asyncio.run(run())


@pytest.mark.parametrize("contacts", [3, 5])
def test_rr_plans_each_registered_contact_independently(tmp_path, contacts):
    app, harness, provider = _build(tmp_path, contacts)

    async def run():
        for _ in range(contacts):
            await app.tick()
        for index in range(contacts):
            harness.append_inbound(index, f"联系人{index}来信", key=f"in-{index}")
        for _ in range(contacts * 2):
            await app.tick()
        await app.run_until_idle(max_ticks=40)
        planned = {item.contact.conversation_id for item in provider.requests}
        assert planned == {f"hub-{index}" for index in range(contacts)}

    asyncio.run(run())
