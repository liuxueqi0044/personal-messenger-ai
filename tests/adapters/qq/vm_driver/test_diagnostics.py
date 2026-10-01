import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.vm_driver import worker as worker_module
from messenger_ai.adapters.qq.vm_driver.contracts import WorkerCommand, WorkerKind, WorkerResult, WorkerStatus
from messenger_ai.adapters.qq.vm_driver.diagnostics import WorkerDiagnosticSink, emit_console
from test_worker import _process_facade, _worker


def test_first_failure_survives_later_request_success_and_sink_recreation(tmp_path):
    class Process:
        pid = 93
        exitcode = None
        def is_alive(self): return True
    class Pipe:
        failed = True
        def send(self, value): self.command = WorkerCommand.model_validate(value)
        def poll(self, _): return True
        def recv(self):
            command = self.command
            return WorkerResult(
                request_id=command.request_id, kind=command.kind, binding_id=command.binding_id,
                status=WorkerStatus.FAILED_SAFE if self.failed else WorkerStatus.OK,
                worker_epoch=uuid4(), error_code="worker_action_failed" if self.failed else None,
                evidence={"text": "private-content", "api_key": "secret-key"},
            ).model_dump(mode="json")
    pipe = Pipe()
    process = _process_facade(Process(), pipe)
    process._ensure_status_state()
    process._run_id = "run-test"
    process._diagnostics = WorkerDiagnosticSink(tmp_path, "parent")
    failed = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id="binding")
    process.request(failed, 1)
    first = (tmp_path / "qq-worker-first-failure.json").read_bytes()
    pipe.failed = False
    process.request(WorkerCommand(kind=WorkerKind.OBSERVE, binding_id="binding"), 1)
    status = process.status_snapshot()
    assert status["last_request"]["status"] == "ok"
    assert status["first_failure"]["request_id"] == str(failed.request_id)
    assert status["last_successful_request"]["status"] == "ok"
    successor_sink = WorkerDiagnosticSink(tmp_path, "parent")
    successor_sink.emit({"run_id": "run-test", "error_code": "next_failure"}, freeze_failure=True)
    assert (tmp_path / "qq-worker-first-failure.json").read_bytes() == first
    logs = "".join(path.read_text() for path in tmp_path.iterdir())
    assert "private-content" not in logs and "secret-key" not in logs
    events = [json.loads(line) for line in (tmp_path / "qq-worker-parent.jsonl").read_text().splitlines()]
    assert [event.get("event") for event in events[:4]] == ["start", "end", "start", "end"]


def test_child_stages_persist_without_stdout(tmp_path, monkeypatch):
    worker, _, binding_id = _worker()
    worker._run_id = "run-stage"
    worker._diagnostics = WorkerDiagnosticSink(tmp_path, "child")
    monkeypatch.setattr(worker_module.sys, "stdout", None)
    command = WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id)
    result = worker.execute(command)
    assert result.status is WorkerStatus.OK
    events = [json.loads(line) for line in (tmp_path / "qq-worker-child.jsonl").read_text().splitlines()]
    assert events[0]["event"] == "start"
    assert events[-1]["event"] == "end"
    assert all(event["request_id"] == str(command.request_id) for event in events)


def test_diagnostic_retention_is_bounded_and_failures_are_observable(tmp_path):
    sink = WorkerDiagnosticSink(tmp_path, "child", max_bytes=180, backups=2)
    for _ in range(30):
        sink.emit({"schema": "pmai-qq-worker-stage-event-v1", "event": "start", "kind": "observe"})
    assert len(list(tmp_path.glob("qq-worker-child.jsonl*"))) == 3
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    failed = WorkerDiagnosticSink(blocked, "parent")
    failed.emit({"event": "start"})
    assert failed.errors == 1


def test_console_failure_cannot_change_post_click_outcome(monkeypatch):
    class Broken:
        def write(self, _): raise OSError("closed")
    monkeypatch.setattr(worker_module.sys, "stdout", Broken())
    emit_console({"event": "end"})


def test_windows_spawn_restores_executable_even_when_start_fails(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_bytes(b"fixture")
    monkeypatch.setattr(worker_module.sys, "platform", "win32")
    monkeypatch.setattr(worker_module.sys, "executable", str(python))
    previous = worker_module.mp_spawn.get_executable()
    class Process:
        def start(self):
            assert Path(os.fsdecode(worker_module.mp_spawn.get_executable())) == pythonw
            raise RuntimeError("spawn failed")
    with pytest.raises(RuntimeError, match="spawn failed"):
        worker_module._start_without_console(Process())
    assert worker_module.mp_spawn.get_executable() == previous
