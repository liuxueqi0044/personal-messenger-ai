[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$WheelPath,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')][string]$ReleaseId,
    [string]$StagingRoot,
    [string]$SelectorPackPath,
    [string]$HybridProbeHelperPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if ([string]::IsNullOrWhiteSpace($StagingRoot)) {
    $StagingRoot = Join-Path $projectRoot 'outputs\guest-local-releases'
}
$wheel = (Resolve-Path -LiteralPath $WheelPath -ErrorAction Stop).Path
if ([string]::IsNullOrWhiteSpace($SelectorPackPath)) {
    $SelectorPackPath = Join-Path (Split-Path $projectRoot -Parent) 'qq-vm\deploy-media\qq-runtime-session-final-cc8a7201\selector-pack-session-1.json'
}
$selectorPack = (Resolve-Path -LiteralPath $SelectorPackPath -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $selectorPack -PathType Leaf)) { throw "Selector pack must be a file: $selectorPack" }
if (-not (Test-Path -LiteralPath $StagingRoot)) { New-Item -ItemType Directory -Path $StagingRoot -Force | Out-Null }
$release = Join-Path (Resolve-Path -LiteralPath $StagingRoot -ErrorAction Stop).Path $ReleaseId
if (Test-Path -LiteralPath $release) { throw "Release staging directory already exists: $release" }

$sources = [ordered]@{
    'personal_messenger_ai-0.1.0-py3-none-any.whl' = $wheel
    'run_vm_runtime.py' = (Join-Path $projectRoot 'scripts\run_vm_runtime.py')
    'run_vm_runtime_v2.py' = (Join-Path $projectRoot 'scripts\run_vm_runtime_v2.py')
    'release_qq_observation_quarantine.py' = (Join-Path $projectRoot 'scripts\release_qq_observation_quarantine.py')
    're_evaluate_planning_job.py' = (Join-Path $projectRoot 'scripts\re_evaluate_planning_job.py')
    'qq_session_observed_bootstrap_guest.py' = (Join-Path $PSScriptRoot 'qq_session_observed_bootstrap_guest.py')
    'build_session_observed_runtime_guest.py' = (Join-Path $PSScriptRoot 'build_session_observed_runtime_guest.py')
    'build_visual_selection_acceptance_config_guest.py' = (Join-Path $PSScriptRoot 'build_visual_selection_acceptance_config_guest.py')
    'promote_visual_selection_config_guest.py' = (Join-Path $PSScriptRoot 'promote_visual_selection_config_guest.py')
    'reanchor_message_cursor_guest.py' = (Join-Path $PSScriptRoot 'reanchor_message_cursor_guest.py')
    'reconcile_terminal_send_lane_guest.py' = (Join-Path $PSScriptRoot 'reconcile_terminal_send_lane_guest.py')
    'sanitize_pacing_audit_guest.py' = (Join-Path $PSScriptRoot 'sanitize_pacing_audit_guest.py')
    'qq_visual_selection_acceptance_guest.py' = (Join-Path $PSScriptRoot 'qq_visual_selection_acceptance_guest.py')
    'qq_one_shot_deepseek_reply_guest.py' = (Join-Path $PSScriptRoot 'qq_one_shot_deepseek_reply_guest.py')
    'qq_session_runtime_supervisor_guest.py' = (Join-Path $PSScriptRoot 'qq_session_runtime_supervisor_guest.py')
    'guest_focus_helper.py' = (Join-Path $PSScriptRoot 'guest_focus_helper.py')
    'activate_default_rulepack_guest.py' = (Join-Path $PSScriptRoot 'activate_default_rulepack_guest.py')
    'qq_window_metadata_guest.py' = (Join-Path $PSScriptRoot 'qq_window_metadata_guest.py')
    'selector-pack-session-1.json' = $selectorPack
    'Install-GuestLocalRelease.ps1' = (Join-Path $PSScriptRoot 'Install-GuestLocalRelease.ps1')
    'Start-GuestLocalRuntime.ps1' = (Join-Path $PSScriptRoot 'Start-GuestLocalRuntime.ps1')
    'Test-GuestLocalRelease.ps1' = (Join-Path $PSScriptRoot 'Test-GuestLocalRelease.ps1')
    'GUEST_LOCAL_RELEASE_CONTRACT.md' = (Join-Path $PSScriptRoot 'GUEST_LOCAL_RELEASE_CONTRACT.md')
}
if (-not [string]::IsNullOrWhiteSpace($HybridProbeHelperPath)) {
    $hybridHelper = (Resolve-Path -LiteralPath $HybridProbeHelperPath -ErrorAction Stop).Path
    if ((Split-Path $hybridHelper -Leaf) -ne 'QQ.UiaProbe.exe') { throw 'Hybrid helper must be QQ.UiaProbe.exe' }
    $sources['QQ.UiaProbe.exe'] = $hybridHelper
    # The self-contained single-file WPF publish leaves these native runtime
    # dependencies beside the executable. Freeze the complete tested payload.
    foreach ($dependency in @('D3DCompiler_47_cor3.dll','PenImc_cor3.dll','PresentationNative_cor3.dll','vcruntime140_cor3.dll','wpfgfx_cor3.dll')) {
        $sources[$dependency] = Join-Path (Split-Path $hybridHelper -Parent) $dependency
    }
}
foreach ($source in $sources.Values) {
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Required release source is missing: $source" }
}

New-Item -ItemType Directory -Path $release -Force | Out-Null
try {
    foreach ($item in $sources.GetEnumerator()) { Copy-Item -LiteralPath $item.Value -Destination (Join-Path $release $item.Key) -ErrorAction Stop }
    $files = foreach ($name in $sources.Keys) {
        $path = Join-Path $release $name
        [ordered]@{ path = $name; sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash; length = (Get-Item -LiteralPath $path).Length }
    }
    $manifest = [ordered]@{ schema = 'pmai-guest-local-release-v1'; release_id = $ReleaseId; created_utc = [DateTime]::UtcNow.ToString('o'); files = @($files) }
    [IO.File]::WriteAllText((Join-Path $release 'manifest.json'), ($manifest | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $release 'Test-GuestLocalRelease.ps1') -ReleaseRoot $release | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Release manifest validation failed' }
    Get-ChildItem -LiteralPath $release -File | ForEach-Object { $_.IsReadOnly = $true }
    Write-Output "Frozen guest-local release: $release"
} catch {
    throw
}
