[CmdletBinding(SupportsShouldProcess)]
param(
  [string]$VmName='PersonalMessengerQQ',
  [ValidateRange(0,1024)][int]$MinimumHostReserveGiB=6,
  [ValidateRange(0,1024)][int]$VirtualizationOverheadGiB=1,
  [switch]$Start
)
$ErrorActionPreference = 'Stop'
$vbox='C:\Program Files\Oracle\VirtualBox\VBoxManage.exe'
if(!(Test-Path $vbox)){throw 'VBoxManage missing'}
$info=& $vbox showvminfo $VmName --machinereadable 2>$null
if($LASTEXITCODE){throw "VM not found: $VmName"}
$memoryLine=$info|Where-Object{$_ -match '^memory='}|Select-Object -First 1
if(!$memoryLine){throw 'Blocked: VM memory configuration is absent'}
$guestMB=[int](($memoryLine -split '=')[1].Trim('"'))
if($guestMB -le 0){throw 'Blocked: VM memory configuration is invalid'}
$stateLine=$info|Where-Object{$_ -match '^VMState='}|Select-Object -First 1
$isRunning=$stateLine -match 'running'
$freeBytes=(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory*1KB
$requiredBytes=(($guestMB/1024)+$MinimumHostReserveGiB+$VirtualizationOverheadGiB)*1GB
$ready=$freeBytes -ge $requiredBytes
[pscustomobject]@{vm=$VmName;guest_memory_mb=$guestMB;free_host_gib=[math]::Round($freeBytes/1GB,2);required_host_gib=[math]::Round($requiredBytes/1GB,2);ready=$ready;already_running=$isRunning;mode=if($Start){'start requested'}else{'check only'};note='Guest configured memory is not a peak-process guarantee. Dynamic disk is not RAM.'}|ConvertTo-Json
if($isRunning){return}
if(!$ready){throw 'Blocked: insufficient free host memory. Close idle applications or keep the VM stopped.'}
if($Start -and $PSCmdlet.ShouldProcess($VmName,'Start headless VM')){& $vbox startvm $VmName --type headless;if($LASTEXITCODE){throw 'VBoxManage startvm failed'}}
