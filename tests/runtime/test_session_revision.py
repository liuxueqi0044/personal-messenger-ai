from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from messenger_ai.adapters.qq.models import QQSessionObservedDirectIdentity
from messenger_ai.runtime.session_revision import (
    SCHEMA, revision_snapshot_path, session_revision_number, validate_session_revision,
)


def _write(path: Path, value: dict) -> bytes:
    payload = json.dumps(value, sort_keys=True, indent=2).encode()
    path.write_bytes(payload)
    return payload


def _base(tmp_path: Path) -> dict:
    generation = str(uuid4())
    data = tmp_path / "recovery-generations" / generation / "qq-default-account"
    data.mkdir(parents=True)
    proof = QQSessionObservedDirectIdentity(
        binding_id="session-contact-1", conversation_type="direct",
        type_evidence_source="operator_observed_direct", client_version="test-qq",
        selector_pack_version="test-pack", group_marker_probe_complete=True,
        group_marker_count=0, process_id=10, window_handle=20,
        process_started_at_100ns=30, vm_environment_fingerprint="a" * 64,
        selected_row_runtime_id_hash="b" * 64, header_digest="c" * 64,
    )
    binding = {
        "binding_id": proof.binding_id, "account_id": "qq-default-account",
        "contact_id": proof.binding_id, "hub_conversation_id": "qq-session-conversation-1",
        "participant_signature": proof.participant_signature,
        "platform_conversation_id": "runtime:" + proof.selected_row_runtime_id_hash,
        "conversation_type": "direct", "authorization_scope": "all_direct_including_temporary",
    }
    manifest = _write(data / "generation-manifest.json", {"test_manifest": "immutable"})
    value = {
        "schema": "pmai-v5-runtime-1", "data_dir": str(data),
        "identity_mode": "session_observed_direct", "start_globally_paused": True,
        "model": "test-model", "content_policy_checks_enabled": False,
        "runtime_generation": {"generation_id": generation, "enforce_global_pause": True,
                               "manifest_sha256": hashlib.sha256(manifest).hexdigest()},
        "session_observed_evidence": [proof.model_dump(mode="json")],
        "bindings": [binding],
        "contacts": [{"contact_id": proof.binding_id, "rulepack_status": "active",
                      "binding": copy.deepcopy(binding)}],
        "selector_pack": {"environment_fingerprint": "a" * 64, "client_version": "test-qq",
                          "fixture_suite_version": "test-pack"},
        "bootstrap_last_inbound_once": [], "bootstrap_last_inbound_provenance": {},
        "capability": {"binding_revision": 1},
    }
    _write(data / "runtime-config.json", value)
    return value


def _next(previous: dict) -> dict:
    value = copy.deepcopy(previous)
    data = Path(value["data_dir"])
    number = session_revision_number(previous) + 1
    value["session_binding"] = {
        "schema": SCHEMA, "revision": number,
        "base_config_sha256": hashlib.sha256((data / "runtime-config.json").read_bytes()).hexdigest(),
        "previous_config_sha256": hashlib.sha256(revision_snapshot_path(data, number - 1).read_bytes()).hexdigest(),
    }
    for proof in value["session_observed_evidence"]:
        proof["process_id"] += 1
        proof["window_handle"] += 1
        proof["process_started_at_100ns"] += 1
        proof["selected_row_runtime_id_hash"] = str(number) * 64
    for binding in [*value["bindings"], *(contact["binding"] for contact in value["contacts"])]:
        binding["platform_conversation_id"] = "runtime:" + str(number) * 64
    return value


def test_consecutive_session_revisions_preserve_root(tmp_path):
    base = _base(tmp_path)
    root = Path(base["data_dir"]) / "runtime-config.json"
    original = root.read_bytes()
    previous = base
    for number in (2, 3):
        value = _next(previous)
        candidate = validate_session_revision(value, trusted_runtime_root=tmp_path, require_snapshot=False)
        assert candidate is not None and candidate.revision == number
        _write(candidate.snapshot_path, value)
        actual = validate_session_revision(value, trusted_runtime_root=tmp_path)
        assert actual is not None and actual.base_config == base
        previous = value
    assert root.read_bytes() == original
    assert session_revision_number(base) == 1
    assert validate_session_revision(base, trusted_runtime_root=tmp_path) is None


@pytest.mark.parametrize("change", [
    "policy", "persona", "adoption", "business_revision", "contact", "identity", "selector", "pause",
])
def test_only_session_locators_are_mutable(tmp_path, change):
    value = _next(_base(tmp_path))
    if change == "policy": value["content_policy_checks_enabled"] = True
    elif change == "persona": value["contacts"][0]["rulepack_status"] = "other"
    elif change == "adoption": value["bootstrap_last_inbound_once"] = ["qq-session-conversation-1"]
    elif change == "business_revision": value["capability"]["binding_revision"] = 2
    elif change == "contact": value["contacts"].clear()
    elif change == "identity": value["session_observed_evidence"][0]["header_digest"] = "f" * 64
    elif change == "selector": value["selector_pack"]["fixture_suite_version"] = "other"
    elif change == "pause": value["start_globally_paused"] = False
    with pytest.raises(ValueError):
        validate_session_revision(value, trusted_runtime_root=tmp_path, require_snapshot=False)


@pytest.mark.parametrize("change", ["base_hash", "previous_hash", "skip", "wrong_file_revision", "alter_intermediate"])
def test_hash_chain_and_order_cannot_be_bypassed(tmp_path, change):
    second = _next(_base(tmp_path))
    second_path = revision_snapshot_path(Path(second["data_dir"]), 2)
    _write(second_path, second)
    third = _next(second)
    if change == "base_hash": third["session_binding"]["base_config_sha256"] = "0" * 64
    elif change == "previous_hash": third["session_binding"]["previous_config_sha256"] = "0" * 64
    elif change == "skip": second_path.unlink()
    elif change == "wrong_file_revision":
        second["session_binding"]["revision"] = 3
        _write(second_path, second)
    elif change == "alter_intermediate": second_path.write_bytes(second_path.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        validate_session_revision(third, trusted_runtime_root=tmp_path, require_snapshot=False)


@pytest.mark.parametrize("bad", [True, False, 0, 1, -1, 2.0, "2", "../../other", 10001])
def test_revision_metadata_is_strict(bad):
    with pytest.raises(ValueError):
        session_revision_number({"session_binding": {
            "schema": SCHEMA, "revision": bad,
            "base_config_sha256": "a" * 64, "previous_config_sha256": "b" * 64,
        }})


def test_candidate_cannot_escape_generation_or_use_hardlink(tmp_path):
    value = _next(_base(tmp_path))
    other = copy.deepcopy(value)
    other["data_dir"] = str(tmp_path / "unrelated")
    with pytest.raises(ValueError, match="data directory"):
        validate_session_revision(other, trusted_runtime_root=tmp_path, require_snapshot=False)
    snapshot = revision_snapshot_path(Path(value["data_dir"]), 2)
    _write(snapshot, value)
    alias = tmp_path / "alias.json"
    alias.hardlink_to(snapshot)
    with pytest.raises(ValueError, match="private regular"):
        validate_session_revision(value, trusted_runtime_root=tmp_path)


def test_current_dict_must_match_immutable_snapshot(tmp_path):
    value = _next(_base(tmp_path))
    _write(revision_snapshot_path(Path(value["data_dir"]), 2), value)
    value["session_observed_evidence"][0]["process_id"] += 1
    with pytest.raises(ValueError, match="does not match immutable"):
        validate_session_revision(value, trusted_runtime_root=tmp_path)
