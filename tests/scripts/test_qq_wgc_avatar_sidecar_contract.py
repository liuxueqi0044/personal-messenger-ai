from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "scripts" / "qq_wgc_avatar_sidecar_managed"
PROJECT = SIDECAR / "QQ.WgcAvatar.csproj"
PROGRAM = SIDECAR / "Program.cs"
DLL = SIDECAR / "bin" / "Release" / "net10.0-windows10.0.26100.0" / "QQ.WgcAvatar.dll"


def test_managed_project_is_pinned_and_warning_clean() -> None:
    project = PROJECT.read_text(encoding="utf-8")
    assert "<UseAppHost>false</UseAppHost>" in project
    assert "<PlatformTarget>x64</PlatformTarget>" in project
    assert "<TreatWarningsAsErrors>true</TreatWarningsAsErrors>" in project
    assert re.search(
        r'PackageReference\s+Include="Vortice\.Direct3D11"\s+Version="3\.8\.3"',
        project,
    )


def test_managed_program_has_wgc_guards_and_no_desktop_input_path() -> None:
    source = PROGRAM.read_text(encoding="utf-8")
    for required in (
        "CreateForWindow",
        "CreateFreeThreaded",
        "IsCursorCaptureEnabled = false",
        "FrameCount = 3",
        "CryptographicOperations.ZeroMemory",
        "GetWindowThreadProcessId",
        "GetProcessById",
    ):
        assert required in source
    for forbidden in (
        "GetDC",
        "BitBlt",
        "PrintWindow",
        "SetForegroundWindow",
        "mouse_event",
        "keybd_event",
        "SendInput",
        "Clipboard",
        "GraphicsCapturePicker",
    ):
        assert forbidden not in source


@pytest.mark.parametrize(
    "argv,expected_status",
    [([], "KEY_STDIN_INVALID"), (["unexpected"], "ARGUMENTS_FORBIDDEN")],
)
def test_built_managed_sidecar_rejects_invalid_invocation(
    argv, expected_status
) -> None:
    if not DLL.exists():
        pytest.skip("managed sidecar artifact is not built")
    completed = subprocess.run(
        ["dotnet", str(DLL), *argv],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert completed.stdout == (
        '{"protocol":"qq-wgc-avatar-v1","succeeded":false,'
        f'"status":"{expected_status}"}}\n'
    )
