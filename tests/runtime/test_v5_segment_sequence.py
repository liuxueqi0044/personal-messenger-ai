from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parent))
from test_v5_daemon_acceptance import _build
from messenger_ai.llm import ReplyAction, ReplyPlan, ReplyPlanResult

class ThreeSegmentProvider:
    def __init__(self, action=ReplyAction.DRAFT): self.action=action; self.requests=[]
    async def plan_reply(self, request):
        self.requests.append(request)
        segments=['one','two','three'] if self.action is ReplyAction.DRAFT else []
        return ReplyPlanResult(request_id=request.request_id,rule_version=request.rules.rule_version,context_fingerprint=request.context_fingerprint,plan=ReplyPlan(action=self.action,reply_text=' '.join(segments),reply_segments=segments,confidence=1,selection_reason='provider chose no send'),model='segments',latency_ms=7)

def _advance_due(app,harness):
    row=app.pacing.connection.execute("select earliest_send_at from m10_plans where status in ('waiting','due') order by earliest_send_at limit 1").fetchone()
    if row: harness.clock.set(datetime.fromisoformat(row['earliest_send_at'])+timedelta(seconds=1))

def test_three_segments_verify_in_order_and_bot_echo_keeps_plan(tmp_path):
    app,harness,_=_build(tmp_path,3); provider=ThreeSegmentProvider(); app.planning.planner.provider=provider
    async def run():
        for _ in range(3): await app.tick()
        harness.append_inbound(0,'go',key='go')
        for _ in range(8): await app.tick()
        for text in ['one','two','three']:
            harness.register_send_text(text); _advance_due(app,harness)
            for _ in range(8): await app.tick()
        assert [x.text for x in harness._histories['qq-0'] if x.direction.value=='outbound']==['one','two','three']
    asyncio.run(run())

def test_human_takeover_after_first_segment_cancels_remaining(tmp_path):
    app,harness,_=_build(tmp_path,3); provider=ThreeSegmentProvider(); app.planning.planner.provider=provider
    async def run():
        for _ in range(3): await app.tick()
        harness.append_inbound(0,'go',key='go')
        for _ in range(8): await app.tick()
        harness.register_send_text('one'); _advance_due(app,harness)
        for _ in range(8): await app.tick()
        harness.append_human_outbound(0,'manual',key='manual')
        for _ in range(6): await app.tick()
        for _ in range(10): _advance_due(app,harness); await app.tick()
        assert [x.text for x in harness._histories['qq-0'] if x.direction.value=='outbound']==['one','manual']
    asyncio.run(run())

def test_ignore_creates_no_draft_due_or_commit(tmp_path):
    app,harness,_=_build(tmp_path,3); app.planning.planner.provider=ThreeSegmentProvider(ReplyAction.IGNORE)
    async def run():
        for _ in range(3): await app.tick()
        harness.append_inbound(0,'ignore',key='i')
        for _ in range(10): await app.tick()
        assert app.pacing.connection.execute('select count(*) from m10_plans').fetchone()[0]==0
        assert not [x for x in harness.port.requests if x.kind.value=='commit']
        decision=app.state.connection.execute('select request_id,action,selection_reason,model,latency_ms from runtime_planner_decisions').fetchone()
        assert decision['request_id']
        assert tuple(decision)[1:]==('ignore','provider chose no send','segments',7)
        evaluation=app.state.connection.execute('select action,outcome from runtime_planner_evaluations').fetchone()
        assert tuple(evaluation)==('ignore','ignore')
    asyncio.run(run())
