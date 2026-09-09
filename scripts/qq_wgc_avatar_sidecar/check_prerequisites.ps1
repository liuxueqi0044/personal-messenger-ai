[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$sdkVersion = '10.0.26100.0'
$missing = [System.Collections.Generic.List[string]]::new()
$programFilesX86 = ${env:ProgramFiles(x86)}
$vswhere = if ($programFilesX86) {
    Join-Path $programFilesX86 'Microsoft Visual Studio\Installer\vswhere.exe'
} else {
    $null
}

$installPath = $null
if ($vswhere -and (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
    $installPath = & $vswhere -latest -products '*' `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath 2>$null | Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($installPath)) {
    $missing.Add('VS2022_MSVC_V143')
}

$msbuild = $null
if ($installPath) {
    $candidate = Join-Path $installPath 'MSBuild\Current\Bin\MSBuild.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $msbuild = $candidate
    }
}
if (-not $msbuild) {
    $missing.Add('MSBUILD')
}

$cl = $null
if ($installPath) {
    $toolsRoot = Join-Path $installPath 'VC\Tools\MSVC'
    if (Test-Path -LiteralPath $toolsRoot -PathType Container) {
        $cl = Get-ChildItem -LiteralPath $toolsRoot -Directory |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'bin\Hostx64\x64\cl.exe' } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            Select-Object -First 1
    }
}
if (-not $cl) {
    $missing.Add('MSVC_X64_COMPILER')
}

$sdkRoot = if ($programFilesX86) { Join-Path $programFilesX86 'Windows Kits\10' } else { $null }
$requiredSdkPaths = @(
    (Join-Path $sdkRoot "Include\$sdkVersion\um\windows.graphics.capture.interop.h"),
    (Join-Path $sdkRoot "Include\$sdkVersion\um\windows.graphics.capture.h"),
    (Join-Path $sdkRoot "Lib\$sdkVersion\um\x64\windowsapp.lib"),
    (Join-Path $sdkRoot "Lib\$sdkVersion\um\x64\d3d11.lib"),
    (Join-Path $sdkRoot "Lib\$sdkVersion\um\x64\dxgi.lib")
)
if ($requiredSdkPaths | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }) {
    $missing.Add("WINDOWS_SDK_$sdkVersion`_X64")
}

if ($missing.Count -gt 0) {
    [pscustomobject]@{
        status = 'MISSING_TOOLCHAIN'
        required_sdk = $sdkVersion
        missing = @($missing | Select-Object -Unique)
    } | ConvertTo-Json -Compress
    exit 2
}

[pscustomobject]@{
    status = 'READY'
    required_sdk = $sdkVersion
    msbuild = $msbuild
    compiler = $cl
} | ConvertTo-Json -Compress
exit 0
