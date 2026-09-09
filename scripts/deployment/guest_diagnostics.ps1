[CmdletBinding()] param()
$qq=Get-Command QQ.exe -ErrorAction SilentlyContinue
$python=(py -3.12 --version 2>$null)
$uia=(py -3.12 -c "import importlib.util; print('installed' if importlib.util.find_spec('uiautomation') else 'missing')" 2>$null)
$dpi=(Get-ItemProperty 'HKCU:\Control Panel\Desktop' -ErrorAction SilentlyContinue).LogPixels
[pscustomobject]@{qq=if($qq){$qq.Source}else{'missing'};python=if($python){$python}else{'missing'};uiautomation=if($uia){$uia}else{'missing'};guest_marker=if($env:PERSONAL_MESSENGER_VM_GUEST){$env:PERSONAL_MESSENGER_VM_GUEST}else{'missing'};dpi=if($dpi){$dpi}else{'missing'};profile='missing unless runtime certification supplies one'}|ConvertTo-Json
