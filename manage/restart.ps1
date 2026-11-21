<#
.SYNOPSIS
  Bring the OCR service + Cloudflare tunnel back up (no reboot needed).

.DESCRIPTION
  Restarts the two scheduled tasks (OCR-Server, Cloudflare-Tunnel). Before the
  restart it FLUSHES every stale OCR python process -- Task Scheduler's stop does
  not kill the detached worker tree, and several uvicorn masters can co-bind the
  port, so an old generation keeps serving old code. The flush kills port owners,
  any python running serve.py, their whole worker trees, and orphaned uvicorn
  workers (that last sweep is skipped if any OpenWebUI python is running, so it is
  never touched). Then it waits until the service is ready.

  Run from an ELEVATED PowerShell: the workers run elevated, so a non-elevated
  shell cannot read their command lines or kill them. The OCR-Watchdog task runs
  this elevated automatically.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\manage\restart.ps1
#>
param(
  # Stop the OCR service and leave it stopped (the tunnel keeps running).
  # Used by manage\Stop.cmd; the watchdog must be disabled too or it restarts it.
  [switch]$StopOnly
)
$ErrorActionPreference = "Continue"
$port = [int]($env:OCR_PORT); if (-not $port) { $port = 8000 }
$tasks = if ($StopOnly) { @("OCR-Server") } else { @("OCR-Server","Cloudflare-Tunnel") }

function Get-PortOwners([int]$port) {
  @(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
    Select-Object -Expand OwningProcess -Unique | Where-Object { $_ -gt 4 })
}

# Kill every OCR master/worker so no stale generation survives the restart.
function Invoke-OcrFlush([int]$port) {
  $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'"
  $owPresent = [bool]($procs | Where-Object { $_.CommandLine -match 'open[-_]?webui' })
  $kill = New-Object System.Collections.Generic.HashSet[int]

  # seed: python running serve.py / the repo, plus whatever holds the port
  foreach ($p in $procs) {
    if ($p.CommandLine -and ($p.CommandLine -like '*serve.py*' -or $p.CommandLine -like '*paddleocr-prototype*')) { [void]$kill.Add([int]$p.ProcessId) }
  }
  foreach ($o in (Get-PortOwners $port)) { [void]$kill.Add([int]$o) }

  # expand to worker trees (real parent PID or multiprocessing spawn_main parent_pid)
  for ($i = 0; $i -lt 8; $i++) {
    $added = $false
    foreach ($p in $procs) {
      $pidv = [int]$p.ProcessId
      if ($kill.Contains($pidv)) { continue }
      $ppid = [int]$p.ParentProcessId
      $sp = $null
      if ($p.CommandLine -and ($p.CommandLine -match 'parent_pid=(\d+)')) { $sp = [int]$Matches[1] }
      if ($kill.Contains($ppid) -or ($sp -ne $null -and $kill.Contains($sp))) { [void]$kill.Add($pidv); $added = $true }
    }
    if (-not $added) { break }
  }

  # orphaned uvicorn workers (parent already dead) -- only safe when no OpenWebUI
  # python is running (it also uses multiprocessing spawn_main)
  if (-not $owPresent) {
    $alive = @{}; foreach ($p in $procs) { $alive[[int]$p.ProcessId] = $true }
    foreach ($p in $procs) {
      $pidv = [int]$p.ProcessId
      if ($kill.Contains($pidv)) { continue }
      if ($p.CommandLine -and ($p.CommandLine -match 'spawn_main') -and ($p.CommandLine -match 'parent_pid=(\d+)') -and (-not $alive.ContainsKey([int]$Matches[1]))) { [void]$kill.Add($pidv) }
    }
  }

  foreach ($id in @($kill)) { try { Stop-Process -Id $id -Force -ErrorAction Stop } catch {} }
  return @($kill).Count
}

Write-Host "Stopping tasks..." -ForegroundColor Cyan
foreach ($t in $tasks) { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue }
Start-Sleep 2

Write-Host "Flushing stale OCR workers..." -ForegroundColor Cyan
$n = Invoke-OcrFlush $port
Write-Host "  killed $n OCR process(es)"
Start-Sleep 2

# make sure the port is actually free before relaunching
for ($k = 0; $k -lt 4; $k++) {
  $own = Get-PortOwners $port
  if (-not $own) { break }
  foreach ($o in $own) {
    try { Stop-Process -Id $o -Force -ErrorAction Stop }
    catch { Write-Host "  (could not kill PID $o on :$port -- run elevated)" -ForegroundColor Yellow }
  }
  Start-Sleep 2
}

if ($StopOnly) {
  if (Get-PortOwners $port) { Write-Host "  port :$port is still held -- run elevated" -ForegroundColor Yellow }
  else { Write-Host "  STOPPED: nothing is listening on :$port" -ForegroundColor Green }
  return
}

Write-Host "Starting tasks..." -ForegroundColor Cyan
foreach ($t in $tasks) { Start-ScheduledTask -TaskName $t }

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
if (-not $ready) { Write-Host "  still not ready -- check: powershell -File .\setup\setup.ps1 -Check" -ForegroundColor Yellow }

try {
  $p = Invoke-WebRequest "https://ocr.voiceaccountant.com/healthz" -UseBasicParsing -TimeoutSec 20
  Write-Host "  PUBLIC: HTTP $($p.StatusCode) $($p.Content)" -ForegroundColor Green
} catch { Write-Host "  PUBLIC: not reachable yet ($($_.Exception.Message))" -ForegroundColor Yellow }
