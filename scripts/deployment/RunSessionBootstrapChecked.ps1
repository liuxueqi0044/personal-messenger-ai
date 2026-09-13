param([ValidateSet(1,2)][int]$BindingIndex=1)
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
$statusPath='C:\PMAI\data\qq-session-bootstrap-launcher.json'
$python='C:\PMAI\app\.venv\Scripts\python.exe'
$root=Split-Path -Parent $MyInvocation.MyCommand.Path
$script=Join-Path $root 'qq_session_observed_bootstrap_guest.py'
$pack=Join-Path $root 'selector-pack-session-1.json'
$stderr='C:\PMAI\logs\qq-session-bootstrap.stderr.log'
$stdout='C:\PMAI\logs\qq-session-bootstrap.stdout.log'
$expectedScript='E2F5F14C61AF3BFE41D2100E3C49F719252C1D79D42048E585BB52931138AADA'
$expectedPack='99064F50690B28128C4FE1DA05A10A0884B3E365583CE3D330892F0AF11CCD81'
$status=[ordered]@{schema='pmai-qq-session-bootstrap-launcher-v1';state='running';stage='initialize';binding_index=$BindingIndex;succeeded=$false;python_exit_code=$null;report_exists=$false;error_code=$null}
function Save { $status.updated_utc=[DateTime]::UtcNow.ToString('o'); [IO.Directory]::CreateDirectory((Split-Path $statusPath))|Out-Null; [IO.File]::WriteAllText($statusPath,($status|ConvertTo-Json -Depth 4),[Text.UTF8Encoding]::new($false)) }
try {
 Save
 $status.stage='paths'; Save
 if(-not(Test-Path $python -PathType Leaf)-or -not(Test-Path $script -PathType Leaf)-or -not(Test-Path $pack -PathType Leaf)){throw 'BOOTSTRAP_PATH_MISSING'}
 $status.stage='hashes'; Save
 if((Get-FileHash $script -Algorithm SHA256).Hash -cne $expectedScript -or (Get-FileHash $pack -Algorithm SHA256).Hash -cne $expectedPack){throw 'BOOTSTRAP_MEDIA_HASH_MISMATCH'}
 [IO.Directory]::CreateDirectory('C:\PMAI\logs')|Out-Null
 $report=if($BindingIndex -eq 1){'C:\PMAI\data\qq-session-observed-bootstrap-1.json'}else{'C:\PMAI\data\qq-session-observed-bootstrap-2.json'}
 if(Test-Path $report -PathType Leaf){Remove-Item -LiteralPath $report -Force}
 $status.stage='python'; Save
 $env:PERSONAL_MESSENGER_VM_GUEST='1'
 & $python $script --selector-pack $pack --binding-id ('session-contact-'+$BindingIndex) --operator-observed-direct 1> $stdout 2> $stderr
 $status.python_exit_code=$LASTEXITCODE
 $status.report_exists=Test-Path $report -PathType Leaf
 $status.stderr_length=if(Test-Path $stderr){(Get-Item $stderr).Length}else{0}
 $status.stderr_sha256=if(Test-Path $stderr){(Get-FileHash $stderr -Algorithm SHA256).Hash}else{$null}
 if($status.report_exists){$status.report_sha256=(Get-FileHash $report -Algorithm SHA256).Hash}
 if($LASTEXITCODE -ne 0 -or -not $status.report_exists){throw 'BOOTSTRAP_PYTHON_FAILED'}
 $status.state='succeeded'; $status.stage='complete'; $status.succeeded=$true; Save; exit 0
} catch {
 $status.state='failed'; $status.succeeded=$false
 $status.error_code=if($_.Exception.Message -match '^[A-Z0-9_]+$'){$_.Exception.Message}else{'BOOTSTRAP_LAUNCH_FAILED'}
 $status.exception_type=$_.Exception.GetType().Name
 $status.script_line=$_.InvocationInfo.ScriptLineNumber
 Save; exit 2
}
