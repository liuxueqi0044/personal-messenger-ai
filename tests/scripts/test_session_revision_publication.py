from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from messenger_ai.runtime import config_publication
from messenger_ai.runtime.session_revision import validate_session_revision


REPOSITORY = Path(__file__).parents[2]
DATABASES = (
    "runtime.sqlite3", "hub.sqlite3", "memory.sqlite3", "pacing.sqlite3",
    "rules.sqlite3", "authorization.sqlite3", "qq-vm-bridge.sqlite3",
    "qq-vm-bridge.cursor.sqlite3",
)
WRITE_STAGES = (
    "journal", "data_fence", "snapshot", "registration_1",
    "registration_2", "registration_3", "canonical",
)


def _helpers():
    source = Path(__file__).with_name("test_build_session_observed_runtime_guest.py")
    spec = importlib.util.spec_from_file_location("session_revision_test_helpers", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _all_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


def _config(case) -> dict:
    return json.loads(case.output.read_text(encoding="utf-8"))


def _refresh_args(case) -> list[str]:
    return [
        "--refresh-current-session", "--expected-current-generation", case.generation_id,
        "--expected-current-config-sha256", hashlib.sha256(case.output.read_bytes()).hexdigest(),
    ]


def _refresh_bootstraps(case, revision: int = 2) -> None:
    for index in (1, 2, 3):
        evidence = case.helpers._evidence(f"session-contact-{index}", str(index)).model_copy(update={
            "process_id": 1000 + revision,
            "window_handle": 2000 + revision,
            "process_started_at_100ns": 3000 + revision,
            "selected_row_runtime_id_hash": hashlib.sha256(f"{revision}:{index}".encode()).hexdigest(),
        })
        case.helpers._write_bootstrap(
            case.bootstrap_root / f"qq-session-observed-bootstrap-{index}.json", evidence,
            latest_text=f"new-session-content-{revision}-{index}",
        )


@pytest.fixture
def generation(tmp_path: Path, monkeypatch):
    helpers = _helpers()
    module = helpers._load_module()
    bootstrap_root = tmp_path / "bootstraps"
    bootstrap_root.mkdir()
    selector = tmp_path / "selector.json"
    selector.write_text(json.dumps({
        "environment_fingerprint": "a" * 64,
        "client_version": "9.9.20", "fixture_suite_version": "session-pack-v1",
    }), encoding="utf-8")
    source_rules = tmp_path / "source-rules.sqlite3"
    helpers._write_rules(source_rules)
    runtime_root = tmp_path / "runtime"
    output = tmp_path / "runtime.json"
    for key, value in {
        "RUNTIME_ROOT": runtime_root, "BOOTSTRAP_ROOT": bootstrap_root,
        "OUTPUT": output, "SOURCE_RULES": source_rules, "SELECTOR_PACK": selector,
    }.items():
        monkeypatch.setattr(module, key, value)
    fence_events = []
    monkeypatch.setattr(module._RuntimeBuildFence, "acquire", lambda _self: fence_events.append("acquire"))
    monkeypatch.setattr(module._RuntimeBuildFence, "close", lambda _self: fence_events.append("close"))
    monkeypatch.setattr(module, "_validate_frozen_candidate", lambda *_args: None)
    for index in (1, 2, 3):
        helpers._write_bootstrap(
            bootstrap_root / f"qq-session-observed-bootstrap-{index}.json",
            helpers._evidence(f"session-contact-{index}", str(index)),
            latest_text=f"original-adoption-{index}",
        )
    generation_id = str(uuid4())
    assert module.main([
        "--isolated-recovery-generation", generation_id,
        "--contact-index", "1", "--contact-index", "2", "--contact-index", "3",
        "--adopt-latest-inbound-index", "3",
    ]) == 0
    data_root = Path(json.loads(output.read_text(encoding="utf-8"))["data_dir"])
    for name in DATABASES:
        with closing(sqlite3.connect(data_root / name)) as db:
            db.execute("CREATE TABLE publication_test_sentinel(value TEXT)")
            db.execute("INSERT INTO publication_test_sentinel VALUES(?)", (name,))
            if name == "runtime.sqlite3":
                db.executescript("""
                    CREATE TABLE runtime_global_control(
                      singleton INTEGER PRIMARY KEY, revision INTEGER, paused INTEGER, reason TEXT
                    );
                    INSERT INTO runtime_global_control VALUES(1,7,1,'operator');
                    CREATE TABLE runtime_conversations(
                      conversation_id TEXT PRIMARY KEY, contact_id TEXT, account_id TEXT
                    );
                """)
                db.executemany("INSERT INTO runtime_conversations VALUES(?,?,?)", [
                    (f"qq-session-conversation-{index}", f"session-contact-{index}", module.ACCOUNT_ID)
                    for index in (1, 2, 3)
                ])
            db.commit()
    case = SimpleNamespace(
        root=tmp_path, helpers=helpers, module=module, output=output,
        runtime_root=runtime_root, data_root=data_root, bootstrap_root=bootstrap_root,
        generation_id=generation_id, fence_events=fence_events,
    )
    _refresh_bootstraps(case)
    fence_events.clear()
    return case


def _stage_path(case, stage: str) -> Path:
    return {
        "journal": config_publication.publication_marker(case.output),
        "data_fence": case.data_root / ".config-publication.json",
        "snapshot": case.data_root / "runtime-config.session-2.json",
        "registration_1": case.data_root / "registered-session-scope.json",
        "registration_2": case.data_root / "registered-session-scope-2.json",
        "registration_3": case.data_root / "registered-session-scope-3.json",
        "canonical": case.output,
    }[stage]


def test_refresh_publishes_consecutive_revisions_without_business_or_adoption_writes(
    generation, monkeypatch,
) -> None:
    case = generation
    original = _config(case)
    root_bytes = (case.data_root / "runtime-config.json").read_bytes()
    manifest_bytes = (case.data_root / "generation-manifest.json").read_bytes()
    databases = {name: (case.data_root / name).read_bytes() for name in DATABASES}
    adoption_registration = json.loads(
        (case.data_root / "registered-session-scope-3.json").read_text(encoding="utf-8")
    )["initial_adoption"]
    previous_bytes = case.output.read_bytes()
    candidate_checks = []
    immutable_snapshots = {}

    def validate_candidate(path, digest):
        assert case.fence_events[-1] == "acquire"
        assert case.output.read_bytes() == previous_bytes
        assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
        candidate_checks.append(path)

    monkeypatch.setattr(case.module, "_validate_frozen_candidate", validate_candidate)
    for revision in (2, 3):
        if revision == 3:
            _refresh_bootstraps(case, revision)
        assert case.module.main(_refresh_args(case)) == 0
        current = _config(case)
        metadata = current["session_binding"]
        assert metadata == {
            "schema": "pmai-qq-session-binding-v1", "revision": revision,
            "base_config_sha256": hashlib.sha256(root_bytes).hexdigest(),
            "previous_config_sha256": hashlib.sha256(previous_bytes).hexdigest(),
        }
        snapshot = case.data_root / f"runtime-config.session-{revision}.json"
        assert snapshot.read_bytes() == case.output.read_bytes()
        assert all(path.read_bytes() == payload for path, payload in immutable_snapshots.items())
        immutable_snapshots[snapshot] = snapshot.read_bytes()
        validated = validate_session_revision(current, trusted_runtime_root=case.runtime_root)
        assert validated is not None and validated.revision == revision
        assert current["runtime_generation"] == original["runtime_generation"]
        assert current["data_dir"] == original["data_dir"]
        assert current["start_globally_paused"] is True
        for field in ("bootstrap_last_inbound_once", "bootstrap_last_inbound_provenance"):
            assert current[field] == original[field]
        assert current["bootstrap_last_inbound_once"] == ["qq-session-conversation-3"]
        assert json.loads((case.data_root / "registered-session-scope-3.json").read_text(
            encoding="utf-8"
        ))["initial_adoption"] == adoption_registration
        assert (case.data_root / "runtime-config.json").read_bytes() == root_bytes
        assert (case.data_root / "generation-manifest.json").read_bytes() == manifest_bytes
        assert {name: (case.data_root / name).read_bytes() for name in DATABASES} == databases
        previous_bytes = case.output.read_bytes()
    assert [path.name for path in candidate_checks] == [
        "runtime-config.session-2.json", "runtime-config.session-3.json",
    ]
    assert case.fence_events == ["acquire", "close", "acquire", "close"]
    assert not config_publication.publication_marker(case.output).exists()
    assert not (case.data_root / ".config-publication.json").exists()


@pytest.mark.parametrize("failure", [
    "generation", "digest", "stable_identity", "missing_bootstrap", "mixed_process",
    "missing_registration", "extra_runtime_contact", "extra_registration", "unpaused",
    "invalid_pause", "missing_database", "unchanged_evidence",
])
def test_refresh_rejection_does_not_write_any_files(generation, failure: str) -> None:
    case = generation
    args = _refresh_args(case)
    if failure == "generation":
        args[2] = str(uuid4())
    elif failure == "digest":
        args[4] = "0" * 64
    elif failure in {"stable_identity", "mixed_process"}:
        path = case.bootstrap_root / "qq-session-observed-bootstrap-2.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        proof = case.helpers.QQSessionObservedDirectIdentity.model_validate(report["evidence"])
        updates = {"header_digest": "e" * 64} if failure == "stable_identity" else {"process_id": 12345}
        case.helpers._write_bootstrap(path, proof.model_copy(update=updates))
    elif failure == "missing_bootstrap":
        (case.bootstrap_root / "qq-session-observed-bootstrap-2.json").unlink()
    elif failure == "missing_registration":
        (case.data_root / "registered-session-scope-2.json").unlink()
    elif failure == "extra_registration":
        (case.data_root / "registered-session-scope-4.json").write_bytes(b"{}")
    elif failure in {"extra_runtime_contact", "unpaused", "invalid_pause"}:
        with closing(sqlite3.connect(case.data_root / "runtime.sqlite3")) as db:
            if failure in {"unpaused", "invalid_pause"}:
                db.execute("UPDATE runtime_global_control SET paused=?", (0 if failure == "unpaused" else 2,))
            else:
                db.execute("INSERT INTO runtime_conversations VALUES(?,?,?)", (
                    "qq-session-conversation-4", "session-contact-4", case.module.ACCOUNT_ID,
                ))
            db.commit()
    elif failure == "missing_database":
        (case.data_root / "memory.sqlite3").unlink()
    elif failure == "unchanged_evidence":
        for index, evidence in enumerate(_config(case)["session_observed_evidence"], start=1):
            proof = case.helpers.QQSessionObservedDirectIdentity.model_validate(evidence)
            case.helpers._write_bootstrap(
                case.bootstrap_root / f"qq-session-observed-bootstrap-{index}.json", proof,
            )
    before = _all_bytes(case.root)

    with pytest.raises((RuntimeError, ValueError)):
        case.module.main(args)

    assert _all_bytes(case.root) == before
    assert case.fence_events == ["acquire", "close"]


@pytest.mark.parametrize("invalid", [True, "1", 1.0])
def test_bootstrap_cannot_coerce_process_locators_before_strict_validation(generation, invalid) -> None:
    case = generation
    for path in case.bootstrap_root.glob("qq-session-observed-bootstrap-*.json"):
        report = json.loads(path.read_text(encoding="utf-8"))
        for field in ("process_id", "window_handle", "process_started_at_100ns"):
            report["evidence"][field] = invalid
        path.write_text(json.dumps(report), encoding="utf-8")
    before = _all_bytes(case.root)
    with pytest.raises(RuntimeError, match="process locators"):
        case.module.main(_refresh_args(case))
    assert _all_bytes(case.root) == before


@pytest.mark.parametrize("extra_args", [
    ["--contact-index", "1"], ["--include-contact-2"], ["--additional-contact-index", "4"],
    ["--adopt-latest-inbound-index", "3"], ["--refresh-session-index", "1"],
    ["--migrate-header-digest-index", "1"], ["--visual-label", "1=Alice"],
    ["--isolated-recovery-generation", "a8d03bfb-ce2c-48c1-88ea-f6ff19a03538"],
])
def test_refresh_forbids_scope_and_adoption_options_without_writes(generation, extra_args) -> None:
    case = generation
    before = _all_bytes(case.root)
    with pytest.raises(SystemExit) as rejected:
        case.module.main(_refresh_args(case) + extra_args)
    assert rejected.value.code == 2
    assert _all_bytes(case.root) == before


def test_refresh_busy_runtime_fence_prevents_any_writes(generation, monkeypatch) -> None:
    case = generation
    before = _all_bytes(case.root)

    def busy(_self):
        raise RuntimeError("runtime must be stopped")

    monkeypatch.setattr(case.module._RuntimeBuildFence, "acquire", busy)
    with pytest.raises(RuntimeError, match="runtime must be stopped"):
        case.module.main(_refresh_args(case))
    assert _all_bytes(case.root) == before


def test_frozen_candidate_rejection_rolls_back_every_metadata_write(generation, monkeypatch) -> None:
    case = generation
    before = _all_bytes(case.root)
    checked = []

    def reject(path, digest):
        assert path.name == "runtime-config.session-2.json"
        assert path.read_bytes() != case.output.read_bytes()
        assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
        assert json.loads((case.data_root / "registered-session-scope-3.json").read_text(
            encoding="utf-8"
        ))["session_evidence"]["process_id"] == 1002
        checked.append(path)
        raise RuntimeError("frozen candidate rejected")

    monkeypatch.setattr(case.module, "_validate_frozen_candidate", reject)
    with pytest.raises(RuntimeError, match="frozen candidate rejected"):
        case.module.main(_refresh_args(case))
    assert len(checked) == 1
    assert _all_bytes(case.root) == before


@pytest.mark.parametrize("stage", WRITE_STAGES)
@pytest.mark.parametrize("when", ["before", "after"])
def test_refresh_write_failure_restores_previous_bytes(generation, monkeypatch, stage, when) -> None:
    case = generation
    before = _all_bytes(case.root)
    target = _stage_path(case, stage)
    original_atomic = config_publication.atomic_bytes
    injected = False

    def fail_once(path, payload):
        nonlocal injected
        matches = Path(path) == target and not injected
        if matches:
            injected = True
            if when == "before":
                raise OSError("injected publication failure")
        original_atomic(path, payload)
        if matches:
            raise OSError("injected publication failure")

    monkeypatch.setattr(config_publication, "atomic_bytes", fail_once)
    monkeypatch.setattr(case.module, "atomic_bytes", fail_once)
    with pytest.raises(OSError, match="injected publication failure"):
        case.module.main(_refresh_args(case))
    assert injected
    # __enter__ failures retain the write-ahead fence for the next stopped build.
    config_publication.ConfigPublication.recover(case.output, runtime_root=case.runtime_root)
    assert _all_bytes(case.root) == before
    assert case.module.main(_refresh_args(case)) == 0


_HARD_EXIT_SCRIPT = r"""
import importlib.util, json, os, sys
from pathlib import Path
from messenger_ai.runtime import config_publication
spec = importlib.util.spec_from_file_location('interrupted_revision_builder', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
settings = json.loads(sys.argv[2])
for key, value in settings['paths'].items():
    setattr(module, key, Path(value))
module._RuntimeBuildFence.acquire = lambda self: None
module._RuntimeBuildFence.close = lambda self: None
module._validate_frozen_candidate = lambda *args: None
original = config_publication.atomic_bytes
def interrupted_write(path, value):
    original(path, value)
    if Path(path) == Path(settings['target']):
        os._exit(73)
config_publication.atomic_bytes = interrupted_write
module.atomic_bytes = interrupted_write
module.main(settings['args'])
raise AssertionError('hard interruption was not reached')
"""


@pytest.mark.parametrize("stage", WRITE_STAGES)
def test_hard_process_exit_is_fenced_and_next_build_recovers(generation, monkeypatch, stage) -> None:
    case = generation
    before = _all_bytes(case.root)
    args = _refresh_args(case)
    settings = {
        "paths": {
            "RUNTIME_ROOT": str(case.runtime_root), "BOOTSTRAP_ROOT": str(case.bootstrap_root),
            "OUTPUT": str(case.output),
        },
        "target": str(_stage_path(case, stage)), "args": args,
    }
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(REPOSITORY / "src"), environment.get("PYTHONPATH"),
    ]))
    completed = subprocess.run(
        [sys.executable, "-c", _HARD_EXIT_SCRIPT, str(case.module.__file__), json.dumps(settings)],
        cwd=REPOSITORY, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 73, completed.stdout + completed.stderr
    with pytest.raises(RuntimeError, match="publication incomplete"):
        config_publication.assert_publication_complete(case.output)
    if stage != "journal":
        with pytest.raises(RuntimeError, match="publication incomplete"):
            config_publication.assert_data_publication_complete(case.data_root)
    for name in DATABASES:
        relative = str((case.data_root / name).relative_to(case.root))
        assert (case.data_root / name).read_bytes() == before[relative]
    # The decorator recovers before refresh validation. Check the whole original
    # tree at that boundary, then continue with a normal successful publication.
    refresh = case.module._refresh_current_session
    recovered = []

    def inspect_recovery(**kwargs):
        assert _all_bytes(case.root) == before
        recovered.append(True)
        return refresh(**kwargs)

    monkeypatch.setattr(case.module, "_refresh_current_session", inspect_recovery)
    assert case.module.main(args) == 0
    assert recovered == [True]
    assert _config(case)["session_binding"]["revision"] == 2
    assert not config_publication.publication_marker(case.output).exists()
