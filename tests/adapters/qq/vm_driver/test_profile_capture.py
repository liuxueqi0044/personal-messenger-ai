from datetime import UTC, datetime, timedelta
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

import pytest

from messenger_ai.adapters.qq.vm_driver import profile_identity as capture_module
from messenger_ai.adapters.qq.vm_driver.profile_identity import (
    MAX_STDERR_BYTES, MAX_STDOUT_BYTES, ProfileCaptureError,
    capture_current_profile, run_profile_helper,
)
from messenger_ai.observability import SecretNotFoundError, SecretStoreError


START = datetime(2026, 10, 2, tzinfo=UTC)
KEY = b"k" * 32
HEADER = "a" * 64
RIGHT = "b" * 64
GUEST = {"certified": True, "machine": "PMAI-QQVM", "user": "qqbot", "hypervisor": "virtualbox"}


def reports():
    header = dict(
        probe_version="qq-uia-guest-header-v1", mode="guest_foreground_header_inspect",
        succeeded=True, status="HEADER_CAPTURED", header_candidate_count=1,
        process_id=1234, window_handle=9001, guest_environment=dict(GUEST),
        active_header_digest=HEADER, right_region_structure_digest=RIGHT,
        privacy=dict(exact_hwnd=True, emitted_control_names=False, emitted_chat_text=False,
                     mouse_input_used=False, keyboard_input_used=False, clipboard_used=False,
                     transient_navigation_performed=False, composer_or_send_accessed=False),
    )
    profile = dict(
        probe_version="qq-uia-guest-foreground-identity-v1",
        mode="guest_foreground_current_chat_identity", succeeded=True, status="STABLE_IDENTITY_CAPTURED",
        process_id=1234, window_handle=9001, is_maximized=True,
        is_foreground_before=True, is_foreground_after=True, guest_environment=dict(GUEST),
        active_header_digest=HEADER, right_region_structure_digest=RIGHT,
        header_candidate_count=1, identity_candidate_count=1,
        profile_id_hmac="c" * 64, identity_evidence_type="explicit_labeled_qq_id",
        profile_structure_digest="d" * 64,
        privacy=dict(exact_hwnd=True, raw_profile_id_emitted=False, hmac_key_from_stdin_only=True,
                     emitted_chat_text=False, emitted_control_names=False, desktop_capture_supported=False,
                     mouse_input_used=False, keyboard_input_used=False, clipboard_used=False,
                     foreground_requested=True, composer_send_attempted=False, composer_or_send_accessed=False,
                     write_actions_supported=False, hmac_key_emitted=False, transient_navigation_performed=True),
        recovery=dict(attempted=True, original_view_restored=True, foreground_changed=False),
    )
    return [header, profile]


class FakeClock:
    def __init__(self):
        self.utc = START
        self.tick = 100.0

    def advance(self, seconds, utc_seconds=None):
        self.tick += seconds
        self.utc += timedelta(seconds=seconds if utc_seconds is None else utc_seconds)


class Store:
    def __init__(self, key=KEY, error=None):
        self.key = key
        self.error = error
        self.reads = []

    def get_secret(self, name):
        self.reads.append(name)
        if self.error:
            raise self.error
        return self.key

    def get_or_create_hmac_key(self, *_args):
        raise AssertionError("production capture must not create a secret")


class Runner:
    def __init__(self, clock, outputs=None, durations=(7, 8)):
        self.clock = clock
        self.outputs = reports() if outputs is None else outputs
        self.durations = durations
        self.calls = []

    def __call__(self, command, **kwargs):
        index = len(self.calls)
        self.calls.append((command, kwargs))
        self.clock.advance(self.durations[index])
        value = self.outputs[index]
        if isinstance(value, Exception):
            raise value
        return subprocess.CompletedProcess(command, 0, json.dumps(value).encode(), b"")


@pytest.fixture
def harness(tmp_path):
    clock = FakeClock()
    runner = Runner(clock)
    store = Store()
    params = dict(
        pid=1234, hwnd=9001, vault=tmp_path / "nonexistent-vault",
        environment_fingerprint="e" * 64, selector_pack_version="selector-v1",
        deadline=START + timedelta(seconds=30), runner=runner,
        clock=lambda: clock.utc, monotonic=lambda: clock.tick, secret_store=store,
    )
    return params, clock, runner, store


def capture(harness, **updates):
    return capture_current_profile("helper.exe", **{**harness[0], **updates})


def failure(call, code):
    with pytest.raises(ProfileCaptureError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def test_shared_budget_secret_only_in_stdin_and_redacted_strict_projection(harness):
    result = capture(harness)
    _, _, runner, store = harness
    assert store.reads == ["qq.identity.hmac"]
    assert [call[1]["timeout"] for call in runner.calls] == [30, 23]
    assert runner.calls[0][1]["input"] is None
    assert runner.calls[1][1]["input"] == HEADER.encode() + b"\n" + base64.b64encode(KEY) + b"\n"
    for command, kwargs in runner.calls:
        assert command[:5] == ["helper.exe", "--target-qq-pid", "1234", "--target-qq-hwnd", "9001"]
        assert "vault" not in repr(command) and KEY.decode() not in repr(command)
        assert kwargs["text"] is False and kwargs["check"] is False
    assert result == dict(process_id=1234, window_handle=9001,
        environment_fingerprint="e" * 64, selector_pack_version="selector-v1",
        active_header_digest=HEADER, right_region_structure_digest=RIGHT,
        profile_structure_digest="d" * 64, profile_id_hmac="c" * 64, candidate_count=1)


@pytest.mark.parametrize("error,code", [
    (SecretNotFoundError("private path or contents"), "IDENTITY_KEY_MISSING"),
    (SecretStoreError("private path or contents"), "IDENTITY_KEY_UNAVAILABLE"),
    (OSError("private path or contents"), "IDENTITY_KEY_UNAVAILABLE"),
])
def test_missing_or_unreadable_key_prevents_any_ui_action(harness, error, code):
    exc = failure(lambda: capture(harness, secret_store=Store(error=error)), code)
    assert harness[2].calls == []
    assert "private" not in "".join(traceback.format_exception(exc))


@pytest.mark.parametrize("key", [b"", b"k" * 31, b"k" * 129, "k" * 32, None])
def test_invalid_key_fails_without_helper_calls(harness, key):
    failure(lambda: capture(harness, secret_store=Store(key=key)), "IDENTITY_KEY_INVALID")
    assert harness[2].calls == []


def test_absent_vault_is_not_created_in_production(harness):
    vault = harness[0]["vault"]
    failure(lambda: capture(harness, secret_store=None), "IDENTITY_KEY_UNAVAILABLE")
    assert not vault.exists()
    assert harness[2].calls == []


def test_expired_entry_deadline_reads_no_secret_or_ui(harness):
    failure(lambda: capture(harness, deadline=START), "CAPTURE_DEADLINE_EXHAUSTED")
    assert harness[3].reads == [] and harness[2].calls == []


def test_header_consuming_whole_budget_never_opens_profile(harness):
    harness[2].durations = (30, 0)
    failure(lambda: capture(harness), "CAPTURE_DEADLINE_EXHAUSTED")
    assert len(harness[2].calls) == 1


def test_late_success_is_not_returned_after_total_deadline(harness):
    harness[2].durations = (7, 24)
    failure(lambda: capture(harness), "CAPTURE_DEADLINE_EXHAUSTED")
    assert len(harness[2].calls) == 2


def test_utc_rollback_does_not_extend_monotonic_total_budget(harness):
    clock, runner = harness[1:3]
    def adjusted(*args, **kwargs):
        result = runner(*args, **kwargs)
        clock.utc -= timedelta(seconds=100)
        return result
    assert capture(harness, runner=adjusted)["profile_id_hmac"] == "c" * 64
    assert [call[1]["timeout"] for call in runner.calls] == [30, 23]


def test_utc_forward_jump_exhausts_budget_before_profile(harness):
    clock, runner = harness[1:3]
    def adjusted(*args, **kwargs):
        result = runner(*args, **kwargs)
        clock.utc += timedelta(seconds=100)
        return result
    failure(lambda: capture(harness, runner=adjusted), "CAPTURE_DEADLINE_EXHAUSTED")
    assert len(runner.calls) == 1


def test_secret_lookup_time_is_part_of_total_budget(harness):
    clock = harness[1]
    class SlowStore(Store):
        def get_secret(self, name):
            clock.advance(10)
            return super().get_secret(name)
    assert capture(harness, secret_store=SlowStore())["profile_id_hmac"] == "c" * 64
    assert [call[1]["timeout"] for call in harness[2].calls] == [20, 13]


@pytest.mark.parametrize("field,value,code", [
    ("process_id", 1235, "HEADER_SCOPE_MISMATCH"),
    ("window_handle", 9002, "HEADER_SCOPE_MISMATCH"),
    ("header_candidate_count", True, "HEADER_SCOPE_MISMATCH"),
    ("header_candidate_count", 2, "HEADER_SCOPE_MISMATCH"),
    ("active_header_digest", "raw private data", "HEADER_DIGEST_INVALID"),
    ("right_region_structure_digest", None, "RIGHT_REGION_DIGEST_INVALID"),
    ("guest_environment", {**GUEST, "certified": 1}, "GUEST_ENVIRONMENT_MISMATCH"),
    ("privacy", {}, "HEADER_PRIVACY_CONTRACT_FAILED"),
])
def test_invalid_header_stops_before_profile_action(harness, field, value, code):
    harness[2].outputs[0][field] = value
    failure(lambda: capture(harness), code)
    assert len(harness[2].calls) == 1


@pytest.mark.parametrize("field,value,code", [
    ("window_handle", 2, "HWND_OR_PROCESS_MISMATCH"),
    ("active_header_digest", "f" * 64, "HEADER_MISMATCH"),
    ("right_region_structure_digest", "f" * 64, "RIGHT_REGION_MISMATCH"),
    ("identity_candidate_count", 2, "PROFILE_CANDIDATE_NOT_UNIQUE"),
    ("recovery", {"attempted": True, "original_view_restored": False, "foreground_changed": False}, "RECOVERY_CONTRACT_FAILED"),
    ("raw_profile_id", "1234567890", "PRIVACY_CONTRACT_FAILED"),
])
def test_existing_strict_profile_parser_remains_the_authority(harness, field, value, code):
    harness[2].outputs[1][field] = value
    exc = failure(lambda: capture(harness), code)
    assert exc.diagnostic == {"stage": "profile"}
    assert "1234567890" not in repr(exc)


@pytest.mark.parametrize("stage_index", [0, 1])
def test_timeout_drops_exception_payload_and_does_not_retry(harness, stage_index):
    harness[2].outputs[stage_index] = subprocess.TimeoutExpired(
        ["private argument"], 99, output=b"private output", stderr=b"private stderr")
    exc = failure(lambda: capture(harness), "HELPER_TIMEOUT")
    assert exc.diagnostic == {"stage": "header" if stage_index == 0 else "profile"}
    assert "private" not in "".join(traceback.format_exception(exc))
    assert len(harness[2].calls) == stage_index + 1


@pytest.mark.parametrize("stdout,stderr,code", [
    (b"private stdout", b"private stderr", "HELPER_INVALID_OUTPUT"),
    (b"\xff", b"", "HELPER_INVALID_OUTPUT"),
    (b"[]", b"", "HELPER_INVALID_OUTPUT"),
    (b"x" * (MAX_STDOUT_BYTES + 1), b"", "HELPER_OUTPUT_LIMIT"),
    (b"{}", b"x" * (MAX_STDERR_BYTES + 1), "HELPER_OUTPUT_LIMIT"),
    (json.dumps({"succeeded": False, "status": "PRIVATE_QQ_1234567890"}).encode(), b"", "HELPER_FAILED"),
    (json.dumps({"succeeded": False, "status": "RESTORATION_FAILED"}).encode(), b"", "RESTORATION_FAILED"),
], ids=["invalid-json", "invalid-utf8", "not-object", "stdout-limit", "stderr-limit", "unsafe-status", "known-status"])
def test_runner_output_is_bounded_and_diagnostics_never_echo_raw(stdout, stderr, code):
    runner = lambda command, **_kwargs: subprocess.CompletedProcess(command, 2, stdout, stderr)
    exc = failure(lambda: run_profile_helper(["helper"], stage="profile", timeout=1, runner=runner), code)
    assert exc.diagnostic["stdout_bytes"] == len(stdout)
    assert exc.diagnostic["stdout_sha256"] == hashlib.sha256(stdout).hexdigest()
    assert "private stdout" not in repr(exc.diagnostic) and "1234567890" not in repr(exc.diagnostic)


def test_production_runner_reads_a_real_bounded_subprocess_without_shell():
    result = run_profile_helper([sys.executable, "-c", "print('{\"succeeded\":true}')"],
                                stage="header", timeout=5)
    assert result == {"succeeded": True}


def test_production_runner_timeout_kills_and_reaps_only_its_process(monkeypatch):
    launched = []
    original = capture_module.subprocess.Popen
    def recording_popen(*args, **kwargs):
        process = original(*args, **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(capture_module.subprocess, "Popen", recording_popen)
    started = time.monotonic()
    exc = failure(lambda: run_profile_helper(
        [sys.executable, "-c", "import time; time.sleep(10)"], stage="profile", timeout=0.1,
    ), "HELPER_TIMEOUT")
    assert exc.diagnostic == {"stage": "profile"}
    assert len(launched) == 1 and launched[0].poll() is not None
    assert time.monotonic() - started < 3


def test_parsing_does_not_escape_original_total_deadline(harness, monkeypatch):
    original = capture_module.parse_guest_foreground_profile_report
    def slow_parse(*args, **kwargs):
        result = original(*args, **kwargs)
        harness[1].advance(20)
        return result
    monkeypatch.setattr(capture_module, "parse_guest_foreground_profile_report", slow_parse)
    failure(lambda: capture(harness), "CAPTURE_DEADLINE_EXHAUSTED")


def test_start_failure_exception_does_not_expose_path_or_stdin(harness):
    harness[2].outputs[0] = OSError("private helper path or secret payload")
    exc = failure(lambda: capture(harness), "HELPER_START_FAILED")
    assert exc.diagnostic == {"stage": "header"}
    assert "private" not in "".join(traceback.format_exception(exc))


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_production_runner_kills_real_output_flood_with_bounded_metadata(stream):
    command = [sys.executable, "-c", f"import sys; sys.{stream}.write('x'*200000); sys.{stream}.flush()"]
    exc = failure(lambda: run_profile_helper(command, stage="header", timeout=5), "HELPER_OUTPUT_LIMIT")
    assert exc.diagnostic["output_limit_exceeded"] is True
    assert len(json.dumps(exc.diagnostic)) < 512


@pytest.mark.parametrize("updates", [
    {"pid": True}, {"hwnd": 0}, {"environment_fingerprint": "private"},
    {"selector_pack_version": "line\nfeed"}, {"key_name": "../key"},
    {"deadline": START.replace(tzinfo=None)},
])
def test_invalid_inputs_do_not_read_secret_or_touch_ui(harness, updates):
    failure(lambda: capture(harness, **updates), "CAPTURE_INPUT_INVALID")
    assert harness[3].reads == [] and harness[2].calls == []


def load_cli():
    path = Path(__file__).parents[4] / "scripts" / "qq_guest_profile_probe.py"
    spec = importlib.util.spec_from_file_location("profile_capture_cli_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_capture_forwards_total_deadline_without_key_creation(monkeypatch, tmp_path):
    cli = load_cli()
    calls = []
    monkeypatch.setattr(cli, "capture_current_profile", lambda *a, **k: calls.append((a, k)) or {"safe": True})
    deadline = START + timedelta(seconds=90)
    assert cli.capture("helper", pid=1, hwnd=2, vault=tmp_path,
        environment_fingerprint="e" * 64, selector_pack_version="v", deadline=deadline) == {"safe": True}
    assert calls[0][1]["deadline"] == deadline
    assert calls[0][1]["key_name"] == "qq.identity.hmac"


def test_cli_explicit_initialization_never_calls_capture(monkeypatch, tmp_path, capsys):
    cli = load_cli()
    created = []
    class InitStore:
        def __init__(self, root):
            assert root == tmp_path
        def get_or_create_hmac_key(self, name):
            created.append(name)
            return KEY
    monkeypatch.setattr(cli, "WindowsDPAPISecretStore", InitStore)
    monkeypatch.setattr(cli, "capture", lambda *_a, **_k: pytest.fail("initialization must not inspect QQ"))
    monkeypatch.setattr(sys, "argv", ["probe", "--vault", str(tmp_path), "--initialize-key"])
    assert cli.main() == 0
    assert created == ["qq.identity.hmac"]
    assert json.loads(capsys.readouterr().out) == {"succeeded": True, "status": "IDENTITY_KEY_READY"}
