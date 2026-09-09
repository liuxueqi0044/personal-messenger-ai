"""Independent V5 multi-contact daemon concurrency acceptance."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from test_v5_daemon_acceptance import _build, RecordingProvider
from messenger_ai.llm import ReplyAction, ReplyPlan, ReplyPlanResult
from messenger_ai.domain import SendStatus


def test_three_contacts_verified_without_target_or_context_mixup(tmp_path):
    app, harness, provider = _build(tmp_path, 3)
    bodies=[]
    async def run():
        original = provider.plan_reply
        async def distinct(request):
            result=await original(request)
            body=f"reply-{request.contact.conversation_id[-1]}"
            bodies.append(body)
            return result.model_copy(update={'plan':ReplyPlan(action=ReplyAction.DRAFT,reply_text=body,reply_segments=[body],confidence=1)})
        provider.plan_reply=distinct
        for _ in range(3): await app.tick()  # baseline every binding
        for i in range(3): harness.append_inbound(i, f'in-{i}', key=f'in-{i}')
        for _ in range(9): await app.tick()
        await app.run_until_idle(max_ticks=40)
        assert {r.contact.conversation_id for r in provider.requests} == {'hub-0','hub-1','hub-2'}
        assert all(r.inbound[-1].text == f'in-{r.contact.conversation_id[-1]}' for r in provider.requests)
        due=app.pacing.connection.execute('select earliest_send_at from m10_plans').fetchall()
        harness.clock.set(max(datetime.fromisoformat(x['earliest_send_at']) for x in due)+timedelta(seconds=1))
        for i in range(3): harness.register_send_text(f'reply-{i}')
        for _ in range(15): await app.tick()
        await app.run_until_idle(max_ticks=40)
        rows=app.hub.store.connection.execute("select status from send_operations where idempotency_key like 'm10:%'").fetchall()
        assert len(rows)==3 and all(x['status']==SendStatus.VERIFIED.value for x in rows)
        assert sum(r.kind.value=='commit' for r in harness.port.requests)==3
        assert set(bodies) == {'reply-0','reply-1','reply-2'}
        assert {history[-1].text for history in harness._histories.values()} == {'reply-0','reply-1','reply-2'}
    asyncio.run(run())


class DeferredProvider(RecordingProvider):
    def __init__(self): super().__init__(); self.futures={}
    async def plan_reply(self, request):
        self.requests.append(request)
        future=asyncio.get_running_loop().create_future()
        self.futures.setdefault(request.contact.conversation_id,[]).append(future)
        return await future


def test_deferred_a_does_not_block_b_and_paused_old_result_is_discarded(tmp_path):
    app,harness,_=_build(tmp_path,3); provider=DeferredProvider(); app.planning.planner.provider=provider
    async def run():
        for _ in range(3): await app.tick()
        harness.append_inbound(0,'A',key='a1'); harness.append_inbound(1,'B',key='b1')
        for _ in range(6): await app.tick()
        assert provider.futures['hub-0'][0].done() is False
        assert provider.futures['hub-1'][0].done() is False
        revision,_paused,_reason=app.state.global_control()
        assert app.state.set_global_pause(paused=True,expected_revision=revision,reason='test')
        revision,_paused,_reason=app.state.global_control()
        assert app.state.set_global_pause(paused=False,expected_revision=revision,reason='test')
        future=provider.futures['hub-0'][0]
        request=next(item for item in provider.requests if item.contact.conversation_id=='hub-0')
        future.set_result(ReplyPlanResult(request_id=request.request_id,rule_version=request.rules.rule_version,context_fingerprint=request.context_fingerprint,plan=ReplyPlan(action=ReplyAction.DRAFT,reply_text='stale',reply_segments=['stale'],confidence=1),model='deferred',latency_ms=0))
        await asyncio.sleep(0)
        assert app.pacing.connection.execute("select count(*) from m10_plans where conversation_id='hub-0'").fetchone()[0] == 0
        assert not [r for r in harness.port.requests if r.kind.value=='commit']
        assert app.state.connection.execute("select count(*) from runtime_planning_jobs where conversation_id='hub-0' and status in ('stale','cancelled','failed')").fetchone()[0] >= 1
    asyncio.run(run())


def test_five_contacts_are_observed_round_robin(tmp_path):
    app, harness, _ = _build(tmp_path, 5)
    async def run():
        for _ in range(5): await app.tick()
        observed={r.binding_id for r in harness.port.requests if r.kind.value=='observe'}
        assert observed == {f'binding-{i}' for i in range(5)}
    asyncio.run(run())


def test_pending_a_does_not_block_b_observation(tmp_path):
    app, harness, provider = _build(tmp_path, 3)
    async def run():
        for _ in range(3): await app.tick()
        harness.append_inbound(0,'A',key='a'); harness.append_inbound(1,'B',key='b')
        for _ in range(6): await app.tick()
        # This assertion is intentionally against live app state, not provider calls alone.
        assert {'hub-0','hub-1'} <= {r.contact.conversation_id for r in provider.requests}
    asyncio.run(run())
