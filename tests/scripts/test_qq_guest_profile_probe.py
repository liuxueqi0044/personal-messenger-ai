import hashlib
import importlib.util
from pathlib import Path
import subprocess

import pytest


PATH = Path(__file__).parents[2] / "scripts" / "qq_guest_profile_probe.py"
SPEC = importlib.util.spec_from_file_location("qq_guest_profile_probe", PATH)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_invalid_helper_output_reports_only_safe_metadata(monkeypatch) -> None:
    raw = "private data that must never be returned"
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 7, raw, "secret stderr"))
    with pytest.raises(probe.GuestProfileProbeError) as caught:
        probe._run(["helper"], stage="header")
    exc = caught.value
    assert exc.code == "HELPER_INVALID_OUTPUT"
    assert exc.diagnostic == {
        "stage": "header", "exit_code": 7,
        "stdout_bytes": len(raw.encode()),
        "stdout_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "stderr_bytes": len(b"secret stderr"),
        "stderr_sha256": hashlib.sha256(b"secret stderr").hexdigest(),
    }
    assert raw not in repr(exc.diagnostic)
    assert "secret stderr" not in repr(exc.diagnostic)


def test_timeout_is_stage_classified(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 60)
    monkeypatch.setattr(probe.subprocess, "run", timeout)
    with pytest.raises(probe.GuestProfileProbeError) as caught:
        probe._run(["helper"], stage="profile")
    assert caught.value.code == "HELPER_TIMEOUT"
    assert caught.value.diagnostic == {"stage": "profile"}
