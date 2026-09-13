from pathlib import Path
import importlib.util, sqlite3
import pytest
S=Path(__file__).parents[2]/'scripts/deployment/cancel_stale_hub_trigger_guest.py';sp=importlib.util.spec_from_file_location('m',S);m=importlib.util.module_from_spec(sp);assert sp.loader;sp.loader.exec_module(m)
def target(contact=1):
 return dict(conversation_id=f'qq-session-conversation-{contact}',outbox_id=('4b10624a-c907-402f-bb52-122a16bc2b67' if contact==1 else '148c7b0b-7e40-4984-949d-22f93d712c0d'),event_type='conversation.stable_window',status='pending',attempt_count=0,created_at=('2026-09-11T07:13:55.633384+00:00' if contact==1 else '2026-09-12T06:48:47.240458+00:00'),available_at=('2026-09-11T07:14:01.586643+00:00' if contact==1 else '2026-09-12T06:48:52.240458+00:00'),pause_reason=('message_anchor_gap' if contact==1 else 'driver_quarantine:read_only_observe_timeout'),operator_id='codex_operator',reason_code='STALE_HUB_TRIGGER')
def setup(p,t):
 with sqlite3.connect(p/'runtime.sqlite3') as d:d.execute('create table runtime_conversations(conversation_id,paused,pause_reason)');d.execute('create table runtime_planning_jobs(conversation_id,status)');d.execute('insert into runtime_conversations values(?,?,?)',(t.conversation_id,1,t.pause_reason))
 with sqlite3.connect(p/'pacing.sqlite3') as d:d.execute('create table m10_plans(conversation_id,status)')
 with sqlite3.connect(p/'hub.sqlite3') as d:d.execute('create table drafts(draft_id,conversation_id)');d.execute('create table send_operations(operation_id,draft_id,status)');d.execute('create table outbox(outbox_id,status,attempt_count,event_type,aggregate_id,created_at,available_at,version)');d.execute('insert into outbox values(?,?,?,?,?,?,?,?)',(t.outbox_id,t.status,t.attempt_count,t.event_type,t.conversation_id,t.created_at,t.available_at,3))
def ns(**x):return __import__('argparse').Namespace(**x)
@pytest.mark.parametrize('contact',[1,2])
def test_contact_cas_and_idempotence(tmp_path,contact):
 t=ns(**target(contact));setup(tmp_path,t);assert not m.settle(tmp_path,t)['idempotent'];assert m.settle(tmp_path,t)['idempotent']
 with sqlite3.connect(tmp_path/'hub.sqlite3') as d:assert d.execute('select status,version from outbox').fetchone()==('cancelled',4);assert d.execute('select count(*) from stale_hub_trigger_cancel_audit').fetchone()==(1,)
def test_invalid_parameter_rejected(tmp_path):
 t=ns(**target());setup(tmp_path,t);t.event_type='other';
 with pytest.raises(RuntimeError,match='TARGET_INVALID'):m.settle(tmp_path,t)
def test_fixed_field_drift_rejected(tmp_path):
 t=ns(**target(2));setup(tmp_path,t)
 with sqlite3.connect(tmp_path/'hub.sqlite3') as d:d.execute("update outbox set available_at='drift'")
 with pytest.raises(RuntimeError,match='OUTBOX_CAS_DRIFT'):m.settle(tmp_path,t)
