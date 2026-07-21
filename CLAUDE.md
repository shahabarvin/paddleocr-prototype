# CLAUDE.md — agent quick guide

GPU-accelerated OCR **API** (PaddleOCR PP-StructureV3): async, seat-limited,
bearer-authenticated, deployed behind a Cloudflare Tunnel. Read **README.md** for
full detail; this file is the fast path to stand it up on a new machine.

## Set up on a new machine (Windows)

Prerequisites the scripts do NOT install: **Python 3.12** (`py -3.12`) and — only
for the public tunnel — **cloudflared** (`winget install --id Cloudflare.cloudflared`).

```powershell
git clone https://github.com/shahabarvin/paddleocr-prototype && cd paddleocr-prototype
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Warm     # venv + deps + auto GPU/CPU + models
.venv\Scripts\python keys.py create "laravel-prod"             # create a bearer token (shown once)
.venv\Scripts\python serve.py                                  # run on 127.0.0.1:8000
```

- `setup.ps1` auto-detects the GPU (via `nvidia-smi`) and installs
  `paddlepaddle-gpu` (cu129 default; add `-Cuda cu126`/`cu118` for older GPUs, or
  `-Cpu` to force CPU).
- Verify or debug anytime (no AI needed): `powershell -File .\setup.ps1 -Check`
  — the doctor reports every prerequisite (python, packages, GPU/CUDA, models,
  keys, cloudflared, autostart tasks, live service) and what to fix.

## Expose to the internet (Cloudflare Tunnel, same account)

```powershell
powershell -File .\setup_tunnel.ps1 -Hostname ocr2.voiceaccountant.com -TunnelName ocr2-tunnel
powershell -File .\install_autostart.ps1 -TunnelName ocr2-tunnel   # elevated; starts service+tunnel at boot
```

The service always binds `127.0.0.1`; the tunnel is the only ingress. Auth is a
bearer token (see `keys.py`). Interactive docs + tester: `GET /docs`; spec: `GET
/openapi.yaml`; liveness: `GET /healthz`.

## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest          # 38 tests, ~2s, no GPU needed
```

## Layout

- `app.py` — `OCRService` core (framework-agnostic) + the local eval UI.
- `service.py` / `serve.py` — production API + multi-worker launcher.
- `store.py` — SQLite shared store (seats, dedup, results). `auth.py` / `keys.py`
  — revocable bearer keys. `openapi.yaml` — contract + Laravel integration guide.
- `setup.ps1` (bootstrap/doctor), `setup_tunnel.ps1`, `install_autostart.ps1`,
  `start_ocr.cmd` — reproducible, path-independent deployment.
- Git-ignored (never commit): `api_keys.json`, `state.db*`, `output/`, `samples/`,
  `.venv/`, `*.log`.
