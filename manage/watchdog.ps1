<#
.SYNOPSIS
  Self-heal: if the OCR service is down, run restart.ps1.

.DESCRIPTION
  Registered by install_autostart.ps1 as the scheduled task "OCR-Watchdog",
  which repeats every few minutes. Liveness = "is something listening on the
  port" (a local TCP check, so no HTTP/proxy false positives, and a service
  that is still warming up already holds the port and counts as up). If the
  port is not listening, it runs restart.ps1. Triggers are logged to watchdog.log.
#>
$ErrorActionPreference = "Continue"
$port = [int]($env:OCR_PORT); if (-not $port) { $port = 8000 }

$listening = [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)

if (-not $listening) {
  $stamp = [DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss")
  Add-Content -Path (Join-Path $PSScriptRoot "watchdog.log") -Encoding UTF8 `
    -Value "$stamp  port :$port not listening -> running restart.ps1"
  & (Join-Path $PSScriptRoot "restart.ps1") | Out-Null
}
