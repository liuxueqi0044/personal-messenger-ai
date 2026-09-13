[CmdletBinding()]
param(
  [string]$ProjectPath='.',
  [string]$QQPath
)

$ErrorActionPreference='Stop'
function Get-FileVersion([string]$Path){
  if(-not $Path){return 'missing'}
  try{return (Get-Item -LiteralPath $Path -ErrorAction Stop).VersionInfo.ProductVersion}catch{return 'unavailable'}
}
function Find-QQPath {
  $process=Get-Process -Name 'QQ' -ErrorAction SilentlyContinue | Where-Object {$_.Path} | Select-Object -First 1
  if($process){return [pscustomobject]@{Path=$process.Path;Source='process'}}
  foreach($key in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')){
    foreach($entry in @(Get-ItemProperty -Path $key -ErrorAction SilentlyContinue | Where-Object {$_.DisplayName -match '(^QQ$|^Tencent QQ|腾讯QQ)'})){
      $candidates=@()
      if($entry.InstallLocation){$candidates+=Join-Path $entry.InstallLocation 'QQ.exe'}
      if($entry.DisplayIcon){$candidates+=(($entry.DisplayIcon -split ',')[0]).Trim().Trim('"')}
      foreach($candidate in $candidates){
        if($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)){return [pscustomobject]@{Path=$candidate;Source='registry'}}
      }
    }
  }
  foreach($candidate in @('C:\Program Files\Tencent\QQNT\QQ.exe','C:\Program Files (x86)\Tencent\QQNT\QQ.exe')){
    if(Test-Path -LiteralPath $candidate -PathType Leaf){return [pscustomobject]@{Path=$candidate;Source='common-path'}}
  }
  return [pscustomobject]@{Path=$null;Source='missing'}
}

if($PSBoundParameters.ContainsKey('QQPath')){
  if(-not (Test-Path -LiteralPath $QQPath -PathType Leaf)){throw "QQPath does not exist or is not a file: $QQPath"}
  $qq=[pscustomobject]@{Path=(Resolve-Path -LiteralPath $QQPath -ErrorAction Stop).Path;Source='parameter'}
}else{$qq=Find-QQPath}
$venv=Join-Path $ProjectPath '.venv\Scripts\python.exe'
$pythonVersion=if(Test-Path -LiteralPath $venv -PathType Leaf){(& $venv --version 2>$null | Out-String).Trim()}else{'missing'}
function Test-PythonModule([string]$Module){
  if(-not (Test-Path -LiteralPath $venv -PathType Leaf)){return 'missing'}
  $result=(& $venv -c "import importlib.util; print('installed' if importlib.util.find_spec('$Module') else 'missing')" 2>$null | Out-String).Trim()
  if($LASTEXITCODE -ne 0){return 'unavailable'}
  return $result
}
$dpi=(Get-ItemProperty 'HKCU:\Control Panel\Desktop' -ErrorAction SilentlyContinue).LogPixels
[pscustomobject][ordered]@{
  qq=[ordered]@{path=if($qq.Path){$qq.Path}else{'missing'};version=(Get-FileVersion $qq.Path);source=$qq.Source}
  python=[ordered]@{executable=if(Test-Path -LiteralPath $venv -PathType Leaf){$venv}else{'missing'};version=$pythonVersion}
  dependencies=[ordered]@{uiautomation=(Test-PythonModule 'uiautomation');pywin32=(Test-PythonModule 'win32api')}
  guest_marker=if($env:PERSONAL_MESSENGER_VM_GUEST){$env:PERSONAL_MESSENGER_VM_GUEST}else{'missing'}
  dpi=if($dpi){$dpi}else{'missing'}
  certification='not completed'
}|ConvertTo-Json -Depth 4
