# CLAUDE.md — agent quick guide

GPU-accelerated OCR **API** (PaddleOCR PP-StructureV3): async, seat-limited,
bearer-authenticated, deployed behind a Cloudflare Tunnel. Read **README.md** for
full detail; this file is the fast path to stand it up on a new machine.

## Set up on a new machine (Windows)

Prerequisites the scripts do NOT install: **Python 3.12** (`py -3.12`) and — only
for the public tunnel — **cloudflared** (`winget install --id Cloudflare.cloudflared`).

```powershell
git clone https://github.com/shahabarvin/paddleocr-prototype && cd paddleocr-prototype
powershell -ExecutionPolicy Bypass -File .\setup\setup.ps1 -Warm     # venv + deps + auto GPU/CPU + models
.venv\Scripts\python src\keys.py create "laravel-prod"             # create a bearer token (shown once)
.venv\Scripts\python src\serve.py                                  # run on 127.0.0.1:8000
```

- `setup.ps1` auto-detects the GPU (via `nvidia-smi`) and installs
  `paddlepaddle-gpu` (cu129 default; add `-Cuda cu126`/`cu118` for older GPUs, or
  `-Cpu` to force CPU).
- Verify or debug anytime (no AI needed): `powershell -File .\setup\setup.ps1 -Check`
  — the doctor reports every prerequisite (python, packages, GPU/CUDA, models,
  keys, cloudflared, autostart tasks, live service) and what to fix.

## Expose to the internet (Cloudflare Tunnel, same account)

```powershell
powershell -File .\setup\setup_tunnel.ps1 -Hostname ocr2.voiceaccountant.com -TunnelName ocr2-tunnel
powershell -File .\setup\install_autostart.ps1 -TunnelName ocr2-tunnel   # elevated; starts service+tunnel at boot
```

The service always binds `127.0.0.1`; the tunnel is the only ingress. Auth is a
bearer token (see `keys.py`). Interactive docs + tester: `GET /docs`; spec: `GET
/openapi.yaml`; liveness: `GET /healthz`.

**Recovery is automatic:** the `OCR-Watchdog` task checks every 3 min and runs
`manage\restart.ps1` if the service is down; a reboot/power-cycle also starts
everything at boot.

**Day-to-day control (no command line):** double-click a file in `manage\` —
`Status`, `Start`, `Stop`, `Restart`, `Set-Workers`, `Set-Cleanup`, `Dashboard`.
The ones that change the service ask for admin (UAC). `Stop` keeps it off until
`Start` or a reboot. `Set-Workers` (parallel OCR processes) and `Set-Cleanup`
(delete job files after N days; default never) write the git-ignored
`data\settings.local.cmd` and restart the service.

## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest          # 53 tests, ~2s, no GPU needed
```

## Layout

- `src/` — the application. `app.py` — `OCRService` core (framework-agnostic) + the local eval UI.
- `service.py` / `serve.py` — production API + multi-worker launcher.
- `store.py` — SQLite shared store (seats, dedup, results). `auth.py` / `keys.py`
  — revocable bearer keys. `openapi.yaml` — contract + Laravel integration guide.
- `setup/` — one-time install: `setup.ps1` (bootstrap/doctor), `setup_tunnel.ps1`,
  `install_autostart.ps1` (registers the scheduled tasks; re-run it elevated if
  the repo folder moves, since the tasks store absolute paths).
- `manage/` — running it: `start_ocr.cmd` (what the OCR-Server task runs),
  `restart.ps1`, `watchdog.ps1`, and the double-click control files.
- `tools/` — manual scripts run from the repo root (`smoke_test.py`,
  `test_api_client.py`, `test_e2e.py`); `tests/` is the pytest suite.
- `data/` — machine-local state, git-ignored (never commit): `api_keys.json`,
  `state.db*`, `settings.local.cmd`. Also ignored: `output/`, `samples/`, `.venv/`,
  `*.log`.
