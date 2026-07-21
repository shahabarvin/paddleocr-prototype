# PaddleOCR service — layout-aware OCR API for LLM preprocessing

A GPU-accelerated OCR service built on PaddleOCR **PP-StructureV3**. It turns
document images/PDFs into layout-aware **markdown** + spatial **plain text**, as a
preprocessing step before an LLM. Ships as two parts over one shared core:

- **`service.py`** — the production API: multi-worker, seat-limited,
  bearer-authenticated, asynchronous (submit → poll). This is what a backend
  (e.g. a Laravel queue) calls. Deployed here behind a Cloudflare Tunnel at
  **`https://ocr.voiceaccountant.com`**.
- **`app.py`** — a local eval UI (upload a file in the browser; see markdown,
  overlays and timings). For inspecting quality — not the production path.

Both are thin layers over the framework-agnostic `OCRService` core in `app.py`.

## Setup — one command

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1          # venv + deps + GPU build + health check
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Warm    # also download/warm the models (~1-2 GB)
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Check   # doctor only (installs nothing)
```

`setup.ps1` needs **Python 3.12** on PATH (`py -3.12`). It creates the venv,
installs dependencies, auto-detects the GPU and installs `paddlepaddle-gpu` (cu129
by default; `-Cuda cu126`/`cu118` or `-Cpu` to override), then runs a health
check. Re-running is safe and doubles as a **fixer**.

The **`-Check` doctor** verifies Python, packages, GPU/CUDA, models, API keys,
cloudflared, the autostart tasks and the live service — and prints exactly what to
fix. So a fresh Windows install can be brought up (or debugged) **without AI**.

<details><summary>Manual setup (what <code>setup.ps1</code> automates)</summary>

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
# GPU: replace the CPU wheel (CUDA libs come bundled as pip deps, no toolkit needed).
# cu118 / cu126 / cu129 by GPU generation; Blackwell (RTX 50-series) needs cu129.
.venv\Scripts\python -m pip uninstall -y paddlepaddle
.venv\Scripts\python -m pip install "paddlepaddle-gpu==3.3.1" `
  -i https://www.paddlepaddle.org.cn/packages/stable/cu129/ --extra-index-url https://pypi.org/simple
```
</details>

The first ever start downloads the models (~1–2 GB) into
`~/.paddlex/official_models` (counted as "model load", never as inference).
Inference runs on GPU by default; a GPU request falls back to CPU automatically
if no CUDA build is present.

## Run the production API

```powershell
.venv\Scripts\python serve.py      # 2 workers on http://127.0.0.1:8000
```

- **Two worker processes**, each with its own GPU pipeline. A shared SQLite store
  (`state.db`, WAL) coordinates seats + a job queue + results — **no Redis needed**
  on a single machine.
- **Async + seat-limited:** submit returns a `process_id` immediately; at most
  `OCR_SEATS` (default **3**) jobs run at once (excess → `429`), then poll for the
  result.
- **Idempotent:** pass a stable `key`; re-submits return the existing job / cached
  result — safe for queue retries.
- **Authenticated:** bearer token on all `/api/*` endpoints (see *Auth*).

### Endpoints

`POST /api/ocr` — multipart form:

| field | default | notes |
|---|---|---|
| `file` | — | jpg/png/pdf/bmp/webp. Max 20 MB (`413` if larger). |
| `key` | hash(file) | **idempotency key** — pass your job/document id. Re-runs never start a second OCR. |
| `model` | `server` | `server` (quality) or `mobile` (fast) |
| `device` | gpu | `gpu` / `cpu` |
| `enhance` | `0` | `1` = denoise+contrast pre-pass for degraded scans |
| `overlays` | `0` | `1` = save detection overlay PNGs |

| Response | HTTP | Body |
|---|---|---|
| admitted | 202 | `{status:"started", process_id}` |
| same job running | 202 | `{status:"in_progress", process_id}` |
| same job finished | 200 | `{status:"done", process_id, result}` |
| all seats busy | 429 | `{status:"no_seat"}` + `Retry-After` |

`GET /api/ocr/{process_id}` → `200 {status:"done", result}` / `{status:"failed", error}`, or `202` while queued/processing.
`GET /api/status` → worker id + live seat usage.

### Interactive docs

- **`GET /docs`** — Swagger UI: browse + **Try-it-out** tester + **Authorize**
  button for the bearer token.
- **`GET /openapi.yaml`** — the full spec (also embeds a complete Laravel
  integration guide with a copy-paste example).
- `GET /healthz` — public liveness probe.

Hand `https://ocr.voiceaccountant.com/openapi.yaml` (or `/docs`) to the client.

### Auth (bearer tokens)

All `/api/*` and `/files/*` endpoints require `Authorization: Bearer <token>`
(`/healthz`, `/docs`, `/openapi.yaml` are public). Tokens are managed on the
server with [keys.py](keys.py) — multiple keys, each individually revocable, no
restart needed:

```powershell
.venv\Scripts\python keys.py create "laravel-prod"   # prints the token ONCE
.venv\Scripts\python keys.py list                     # id, active/revoked, label
.venv\Scripts\python keys.py revoke <id>              # kill a key in ~seconds
```

Only SHA-256 hashes are stored in `api_keys.json` (git-ignored); the plaintext
token is shown only at creation. With auth on and no keys yet, every request is
`401` until you create one.

### Config (env)

`OCR_WORKERS` (2) · `OCR_SEATS` (3) · `OCR_HOST` (127.0.0.1) · `OCR_PORT` (8000) ·
`OCR_DEVICE` (gpu) · `OCR_DB` (state.db) · `OCR_AUTH` (on; `off` for local dev) ·
`OCR_KEYS_FILE` (api_keys.json) · `OCR_MAX_UPLOAD_MB` (20) · `OCR_DEBUG` (1; writes
`output/<process_id>/debug.json` with the request input + output — `0` to disable).

## Deployment & portability

Exposed to the internet via **Cloudflare Tunnel** — no port-forwarding; the
service stays bound to `127.0.0.1` (the tunnel is the only ingress). One command
connects a machine to a subdomain on your Cloudflare account (logs in, creates the
tunnel, adds the DNS route, writes `~/.cloudflared/config.yml`):

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_tunnel.ps1 -Hostname ocr.voiceaccountant.com
```

**Auto-start on boot** (survives reboots headlessly, no login required) — run once
in an **elevated** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
```

It registers two Windows scheduled tasks: **`OCR-Server`** (runs
[start_ocr.cmd](start_ocr.cmd) → `serve.py`) and **`Cloudflare-Tunnel`** (runs the
tunnel). Both scripts derive all paths at runtime, so they work after re-cloning
anywhere.

### A second machine / subdomain

Same Cloudflare account, a new subdomain, a separate tunnel — no code changes:

```powershell
git clone https://github.com/shahabarvin/paddleocr-prototype && cd paddleocr-prototype
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\setup_tunnel.ps1 -Hostname ocr2.voiceaccountant.com -TunnelName ocr2-tunnel
.venv\Scripts\python keys.py create "laravel-prod"
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -TunnelName ocr2-tunnel   # elevated
```

Security: bearer auth is the app-level guard; optionally add **Cloudflare Access**
(service token) at the edge for a second layer before traffic reaches the origin.

## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

Unit tests (no GPU) cover the SQLite store (seats, dedup, stale-reclaim, TTL),
bearer auth + key rotation, and the OCR helpers (device fallback, enhance, text
normalization). An API suite drives the real FastAPI endpoints (auth, `413` size
limit, async submit→poll, idempotency, public `/docs` + `/openapi.yaml`) with the
GPU work mocked — so the whole suite runs in ~2 s on any machine.

## Laravel integration

The full behavioral guide (async flow, idempotency, `429` backoff, queue tuning,
which result field to use) plus a copy-paste PHP example live in
**[openapi.yaml](openapi.yaml)** (`info.description`) — hand that file or the
`/openapi.yaml` URL to the client. In short: send `Authorization: Bearer <token>`,
`POST /api/ocr` with your job id as `key`, then poll `GET /api/ocr/{id}`; set the
queue worker concurrency ≈ 3 and job `retry_after` > 120 s.

## Performance (RTX 5060 Ti, 16 GB — measured)

| Metric | Value |
|---|---|
| Model load at startup (per worker) | ~10–25 s |
| Steady-state inference (server tier) | ~0.5–0.75 s/page |
| Throughput — 1 worker | ~2.2 img/s |
| Throughput — **2 workers** | **~3.5 img/s** (both pipelines, split ~50/50) |
| Throughput — 3 / 4 workers | ~4.1 / ~4.3 img/s (diminishing) |
| `enhance=1` | ~40 % slower |

The single GPU is the ceiling (two CUDA contexts time-slice one card), so beyond
2 workers throughput barely moves. More throughput = another GPU (~linear), the
`mobile` tier (fewer models), or batching. VRAM/power headroom does **not**
translate to throughput — the card is compute/launch-bound, not memory-bound.

## OCR quality notes

- **`markdown` vs `layout_text`:** use `result.markdown` for
  reports/papers/tables; use `result.layout_text` (plain text on a monospace
  grid) for receipts/forms/tickets where columns and line-breaks matter.
- **`enhance=1`** (denoise + CLAHE + upscale) helps faded/crumpled/smudged scans.
  On a degraded receipt it recovered 29/42 known tokens vs 27 raw; binarization
  (23), detection-threshold lowering (24) and unwarping (21) all measured
  *worse*, so they are intentionally not applied.
- **Model tiers:** `server` (quality — large layout model, table structure,
  formula recognition) vs `mobile` (faster; no table/formula structure). Both stay
  cached in memory, so switching is free.
- **Known recognition limits** (rec-model behavior, best fixed downstream by the
  LLM): currency symbols tight against digits (e.g. `₹360.00` — see
  [rupee_test.py](rupee_test.py)), and occasional dropped intra-word spaces.
  Decorative separator lines are filtered out of both text views.

## The eval UI (`app.py`)

```powershell
.venv\Scripts\python app.py        # http://127.0.0.1:8000  (browser UI)
```

Upload a JPG/PNG/PDF, pick tier + device + enhance, and inspect the markdown,
layout text, rendered preview, and detection overlays with a timing breakdown.
Quality inspection only — no auth, no seats. Don't run it and `serve.py` on the
same port at once.

## Reusable core

`OCRService` in [app.py](app.py) is framework-agnostic (no web imports):
`load()` once per process, `process(path, tier=, device=, enhance=)` per
document → a JSON-serializable dict. Both the API and the eval UI wrap it.
