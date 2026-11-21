<#
.SYNOPSIS
  One entry point behind the double-click files in this folder.

.DESCRIPTION
  status   - show whether the service is up (no admin needed)
  start    - start the service (and re-enable the watchdog)
  stop     - stop the service until Start is clicked or the PC reboots
  restart  - restart the service + tunnel
  workers  - set how many OCR workers run in parallel, then restart
  cleanup  - set after how many days job files are deleted (0 = never), then restart

  The service itself is run by Windows scheduled tasks, so nothing here needs to
  stay open. Everything except status needs admin rights and asks for them (UAC).
  Settings are stored in data\settings.local.cmd, which start_ocr.cmd loads.
#>
param(
  [Parameter(Mandatory)][ValidateSet("status","start","stop","restart","workers","cleanup")][string]$Action,
  [int]$Workers = 0,
  [int]$Days = -1,
  [switch]$NoPause          # for scripted use: do not wait for Enter at the end
)
$ErrorActionPreference = "Continue"
$root     = Split-Path $PSScriptRoot -Parent
$settings = Join-Path $root "data\settings.local.cmd"
$port     = 8000

function Wait-Close { if (-not $NoPause) { Write-Host ""; Read-Host "Press Enter to close" | Out-Null } }

# data\settings.local.cmd holds "set NAME=VALUE" lines.
function Get-Setting([string]$name, $default) {
  if (Test-Path $settings) {
    $m = Select-String -Path $settings -Pattern "^\s*set\s+$name=(.*)$" | Select-Object -Last 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim() }
  }
  return $default
}
function Set-Setting([string]$name, [string]$value) {
  New-Item -ItemType Directory -Force (Split-Path $settings) | Out-Null
  $lines = @()
  if (Test-Path $settings) { $lines = @(Get-Content $settings | Where-Object { $_ -match '^\s*set\s+\w+=' -and $_ -notmatch "^\s*set\s+$name=" }) }
  Set-Content -Path $settings -Encoding ASCII -Value (@("REM Machine-local OCR settings (written by the files in manage\).") + $lines + "set $name=$value")
}
function Format-Cleanup($d) { if ([int]$d -le 0) { "never (files are kept)" } else { "after $d day(s)" } }

function Show-Status {
  $listening = [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
  $ready = $false
  if ($listening) {
    try { $ready = (Invoke-RestMethod "http://127.0.0.1:$port/healthz" -TimeoutSec 4).ready } catch {}
  }
  if ($ready)         { Write-Host "Service : RUNNING (ready)" -ForegroundColor Green }
  elseif ($listening) { Write-Host "Service : STARTING (warming up)" -ForegroundColor Yellow }
  else                { Write-Host "Service : STOPPED" -ForegroundColor Red }
  Write-Host "Workers : $(Get-Setting 'OCR_WORKERS' 2) (configured)"
  Write-Host "Delete job files  : $(Format-Cleanup (Get-Setting 'OCR_FILES_KEEP_DAYS' 0))"
  foreach ($t in "OCR-Server","Cloudflare-Tunnel","OCR-Watchdog") {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    Write-Host ("{0,-18}: {1}" -f $t, $(if ($task) { $task.State } else { "not installed" }))
  }
  $gpu = & nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>$null
  if ($gpu) { Write-Host "GPU memory        : $gpu" }
}

if ($Action -eq "status") { Show-Status; Wait-Close; return }

# Ask for values before elevating, so the prompt is in the visible window.
if ($Action -eq "workers") {
  if ($Workers -lt 1) {
    Write-Host "Current workers: $(Get-Setting 'OCR_WORKERS' 2)"
    $Workers = [int](Read-Host "New number of workers (1-4)")
  }
  if ($Workers -lt 1 -or $Workers -gt 4) { Write-Host "Workers must be between 1 and 4." -ForegroundColor Red; Wait-Close; return }
}
if ($Action -eq "cleanup") {
  if ($Days -lt 0) {
    Write-Host "Job files (input, result, debug) are saved under output\."
    Write-Host "Currently deleted: $(Format-Cleanup (Get-Setting 'OCR_FILES_KEEP_DAYS' 0))"
    $answer = Read-Host "Delete files older than how many days? (0 = never delete)"
    if ($answer -notmatch '^\d+$') { Write-Host "Please enter a whole number." -ForegroundColor Red; Wait-Close; return }
    $Days = [int]$answer
  }
  if ($Days -gt 3650) { Write-Host "Days must be 0 to 3650." -ForegroundColor Red; Wait-Close; return }
}

# Everything below changes the service -> needs admin.
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
  $a = @("-NoProfile","-ExecutionPolicy","Bypass","-File","`"$PSCommandPath`"","-Action",$Action)
  if ($Workers -gt 0) { $a += @("-Workers",$Workers) }
  if ($Days -ge 0)    { $a += @("-Days",$Days) }
  Start-Process powershell -Verb RunAs -ArgumentList $a
  return
}

$restart = Join-Path $PSScriptRoot "restart.ps1"
if ($Action -eq "stop") {
  # Disable the watchdog first, or it would bring the service back within minutes.
  Disable-ScheduledTask -TaskName "OCR-Watchdog" -ErrorAction SilentlyContinue | Out-Null
  & $restart -StopOnly
  Write-Host "The service stays off until you run Start (or the PC reboots)." -ForegroundColor Yellow
} else {
  if ($Action -eq "workers") {
    Set-Setting "OCR_WORKERS" $Workers
    Write-Host "Workers set to $Workers. Restarting..." -ForegroundColor Cyan
  }
  if ($Action -eq "cleanup") {
    Set-Setting "OCR_FILES_KEEP_DAYS" $Days
    Write-Host "Job files will be deleted: $(Format-Cleanup $Days). Restarting..." -ForegroundColor Cyan
  }
  Enable-ScheduledTask -TaskName "OCR-Watchdog" -ErrorAction SilentlyContinue | Out-Null
  & $restart
}
Write-Host ""
Show-Status
Wait-Close
