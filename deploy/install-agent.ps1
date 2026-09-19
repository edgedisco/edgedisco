param(
  [Parameter(Mandatory=$true)][string]$ServerUrl,
  [Parameter(Mandatory=$true)][string]$EnrollmentToken,
  [string]$InstallDir = "$env:ProgramData\AIInventory"
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$config = @{ server_url=$ServerUrl; enrollment_token=$EnrollmentToken; scan_interval_seconds=300 } | ConvertTo-Json
Set-Content -Path "$InstallDir\agent.json" -Value $config -Encoding UTF8
Write-Host "Configuration written. Install the Python package, then register ai-inventory as a Windows service with your endpoint-management tool."
