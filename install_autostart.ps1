# Registers two scheduled tasks that start at boot (no login required):
#   OCR-Server        -> start_ocr.cmd (the 2-worker OCR service)
#   Cloudflare-Tunnel -> cloudflared tunnel run ocr-tunnel
# Run this ONCE in an ELEVATED (Administrator) PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
$ErrorActionPreference = "Stop"

$proj = "D:\projects\paddleocr-prototype"
$cf   = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$user = "$env:USERDOMAIN\$env:USERNAME"

# S4U = "run whether user is logged on or not", without storing a password.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Highest
$trigger   = New-ScheduledTaskTrigger -AtStartup
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "OCR-Server" -Force -Principal $principal -Trigger $trigger -Settings $settings `
    -Action (New-ScheduledTaskAction -Execute "$proj\start_ocr.cmd" -WorkingDirectory $proj) `
    -Description "PaddleOCR multi-worker service (127.0.0.1:8000)"

Register-ScheduledTask -TaskName "Cloudflare-Tunnel" -Force -Principal $principal -Trigger $trigger -Settings $settings `
    -Action (New-ScheduledTaskAction -Execute $cf -Argument "tunnel run ocr-tunnel") `
    -Description "Cloudflare tunnel for ocr.voiceaccountant.com"

Write-Host "`nRegistered tasks (start at boot):"
Get-ScheduledTask -TaskName "OCR-Server","Cloudflare-Tunnel" | Format-Table TaskName, State
Write-Host "Start them now without rebooting:  Start-ScheduledTask OCR-Server ; Start-ScheduledTask Cloudflare-Tunnel"
