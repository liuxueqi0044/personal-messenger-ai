import importlib.util
import json
from pathlib import Path

import pytest


PATH = Path(__file__).parents[2] / "scripts" / "deployment" / "qq_supervised_single_send_guest.py"
SPEC = importlib.util.spec_from_file_location("qq_supervised_single_send_guest", PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_fixed_message_is_plain_greeting() -> None:
    assert module.TEXT == "你好呀"


def test_send_intent_can_only_be_recorded_once(tmp_path, monkeypatch) -> None:
    intent = tmp_path / "intent.json"
    monkeypatch.setattr(module, "INTENT", intent)
    module._record_once_intent("run-one")
    value = json.loads(intent.read_text(encoding="utf-8"))
    assert value["run_id"] == "run-one"
    with pytest.raises(FileExistsError):
        module._record_once_intent("run-two")


def test_evidence_requires_session_digests(tmp_path) -> None:
    path = tmp_path / "evidence.json"
    evidence = {"schema": "pmai-qq-supervised-send-evidence-v1",
        "process_id": 7, "window_handle": 9,
        "expected_header_digest": "a" * 64,
        "expected_composer_runtime_id_hash": "b" * 64}
    path.write_text(json.dumps(evidence), encoding="utf-8")
    assert module._evidence(path)["process_id"] == 7
