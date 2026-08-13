<#
.SYNOPSIS
  Bring the OCR service + Cloudflare tunnel back up (no reboot needed).

.DESCRIPTION
  Restarts the two scheduled tasks (OCR-Server, Cloudflare-Tunnel), frees port
  8000 if a stale worker is stuck, and waits until the service is ready. On boot
  the tasks start automatically; run this only to recover without rebooting.
  If a stuck worker refuses to die, run this from an ELEVATED PowerShell.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\restart.ps1
#>
$ErrorActionPreference = "Continue"
$port = [int]($env:OCR_PORT); if (-not $port) { $port = 8000 }

Write-Host "Stopping tasks..." -ForegroundColor Cyan
foreach ($t in "OCR-Server","Cloudflare-Tunnel") { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue }
Start-Sleep 2

# best-effort: free the port if a stale worker is still listening
Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
  Select-Object -Expand OwningProcess -Unique |
  ForEach-Object {
    try { Stop-Process -Id $_ -Force -ErrorAction Stop }
    catch { Write-Host "  (could not kill PID $_ -- try running elevated)" -ForegroundColor Yellow }
  }
Start-Sleep 1

Write-Host "Starting tasks..." -ForegroundColor Cyan
foreach ($t in "OCR-Server","Cloudflare-Tunnel") { Start-ScheduledTask -TaskName $t }

Write-Host "Waiting for the service to warm up..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep 4
  try {
    if ((Invoke-WebRequest "http://127.0.0.1:$port/healthz" -UseBasicParsing -TimeoutSec 3).Content -match 'ready.:true') {
      Write-Host "  UP: service ready on 127.0.0.1:$port  (~$(($i+1)*4)s)" -ForegroundColor Green
      $ready = $true; break
    }
  } catch {}
}
if (-not $ready) { Write-Host "  still not ready -- check: powershell -File .\setup.ps1 -Check" -ForegroundColor Yellow }

try {
  $p = Invoke-WebRequest "https://ocr.voiceaccountant.com/healthz" -UseBasicParsing -TimeoutSec 20
  Write-Host "  PUBLIC: HTTP $($p.StatusCode) $($p.Content)" -ForegroundColor Green
} catch { Write-Host "  PUBLIC: not reachable yet ($($_.Exception.Message))" -ForegroundColor Yellow }
