[CmdletBinding(SupportsShouldProcess)]
param(
  [Parameter(Mandatory = $true)][ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })][string]$IsoPath,
  [string]$VmName = 'PersonalMessengerQQ',
  [int]$MemoryMB = 8192,
  [int]$CpuCount = 4,
  [int]$DiskSizeMB = 81920
)

$vbox = 'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe'
function Invoke-VBox([string[]]$Arguments) { & $vbox @Arguments; if ($LASTEXITCODE -ne 0) { throw "VBoxManage failed: $Arguments" } }
if (-not (Test-Path -LiteralPath $vbox)) { throw 'VirtualBox VBoxManage.exe was not found.' }
if ($MemoryMB -lt 4096 -or $CpuCount -lt 2 -or $DiskSizeMB -lt 65536) { throw 'Guest resource values are below the supported deployment floor.' }

# Review with -WhatIf first. It never starts the VM or installs Windows.
$proposal = [ordered]@{
  vm_name = $VmName; memory_mb = $MemoryMB; cpu_count = $CpuCount; disk_size_mb = $DiskSizeMB
  iso_path = (Resolve-Path -LiteralPath $IsoPath).Path; network = 'NAT'; clipboard = 'disabled'; drag_and_drop = 'disabled'
  display = 'fixed guest resolution and DPI must be recorded during certification'
  authorization_pending = 'Windows installation/activation and QQ guest login are user-confirmed steps.'
}
$proposal | ConvertTo-Json
if ($VmName -notmatch '^[A-Za-z0-9_-]{1,64}$') { throw 'VmName contains unsupported characters.' }
$help = ((& $vbox modifyvm --help 2>&1) -join "`n")
if ($LASTEXITCODE -ne 0 -or $help -notmatch '--tpm-type') { throw 'This VBoxManage does not advertise TPM support.' }
if (-not $PSCmdlet.ShouldProcess($VmName, 'Create a stopped QQ Windows guest')) { return }
$registered=& $vbox list vms; if($LASTEXITCODE -ne 0){throw 'VBoxManage list vms failed'}; $registered | Select-String -SimpleMatch ('"' + $VmName + '"') | ForEach-Object { throw "VM already exists: $VmName" }
$vmRoot = Join-Path (Split-Path -Parent $IsoPath) $VmName
if (Test-Path -LiteralPath $vmRoot) { throw "Refusing to reuse existing path: $vmRoot" }
Invoke-VBox -Arguments @('createvm','--name',$VmName,'--ostype','Windows11_64','--basefolder',(Split-Path -Parent $vmRoot),'--register')
Invoke-VBox -Arguments @('modifyvm',$VmName,'--memory',$MemoryMB,'--cpus',$CpuCount,'--firmware','efi','--tpm-type','2.0','--nic1','nat','--clipboard','disabled','--draganddrop','disabled','--audio','none','--usb','off','--vram','128')
Invoke-VBox @('createmedium','disk','--filename',(Join-Path $vmRoot "$VmName.vdi"),'--size',$DiskSizeMB,'--format','VDI','--variant','Standard')
Invoke-VBox @('storagectl',$VmName,'--name','SATA','--add','sata','--controller','IntelAhci')
Invoke-VBox @('storageattach',$VmName,'--storagectl','SATA','--port','0','--device','0','--type','hdd','--medium',(Join-Path $vmRoot "$VmName.vdi"))
Invoke-VBox @('storagectl',$VmName,'--name','IDE','--add','ide')
Invoke-VBox @('storageattach',$VmName,'--storagectl','IDE','--port','0','--device','0','--type','dvddrive','--medium',(Resolve-Path -LiteralPath $IsoPath))
