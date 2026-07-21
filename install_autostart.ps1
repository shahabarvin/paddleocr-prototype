<#
.SYNOPSIS
  Register two scheduled tasks that start at boot (no login required):
    OCR-Server        -> start_ocr.cmd (the multi-worker OCR service)
    Cloudflare-Tunnel -> cloudflared tunnel run <TunnelName>

  Portable: all paths are derived at runtime, so it works after re-cloning to a
  new machine/folder. Run ONCE in an ELEVATED (Administrator) PowerShell:

    powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
    powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -TunnelName ocr2-tunnel

  Remove with:  Unregister-ScheduledTask OCR-Server,Cloudflare-Tunnel -Confirm:$false
#>
param(
  [string]$TunnelName = "ocr-tunnel"
)
$ErrorActionPreference = "Stop"

$proj = $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"

# locate cloudflared
$cf = $null
if (Get-Command cloudflared -ErrorAction SilentlyContinue) { $cf = (Get-Command cloudflared).Source }
foreach ($p in @("$env:ProgramFiles\cloudflared\cloudflared.exe",
                 "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe")) {
  if (-not $cf -and (Test-Path $p)) { $cf = $p }
}
if (-not $cf) { throw "cloudflared not found. Install it (winget install Cloudflare.cloudflared) and set up the tunnel first (setup_tunnel.ps1)." }

# S4U = "run whether user is logged on or not", no stored password.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Highest
$trigger   = New-ScheduledTaskTrigger -AtStartup
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "OCR-Server" -Force -Principal $principal -Trigger $trigger -Settings $settings `
    -Action (New-ScheduledTaskAction -Execute (Join-Path $proj "start_ocr.cmd") -WorkingDirectory $proj) `
    -Description "PaddleOCR multi-worker service (127.0.0.1:8000)"

Register-ScheduledTask -TaskName "Cloudflare-Tunnel" -Force -Principal $principal -Trigger $trigger -Settings $settings `
    -Action (New-ScheduledTaskAction -Execute $cf -Argument "tunnel run $TunnelName") `
    -Description "Cloudflare tunnel '$TunnelName'"

Write-Host "`nRegistered (start at boot):" -ForegroundColor Green
Get-ScheduledTask -TaskName "OCR-Server","Cloudflare-Tunnel" | Format-Table TaskName, State
Write-Host "Start now without rebooting:  Start-ScheduledTask OCR-Server ; Start-ScheduledTask Cloudflare-Tunnel"
