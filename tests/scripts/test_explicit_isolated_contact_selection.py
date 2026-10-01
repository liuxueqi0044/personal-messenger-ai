"""Synthetic generation and forwarding checks; no VM, provider, or UI actions."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from messenger_ai.rules.models import HumanApproval, RuleSource
from messenger_ai.rules.service import AtomicRulePackStore
from test_build_session_observed_runtime_guest import (
    _evidence,
    _load_module,
    _write_bootstrap,
)

DEPLOYMENT = Path(__file__).parents[2] / "scripts" / "deployment"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


def _configure(tmp_path, monkeypatch, indices=(3, 4)):
    module = _load_module()
    runtime_root = tmp_path / "runtime"
    default_root = runtime_root / module.ACCOUNT_ID
    default_root.mkdir(parents=True)
    for name in ("memory.sqlite3", "qq-vm-bridge.cursor.sqlite3"):
        with sqlite3.connect(default_root / name) as db:
            db.execute("CREATE TABLE preserved(value TEXT)")
            db.execute("INSERT INTO preserved VALUES('synthetic old state')")
    with sqlite3.connect(default_root / "runtime.sqlite3") as db:
        db.executescript("""
            CREATE TABLE runtime_global_control(singleton INTEGER PRIMARY KEY, paused INTEGER);
            INSERT INTO runtime_global_control VALUES(1,1);
            CREATE TABLE runtime_conversations(account_id TEXT, conversation_id TEXT, contact_id TEXT);
            INSERT INTO runtime_conversations VALUES
              ('qq-default-account','qq-session-conversation-1','session-contact-1'),
              ('qq-default-account','qq-session-conversation-2','session-contact-2');
        """)
    for name in ("registered-session-scope.json", "registered-session-scope-2.json"):
        (default_root / name).write_text('{"synthetic":"old registry"}', encoding="utf-8")
    bootstrap_root = tmp_path / "bootstraps"
    bootstrap_root.mkdir()
    for ordinal, index in enumerate(sorted(set(indices)), start=1):
        _write_bootstrap(
            bootstrap_root / f"qq-session-observed-bootstrap-{index}.json",
            _evidence(f"session-contact-{index}", str(ordinal)),
            latest_text=f"synthetic baseline {index}",
        )
    selector = tmp_path / "selector.json"
    selector.write_text(json.dumps({
        "environment_fingerprint": "a" * 64,
        "client_version": "9.9.20",
        "fixture_suite_version": "session-pack-v1",
    }), encoding="utf-8")
    rules = tmp_path / "rules.sqlite3"
    store = AtomicRulePackStore(str(rules))
    draft = store.ingest(RuleSource(name="synthetic.yaml", content=(
        b"schema_version: 1\nrulepack_id: synthetic\n"
        b"persona:\n  identity: synthetic persona\n  tone: [warm]\n"
        b"required_behaviors: []\nprohibited_behaviors: []\n"
        b"escalation_rules: []\npacing: {}\ncontacts: {}\nexamples: {}\n"
    )))
    store.activate(draft.draft_id, HumanApproval(approver_id="test", reason="test"))
    store.connection.close()
    (default_root / "rules.sqlite3").write_bytes(rules.read_bytes())
    output = tmp_path / "runtime.json"
    output.write_text(json.dumps({
        "schema": "pmai-v5-runtime-1", "data_dir": str(default_root),
        "contacts": [{"contact_id": f"session-contact-{i}"} for i in (1, 2)],
    }), encoding="utf-8")
    for key, value in {
        "RUNTIME_ROOT": runtime_root, "BOOTSTRAP_ROOT": bootstrap_root,
        "OUTPUT": output, "SOURCE_RULES": rules, "SELECTOR_PACK": selector,
    }.items():
        monkeypatch.setattr(module, key, value)
    module._use_data_root(default_root)
    return module, default_root


def _options(name: str, indices) -> list[str]:
    return [value for index in indices for value in (name, str(index))]


def _rule_rows(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return db.execute("SELECT * FROM m7_rulepack_sources ORDER BY draft_id").fetchall()


@pytest.mark.parametrize("indices", [(3,), (4, 3, 4), (1, 9999)])
def test_explicit_generation_uses_exact_set_and_preserves_old_data(
    tmp_path, monkeypatch, indices
):
    module, default_root = _configure(tmp_path, monkeypatch, indices)
    def unexpected_discovery():
        pytest.fail("explicit selection must not discover prior contacts")

    monkeypatch.setattr(module, "_existing_registration_indices", unexpected_discovery)
    monkeypatch.setattr(module, "_existing_runtime_indices", unexpected_discovery)
    before = _snapshot(default_root)
    source_bytes = module.SOURCE_RULES.read_bytes()
    previous_config = module.OUTPUT.read_bytes()
    selected = sorted(set(indices))
    generation_id = str(uuid4())
    arguments = ["--isolated-recovery-generation", generation_id]
    arguments += _options("--contact-index", indices)
    arguments += _options("--refresh-session-index", selected)
    arguments += _options("--adopt-latest-inbound-index", selected)
    for index in selected:
        arguments += ["--visual-label", f"{index}=synthetic-{index}"]
    assert module.main(arguments) == 0
    config = json.loads(module.OUTPUT.read_text(encoding="utf-8"))
    root = module.RUNTIME_ROOT / "recovery-generations" / generation_id / module.ACCOUNT_ID
    expected_ids = [f"session-contact-{index}" for index in selected]
    assert [item["contact_id"] for item in config["contacts"]] == expected_ids
    assert [item["binding_id"] for item in config["bindings"]] == expected_ids
    assert [item["binding_id"] for item in config["session_observed_evidence"]] == expected_ids
    manifest = json.loads((root / "generation-manifest.json").read_text(encoding="utf-8"))
    assert [item["binding_id"] for item in manifest["contacts"]] == expected_ids
    assert set(config["visual_selection"]["labels"]) == set(expected_ids)
    assert config["bootstrap_last_inbound_once"] == [
        f"qq-session-conversation-{index}" for index in selected
    ]
    assert config["start_globally_paused"] is True
    assert config["runtime_generation"]["enforce_global_pause"] is True
    assert config["content_policy_checks_enabled"] is False
    assert set(path.name for path in root.glob("registered-session-scope*.json")) == {
        "registered-session-scope.json" if index == 1 else f"registered-session-scope-{index}.json"
        for index in selected
    }
    assert (root / "previous-runtime-config.json").read_bytes() == previous_config
    assert _rule_rows(root / "rules.sqlite3") == _rule_rows(module.SOURCE_RULES)
    assert b"synthetic persona" in source_bytes
    assert module.SOURCE_RULES.read_bytes() == source_bytes
    assert _snapshot(default_root) == before
    first = _snapshot(root)
    assert module.main(arguments) == 0
    assert _snapshot(root) == first
    assert _snapshot(default_root) == before


@pytest.mark.parametrize("options", [
    ["--contact-index", "0"],
    ["--contact-index", "10000"],
    ["--contact-index", "not-an-integer"],
    ["--contact-index"],
    ["--contact-index", "3", "--include-contact-2"],
    ["--contact-index", "3", "--additional-contact-index", "4"],
    ["--contact-index", "3", "--adopt-latest-inbound-index", "1"],
    ["--contact-index", "3", "--refresh-session-index", "1"],
    ["--contact-index", "3", "--contact-index", "4", "--refresh-session-index", "3"],
    ["--contact-index", "3", "--migrate-header-digest-index", "3"],
    ["--contact-index", "3", "--refresh-session-index", "3", "--migrate-header-digest-index", "4"],
    ["--contact-index", "3", "--visual-label", "1=synthetic"],
    ["--contact-index", "3", "--visual-label", "3=synthetic", "--visual-label", "3=duplicate"],
    ["--contact-index", "3", "--visual-label", "bad"],
    ["--additional-contact-index", "0"],
    ["--additional-contact-index", "2"],
    ["--additional-contact-index", "3", "--refresh-session-index", "3"],
    ["--visual-label", "3=synthetic"],
])
def test_invalid_scope_is_rejected_before_generation_or_state_writes(
    tmp_path, monkeypatch, options
):
    module, _ = _configure(tmp_path, monkeypatch)
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit) as exc:
        module.main(["--isolated-recovery-generation", str(uuid4()), *options])
    assert exc.value.code == 2
    assert not (module.RUNTIME_ROOT / "recovery-generations").exists()
    assert _snapshot(tmp_path) == before


def test_explicit_selection_requires_isolation_before_any_write(tmp_path, monkeypatch):
    module, _ = _configure(tmp_path, monkeypatch)
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit) as exc:
        module.main(["--contact-index", "3"])
    assert exc.value.code == 2
    assert _snapshot(tmp_path) == before


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, DEPLOYMENT / "host" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("indices", [(3,), (4, 3, 4)])
def test_host_and_wrapper_forward_exact_isolated_set_to_real_builder(
    tmp_path, monkeypatch, indices
):
    builder, default_root = _configure(tmp_path, monkeypatch, indices)
    before = _snapshot(default_root)
    wrapper = _load_script("selection_wrapper", "run_session_builder_guest_wrapper.py")
    monkeypatch.setattr(wrapper, "load_builder", lambda _: builder)
    monkeypatch.setattr(wrapper, "RUNTIME_CONFIG", builder.OUTPUT)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace())
    host = _load_script("selection_host", "run_session_builder_host.py")
    generation = str(uuid4())
    calls = []

    class Process:
        Status = 500
        ExitCode = -1

        def WaitForArray(self, events, timeout):
            return events[0]

    class Guest:
        def ProcessCreate(self, executable, arguments, *_):
            calls.append(arguments)
            with monkeypatch.context() as context:
                context.setattr(sys, "argv", arguments[1:])
                code = wrapper.main()
            process = Process()
            process.ExitCode = code
            return process

    result = tmp_path / "wrapper-result.json"
    status, code = host._run_guest(
        Guest(), "r20261001-02", list(indices), [], [], generation, [], str(result)
    )
    assert (status, code) == (500, 0)
    assert calls[0].count("--contact-index") == len(indices)
    config = json.loads(builder.OUTPUT.read_text(encoding="utf-8"))
    assert [item["contact_id"] for item in config["contacts"]] == [
        f"session-contact-{index}" for index in sorted(set(indices))
    ]
    report = json.loads(result.read_text(encoding="utf-8"))
    assert report["succeeded"] is True
    assert report["contact_indices"] == sorted(set(indices))
    assert _snapshot(default_root) == before


@pytest.mark.parametrize("indices, expected", [
    ([1], ["--refresh-session-index", "1"]),
    ([1, 2, 3], ["--include-contact-2", "--additional-contact-index", "3",
                 "--refresh-session-index", "1", "--refresh-session-index", "2",
                 "--refresh-session-index", "3"]),
    ([3], ["--additional-contact-index", "3", "--refresh-session-index", "3"]),
])
def test_nonisolated_wrapper_preserves_legacy_refresh_arguments(
    tmp_path, monkeypatch, indices, expected
):
    wrapper = _load_script("legacy_selection_wrapper", "run_session_builder_guest_wrapper.py")
    calls = []
    monkeypatch.setattr(wrapper, "load_builder", lambda _: SimpleNamespace(
        main=lambda args: calls.append(args) or 0
    ))
    monkeypatch.setattr(wrapper, "RUNTIME_CONFIG", tmp_path / "not-created.json")
    monkeypatch.setattr(sys, "argv", [
        "wrapper", "--release-id", "r20261001-02", "--result", str(tmp_path / "report.json"),
        *_options("--contact-index", indices),
    ])
    assert wrapper.main() == 0
    assert calls == [expected]


def test_omitted_explicit_selection_keeps_isolated_compatibility_and_discovery(
    tmp_path, monkeypatch
):
    module, default_root = _configure(tmp_path, monkeypatch, (1, 2, 3))
    before = _snapshot(default_root)
    generation = str(uuid4())
    assert module.main([
        "--isolated-recovery-generation", generation,
        "--include-contact-2", "--additional-contact-index", "3",
    ]) == 0
    config_bytes = module.OUTPUT.read_bytes()
    config = json.loads(config_bytes)
    assert [item["contact_id"] for item in config["contacts"]] == [
        "session-contact-1", "session-contact-2", "session-contact-3"
    ]
    # The legacy path still discovers registrations when no selection is supplied.
    assert module.main(["--isolated-recovery-generation", generation]) == 0
    assert module.OUTPUT.read_bytes() == config_bytes
    assert _snapshot(default_root) == before


@pytest.mark.parametrize("initial, changed", [((3,), (4,)), ((3,), (3, 4)), ((3, 4), (3,))])
def test_existing_generation_rejects_changed_explicit_set_before_any_write(
    tmp_path, monkeypatch, initial, changed
):
    module, _ = _configure(tmp_path, monkeypatch, (3, 4))
    generation = str(uuid4())
    assert module.main([
        "--isolated-recovery-generation", generation, *_options("--contact-index", initial)
    ]) == 0
    before = _snapshot(tmp_path)
    # Fail immediately if the retry reaches directory preparation or a write.
    def unexpected_write(*_args, **_kwargs):
        pytest.fail("changed explicit selection reached a write-capable step")

    monkeypatch.setattr(module, "_prepare_isolated_generation_root", unexpected_write)
    monkeypatch.setattr(module, "_atomic_json", unexpected_write)
    monkeypatch.setattr(module, "_install_rules_database", unexpected_write)
    with pytest.raises(RuntimeError, match="contact selection changed"):
        module.main([
            "--isolated-recovery-generation", generation,
            *_options("--contact-index", changed),
        ])
    assert _snapshot(tmp_path) == before
