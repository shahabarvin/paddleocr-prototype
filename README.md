# PaddleOCR service — layout-aware OCR API

A GPU-accelerated OCR service built on PaddleOCR **PP-StructureV3**. It turns
document images and PDFs into layout-aware **markdown**, spatial **plain text**
and per-line **coordinates**, as a preprocessing step before an LLM. A backend
(here: a Laravel queue) submits a file, gets a job id, and polls for the result.

Production instance: **`https://ocr.voiceaccountant.com`** (behind a Cloudflare
Tunnel). Windows only; the scripts are PowerShell.

- [Set up a machine from scratch](#set-up-a-machine-from-scratch)
- [Day-to-day control](#day-to-day-control)
- [Using the API](#using-the-api)
- [Configuration](#configuration)
- [Repository layout](#repository-layout)

## Set up a machine from scratch

Follow these in order. Steps 1–4 give you a working local service; 5–7 put it on
the internet and make it survive reboots.

### 1. Install the prerequisites

The scripts do **not** install these:

| What | How | Needed for |
|---|---|---|
| Python **3.12** | python.org installer (must provide `py -3.12`) | everything |
| Git | `winget install Git.Git` | cloning |
| NVIDIA driver | a current driver for your card | GPU speed (without it the service runs on CPU, much slower) |
| cloudflared | `winget install --id Cloudflare.cloudflared` | step 5 only |

### 2. Clone and install

```powershell
git clone https://github.com/shahabarvin/paddleocr-prototype
cd paddleocr-prototype
powershell -ExecutionPolicy Bypass -File .\setup\setup.ps1 -Warm
```

This creates `.venv`, installs the dependencies, detects the GPU and installs the
matching `paddlepaddle-gpu` build, downloads the models (~1–2 GB, into
`~/.paddlex/official_models`) and finishes with a health check. It takes a while
the first time. Re-running it is safe and repairs a broken install.

Options: `-Cuda cu126` or `-Cuda cu118` for older GPUs (default `cu129`, required
for RTX 50-series), `-Cpu` to force CPU.

### 3. Create an API key

```powershell
.venv\Scripts\python src\keys.py create "laravel-prod"
```

The token is printed **once** — copy it now. Until a key exists every API call
returns `401`.

### 4. Try it locally

```powershell
.venv\Scripts\python src\serve.py
```

Open <http://127.0.0.1:8000/dashboard>, paste the token, and upload a file with
**Quick test**. Stop it with Ctrl+C before continuing.

### 5. Connect a public hostname (Cloudflare Tunnel)

```powershell
powershell -ExecutionPolicy Bypass -File .\setup\setup_tunnel.ps1 -Hostname ocr2.voiceaccountant.com -TunnelName ocr2-tunnel
```

A browser opens once to log in to Cloudflare and pick the domain. The script then
creates the tunnel, adds the DNS record and writes `~/.cloudflared/config.yml`.
Any subdomain of a domain on the account works, at no cost. The service itself
keeps listening on `127.0.0.1` only; the tunnel is the only way in.

> **If this machine already runs another Cloudflare tunnel, do not run this
> script.** It overwrites `~/.cloudflared/config.yml` and the existing tunnel
> stops working. Create the second tunnel by hand with its own config file.

### 6. Start at boot

In an **Administrator** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup\install_autostart.ps1 -TunnelName ocr2-tunnel
```

This registers three Windows scheduled tasks that run without anyone logged in:
`OCR-Server` (the service), `Cloudflare-Tunnel`, and `OCR-Watchdog` (every 3
minutes, restarts the service if it is down). The tasks store the folder's full
path — if you move the repo, run this step again.

Then double-click **`manage\Start.cmd`** to start everything now.

### 7. Verify

```powershell
powershell -ExecutionPolicy Bypass -File .\setup\setup.ps1 -Check
```

The doctor checks Python, packages, GPU, models, API keys, cloudflared, the
scheduled tasks and the live service, and says what to fix. Finally open
`https://<your-hostname>/healthz` — it should return `{"status":"ok","ready":true}`.

Give the client the hostname and the token from step 3. Each machine has its own
keys, queue and results; two machines do not share anything.

## Day-to-day control

No command line needed — double-click a file in [`manage\`](manage/):

| File | What it does |
|---|---|
| `Status.cmd` | Is it running, how many workers, GPU memory in use |
| `Start.cmd` | Start the service |
| `Stop.cmd` | Turn it off until you click Start (or the PC reboots) |
| `Restart.cmd` | Restart the service and the tunnel |
| `Set-Workers.cmd` | Change how many documents are processed in parallel |
| `Set-Cleanup.cmd` | Choose after how many days old job files are deleted (0 = never) |
| `Dashboard.cmd` | Open the live dashboard |

All but `Status` and `Dashboard` ask for administrator permission, and the two
`Set-…` files restart the service to apply the change. The service is
run by Windows, so nothing has to stay open.

The **dashboard** (`/dashboard`) shows seats, the queue and recent jobs, filters
by time, and for each job opens the input file, the result, the debug record and
a view of every recognized line drawn over the page.

## Using the API

Full contract and a copy-paste Laravel example: [`src/openapi.yaml`](src/openapi.yaml),
also served at `/openapi.yaml` and browsable at `/docs`. In short:

```
POST /api/ocr            multipart: file, key        -> 202 {status:"started", process_id}
GET  /api/ocr/{id}                                   -> 202 while running, 200 {status:"done", result}
```

Every `/api/*` and `/files/*` call needs `Authorization: Bearer <token>`.
`/healthz`, `/docs`, `/openapi.yaml` and the dashboard page are public.

| Form field | Default | Notes |
|---|---|---|
| `file` | — | jpg, png, pdf, bmp, webp. Max 20 MB (`413` if larger). |
| `key` | hash of the file | Idempotency key — pass your job or document id. A re-submit returns the existing job or its result, never a second OCR run. |
| `model` | `server` | `server` (quality) or `mobile` (faster, no table or formula structure) |
| `device` | `gpu` | `gpu` or `cpu` |
| `enhance` | `0` | `1` = denoise and contrast pre-pass for faded scans (~40% slower) |
| `overlays` | `0` | `1` = also save detection overlay images |

**Result fields**

- `markdown` — layout-aware markdown; the main text to feed an LLM. If the
  structured markdown drops a value, the service falls back to the complete
  spatial text, so no recognized line is lost.
- `layout_text` — plain text on a monospace grid; better for receipts and forms
  where columns and line breaks matter.
- `lines` — per page, every recognized line as `{text, box:[x1,y1,x2,y2], score}`.
- `page_sizes` — per page `[width, height]` of the raster the boxes refer to.

**Seats and workers.** `OCR_WORKERS` processes run OCR in parallel; `OCR_SEATS`
is how many jobs the service accepts at once, running or waiting. With the
defaults (2 workers, 3 seats) two jobs run, a third waits, and a fourth gets `429`
with `Retry-After` — the client backs off and retries.

**Retention.** Results are kept about 7 days; after that `GET` returns `404` and
the client re-submits. The job files on disk (`output/<date>/<id>/`: input,
result, debug record) are kept forever by default. To delete them automatically
after N days, double-click `manage\Set-Cleanup.cmd` (or set
`OCR_FILES_KEEP_DAYS`); the service then removes old folders about once an hour.
Choosing fewer days than `OCR_TTL_DAYS` means a job can still be listed on the
dashboard after its files are gone.

**Keys**

```powershell
.venv\Scripts\python src\keys.py create "label"   # prints the token once
.venv\Scripts\python src\keys.py list
.venv\Scripts\python src\keys.py revoke <id>      # takes effect in seconds, no restart
```

Only SHA-256 hashes are stored (`data/api_keys.json`).

## Configuration

Environment variables, read at start-up. Put machine-specific values in
`data\settings.local.cmd` (for example `set OCR_WORKERS=1`) — `Set-Workers.cmd`
and `Set-Cleanup.cmd` write that file for you — then restart.

| Variable | Default | |
|---|---|---|
| `OCR_WORKERS` | 2 | parallel OCR processes; each loads its own copy of the models |
| `OCR_SEATS` | 3 | jobs accepted at once |
| `OCR_HOST` / `OCR_PORT` | 127.0.0.1 / 8000 | |
| `OCR_DEVICE` | gpu | falls back to CPU if no CUDA build is present |
| `OCR_TTL_DAYS` | 7 | how long results are kept |
| `OCR_FILES_KEEP_DAYS` | 0 | delete job files older than this many days; 0 = never |
| `OCR_MAX_UPLOAD_MB` | 20 | |
| `OCR_AUTH` | on | `off` for local development only |
| `OCR_DEBUG` | 1 | writes `debug.json` (request + result) next to each job; `0` disables |
| `OCR_DB` / `OCR_KEYS_FILE` | `data/state.db` / `data/api_keys.json` | |

**GPU memory.** Measured on an RTX 5060 Ti (16 GB) with real one-page documents:
each worker settles at 5–7 GB after it has processed large pages, so two workers
hold about 12 GB. A second worker added about 19% throughput while making each
document about 45% slower when both were busy (median 1.1 s alone, 1.6 s
together). If the GPU is shared with something else, one worker is the better
trade.

## Repository layout

```
src/      the application
            app.py       OCRService core (no web imports) + a local eval UI
            service.py   the production API and dashboard
            serve.py     multi-worker launcher
            store.py     SQLite store: seats, queue, dedup, results
            auth.py, keys.py   bearer keys
            openapi.yaml       API contract + Laravel guide
setup/    one-time install: setup.ps1, setup_tunnel.ps1, install_autostart.ps1
manage/   running it: start_ocr.cmd, restart.ps1, watchdog.ps1, the double-click files
tools/    manual scripts, run from the repo root
tests/    pytest suite
data/     machine-local state (keys, database, settings) — not in git
output/   job files — not in git
```

## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

No GPU needed: the suite covers the store, auth and key rotation, the OCR
helpers, and the real FastAPI endpoints with the model work mocked. It runs in a
couple of seconds.

## OCR quality notes

- **`enhance=1`** helps faded, crumpled or smudged scans. It garbles clean scans,
  so the service runs the original first and only falls back to the enhanced
  version when the original yields almost no text.
- **Known recognition limits**, best handled downstream by the LLM: currency
  symbols tight against digits (for example `₹360.00`) and occasional dropped
  spaces inside words. Decorative separator lines are filtered out.
- **Eval UI.** `.venv\Scripts\python src\app.py` serves a browser page for
  inspecting one file's markdown, layout text and overlays with timings. No
  auth, no queue; do not run it on the same port as the service.
