[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidateScript({Test-Path -LiteralPath $_ -PathType Leaf})][string]$IsoPath, [Parameter(Mandatory=$true)][ValidatePattern('^[0-9A-Fa-f]{64}$')][string]$ExpectedSha256)
$actual=(Get-FileHash -LiteralPath $IsoPath -Algorithm SHA256).Hash
[pscustomobject]@{ path=(Resolve-Path -LiteralPath $IsoPath).Path; sha256=$actual; matches=($actual -eq $ExpectedSha256) } | ConvertTo-Json
if($actual -ne $ExpectedSha256){ throw 'ISO SHA-256 mismatch.' }
