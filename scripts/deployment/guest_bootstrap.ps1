[CmdletBinding(SupportsShouldProcess)]
param(
  [Parameter(Mandatory)][string]$ProjectPath,
  [string]$EntryPoint='scripts/run_vm_runtime.py',
  [string]$PythonExecutable
)

$ErrorActionPreference='Stop'
$projectToml=Join-Path $ProjectPath 'pyproject.toml'
if(-not (Test-Path -LiteralPath $projectToml -PathType Leaf)){throw 'ProjectPath lacks pyproject.toml'}
if(-not $PSCmdlet.ShouldProcess($ProjectPath,'create guest venv and install extras')){return}

if($PSBoundParameters.ContainsKey('PythonExecutable')){
  if(-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)){throw "PythonExecutable does not exist or is not a file: $PythonExecutable"}
  $interpreter=(Resolve-Path -LiteralPath $PythonExecutable -ErrorAction Stop).Path
  $interpreterArgs=@()
}else{
  $interpreter='py'
  $interpreterArgs=@('-3.12')
}

$versionOutput=(& $interpreter @interpreterArgs --version 2>&1 | Out-String).Trim()
if($LASTEXITCODE -ne 0){throw "Python interpreter failed --version: $versionOutput"}
$match=[regex]::Match($versionOutput,'Python\s+(\d+)\.(\d+)(?:\.\d+)?')
if(-not $match.Success){throw "Could not determine Python version from: $versionOutput"}
$major=[int]$match.Groups[1].Value
$minor=[int]$match.Groups[2].Value
if($major -ne 3 -or $minor -lt 12){throw "Python >= 3.12 is required; found $versionOutput"}

$venvPath=Join-Path $ProjectPath '.venv'
& $interpreter @interpreterArgs -m venv $venvPath
if($LASTEXITCODE){throw 'venv creation failed'}
$python=Join-Path $venvPath 'Scripts\python.exe'
& $python -m pip install --upgrade pip
if($LASTEXITCODE){throw 'pip upgrade failed'}
& $python -m pip install "$ProjectPath[web,llm,qq-vm]"
if($LASTEXITCODE){throw 'dependency install failed'}
Write-Output "Installed with $versionOutput; configure DPAPI vault then start manually: $EntryPoint"
