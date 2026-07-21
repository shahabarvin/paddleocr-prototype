<#
.SYNOPSIS
  Create (or reuse) a Cloudflare Tunnel and point a hostname at the local service.

.DESCRIPTION
  One command to connect this machine to a subdomain on your Cloudflare account.
  Logs in if needed, creates the named tunnel, adds the DNS route, and writes
  ~/.cloudflared/config.yml (by tunnel UUID, so it runs without cert.pem later).

.EXAMPLE
  # first machine
  powershell -ExecutionPolicy Bypass -File .\setup_tunnel.ps1 -Hostname ocr.voiceaccountant.com

  # a second machine / second subdomain, same Cloudflare account
  powershell -ExecutionPolicy Bypass -File .\setup_tunnel.ps1 -Hostname ocr2.voiceaccountant.com -TunnelName ocr2-tunnel

  Then run it:  cloudflared tunnel run <TunnelName>   (or install_autostart.ps1 -TunnelName <TunnelName>)
#>
param(
  [Parameter(Mandatory=$true)][string]$Hostname,
  [string]$TunnelName = "ocr-tunnel",
  [int]$Port = 8000
)
$ErrorActionPreference = "Stop"

# locate cloudflared
$cf = $null
if (Get-Command cloudflared -ErrorAction SilentlyContinue) { $cf = (Get-Command cloudflared).Source }
foreach ($p in @("$env:ProgramFiles\cloudflared\cloudflared.exe",
                 "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe")) {
  if (-not $cf -and (Test-Path $p)) { $cf = $p }
}
if (-not $cf) { throw "cloudflared not found. Install: winget install --id Cloudflare.cloudflared" }
Write-Host "cloudflared: $cf" -ForegroundColor Cyan

$cfHome = Join-Path $env:USERPROFILE ".cloudflared"
$cert   = Join-Path $cfHome "cert.pem"

# 1) login (opens a browser; pick the domain to authorize)
if (-not (Test-Path $cert)) {
  Write-Host "Logging in to Cloudflare (a browser will open)..." -ForegroundColor Cyan
  & $cf tunnel login
  if (-not (Test-Path $cert)) { throw "Login did not produce $cert" }
}
Write-Host "authorized (cert.pem present)" -ForegroundColor Green

# 2) create tunnel if it doesn't exist
$tunnels = @()
try { $tunnels = & $cf tunnel list --output json 2>$null | ConvertFrom-Json } catch { $tunnels = @() }
$existing = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
if (-not $existing) {
  Write-Host "Creating tunnel '$TunnelName'..." -ForegroundColor Cyan
  & $cf tunnel create $TunnelName | Out-Host
  $tunnels = & $cf tunnel list --output json 2>$null | ConvertFrom-Json
  $existing = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
}
if (-not $existing) { throw "Could not find/create tunnel '$TunnelName'" }
$uuid = $existing.id
Write-Host "tunnel '$TunnelName' id: $uuid" -ForegroundColor Green

# 3) DNS route (creates the proxied CNAME for the hostname)
& $cf tunnel route dns $TunnelName $Hostname | Out-Host

# 4) write config.yml (by UUID + credentials-file -> no cert.pem needed at run time)
$creds = Join-Path $cfHome "$uuid.json"
$config = @"
tunnel: $uuid
credentials-file: $creds

ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:$Port
  - service: http_status:404
"@
Set-Content -Path (Join-Path $cfHome "config.yml") -Value $config -Encoding UTF8
Write-Host "`nWrote $cfHome\config.yml -> $Hostname => http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "Run it:   & `"$cf`" tunnel run $TunnelName"
Write-Host "Autostart: powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -TunnelName $TunnelName"
