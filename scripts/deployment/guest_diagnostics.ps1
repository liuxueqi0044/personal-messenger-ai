[CmdletBinding()] param([string]$ProjectPath='.')
$qqProcess=Get-Process QQ -ErrorAction SilentlyContinue|Select-Object -First 1
$qqPath=if($qqProcess -and $qqProcess.Path){$qqProcess.Path}else{@('C:\Program Files\Tencent\QQNT\QQ.exe','C:\Program Files (x86)\Tencent\QQNT\QQ.exe')|Where-Object{Test-Path $_}|Select-Object -First 1}
$qqVersion=if($qqPath){(Get-Item $qqPath).VersionInfo.ProductVersion}else{'missing'}
$venv=Join-Path $ProjectPath '.venv\Scripts\python.exe'
$python=if(Test-Path $venv){& $venv --version 2>$null}else{'missing'}
$uia=if(Test-Path $venv){& $venv -c "import importlib.util; print('installed' if importlib.util.find_spec('uiautomation') else 'missing')" 2>$null}else{'missing'}
$dpi=(Get-ItemProperty 'HKCU:\Control Panel\Desktop' -ErrorAction SilentlyContinue).LogPixels
[pscustomobject]@{qq_path=if($qqPath){$qqPath}else{'missing'};qq_version=$qqVersion;python=$python;uiautomation=$uia;guest_marker=if($env:PERSONAL_MESSENGER_VM_GUEST){$env:PERSONAL_MESSENGER_VM_GUEST}else{'missing'};dpi=if($dpi){$dpi}else{'missing'};certification='not completed'}|ConvertTo-Json
