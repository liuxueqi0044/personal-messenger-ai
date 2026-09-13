from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from messenger_ai.adapters.qq.vm_driver.contracts import (
    WorkerKind,
    WorkerResult,
    WorkerStatus,
)

SOURCE = (
    Path(__file__).parents[2]
    / "scripts"
    / "deployment"
    / "qq_visual_selection_acceptance_guest.py"
)
SPEC = importlib.util.spec_from_file_location(
    "qq_visual_selection_acceptance_guest", SOURCE
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


ATTEMPT = "11111111-1111-4111-8111-111111111111"
BINDING = "session-contact-2"


def _result(
    kind: WorkerKind,
    status: WorkerStatus,
    *,
    epoch: int,
    error_code: str | None = None,
    evidence: dict[str, object] | None = None,
) -> WorkerResult:
    return WorkerResult(
        request_id=UUID(int=epoch + 100),
        kind=kind,
        status=status,
        worker_epoch=UUID(int=epoch),
        error_code=error_code,
        evidence=evidence or {},
    )


class Owner:
    def __init__(self) -> None:
        self.acquired = False
        self.closed = False

    def acquire(self) -> None:
        self.acquired = True

    def close(self) -> None:
        self.closed = True


class Worker:
    def __init__(self, responses, pid: int, *, retires: bool = True) -> None:
        self.responses = list(responses)
        self.pid = pid
        self.retires = retires
        self.alive = False
        self.started = False
        self.stopped = False
        self.commands = []

    def start(self) -> None:
        self.started = True
        self.alive = True

    def stop(self) -> None:
        self.stopped = True
        if self.retires:
            self.alive = False

    def status_snapshot(self):
        return {"worker_process_id": self.pid, "worker_alive": self.alive}

    def request(self, command, _timeout):
        self.commands.append(command)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result.model_copy(update={"request_id": command.request_id})


def _health(epoch: int) -> WorkerResult:
    return _result(
        WorkerKind.HEALTH,
        WorkerStatus.OK,
        epoch=epoch,
        evidence={"process_id": 101, "window_handle": 202},
    )


def _action(epoch: int) -> WorkerResult:
    return _result(
        WorkerKind.SELECT_ONLY,
        WorkerStatus.FAILED_SAFE,
        epoch=epoch,
        error_code="selection_process_refresh_required",
        evidence={
            "frame_sha256": "a" * 64,
            "model": "deepseek-v4-flash-vision-exp",
            "latency_ms": 37,
            "visual_decision": "match",
            "visual_reason": "exact_label",
            "visual_confidence": 0.99,
            "normalized_label_match": True,
            "png_bytes": "must-not-leak",
            "target_label": "联系人乙",
        },
    )


def _verified(epoch: int) -> WorkerResult:
    return _result(
        WorkerKind.VERIFY_SELECTION_ONLY,
        WorkerStatus.OK,
        epoch=epoch,
        evidence={
            "selection_confirmed": True,
            "target_identity": {
                "binding_id": BINDING,
                "conversation_type": "direct",
                "process_id": 101,
                "window_handle": 202,
                "selected_row_runtime_id_hash": "b" * 64,
                "header_digest": "c" * 64,
                "group_marker_count": 0,
                "message_text": "must-not-leak",
            },
        },
    )


def _arrange(monkeypatch, tmp_path: Path, workers: list[Worker]):
    owner = Owner()
    config = {
        "schema": "pmai-v5-runtime-1",
        "data_dir": str(tmp_path / "data"),
        "secret_vault": str(tmp_path / "vault"),
        "worker_timeout_seconds": 45,
        "visual_selection": {"labels": {BINDING: "联系人乙"}},
    }
    binding = SimpleNamespace(binding_id=BINDING, conversation_type="direct")
    evidence = (SimpleNamespace(binding_id=BINDING),)
    visual = SimpleNamespace(model="deepseek-v4-flash-vision-exp")
    calls = []

    monkeypatch.setattr(MODULE, "QQRuntimeInstanceOwner", lambda: owner)
    monkeypatch.setattr(MODULE, "_require_guest", lambda: None)
    monkeypatch.setattr(MODULE, "load_config", lambda _path: config)
    monkeypatch.setattr(
        MODULE,
        "validate_config",
        lambda _config, api_key: ("pack", (binding,), evidence),
    )
    monkeypatch.setattr(MODULE, "_capability", lambda *_args: None)
    monkeypatch.setattr(
        MODULE, "_validated_visual_selection", lambda *_args: visual
    )

    class Secrets:
        def __init__(self, _path):
            pass

        def get_secret(self, name):
            assert name == "deepseek.api_key"
            return b"secret-api-key"

    monkeypatch.setattr(MODULE, "WindowsDPAPISecretStore", Secrets)

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return workers[len(calls) - 1]

    monkeypatch.setattr(MODULE, "QQVMWorkerProcess", factory)
    return owner, config, calls


def _execute(tmp_path: Path):
    return MODULE.execute(
        config_path=tmp_path / "runtime.json",
        binding_id=BINDING,
        attempt_id=ATTEMPT,
        report_path=tmp_path / "report.json",
    )


def test_success_uses_one_action_worker_then_a_read_only_fresh_worker(
    monkeypatch, tmp_path: Path
) -> None:
    first = Worker([_health(1), _action(1)], 301)
    second = Worker([_health(2), _verified(2)], 302)
    owner, _config, calls = _arrange(monkeypatch, tmp_path, [first, second])

    report = _execute(tmp_path)

    assert report["succeeded"] is True
    assert report["action_attempted"] is True
    assert report["fresh_process_verified"] is True
    assert report["first_worker_retired"] is True
    assert report["second_worker_retired"] is True
    assert report["stages"][1]["visual_decision"] == "match"
    assert report["stages"][1]["visual_reason"] == "exact_label"
    assert report["stages"][1]["visual_confidence"] == 0.99
    assert report["stages"][1]["normalized_label_match"] is True
    assert [item.kind for item in first.commands] == [
        WorkerKind.HEALTH,
        WorkerKind.SELECT_ONLY,
    ]
    assert [item.kind for item in second.commands] == [
        WorkerKind.HEALTH,
        WorkerKind.VERIFY_SELECTION_ONLY,
    ]
    assert calls[0][1]["visual_selection"].model.endswith("vision-exp")
    assert calls[0][1]["visual_api_key"] == "secret-api-key"
    assert "visual_selection" not in calls[1][1]
    assert "visual_api_key" not in calls[1][1]
    assert first.stopped and second.stopped and owner.closed
    serialized = json.dumps(report, ensure_ascii=False)
    assert "secret-api-key" not in serialized
    assert "联系人乙" not in serialized
    assert "must-not-leak" not in serialized


def test_first_generation_rejection_never_starts_verifier(
    monkeypatch, tmp_path: Path
) -> None:
    rejected = _result(
        WorkerKind.SELECT_ONLY,
        WorkerStatus.FAILED_SAFE,
        epoch=1,
        error_code="visual_target_not_certified",
        evidence={
            "frame_sha256": "d" * 64,
            "visual_decision": "not_match",
            "visual_reason": "different_label",
            "visual_confidence": 0.96,
            "normalized_label_match": False,
            "target_label": "联系人乙",
            "observed_label": "联系人丙",
        },
    )
    first = Worker([_health(1), rejected], 301)
    owner, _config, calls = _arrange(monkeypatch, tmp_path, [first])

    report = _execute(tmp_path)

    assert report["status"] == "rejected"
    assert report["action_attempted"] is False
    assert report["stages"][1]["visual_decision"] == "not_match"
    assert report["stages"][1]["visual_reason"] == "different_label"
    assert report["stages"][1]["visual_confidence"] == 0.96
    assert report["stages"][1]["normalized_label_match"] is False
    serialized = json.dumps(report, ensure_ascii=False)
    assert "联系人乙" not in serialized
    assert "联系人丙" not in serialized
    assert "observed_label" not in serialized
    assert len(calls) == 1
    assert first.stopped and owner.closed


def test_first_generation_uncertain_is_not_retried(
    monkeypatch, tmp_path: Path
) -> None:
    uncertain = _result(
        WorkerKind.SELECT_ONLY,
        WorkerStatus.UNCERTAIN,
        epoch=1,
        error_code="worker_timeout_isolated",
    )
    first = Worker([_health(1), uncertain], 301)
    owner, _config, calls = _arrange(monkeypatch, tmp_path, [first])

    report = _execute(tmp_path)

    assert report["status"] == "uncertain"
    assert report["action_attempted"] is None
    assert len(calls) == 1
    assert first.stopped and owner.closed


def test_attempt_id_is_persisted_before_worker_and_cannot_replay(
    monkeypatch, tmp_path: Path
) -> None:
    first = Worker([_health(1), _action(1)], 301)
    second = Worker([_health(2), _verified(2)], 302)
    owner, _config, _calls = _arrange(monkeypatch, tmp_path, [first, second])
    assert _execute(tmp_path)["succeeded"] is True
    assert owner.closed

    next_owner = Owner()
    monkeypatch.setattr(MODULE, "QQRuntimeInstanceOwner", lambda: next_owner)
    with pytest.raises(RuntimeError, match="ATTEMPT_REPLAY"):
        _execute(tmp_path)
    assert next_owner.closed


def test_fresh_process_identity_failure_is_rejected_without_another_action(
    monkeypatch, tmp_path: Path
) -> None:
    failed = _result(
        WorkerKind.VERIFY_SELECTION_ONLY,
        WorkerStatus.FAILED_SAFE,
        epoch=2,
        error_code="session_identity_header_drift",
    )
    first = Worker([_health(1), _action(1)], 301)
    second = Worker([_health(2), failed], 302)
    owner, _config, calls = _arrange(monkeypatch, tmp_path, [first, second])

    report = _execute(tmp_path)

    assert report["status"] == "rejected"
    assert report["action_attempted"] is True
    assert report["fresh_process_verified"] is False
    assert len(calls) == 2
    assert [item.kind for item in second.commands] == [
        WorkerKind.HEALTH,
        WorkerKind.VERIFY_SELECTION_ONLY,
    ]
    assert owner.closed


def test_first_worker_must_be_confirmed_retired_before_verifier_starts(
    monkeypatch, tmp_path: Path
) -> None:
    first = Worker([_health(1), _action(1)], 301, retires=False)
    owner, _config, calls = _arrange(monkeypatch, tmp_path, [first])

    report = _execute(tmp_path)

    assert report["status"] == "uncertain"
    assert report["action_attempted"] is True
    assert report["first_worker_retired"] is False
    assert report["fresh_process_verified"] is False
    assert report["error_code"] == "VISUAL_SELECTION_FIRST_WORKER_NOT_RETIRED"
    assert len(calls) == 1
    assert owner.closed


def test_uncertain_fresh_verification_preserves_action_uncertainty(
    monkeypatch, tmp_path: Path
) -> None:
    uncertain = _result(
        WorkerKind.VERIFY_SELECTION_ONLY,
        WorkerStatus.UNCERTAIN,
        epoch=2,
        error_code="worker_timeout_isolated",
    )
    first = Worker([_health(1), _action(1)], 301)
    second = Worker([_health(2), uncertain], 302)
    owner, _config, calls = _arrange(monkeypatch, tmp_path, [first, second])

    report = _execute(tmp_path)

    assert report["status"] == "uncertain"
    assert report["action_attempted"] is True
    assert report["fresh_process_verified"] is False
    assert report["error_code"] == "worker_timeout_isolated"
    assert len(calls) == 2
    assert first.stopped and second.stopped and owner.closed


def test_fresh_verification_exception_preserves_action_uncertainty(
    monkeypatch, tmp_path: Path
) -> None:
    first = Worker([_health(1), _action(1)], 301)
    second = Worker([_health(2), TimeoutError("sensitive timeout")], 302)
    owner, _config, calls = _arrange(monkeypatch, tmp_path, [first, second])

    report = _execute(tmp_path)

    assert report["status"] == "uncertain"
    assert report["action_attempted"] is True
    assert report["fresh_process_verified"] is False
    assert report["error_code"] == "VISUAL_SELECTION_FRESH_VERIFY_UNCERTAIN"
    assert "sensitive timeout" not in json.dumps(report)
    assert len(calls) == 2
    assert first.stopped and second.stopped and owner.closed
