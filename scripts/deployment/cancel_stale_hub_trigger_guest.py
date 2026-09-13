"""CAS-cancel one explicitly whitelisted stale hub trigger without payload access."""
from __future__ import annotations
import argparse, ctypes, getpass, hashlib, json, os, re, sqlite3
from datetime import UTC, datetime
from pathlib import Path

REPORT_SCHEMA="pmai-stale-hub-trigger-cancel-v2"
_EVENTS={"conversation.stable_window"}; _STATUS={"pending"}; _PAUSES={"message_anchor_gap","driver_quarantine:read_only_observe_timeout"}
_ID=re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"); _TIME=re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.+-]+$")
_OPERATOR=re.compile(r"[a-z][a-z0-9_-]{2,63}"); _REASON=re.compile(r"[A-Z][A-Z0-9_]{2,63}")

def _identity(t: argparse.Namespace)->str:
 return hashlib.sha256("|".join((t.outbox_id,t.conversation_id,t.event_type,t.status,str(t.attempt_count),t.created_at,t.available_at)).encode()).hexdigest()
def _valid(t: argparse.Namespace)->None:
 if any(_ID.fullmatch(x) is None for x in (t.outbox_id,t.conversation_id)) or t.event_type not in _EVENTS or t.status not in _STATUS or t.attempt_count!=0 or not _TIME.fullmatch(t.created_at) or not _TIME.fullmatch(t.available_at): raise RuntimeError("TARGET_INVALID")
 if t.pause_reason not in _PAUSES: raise RuntimeError("PAUSE_INVALID")
 if _OPERATOR.fullmatch(t.operator_id) is None: raise RuntimeError("OPERATOR_INVALID")
 if _REASON.fullmatch(t.reason_code) is None: raise RuntimeError("REASON_INVALID")
class _Mutex:
 def __init__(self):
  ident="\\".join(x for x in (os.environ.get("USERDOMAIN"),getpass.getuser()) if x); self.k=ctypes.WinDLL("kernel32",use_last_error=True);self.h=None;self.name="Local\\PersonalMessengerAI.QQRuntime."+hashlib.sha256(ident.casefold().encode()).hexdigest()[:24]
 def acquire(self):
  ctypes.set_last_error(0);h=self.k.CreateMutexW(None,True,self.name)
  if not h: raise RuntimeError("RUNTIME_MUTEX_UNAVAILABLE")
  if ctypes.get_last_error()==183: self.k.CloseHandle(h);raise RuntimeError("RUNTIME_MUTEX_HELD")
  self.h=h
 def close(self):
  if self.h:self.k.ReleaseMutex(self.h);self.k.CloseHandle(self.h);self.h=None
def _stopped(data:Path)->None:
 try:s=json.loads((data.parents[1]/"qq-session-runtime-status.json").read_text(encoding="utf-8"))
 except Exception as e:raise RuntimeError("RUNTIME_STATUS_UNAVAILABLE") from e
 if s.get("schema")!="pmai-qq-session-runtime-status-v2" or s.get("runtime_process_alive") is not False:raise RuntimeError("RUNTIME_NOT_STOPPED")
 pid=s.get("runtime_process_id")
 if not isinstance(pid,int) or isinstance(pid,bool) or pid<=0:raise RuntimeError("RUNTIME_PID_UNAVAILABLE")
 h=ctypes.WinDLL("kernel32",use_last_error=True).OpenProcess(0x1000,False,pid)
 if h:ctypes.WinDLL("kernel32").CloseHandle(h);raise RuntimeError("RUNTIME_PID_STILL_ALIVE")
 if ctypes.get_last_error()!=87:raise RuntimeError("RUNTIME_PID_PROBE_INDETERMINATE")
def _one(d,sql,args):return d.execute(sql,args).fetchone()
def _lanes(data:Path,t:argparse.Namespace)->None:
 with sqlite3.connect(data/'runtime.sqlite3') as d:
  if _one(d,"SELECT paused,pause_reason FROM runtime_conversations WHERE conversation_id=?",(t.conversation_id,))!=(1,t.pause_reason):raise RuntimeError("RUNTIME_PAUSE_DRIFT")
  if _one(d,"SELECT 1 FROM runtime_planning_jobs WHERE conversation_id=? AND status IN ('pending','running') LIMIT 1",(t.conversation_id,)):raise RuntimeError("RUNTIME_PLAN_NOT_SETTLED")
 with sqlite3.connect(data/'pacing.sqlite3') as d:
  if _one(d,"SELECT 1 FROM m10_plans WHERE conversation_id=? AND status IN ('waiting','due_for_revalidation') LIMIT 1",(t.conversation_id,)):raise RuntimeError("PACING_NOT_SETTLED")
 with sqlite3.connect(data/'hub.sqlite3') as d:
  if _one(d,"SELECT 1 FROM send_operations s JOIN drafts d ON d.draft_id=s.draft_id WHERE d.conversation_id=? AND s.status NOT IN ('verified','failed','uncertain','cancelled') LIMIT 1",(t.conversation_id,)):raise RuntimeError("SEND_NOT_SETTLED")
  if _one(d,"SELECT 1 FROM outbox o LEFT JOIN send_operations s ON s.operation_id=o.aggregate_id LEFT JOIN drafts d ON d.draft_id=s.draft_id WHERE o.outbox_id<>? AND o.status IN ('pending','dispatching') AND (o.aggregate_id=? OR d.conversation_id=?) LIMIT 1",(t.outbox_id,t.conversation_id,t.conversation_id)):raise RuntimeError("RELATED_OUTBOX_NOT_SETTLED")
def settle(data:Path,t:argparse.Namespace)->dict:
 _valid(t);_lanes(data,t);identity=_identity(t)
 with sqlite3.connect(data/'hub.sqlite3',isolation_level=None) as d:
  d.execute('BEGIN IMMEDIATE')
  try:
   d.execute("CREATE TABLE IF NOT EXISTS stale_hub_trigger_cancel_audit(audit_id INTEGER PRIMARY KEY AUTOINCREMENT,outbox_id TEXT NOT NULL,conversation_id TEXT NOT NULL,event_type TEXT NOT NULL,original_status TEXT NOT NULL,original_attempt_count INTEGER NOT NULL,original_version INTEGER NOT NULL,created_at_source TEXT NOT NULL,available_at_source TEXT NOT NULL,operator_id TEXT NOT NULL,reason_code TEXT NOT NULL,created_at TEXT NOT NULL,identity_sha256 TEXT NOT NULL,UNIQUE(outbox_id,operator_id,reason_code,identity_sha256))")
   row=_one(d,"SELECT outbox_id,status,attempt_count,event_type,aggregate_id,created_at,available_at,version FROM outbox WHERE outbox_id=?",(t.outbox_id,))
   if row is None:raise RuntimeError("OUTBOX_MISSING")
   audit=_one(d,"SELECT original_version FROM stale_hub_trigger_cancel_audit WHERE outbox_id=? AND operator_id=? AND reason_code=? AND identity_sha256=?",(t.outbox_id,t.operator_id,t.reason_code,identity))
   expected=(t.outbox_id,t.status,t.attempt_count,t.event_type,t.conversation_id,t.created_at,t.available_at)
   if tuple(row[:7])!=expected:
    if audit and tuple(row[:7])==(t.outbox_id,'cancelled',t.attempt_count,t.event_type,t.conversation_id,t.created_at,t.available_at) and int(row[7])==int(audit[0])+1:
     d.execute('COMMIT');return {'status':'succeeded','idempotent':True,'outbox_id':t.outbox_id}
    raise RuntimeError("OUTBOX_CAS_DRIFT")
   if audit:raise RuntimeError("AUDIT_STATE_DRIFT")
   n=d.execute("UPDATE outbox SET status='cancelled',version=version+1 WHERE outbox_id=? AND status=? AND attempt_count=? AND event_type=? AND aggregate_id=? AND created_at=? AND available_at=?",expected).rowcount
   if n!=1:raise RuntimeError("OUTBOX_CAS_DRIFT")
   d.execute("INSERT INTO stale_hub_trigger_cancel_audit(outbox_id,conversation_id,event_type,original_status,original_attempt_count,original_version,created_at_source,available_at_source,operator_id,reason_code,created_at,identity_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(t.outbox_id,t.conversation_id,t.event_type,t.status,t.attempt_count,int(row[7]),t.created_at,t.available_at,t.operator_id,t.reason_code,datetime.now(UTC).isoformat(),identity))
   d.execute('COMMIT');return {'status':'succeeded','idempotent':False,'outbox_id':t.outbox_id}
  except BaseException:
   if d.in_transaction:d.execute('ROLLBACK')
   raise
def main()->int:
 p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--conversation-id',required=True);p.add_argument('--outbox-id',required=True);p.add_argument('--event-type',required=True);p.add_argument('--status',required=True);p.add_argument('--attempt-count',type=int,required=True);p.add_argument('--created-at',required=True);p.add_argument('--available-at',required=True);p.add_argument('--pause-reason',required=True);p.add_argument('--operator-id',required=True);p.add_argument('--reason-code',required=True);p.add_argument('--report',type=Path,required=True);t=p.parse_args();r={'schema':REPORT_SCHEMA,'succeeded':False};m=_Mutex()
 try:m.acquire();_stopped(t.data_dir);r.update(settle(t.data_dir,t));r['succeeded']=True;code=0
 except Exception as e:r['error_code']=str(e) if re.fullmatch(r'[A-Z][A-Z0-9_]{2,127}',str(e)) else type(e).__name__;code=2
 finally:m.close();tmp=t.report.with_suffix(t.report.suffix+'.tmp');tmp.write_text(json.dumps(r,sort_keys=True,separators=(',',':')),encoding='utf-8');os.replace(tmp,t.report)
 return code
if __name__=='__main__':raise SystemExit(main())
