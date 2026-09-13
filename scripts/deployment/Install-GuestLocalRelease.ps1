[CmdletBinding()]
param([Parameter(Mandatory)][string]$SourceReleaseRoot)

$ErrorActionPreference = 'Stop'
$statusPath = 'C:\PMAI\data\guest-local-release-install.json'
function Save-Status([hashtable]$Value) { $Value.updated_utc = [DateTime]::UtcNow.ToString('o'); New-Item -ItemType Directory -Force -Path (Split-Path $statusPath) | Out-Null; [IO.File]::WriteAllText($statusPath, ($Value | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false)) }
function Assert-Guest {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent(); $principal=[Security.Principal.WindowsPrincipal]::new($identity); $computer=Get-CimInstance Win32_ComputerSystem
    if ($env:COMPUTERNAME -ne 'PMAI-QQVM' -or $computer.Name -cne 'PMAI-QQVM' -or $computer.Model -notmatch 'VirtualBox' -or $computer.Manufacturer -notmatch '(Oracle|innotek)' -or $identity.Name -notlike '*\qqbot' -or $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'GUEST_GUARD_FAILED' }
}
function Assert-ReleaseChild([string]$Path, [string]$Root) {
    $full=[IO.Path]::GetFullPath($Path); $prefix=[IO.Path]::GetFullPath($Root).TrimEnd('\')+'\'
    if (-not $full.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'RELEASE_TARGET_PATH_INVALID' }
    return $full
}
$status = [ordered]@{ schema='pmai-guest-local-release-install-v1'; state='running'; succeeded=$false; stage='source_validation'; release_id=$null; error_code=$null }
try {
    Save-Status $status
    $status.stage='guest_guard'; Save-Status $status; Assert-Guest
    $source = (Resolve-Path -LiteralPath $SourceReleaseRoot -ErrorAction Stop).Path
    $testScript = Join-Path $source 'Test-GuestLocalRelease.ps1'
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $testScript -ReleaseRoot $source | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'SOURCE_RELEASE_INVALID' }
    $manifest = Get-Content -LiteralPath (Join-Path $source 'manifest.json') -Raw | ConvertFrom-Json
    $status.release_id = [string]$manifest.release_id
    if ($status.release_id -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') { throw 'RELEASE_ID_INVALID' }
    $targetRoot = 'C:\PMAI\app\releases'
    $target = Assert-ReleaseChild (Join-Path $targetRoot $status.release_id) $targetRoot
    $incoming = Assert-ReleaseChild (Join-Path $targetRoot ('.incoming-' + $status.release_id)) $targetRoot
    if (Test-Path -LiteralPath $target) {
        $status.stage = 'existing_source_match'; Save-Status $status
        $sourceManifestHash = (Get-FileHash -LiteralPath (Join-Path $source 'manifest.json') -Algorithm SHA256).Hash
        $targetManifest = Join-Path $target 'manifest.json'
        if (-not (Test-Path -LiteralPath $targetManifest -PathType Leaf) -or (Get-FileHash -LiteralPath $targetManifest -Algorithm SHA256).Hash -cne $sourceManifestHash) { throw 'EXISTING_RELEASE_SOURCE_MANIFEST_MISMATCH' }
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $target 'Test-GuestLocalRelease.ps1') -ReleaseRoot $target | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'EXISTING_RELEASE_INVALID' }
    }
    $status.stage='installed_wheel_validation'; Save-Status $status
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $testScript -ReleaseRoot $source -VerifyInstalled | Out-Host
    if ($LASTEXITCODE -ne 0) {
        $status.stage='install_local_wheel'; Save-Status $status
        $python='C:\PMAI\app\.venv\Scripts\python.exe'; $wheel=Join-Path $source 'personal_messenger_ai-0.1.0-py3-none-any.whl'
        & $python -m pip install --no-index --disable-pip-version-check --no-deps --force-reinstall $wheel 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'PIP_INSTALL_FAILED' }
        $status.stage='installed_wheel_revalidation'; Save-Status $status
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $testScript -ReleaseRoot $source -VerifyInstalled | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'INSTALLED_PACKAGE_MISMATCH_AFTER_INSTALL' }
    }
    if (Test-Path -LiteralPath $target) {
        $status.state='succeeded'; $status.succeeded=$true; $status.stage='already_present'; Save-Status $status; exit 0
    }
    if (Test-Path -LiteralPath $incoming) { throw 'INCOMING_RELEASE_ALREADY_EXISTS' }
    $status.stage='copy'; Save-Status $status
    New-Item -ItemType Directory -Force -Path $incoming | Out-Null
    Get-ChildItem -LiteralPath $source -Force | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $incoming -Recurse -Force }
    $status.stage='target_validation'; Save-Status $status
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $incoming 'Test-GuestLocalRelease.ps1') -ReleaseRoot $incoming | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'COPIED_RELEASE_INVALID' }
    Assert-ReleaseChild $target $targetRoot | Out-Null
    Move-Item -LiteralPath $incoming -Destination $target
    $status.state='succeeded'; $status.succeeded=$true; $status.stage='complete'; Save-Status $status
    exit 0
} catch {
    $status.state='failed'; $status.error_code=$_.Exception.Message; Save-Status $status; exit 2
}
