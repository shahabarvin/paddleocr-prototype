<#
.SYNOPSIS
  One-command setup + health check (doctor/fixer) for the PaddleOCR service.

.DESCRIPTION
  Creates the venv, installs dependencies, installs the matching PaddlePaddle GPU
  build (unless -Cpu or no GPU), optionally warms the models, then runs health
  checks. Re-running is safe and idempotent, so it doubles as a fixer. Use -Check
  for a read-only diagnostic that installs nothing.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup.ps1            # full setup
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Warm      # + download/warm models
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Check     # doctor only
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Cpu       # CPU-only (skip GPU wheel)
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Cuda cu126
#>
param(
  [switch]$Check,
  [switch]$Cpu,
  [string]$Cuda = "cu129",
  [switch]$Warm
)
# Native tools (python/pip/nvidia-smi) print harmless INFO/notices to stderr;
# with 'Stop' PowerShell 5.1 would abort on those, so we run in 'Continue' and
# check exit codes explicitly for the steps that matter.
$ErrorActionPreference = "Continue"
$proj = $PSScriptRoot
$venv = Join-Path $proj ".venv"
$py   = Join-Path $venv "Scripts\python.exe"

function Info($m){ Write-Host $m -ForegroundColor Cyan }
function Ok($m)  { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Warn($m){ Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Bad($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red }
function Have($c){ [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function PyOut([string]$code){ $o = & $py -c $code 2>$null; return ($o | Out-String).Trim() }

function Find-Cloudflared {
  if (Have "cloudflared") { return (Get-Command cloudflared).Source }
  foreach ($p in @("$env:ProgramFiles\cloudflared\cloudflared.exe",
                   "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe")) {
    if (Test-Path $p) { return $p }
  }
  return $null
}

function Doctor {
  Info "`n== Health check =="
  $issues = 0

  if (Test-Path $py) {
    $ver = (& $py --version 2>$null | Out-String).Trim()
    if ($ver -match "3\.12") { Ok "venv python: $ver" } else { Warn "venv python is '$ver' (3.12 recommended)" }
  } else { Bad "venv missing -> run setup.ps1 (without -Check)"; return 1 }

  if ((PyOut "import importlib.util as u; print(all(u.find_spec(m) for m in ['paddleocr','paddle','fastapi','uvicorn','cv2']))") -eq "True") {
    Ok "core packages present (paddleocr, paddle, fastapi, uvicorn, cv2)"
  } else { Bad "missing core packages -> run setup.ps1"; $issues++ }

  if (Have "nvidia-smi") {
    $name = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
    Ok "NVIDIA GPU: $name"
    if ((PyOut "import paddle; print(paddle.device.is_compiled_with_cuda())") -eq "True") {
      Ok "paddle CUDA build, visible GPUs: $(PyOut 'import paddle; print(paddle.device.cuda.device_count())')"
    } else { Warn "paddle is the CPU build -> re-run setup.ps1 for GPU speed" }
  } else { Warn "no NVIDIA GPU detected -> will run on CPU (much slower)" }

  $models = Join-Path $env:USERPROFILE ".paddlex\official_models"
  if ((Test-Path $models) -and (Get-ChildItem $models -ErrorAction SilentlyContinue)) { Ok "models cached in $models" }
  else { Warn "models not downloaded yet (first run / -Warm downloads ~1-2 GB)" }

  $keys = Join-Path $proj "api_keys.json"
  if (Test-Path $keys) {
    $active = PyOut "import json;print(sum(1 for k in json.load(open(r'$keys',encoding='utf-8'))['keys'] if k.get('active')))"
    if ([int]$active -gt 0) { Ok "$active active API key(s)" } else { Warn "no ACTIVE API key -> .venv\Scripts\python keys.py create label" }
  } else { Warn "no api_keys.json -> auth ON but every request is 401 until: .venv\Scripts\python keys.py create label" }

  if (Find-Cloudflared) { Ok "cloudflared installed" } else { Warn "cloudflared not installed (needed only for the public tunnel)" }

  $tasks = @(Get-ScheduledTask -TaskName "OCR-Server","Cloudflare-Tunnel" -ErrorAction SilentlyContinue)
  if ($tasks.Count -eq 2) { Ok "autostart tasks registered (OCR-Server, Cloudflare-Tunnel)" }
  else { Warn "autostart not set up -> run install_autostart.ps1 as Admin" }

  try {
    $h = Invoke-WebRequest "http://127.0.0.1:8000/healthz" -UseBasicParsing -TimeoutSec 3
    Ok "service UP on 127.0.0.1:8000 ($($h.Content))"
  } catch { Warn "service not running on :8000 -> .venv\Scripts\python serve.py (or reboot for autostart)" }

  Info "`n== $(if($issues){"$issues blocking issue(s)"}else{'all good'}) =="
  return $issues
}

if ($Check) { exit (Doctor) }

# ---------------------------------------------------------------- setup ----
Info "== PaddleOCR setup =="

if (-not (Have "py")) { Bad "'py' launcher not found. Install Python 3.12 from https://www.python.org/downloads/"; exit 1 }

if (-not (Test-Path $py)) {
  Info "Creating venv with Python 3.12..."
  & py -3.12 -m venv $venv 2>$null
  if (-not (Test-Path $py)) { Bad "venv creation failed. Check: py -3.12 --version"; exit 1 }
}
Ok "venv ready"

Info "Installing requirements (this can take a while)..."
& $py -m pip install --upgrade pip *> $null
& $py -m pip install -r (Join-Path $proj "requirements.txt")
if ($LASTEXITCODE -ne 0) { Bad "pip install -r requirements.txt failed"; exit 1 }
if (Test-Path (Join-Path $proj "requirements-dev.txt")) { & $py -m pip install -r (Join-Path $proj "requirements-dev.txt") }
Ok "requirements installed"

$hasGpu = Have "nvidia-smi"
if ($Cpu -or -not $hasGpu) {
  if (-not $hasGpu) { Warn "no NVIDIA GPU -> keeping CPU build of PaddlePaddle" } else { Info "-Cpu set -> keeping CPU build" }
} elseif ((PyOut "import paddle; print(paddle.device.is_compiled_with_cuda())") -eq "True") {
  Ok "paddlepaddle-gpu already installed"
} else {
  Info "Installing paddlepaddle-gpu ($Cuda) - large download, please wait..."
  & $py -m pip uninstall -y paddlepaddle *> $null
  & $py -m pip install "paddlepaddle-gpu==3.3.1" -i "https://www.paddlepaddle.org.cn/packages/stable/$Cuda/" --extra-index-url https://pypi.org/simple
  if ($LASTEXITCODE -ne 0) { Bad "paddlepaddle-gpu install failed (try -Cuda cu126, or -Cpu)"; exit 1 }
  Ok "paddlepaddle-gpu installed"
}

if ($Warm) {
  Info "Warming models (first time downloads ~1-2 GB)..."
  & $py -c "from app import OCRService; s=OCRService(); print('  load', s.load('server'), 's on', s.default_device)"
}

$rc = Doctor
Info "`nNext:  .venv\Scripts\python keys.py create laravel-prod   then   .venv\Scripts\python serve.py"
exit $rc
