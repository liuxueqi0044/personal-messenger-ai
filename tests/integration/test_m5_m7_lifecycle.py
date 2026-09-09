from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from messenger_ai.adapters.wechat.sending import (
    AuthorizedWechatSend,
    CapturedSendEvidence,
    CurrentPolicyVersions,
    SemanticBackend,
    SendResultStatus,
    SendRoute,
    WechatBackgroundSendAdapter,
    text_digest,
)
from messenger_ai.domain import (
    AuthorizationType,
    AuthorizedSendCommand,
    Draft,
    InboundMessage,
    PacingPlan,
    Platform,
)
from messenger_ai.execution_guard import (
    ActionInterceptor,
    AdapterCapabilities,
    CapabilityRegistry,
    DesktopState,
    EnvironmentFingerprint,
    EnvironmentFingerprinter,
    ExecutionGuard,
    SnapshotContentionMonitor,
    SupportLevel,
)
from messenger_ai.hub import HubService, SQLiteHubStore
from messenger_ai.memory import SQLiteMemoryStore
from messenger_ai.rules.models import HumanApproval, RuleSource
from messenger_ai.rules.service import AtomicRulePackStore

RULES_V1 = """
schema_version: 1
rulepack_id: integration-pack
required_behaviors: ["确认问题"]
prohibited_behaviors:
  - text: "禁止泄露隐私"
    enforcement: [POLICY_GUARD]
pacing: {}
"""
RULES_V2 = RULES_V1.replace("确认问题", "确认并总结问题")


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 3, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class Desktop:
    def read_state(self) -> DesktopState:
        return DesktopState(
            foreground_window=1,
            keyboard_focus=2,
            pointer_position=(3, 4),
            clipboard_revision=1,
            window_state_digest="stable",
        )


class WechatEnvironment:
    def __init__(self) -> None:
        self.values = {
            "client_version": "4.1.12.55",
            "windows_version": "test-windows",
            "dpi_scale": 1.25,
            "theme": "light",
            "window_mode": "normal",
            "window_signature": "wechat-window",
            "process_signature": "wechat-process",
            "process_id": 202,
            "window_handle": 2002,
        }

    def read_environment(self, platform: Platform) -> dict:
        assert platform is Platform.WECHAT
        return dict(self.values)


ROUTE = SendRoute(
    backend=SemanticBackend.UIA,
    select_operation="uia.selection_item_pattern.select",
    compose_operation="uia.value_pattern.set",
    composer_read_operation="uia.text_pattern.get",
    commit_operation="uia.invoke_pattern.invoke",
    verify_operation="uia.read",
)


class Driver:
    def __init__(self, clock: Clock, fingerprint: str) -> None:
        self.clock = clock
        self._route = ROUTE
        self.composer = ""
        self.commit_calls = 0
        self.evidence = CapturedSendEvidence(
            conversation_id="conversation-1",
            conversation_session_id="session-1",
            identity_digest=hashlib.sha256(b"approved-contact").hexdigest(),
            last_inbound_message_key="inbound-1",
            frame_hash=hashlib.sha256(b"initial-frame").hexdigest(),
            frame_captured_at=clock.now(),
            frame_expires_at=clock.now() + timedelta(seconds=60),
            client_version="4.1.12.55",
            environment_fingerprint=fingerprint,
            process_id=202,
            window_handle=2002,
            dpi_scale=1.25,
            outbound_sequence=0,
        )

    @property
    def route(self) -> SendRoute:
        return self._route

    async def capture(self, target, token):
        token.raise_if_cancelled()
        return self.evidence

    async def select_target(self, target, token):
        token.raise_if_cancelled()
        return target == self.evidence.target

    async def compose_text(self, target, text, token):
        token.raise_if_cancelled()
        self.composer = text
        self.evidence = self.evidence.model_copy(
            update={"frame_hash": hashlib.sha256(text.encode()).hexdigest()}
        )

    async def read_composer_hash(self, target, token):
        token.raise_if_cancelled()
        return text_digest(self.composer)

    async def commit(self, target, token):
        token.raise_if_cancelled()
        self.commit_calls += 1
        return "commit"

    async def read_outbound_since(self, target, after_sequence, token):
        token.raise_if_cancelled()
        return ()


class HubInvalidator:
    """The explicit M7 port adapter, intentionally small enough to audit."""

    def __init__(self, hub: HubService) -> None:
        self.hub = hub
        self.events = []

    def invalidate(self, event) -> None:
        self.events.append(event)
        self.hub.invalidate_for_rule_change(event.new_version)


class RuleVersionAuthority:
    """M5's narrow version port backed by the active M7 RulePack."""

    def __init__(
        self,
        rules: AtomicRulePackStore | None = None,
        *,
        policy_version: str = "policy-v1",
    ) -> None:
        self.rules = rules
        self.policy_version = policy_version

    async def current_versions(
        self, conversation_id: str
    ) -> CurrentPolicyVersions | None:
        if conversation_id != "conversation-1":
            return None
        rule_version = (
            self.rules.resolve("contact-1").rulepack.version
            if self.rules
            else "rules-v1"
        )
        return CurrentPolicyVersions(
            rule_version=rule_version, policy_version=self.policy_version
        )


def approval() -> HumanApproval:
    return HumanApproval(approver_id="owner", reason="reviewed")


def rule_source(name: str, body: str) -> RuleSource:
    return RuleSource(name=name, content=body.encode())


def build_m5(
    clock: Clock,
    *,
    supported: bool,
    versions: RuleVersionAuthority | None = None,
) -> tuple[WechatBackgroundSendAdapter, Driver]:
    environment = WechatEnvironment()
    fingerprint = EnvironmentFingerprint(platform=Platform.WECHAT, **environment.values)
    registry = CapabilityRegistry()
    level = SupportLevel.SUPPORTED if supported else SupportLevel.UNSUPPORTED
    registry.register(
        AdapterCapabilities(
            platform=Platform.WECHAT,
            capability_version="m5-integration-v1",
            client_version=fingerprint.client_version,
            environment_fingerprint=fingerprint.digest,
            observe_background=level,
            resolve_background=level,
            compose_background=level,
            send_background=level,
            verify_background=level,
            confidence=1 if supported else 0,
            fixture_suite_version="integration-v1",
        ),
        fingerprint,
    )
    driver = Driver(clock, fingerprint.digest)
    guard = ExecutionGuard(
        registry=registry,
        fingerprinter=EnvironmentFingerprinter(environment),
        interceptor=ActionInterceptor(),
        contention_monitor=SnapshotContentionMonitor(Desktop()),
    )
    return (
        WechatBackgroundSendAdapter(
            guard=guard,
            driver=driver,
            clock=clock,
            current_policy_versions=versions or RuleVersionAuthority(),
            capability_version="m5-integration-v1",
        ),
        driver,
    )


def m5_request(
    clock: Clock, driver: Driver, *, rule_version: str
) -> AuthorizedWechatSend:
    text = "已收到"
    command = AuthorizedSendCommand(
        draft_id=uuid4(),
        conversation_id=driver.evidence.conversation_id,
        expected_last_message_key=driver.evidence.last_inbound_message_key,
        text_hash=text_digest(text),
        idempotency_key=f"idem-{uuid4()}",
        authorization_type=AuthorizationType.HUMAN,
        authorization_id=uuid4(),
        policy_version="policy-v1",
        expires_at=clock.now() + timedelta(seconds=30),
    )
    return AuthorizedWechatSend(
        command=command,
        body_text=text,
        rule_version=rule_version,
        observation=driver.evidence,
    )


def establish_hub_conversation(hub: HubService, clock: Clock) -> None:
    hub.ingest(
        InboundMessage(
            platform=Platform.WECHAT,
            account_id="wechat-account",
            conversation_id="conversation-1",
            contact_id="contact-1",
            platform_message_key="inbound-1",
            observed_at=clock.now(),
            text="hello",
        )
    )


def test_m7_activation_invalidates_m1_unsent_draft_and_pacing_plan(tmp_path) -> None:
    clock = Clock()
    hub_store = SQLiteHubStore(tmp_path / "hub.sqlite")
    hub = HubService(hub_store, clock=clock)
    try:
        establish_hub_conversation(hub, clock)
        draft = hub.create_draft(
            Draft(
                conversation_id="conversation-1",
                contact_id="contact-1",
                text="draft",
                source_message_keys=("inbound-1",),
                rule_version="old-version",
            )
        )
        plan = hub.schedule_plan(
            PacingPlan(
                conversation_id="conversation-1",
                source_message_keys=("inbound-1",),
                quiet_until=clock.now() + timedelta(seconds=6),
                earliest_send_at=clock.now() + timedelta(seconds=8),
                expires_at=clock.now() + timedelta(minutes=5),
                reading_delay_ms=2000,
                composition_delay_ms=4000,
                inter_message_gap_ms=15000,
                pacing_rule_version="old-version",
            )
        )
        invalidator = HubInvalidator(hub)
        rules = AtomicRulePackStore(invalidation_port=invalidator)
        first = rules.ingest(rule_source("v1.yaml", RULES_V1))
        rules.activate(first.draft_id, approval())
        second = rules.ingest(rule_source("v2.yaml", RULES_V2))
        active = rules.activate(second.draft_id, approval())
        statuses = hub_store.connection.execute(
            "SELECT status FROM drafts WHERE draft_id=?", (str(draft.draft_id),)
        ).fetchone()
        plan_status = hub_store.connection.execute(
            "SELECT status FROM pacing_plans WHERE pacing_plan_id=?",
            (str(plan.pacing_plan_id),),
        ).fetchone()
        assert active.version == second.version
        assert statuses["status"] == "expired"
        assert plan_status["status"] == "cancelled"
        assert [event.new_version for event in invalidator.events] == [
            first.version,
            second.version,
        ]
    finally:
        hub_store.close()


def test_m5_observation_change_stales_old_rule_prepared_send_before_commit(
    tmp_path,
) -> None:
    clock = Clock()
    rules = AtomicRulePackStore()
    first = rules.ingest(rule_source("v1.yaml", RULES_V1))
    active = rules.activate(first.draft_id, approval())
    adapter, driver = build_m5(
        clock, supported=True, versions=RuleVersionAuthority(rules)
    )
    prepared = asyncio.run(
        adapter.prepare_send(m5_request(clock, driver, rule_version=active.version))
    ).prepared
    assert prepared is not None and prepared.rule_version == active.version
    # This is the M5 enforcement boundary available today: changed M4 evidence.
    driver.evidence = driver.evidence.model_copy(
        update={"frame_hash": hashlib.sha256(b"changed-observation").hexdigest()}
    )
    result = asyncio.run(adapter.commit_send(prepared))
    assert result.status is SendResultStatus.REJECTED
    assert result.error_code.value == "STALE_CONTEXT"
    assert driver.commit_calls == 0


def test_m7_rule_activation_stales_prepared_m5_send_via_current_version_port() -> None:
    clock = Clock()
    rules = AtomicRulePackStore()
    first = rules.ingest(rule_source("v1.yaml", RULES_V1))
    pack_v1 = rules.activate(first.draft_id, approval())
    adapter, driver = build_m5(
        clock, supported=True, versions=RuleVersionAuthority(rules)
    )
    prepared = asyncio.run(
        adapter.prepare_send(m5_request(clock, driver, rule_version=pack_v1.version))
    ).prepared
    assert prepared is not None
    second = rules.ingest(rule_source("v2.yaml", RULES_V2))
    rules.activate(second.draft_id, approval())
    result = asyncio.run(adapter.commit_send(prepared))
    assert result.status is SendResultStatus.REJECTED
    assert result.error_code.value == "STALE_CONTEXT"
    assert driver.commit_calls == 0


def test_m5_unsupported_capability_has_no_outbox_or_memory_side_effect(
    tmp_path,
) -> None:
    clock = Clock()
    hub_store = SQLiteHubStore(tmp_path / "hub.sqlite")
    memory_store = SQLiteMemoryStore(tmp_path / "memory.sqlite")
    try:
        adapter, driver = build_m5(clock, supported=False)
        result = asyncio.run(
            adapter.prepare_send(m5_request(clock, driver, rule_version="rules-v1"))
        )
        assert result.status is SendResultStatus.REJECTED
        assert driver.composer == "" and driver.commit_calls == 0
        assert (
            hub_store.connection.execute("SELECT COUNT(1) FROM outbox").fetchone()[0]
            == 0
        )
        assert (
            memory_store.connection.execute(
                "SELECT COUNT(1) FROM memory_messages"
            ).fetchone()[0]
            == 0
        )
    finally:
        hub_store.close()
        memory_store.close()


def test_m7_rollback_resolves_old_version_without_rewriting_history(tmp_path) -> None:
    clock = Clock()
    hub_store = SQLiteHubStore(tmp_path / "hub.sqlite")
    hub = HubService(hub_store, clock=clock)
    try:
        establish_hub_conversation(hub, clock)
        rules = AtomicRulePackStore(invalidation_port=HubInvalidator(hub))
        first = rules.ingest(rule_source("v1.yaml", RULES_V1))
        pack_v1 = rules.activate(first.draft_id, approval())
        historical_draft = hub.create_draft(
            Draft(
                conversation_id="conversation-1",
                contact_id="contact-1",
                text="historical",
                source_message_keys=("inbound-1",),
                rule_version=pack_v1.version,
            )
        )
        adapter, driver = build_m5(
            clock, supported=True, versions=RuleVersionAuthority(rules)
        )
        historical_send = asyncio.run(
            adapter.prepare_send(
                m5_request(clock, driver, rule_version=pack_v1.version)
            )
        ).prepared
        second = rules.ingest(rule_source("v2.yaml", RULES_V2))
        rules.activate(second.draft_id, approval())
        rolled_back = rules.rollback(pack_v1.version, approval())
        assert (
            rules.resolve("contact-1").rulepack.version
            == pack_v1.version
            == rolled_back.version
        )
        saved_draft = hub_store.connection.execute(
            "SELECT rule_version FROM drafts WHERE draft_id=?",
            (str(historical_draft.draft_id),),
        ).fetchone()
        assert saved_draft["rule_version"] == pack_v1.version
        assert (
            historical_send is not None
            and historical_send.rule_version == pack_v1.version
        )
    finally:
        hub_store.close()
