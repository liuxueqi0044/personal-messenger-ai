"""Exercise the launcher's actual argument code without its VM/launch actions."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

LAUNCHER = Path(__file__).parents[2] / "scripts" / "deployment" / "Start-GuestLocalRuntime.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
GENERATION = "11111111-1111-4111-8111-111111111111"


def _argument_harness() -> str:
    source = LAUNCHER.read_text(encoding="utf-8")
    parameters = source[:source.index("$ErrorActionPreference = 'Stop'")]
    validation = source[
        source.index("    $contactIndices ="):
        source.index("    if (-not (Test-Path -LiteralPath $python -PathType Leaf))")
    ]
    arguments = source[
        source.index("        $builderArguments = @()"):
        source.index("        Invoke-Python 'build_runtime_config'")
    ]
    return parameters + "\n$ErrorActionPreference = 'Stop'\ntry {\n" + validation + arguments + (
        "\nConvertTo-Json -InputObject @($builderArguments) -Compress\nexit 0\n"
        "} catch { Write-Output $_.Exception.Message; exit 2 }\n"
    )


def test_launcher_argument_harness_excludes_operational_actions():
    harness = _argument_harness()
    assert "[int[]]$ContactIndex" in harness
    assert "@('--contact-index', [string]$index)" in harness
    for operation in ("Invoke-Python", "Save-Status", "Write-AtomicJson", "Start-Process"):
        assert operation not in harness


@pytest.mark.skipif(not POWERSHELL, reason="PowerShell launcher execution requires PowerShell")
@pytest.mark.parametrize("options, expected", [
    (f"-Phase BuildIsolated -IsolatedRecoveryGeneration {GENERATION} -ContactIndex 3",
     ["--contact-index", "3", "--isolated-recovery-generation", GENERATION]),
    (f"-Phase BuildIsolatedAndStart -IsolatedRecoveryGeneration {GENERATION} -ContactIndex @(4,3,4) -AdoptLatestInboundIndex 3 -VisualLabel @('3=synthetic3','4=synthetic4')",
     ["--contact-index", "3", "--contact-index", "4", "--adopt-latest-inbound-index", "3",
      "--isolated-recovery-generation", GENERATION, "--visual-label", "3=synthetic3", "--visual-label", "4=synthetic4"]),
    ("-Phase BuildAndStart -IncludeContact2 -AdditionalContactIndex 3",
     ["--include-contact-2", "--additional-contact-index", "3"]),
    (f"-Phase BuildIsolated -IsolatedRecoveryGeneration {GENERATION}",
     ["--isolated-recovery-generation", GENERATION]),
    ("-Phase BuildAndStart", []),
])
def test_launcher_forwards_exact_or_legacy_arguments(tmp_path, options, expected):
    harness = tmp_path / "arguments.ps1"
    harness.write_text(_argument_harness(), encoding="utf-8")
    escaped = str(harness).replace("'", "''")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", f"& '{escaped}' {options}"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == expected


@pytest.mark.skipif(not POWERSHELL, reason="PowerShell launcher execution requires PowerShell")
@pytest.mark.parametrize("options", [
    "-Phase BuildAndStart -ContactIndex 3",
    f"-Phase BuildIsolated -IsolatedRecoveryGeneration {GENERATION} -ContactIndex 3 -IncludeContact2",
    f"-Phase BuildIsolated -IsolatedRecoveryGeneration {GENERATION} -ContactIndex 3 -AdditionalContactIndex 4",
    f"-Phase BuildIsolated -IsolatedRecoveryGeneration {GENERATION} -ContactIndex 3 -AdoptLatestInboundIndex 1",
    f"-Phase BuildIsolated -IsolatedRecoveryGeneration {GENERATION} -ContactIndex 0",
    f"-Phase BuildIsolated -IsolatedRecoveryGeneration {GENERATION} -ContactIndex 10000",
    f"-Phase BuildIsolated -IsolatedRecoveryGeneration {GENERATION} -ContactIndex @()",
    "-Phase BuildIsolated -ContactIndex 3",
])
def test_launcher_rejects_invalid_selection(tmp_path, options):
    harness = tmp_path / "arguments.ps1"
    harness.write_text(_argument_harness(), encoding="utf-8")
    escaped = str(harness).replace("'", "''")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", f"& '{escaped}' {options}"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
