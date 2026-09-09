[CmdletBinding()]
param(
    [ValidateSet('Release')]
    [string]$Configuration = 'Release'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $root 'check_prerequisites.ps1')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$programFilesX86 = ${env:ProgramFiles(x86)}
$vswhere = Join-Path $programFilesX86 'Microsoft Visual Studio\Installer\vswhere.exe'
$installPath = & $vswhere -latest -products '*' `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath | Select-Object -First 1
$msbuild = Join-Path $installPath 'MSBuild\Current\Bin\MSBuild.exe'
& $msbuild (Join-Path $root 'qq_wgc_avatar_sidecar.vcxproj') `
    "/p:Configuration=$Configuration" '/p:Platform=x64' '/m' '/nologo'
exit $LASTEXITCODE
