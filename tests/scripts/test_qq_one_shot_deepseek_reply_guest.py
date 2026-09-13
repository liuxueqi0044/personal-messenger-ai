from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from messenger_ai.adapters.qq.models import QQIdentityBinding
from messenger_ai.runtime.one_shot import OneShotReplyResult

SOURCE = (
    Path(__file__).parents[2]
    / "scripts"
    / "deployment"
    / "qq_one_shot_deepseek_reply_guest.py"
)
SPEC = importlib.util.spec_from_file_location("qq_one_shot_reply_guest", SOURCE)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ATTEMPT = "11111111-1111-4111-8111-111111111111"
BINDING = "session-contact-2"
OPERATION = UUID("22222222-2222-4222-8222-222222222222")
PLAN = UUID("33333333-3333-4333-8333-333333333333")


def _binding() -> QQIdentityBinding:
    return QQIdentityBinding(
        hub_conversation_id="conversation",
        contact_id="contact",
        account_id="account",
        platform_conversation_id="qq-conversation",
        participant_signature="signature",
        binding_id=BINDING,
        conversation_type="direct",
        authorization_scope="all_direct_including_temporary",
    )


def _handoff() -> dict[str, object]:
    return {
        "operation_id": str(OPERATION),
        "commit_worker_process_id": 101,
        "verify_worker_process_id": 202,
        "commit_worker_epoch": "44444444-4444-4444-8444-444444444444",
        "verify_health_worker_epoch": "55555555-5555-4555-8555-555555555555",
        "verify_worker_epoch": "55555555-5555-4555-8555-555555555555",
        "first_worker_retired": True,
        "verify_status": "ok",
        "message_text": "must-not-leak",
    }


class Owner:
    def __init__(self) -> None:
        self.closed = False

    def acquire(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class Worker:
    def __init__(self, *, retires: bool = True) -> None:
        self.retires = retires

    def status_snapshot(self):
        return {
            "worker_alive": not self.retires,
            "worker_exit_code": 0 if self.retires else None,
        }


class State:
    def __init__(self) -> None:
        self.one_shot_scope_entered = False
        self.one_shot_scope_args = None

    @contextmanager
    def one_shot_stopped_runtime_scope(self, *, attempt_id, conversation_id):
        self.one_shot_scope_entered = True
        self.one_shot_scope_args = (attempt_id, conversation_id)
        yield


def _arrange(monkeypatch, tmp_path: Path, *, retires: bool = True):
    owner = Owner()
    worker = Worker(retires=retires)
    app = SimpleNamespace(driver=SimpleNamespace(_worker=worker), state=State())
    raw_binding = _binding().model_dump(mode="json")
    config = {
        "data_dir": str(tmp_path / "data"),
        "secret_vault": str(tmp_path / "vault"),
        "contacts": [
            {
                "contact_id": "contact",
                "rulepack_status": "active",
                "binding": raw_binding,
            }
        ],
        "bindings": [raw_binding],
    }
    monkeypatch.setattr(MODULE, "QQRuntimeInstanceOwner", lambda: owner)
    monkeypatch.setattr(MODULE, "_require_guest", lambda: None)
    monkeypatch.setattr(MODULE, "load_config", lambda _path: config)
    monkeypatch.setattr(
        MODULE,
        "validate_config",
        lambda _config, api_key: ("pack", (_binding(),), ("evidence",)),
    )

    class Secrets:
        def __init__(self, _path):
            pass

        def get_secret(self, name):
            assert name == "deepseek.api_key"
            return b"secret-key"

        def get_or_create_hmac_key(self, name):
            assert name == "runtime.authorization.signing"
            return b"signing-key"

    monkeypatch.setattr(MODULE, "WindowsDPAPISecretStore", Secrets)
    build_calls = []

    def build_runtime(config, **kwargs):
        build_calls.append((config, kwargs))
        return app

    monkeypatch.setattr(MODULE, "build_runtime", build_runtime)
    monkeypatch.setattr(MODULE, "_shutdown", lambda _app: None)
    return owner, app, build_calls


def _execute(tmp_path: Path):
    return MODULE.execute(
        config_path=tmp_path / "runtime.json",
        binding_id=BINDING,
        attempt_id=ATTEMPT,
        report_path=tmp_path / "report.json",
        max_wait_seconds=10,
    )


def test_execution_config_removes_every_other_binding_projection() -> None:
    first = _binding().model_dump(mode="json")
    second = {
        **first,
        "binding_id": "session-contact-3",
        "contact_id": "contact-3",
        "hub_conversation_id": "conversation-3",
    }
    config = {
        "contacts": [
            {"contact_id": "contact", "binding": first},
            {"contact_id": "contact-3", "binding": second},
        ],
        "bindings": [first, second],
        "identity_mode": "session_observed_direct",
        "session_observed_evidence": [
            {"binding_id": BINDING},
            {"binding_id": "session-contact-3"},
        ],
        "visual_selection": {
            "labels": {BINDING: "private-one", "session-contact-3": "private-two"}
        },
        "session_identity_migrations": [
            {"binding_id": BINDING},
            {"binding_id": "session-contact-3"},
        ],
        "bootstrap_last_inbound_once": ["conversation", "conversation-3"],
        "bootstrap_last_inbound_provenance": {
            "conversation": {"binding_id": BINDING},
            "conversation-3": {"binding_id": "session-contact-3"},
        },
    }

    scoped = MODULE._single_binding_config(config, binding_id=BINDING)

    assert [item["binding_id"] for item in scoped["bindings"]] == [BINDING]
    assert [item["contact_id"] for item in scoped["contacts"]] == ["contact"]
    assert [
        item["binding_id"] for item in scoped["session_observed_evidence"]
    ] == [BINDING]
    assert set(scoped["visual_selection"]["labels"]) == {BINDING}
    assert [
        item["binding_id"] for item in scoped["session_identity_migrations"]
    ] == [BINDING]
    assert scoped["bootstrap_last_inbound_once"] == ["conversation"]
    assert set(scoped["bootstrap_last_inbound_provenance"]) == {"conversation"}
    assert "private-two" not in json.dumps(scoped)


def test_verified_result_requires_shutdown_witness_and_projects_no_content(
    monkeypatch, tmp_path: Path
) -> None:
    owner, _app, build_calls = _arrange(monkeypatch, tmp_path)

    async def run(**_kwargs):
        return OneShotReplyResult(
            attempt_id=UUID(ATTEMPT),
            binding_id=BINDING,
            state="verified",
            provider_called=True,
            action_attempted=True,
            source_key_hashes=("a" * 64,),
            pacing_plan_id=PLAN,
            operation_id=OPERATION,
            send_status="verified",
            handoff=_handoff(),
        )

    monkeypatch.setattr(MODULE, "run_one_shot_reply", run)
    report = _execute(tmp_path)

    assert report["succeeded"] is True
    assert report["active_worker_retired"] is True
    assert report["active_worker_exit_code"] == 0
    assert report["handoff"]["verify_worker_process_id"] == 202
    assert "must-not-leak" not in json.dumps(report)
    assert "secret-key" not in json.dumps(report)
    assert len(build_calls[0][0]["bindings"]) == 1
    assert build_calls[0][1]["selection_refresh_retry_enabled"] is False
    assert build_calls[0][1]["recover_persistent_state"] is False
    assert _app.state.one_shot_scope_entered is True
    assert _app.state.one_shot_scope_args == (
        UUID(ATTEMPT),
        "conversation",
    )
    assert report["send_action_attempted"] is True
    assert report["selection_action_attempted"] is None
    assert owner.closed


def test_verified_result_becomes_uncertain_when_verify_worker_does_not_retire(
    monkeypatch, tmp_path: Path
) -> None:
    _arrange(monkeypatch, tmp_path, retires=False)

    async def run(**_kwargs):
        return OneShotReplyResult(
            attempt_id=UUID(ATTEMPT),
            binding_id=BINDING,
            state="verified",
            provider_called=True,
            action_attempted=True,
            source_key_hashes=("a" * 64,),
            pacing_plan_id=PLAN,
            operation_id=OPERATION,
            send_status="verified",
            handoff=_handoff(),
        )

    monkeypatch.setattr(MODULE, "run_one_shot_reply", run)
    report = _execute(tmp_path)

    assert report["succeeded"] is False
    assert report["status"] == "uncertain"
    assert report["error_code"] == "ONE_SHOT_VERIFY_WORKER_NOT_RETIRED"


def test_attempt_replay_is_blocked_before_runtime_build(monkeypatch, tmp_path: Path) -> None:
    _arrange(monkeypatch, tmp_path)

    async def run(**_kwargs):
        return OneShotReplyResult(
            attempt_id=UUID(ATTEMPT),
            binding_id=BINDING,
            state="no_new_inbound",
            provider_called=False,
            action_attempted=False,
        )

    monkeypatch.setattr(MODULE, "run_one_shot_reply", run)
    assert _execute(tmp_path)["status"] == "no_new_inbound"
    second = _execute(tmp_path)
    assert second["status"] == "failed"
    assert second["error_code"] == "ONE_SHOT_ATTEMPT_REPLAY"
