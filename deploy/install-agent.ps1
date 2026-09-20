param(
  [Parameter(Mandatory=$true)][string]$ServerUrl,
  [Parameter(Mandatory=$true)][string]$EnrollmentToken,
  [string]$InstallDir = "$env:LOCALAPPDATA\EdgeDisco"
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$config = @{ server_url=$ServerUrl; enrollment_token=$EnrollmentToken; scan_interval_seconds=300 } | ConvertTo-Json
Set-Content -Path "$InstallDir\agent.json" -Value $config -Encoding UTF8
Write-Host "Configuration written. Install EdgeDisco, then register the collector as a scheduled task in this user's interactive context. Do not run it as LocalSystem."
