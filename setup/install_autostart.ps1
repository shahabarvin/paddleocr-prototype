<#
.SYNOPSIS
  Register three scheduled tasks (no login required):
    OCR-Server        -> manage\start_ocr.cmd (the multi-worker OCR service)  [at boot]
    Cloudflare-Tunnel -> cloudflared tunnel run <TunnelName>           [at boot]
    OCR-Watchdog      -> manage\watchdog.ps1 (restart if down)               [every 3 min]

  Portable: all paths are derived at runtime, so it works after re-cloning to a
  new machine/folder. Run ONCE in an ELEVATED (Administrator) PowerShell:

    powershell -ExecutionPolicy Bypass -File .\setup\install_autostart.ps1
    powershell -ExecutionPolicy Bypass -File .\setup\install_autostart.ps1 -TunnelName ocr2-tunnel

  Remove with:
    Unregister-ScheduledTask OCR-Server,Cloudflare-Tunnel,OCR-Watchdog -Confirm:$false
#>
param(
  [string]$TunnelName = "ocr-tunnel"
)
$ErrorActionPreference = "Stop"

$proj = Split-Path $PSScriptRoot -Parent   # repo root (this file lives in setup\)
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
    -Action (New-ScheduledTaskAction -Execute (Join-Path $proj "manage\start_ocr.cmd") -WorkingDirectory $proj) `
    -Description "PaddleOCR multi-worker service (127.0.0.1:8000)"

Register-ScheduledTask -TaskName "Cloudflare-Tunnel" -Force -Principal $principal -Trigger $trigger -Settings $settings `
    -Action (New-ScheduledTaskAction -Execute $cf -Argument "tunnel run $TunnelName") `
    -Description "Cloudflare tunnel '$TunnelName'"

# Self-heal: check /healthz every 3 minutes; if down, run restart.ps1.
$wtrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 3) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "OCR-Watchdog" -Force -Principal $principal -Trigger $wtrigger -Settings $settings `
    -Action (New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument ("-ExecutionPolicy Bypass -NonInteractive -File `"" + (Join-Path $proj "manage\watchdog.ps1") + "`"")) `
    -Description "Restart the OCR service if it goes down (health check every 3 min)"

Write-Host "`nRegistered:" -ForegroundColor Green
Get-ScheduledTask -TaskName "OCR-Server","Cloudflare-Tunnel","OCR-Watchdog" | Format-Table TaskName, State
Write-Host "Start now:  Start-ScheduledTask OCR-Server ; Start-ScheduledTask Cloudflare-Tunnel ; Start-ScheduledTask OCR-Watchdog"
