[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Validate','Bootstrap','BuildAndStart','BuildIsolated','BuildIsolatedAndStart','Pause','Resume','GracefulStop')][string]$Phase,
    [ValidatePattern('^session-contact-[1-9][0-9]{0,3}$')][string]$BindingId = 'session-contact-1',
    [ValidateNotNullOrEmpty()][ValidateRange(1, 9999)][int[]]$ContactIndex = @(),
    [switch]$IncludeContact2,
    [ValidateRange(1, 9999)][int[]]$AdditionalContactIndex = @(),
    [ValidateRange(1, 9999)][int[]]$AdoptLatestInboundIndex = @(),
    [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')][string]$IsolatedRecoveryGeneration,
    [ValidatePattern('^[1-9][0-9]{0,3}=.+$')][string[]]$VisualLabel = @()
)

$ErrorActionPreference = 'Stop'
$release = $PSScriptRoot
$python = 'C:\PMAI\app\.venv\Scripts\python.exe'
$statusPath = 'C:\PMAI\data\guest-local-release-launch.json'
$runtimeStatusPath = 'C:\PMAI\data\qq-session-runtime-status.json'
$controlRequestPath = 'C:\PMAI\data\qq-session-runtime-control-request.json'
$controlResultPath = 'C:\PMAI\data\qq-session-runtime-control-result.json'
function Save-Status([hashtable]$Value) { $Value.updated_utc=[DateTime]::UtcNow.ToString('o'); New-Item -ItemType Directory -Force -Path (Split-Path $statusPath) | Out-Null; [IO.File]::WriteAllText($statusPath, ($Value | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false)) }
function Write-AtomicJson([string]$Path, [hashtable]$Value) {
    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $temporary = Join-Path $directory ('.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class PMAIAtomicMove {
  [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool MoveFileEx(string existingFileName, string newFileName, int flags);
}
'@ -ErrorAction SilentlyContinue
        if (-not [PMAIAtomicMove]::MoveFileEx($temporary, $Path, 0x1 -bor 0x8)) { throw ('CONTROL_REQUEST_REPLACE_FAILED_' + [Runtime.InteropServices.Marshal]::GetLastWin32Error()) }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
    }
}
function Invoke-RuntimeControl([ValidateSet('pause','resume','graceful_stop')][string]$Action) {
    if (-not (Test-Path -LiteralPath $runtimeStatusPath -PathType Leaf)) { throw 'RUNTIME_STATUS_MISSING' }
    try { $runtimeStatus = Get-Content -Raw -LiteralPath $runtimeStatusPath | ConvertFrom-Json } catch { throw 'RUNTIME_STATUS_UNREADABLE' }
    if ($runtimeStatus.schema -ne 'pmai-qq-session-runtime-status-v2' -or -not [bool]$runtimeStatus.runtime_process_alive) { throw 'RUNTIME_NOT_RUNNING' }
    try { [guid]$targetRunId = [string]$runtimeStatus.run_id } catch { throw 'RUNTIME_STATUS_RUN_ID_INVALID' }
    $requestId = [guid]::NewGuid().ToString()
    $request = [ordered]@{ schema='pmai-qq-runtime-control-request-v1'; request_id=$requestId; target_run_id=$targetRunId.ToString(); action=$Action; requested_at=[DateTimeOffset]::UtcNow.ToString('o') }
    $status.stage=('control_' + $Action + '_requested'); Save-Status $status
    Write-AtomicJson $controlRequestPath $request
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    $acceptedPending = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $controlResultPath -PathType Leaf) {
            try { $result = Get-Content -Raw -LiteralPath $controlResultPath | ConvertFrom-Json } catch { $result = $null }
            if ($null -ne $result -and $result.schema -eq 'pmai-qq-runtime-control-result-v1' -and $result.request_id -eq $requestId -and $result.target_run_id -eq $targetRunId.ToString() -and $result.action -eq $Action) {
                if (-not [bool]$result.accepted) { $code = if ($result.error_code) { [string]$result.error_code } else { 'CONTROL_REJECTED' }; throw $code }
                if ($result.state -eq 'pausing' -and $Action -in @('pause','graceful_stop')) { $acceptedPending = $result; Start-Sleep -Milliseconds 200; continue }
                $expectedState = @{ pause='paused'; resume='running'; graceful_stop='stopping' }[$Action]
                if ($result.state -ne $expectedState) { throw ('CONTROL_RESULT_STATE_INVALID_' + [string]$result.state) }
                return $result
            }
        }
        Start-Sleep -Milliseconds 200
    }
    if ($null -ne $acceptedPending) {
        return [pscustomobject][ordered]@{
            schema='pmai-qq-runtime-control-result-v1'
            request_id=$requestId
            target_run_id=$targetRunId.ToString()
            action=$Action
            accepted=$true
            state='pausing'
            error_code='DRAIN_PENDING'
            completed_at=$null
        }
    }
    return [pscustomobject][ordered]@{
        schema='pmai-qq-runtime-control-result-v1'
        request_id=$requestId
        target_run_id=$targetRunId.ToString()
        action=$Action
        accepted=$null
        state='pending'
        error_code='TIMEOUT_UNKNOWN'
        completed_at=$null
    }
}
function Invoke-Python([string]$Stage, [string]$Script, [string[]]$Arguments) {
    $status.stage=$Stage; Save-Status $status
    $logDir='C:\PMAI\data\logs'; New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $log=Join-Path $logDir ('guest-local-release-' + $Stage + '.log')
    & $python (Join-Path $release $Script) @Arguments 2>&1 | Tee-Object -FilePath $log -Append
    if ($LASTEXITCODE -ne 0) { throw "PYTHON_EXIT_$LASTEXITCODE`:$Stage" }
}
$status=[ordered]@{schema='pmai-guest-local-release-launch-v1'; state='running'; succeeded=$false; release_root=$release; phase=$Phase; binding_id=$BindingId; include_contact_2=[bool]$IncludeContact2; additional_contact_indices=@($AdditionalContactIndex); adopt_latest_inbound_indices=@($AdoptLatestInboundIndex); isolated_recovery_generation=$IsolatedRecoveryGeneration; visual_selection_requested=($VisualLabel.Count -gt 0); stage='initialize'; error_code=$null; python_exit_code=$null}
try {
    Save-Status $status
    $contactIndices = @($ContactIndex | Sort-Object -Unique)
    $additionalIndices = @($AdditionalContactIndex | Sort-Object -Unique)
    $adoptIndices = @($AdoptLatestInboundIndex | Sort-Object -Unique)
    if (@($additionalIndices | Where-Object { $_ -in @(1, 2) }).Count -gt 0) { throw 'ADDITIONAL_CONTACT_INDEX_1_OR_2_INVALID' }
    $buildPhases = @('BuildAndStart','BuildIsolated','BuildIsolatedAndStart')
    $isolatedPhases = @('BuildIsolated','BuildIsolatedAndStart')
    if ($contactIndices.Count -gt 0 -and $Phase -notin $isolatedPhases) { throw 'CONTACT_INDEX_REQUIRES_ISOLATED_PHASE' }
    if ($contactIndices.Count -gt 0 -and ($IncludeContact2 -or $additionalIndices.Count -gt 0)) { throw 'CONTACT_INDEX_CONFLICTS_WITH_COMPATIBILITY_OPTIONS' }
    if ($contactIndices.Count -gt 0 -and @($adoptIndices | Where-Object { $_ -notin $contactIndices }).Count -gt 0) { throw 'ADOPT_LATEST_INBOUND_REQUIRES_SELECTED_CONTACT_INDEX' }
    if ($Phase -eq 'BuildAndStart' -and @($adoptIndices | Where-Object { $_ -notin $additionalIndices }).Count -gt 0) { throw 'ADOPT_LATEST_INBOUND_REQUIRES_ADDITIONAL_CONTACT_INDEX' }
    if ($Phase -in $isolatedPhases -and $contactIndices.Count -eq 0 -and @($adoptIndices | Where-Object { $_ -notin (@(1,2) + $additionalIndices) }).Count -gt 0) { throw 'ADOPT_LATEST_INBOUND_REQUIRES_SELECTED_CONTACT_INDEX' }
    if (($additionalIndices.Count -gt 0 -or $adoptIndices.Count -gt 0) -and $Phase -notin $buildPhases) { throw 'ADDITIONAL_CONTACT_OPTIONS_REQUIRE_BUILD_PHASE' }
    if ($Phase -in $isolatedPhases -and -not $IsolatedRecoveryGeneration) { throw 'ISOLATED_RECOVERY_GENERATION_REQUIRED' }
    if ($Phase -notin $isolatedPhases -and $IsolatedRecoveryGeneration) { throw 'ISOLATED_RECOVERY_GENERATION_REQUIRES_ISOLATED_PHASE' }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'GUEST_VENV_PYTHON_MISSING' }
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent(); $principal=[Security.Principal.WindowsPrincipal]::new($identity)
    if ($env:COMPUTERNAME -ne 'PMAI-QQVM' -or $identity.Name -notlike '*\qqbot' -or $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'GUEST_GUARD_FAILED' }
    $status.stage='manifest_validation'; Save-Status $status
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $release 'Test-GuestLocalRelease.ps1') -ReleaseRoot $release -VerifyInstalled | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'LOCAL_RELEASE_OR_INSTALLED_PACKAGE_INVALID' }
    if ($Phase -eq 'Validate') { $status.state='succeeded'; $status.succeeded=$true; $status.stage='complete'; Save-Status $status; exit 0 }
    if ($Phase -in @('Pause','Resume','GracefulStop')) {
        $action = @{ Pause='pause'; Resume='resume'; GracefulStop='graceful_stop' }[$Phase]
        $result = Invoke-RuntimeControl $action
        if ($result.state -eq 'pausing') {
            $status.state='pending'; $status.succeeded=$false; $status.stage='control_drain_pending'; $status.error_code='CONTROL_DRAIN_PENDING'; $status.runtime_control=$result; Save-Status $status
            $result | ConvertTo-Json -Depth 6
            exit 4
        }
        if ($result.state -eq 'pending') {
            $status.state='pending'; $status.succeeded=$false; $status.stage='control_timeout_unknown'; $status.error_code='CONTROL_TIMEOUT_UNKNOWN'; $status.runtime_control=$result; Save-Status $status
            $result | ConvertTo-Json -Depth 6
            exit 3
        }
        $status.state='succeeded'; $status.succeeded=$true; $status.stage='complete'; $status.runtime_control=$result; Save-Status $status
        $result | ConvertTo-Json -Depth 6
        exit 0
    }
    $env:PERSONAL_MESSENGER_VM_GUEST='1'
    if ($Phase -eq 'Bootstrap') {
        $index=$BindingId.Substring('session-contact-'.Length)
        $report=Join-Path 'C:\PMAI\data' ('qq-session-observed-bootstrap-' + $index + '.json')
        if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report -Force }
        Invoke-Python 'bootstrap' 'qq_session_observed_bootstrap_guest.py' @('--selector-pack',(Join-Path $release 'selector-pack-session-1.json'),'--binding-id',$BindingId,'--operator-observed-direct')
        if (-not (Test-Path -LiteralPath $report -PathType Leaf)) { throw 'BOOTSTRAP_REPORT_MISSING' }
    } else {
        if ($Phase -eq 'BuildAndStart' -and (Test-Path -LiteralPath 'C:\PMAI\data\runtime-session-1.json' -PathType Leaf)) {
            try { $currentConfig = Get-Content -Raw -LiteralPath 'C:\PMAI\data\runtime-session-1.json' | ConvertFrom-Json } catch { throw 'CURRENT_RUNTIME_CONFIG_UNREADABLE' }
            if ($null -ne $currentConfig.runtime_generation) { throw 'ISOLATED_RUNTIME_CONFIG_REQUIRES_EXPLICIT_TRANSITION' }
        }
        Invoke-Python 'assert_runtime_stopped' 'run_vm_runtime.py' @('--assert-runtime-stopped')
        $builderArguments = @()
        foreach ($index in $contactIndices) { $builderArguments += @('--contact-index', [string]$index) }
        if ($IncludeContact2) { $builderArguments += '--include-contact-2' }
        foreach ($index in $additionalIndices) { $builderArguments += @('--additional-contact-index', [string]$index) }
        foreach ($index in $adoptIndices) { $builderArguments += @('--adopt-latest-inbound-index', [string]$index) }
        if ($Phase -in $isolatedPhases) { $builderArguments += @('--isolated-recovery-generation', $IsolatedRecoveryGeneration) }
        foreach ($label in $VisualLabel) { $builderArguments += @('--visual-label', $label) }
        Invoke-Python 'build_runtime_config' 'build_session_observed_runtime_guest.py' $builderArguments
        $runtimeConfig = 'C:\PMAI\data\runtime-session-1.json'
        $runtimeConfigDigest = (Get-FileHash -LiteralPath $runtimeConfig -Algorithm SHA256).Hash.ToLowerInvariant()
        try { $builtConfig = Get-Content -Raw -LiteralPath $runtimeConfig | ConvertFrom-Json } catch { throw 'BUILT_RUNTIME_CONFIG_UNREADABLE' }
        if ($Phase -in $isolatedPhases -and $builtConfig.runtime_generation.generation_id -ne $IsolatedRecoveryGeneration) { throw 'BUILT_RUNTIME_GENERATION_MISMATCH' }
        Invoke-Python 'runtime_config_check' 'run_vm_runtime.py' @('--config',$runtimeConfig,'--check','--expected-config-sha256',$runtimeConfigDigest)
        if ($Phase -in @('BuildAndStart','BuildIsolatedAndStart')) {
            $supervisorArguments = @('--expected-config-sha256',$runtimeConfigDigest)
            if ($Phase -eq 'BuildIsolatedAndStart') { $supervisorArguments += @('--expected-generation-id',$IsolatedRecoveryGeneration) }
            Invoke-Python 'runtime_supervisor' 'qq_session_runtime_supervisor_guest.py' $supervisorArguments
        }
    }
    $status.state='succeeded'; $status.succeeded=$true; $status.stage='complete'; Save-Status $status; exit 0
} catch {
    $status.python_exit_code=$LASTEXITCODE; $status.state='failed'; $status.error_code=$_.Exception.Message; Save-Status $status; exit 2
}
