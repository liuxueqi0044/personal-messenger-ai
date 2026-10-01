from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not shutil.which("powershell.exe"),
    reason="Guest release packaging requires Windows PowerShell",
)
DEPLOYMENT = Path(__file__).resolve().parents[2] / "scripts" / "deployment"


def powershell(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    # Python does not apply PowerShell Core's automatic module-path filtering
    # when it launches Windows PowerShell; let 5.1 select its own built-ins.
    env = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(DEPLOYMENT / script), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_explicit_selector_pack_builds_and_manifest_detects_tampering(tmp_path: Path):
    wheel = tmp_path / "synthetic.whl"
    wheel.write_bytes(b"synthetic packaging fixture")
    selector = tmp_path / "private selector.json"
    selector.write_text('{"fixture": "local selector"}', encoding="utf-8")
    staging = tmp_path / "new checkout releases"
    release = staging / "candidate-1"
    try:
        result = powershell(
            "Build-GuestLocalRelease.ps1", "-WheelPath", str(wheel),
            "-ReleaseId", release.name, "-StagingRoot", str(staging),
            "-SelectorPackPath", str(selector),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
        entry = next(e for e in manifest["files"] if e["path"] == "selector-pack-session-1.json")
        installed_selector = release / entry["path"]
        assert installed_selector.read_bytes() == selector.read_bytes()
        assert entry["sha256"].lower() == hashlib.sha256(selector.read_bytes()).hexdigest()
        installed_selector.chmod(stat.S_IWRITE | stat.S_IREAD)
        installed_selector.write_text("tampered", encoding="utf-8")
        validation = powershell("Test-GuestLocalRelease.ps1", "-ReleaseRoot", str(release))
        assert validation.returncode == 2
    finally:
        # Frozen release files are read-only; restore fixture permissions for pytest.
        if release.exists():
            for file in release.iterdir():
                if file.is_file():
                    file.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_missing_explicit_selector_does_not_create_release(tmp_path: Path):
    wheel = tmp_path / "synthetic.whl"
    wheel.write_bytes(b"synthetic packaging fixture")
    staging = tmp_path / "releases"
    result = powershell(
        "Build-GuestLocalRelease.ps1", "-WheelPath", str(wheel),
        "-ReleaseId", "candidate-1", "-StagingRoot", str(staging),
        "-SelectorPackPath", str(tmp_path / "missing.json"),
    )
    assert result.returncode != 0
    assert not staging.exists()
