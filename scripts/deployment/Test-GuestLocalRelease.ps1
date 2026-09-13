[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ReleaseRoot,
    [switch]$VerifyInstalled
)

$ErrorActionPreference = 'Stop'
try {
    $root = (Resolve-Path -LiteralPath $ReleaseRoot -ErrorAction Stop).Path
    $manifestPath = Join-Path $root 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'RELEASE_MANIFEST_MISSING' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.schema -ne 'pmai-guest-local-release-v1' -or [string]::IsNullOrWhiteSpace($manifest.release_id)) { throw 'RELEASE_MANIFEST_INVALID' }
    if ($null -eq $manifest.files -or $manifest.files.Count -lt 1) { throw 'RELEASE_MANIFEST_EMPTY' }
    foreach ($entry in $manifest.files) {
        $relative = [string]$entry.path
        if ([string]::IsNullOrWhiteSpace($relative) -or [IO.Path]::IsPathRooted($relative) -or $relative.Contains('..')) { throw 'RELEASE_MANIFEST_PATH_INVALID' }
        $path = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "RELEASE_FILE_MISSING:$relative" }
        $item = Get-Item -LiteralPath $path
        if ($item.Length -ne [Int64]$entry.length -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -cne [string]$entry.sha256) { throw "RELEASE_FILE_HASH_MISMATCH:$relative" }
    }
    if ($VerifyInstalled) {
        $python = 'C:\PMAI\app\.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'GUEST_VENV_PYTHON_MISSING' }
        $wheel = Join-Path $root 'personal_messenger_ai-0.1.0-py3-none-any.whl'
        $program = @'
import hashlib
import pathlib
import sys
import sysconfig
import zipfile

wheel = pathlib.Path(sys.argv[1])
site = pathlib.Path(sysconfig.get_path("purelib"))
if not site.is_dir():
    raise RuntimeError("SITE_PACKAGES_MISSING")
with zipfile.ZipFile(wheel) as archive:
    members = [name for name in archive.namelist()
               if name.startswith("messenger_ai/") and name.endswith(".py")]
    mismatches = []
    for name in members:
        installed = site / name
        if not installed.is_file() or hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(archive.read(name)).digest():
            mismatches.append(name)
if mismatches:
    print("INSTALLED_PACKAGE_MISMATCH:" + ",".join(mismatches[:5]))
    raise SystemExit(3)
print("INSTALLED_PACKAGE_MATCH:" + str(len(members)))
'@
        $result = $program | & $python - $wheel 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw ('INSTALLED_PACKAGE_MISMATCH:' + $result.Trim()) }
    }
    [pscustomobject]@{ state = 'succeeded'; release_id = $manifest.release_id; verified_file_count = @($manifest.files).Count; installed_package_verified = [bool]$VerifyInstalled }
    exit 0
} catch {
    [pscustomobject]@{ state = 'failed'; error_code = $_.Exception.Message; exception_type = $_.Exception.GetType().Name }
    exit 2
}
