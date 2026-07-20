# PaddleOCR prototype — layout-aware OCR for LLM preprocessing

Minimal local web app to evaluate PaddleOCR **PP-StructureV3** (layout analysis +
PP-OCRv5 *server* models — the highest-quality tier) as a text-extraction step
before sending documents to an LLM.

## Run

```powershell
# one-time setup (Python 3.12 — PaddlePaddle has no 3.14 wheels yet)
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# start
.venv\Scripts\python app.py
```

Open <http://127.0.0.1:8000>, upload a JPG/PNG/PDF.

### GPU (recommended)

`requirements.txt` installs the **CPU** build of PaddlePaddle, so out of the
box the app runs on CPU. To use an NVIDIA GPU, install the matching GPU build
into the same venv (this replaces the CPU wheel; the CUDA runtime libraries
come bundled as pip dependencies — no separate CUDA toolkit needed):

```powershell
.venv\Scripts\python -m pip uninstall -y paddlepaddle
# pick the index for your GPU's CUDA generation: cu118 / cu126 / cu129.
# Blackwell (RTX 50-series, e.g. RTX 5060 Ti) needs cu129 (CUDA 12.8+).
.venv\Scripts\python -m pip install "paddlepaddle-gpu==3.3.1" `
  -i https://www.paddlepaddle.org.cn/packages/stable/cu129/ `
  --extra-index-url https://pypi.org/simple
```

- **First ever start downloads the models (~1–2 GB)** into
  `~/.paddlex/official_models`. That happens during "model load" at server
  startup and is never counted in the reported inference time.
- Inference runs on **GPU by default**, selectable per request (GPU/CPU
  dropdown in the UI, `device` form field in the API, or `OCR_DEVICE=cpu`/`gpu`
  to change the default). A GPU request **falls back to CPU automatically** —
  with a note in the UI — when no CUDA-enabled PaddlePaddle is installed.
- **Enhance** (UI checkbox / `enhance=1` API field) runs a denoise +
  local-contrast (CLAHE) + upscale pre-pass before OCR — measurably better on
  faded, crumpled or smudged scans (e.g. thermal receipts). Leave it off for
  clean documents. On one badly degraded receipt it recovered 29/42 known
  tokens vs 27 raw; binarization (23), detection-threshold lowering (24) and
  unwarping (21) all measured *worse*, so they are intentionally not applied.
- `OCR_UNWARP=1` enables document unwarping (useful for phone photos of
  curved pages; off by default — and, per above, it hurts flat crumpled
  receipts).
- `OCR_HOST=0.0.0.0 OCR_PORT=8000` to reach the app from outside the machine
  (e.g. when testing on a server). No auth — don't leave it exposed.

Linux server quick-start:

```bash
git clone https://github.com/shahabarvin/paddleocr-prototype && cd paddleocr-prototype
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
OCR_HOST=0.0.0.0 .venv/bin/python app.py
```

## What you get per upload

- Timing breakdown: model load (once, at startup) vs. inference vs.
  post-processing, plus browser round-trip.
- Layout-aware **markdown** (headings, columns in reading order, tables as
  markdown tables) — the raw text intended as LLM input.
- **Layout text**: plain text reconstructed from the raw OCR boxes on a
  monospace grid, preserving exact line breaks and column alignment. Use this
  for documents the layout model sees as unstructured (receipts, forms,
  tickets); use markdown for reports/papers/tables.
- Overlay images: detected layout regions and text boxes over the original,
  for visual accuracy checks.
- Everything is also written to `output/<job_id>/` (`result.md`, `viz/*.png`).

## Model tiers (selectable in the UI, `model` form field in the API)

- **Quality (`server`)** — PP-OCRv5 server det/rec + full PP-StructureV3:
  large layout model, region grouping, table structure, formula recognition.
- **Fast (`mobile`)** — PP-OCRv5 mobile det/rec + PP-DocLayout-S; region
  grouping, table structure and formula recognition off. Table/formula
  *text* is still extracted; markdown loses `<table>` markup and reading
  order may suffer on complex multi-column pages.

Both pipelines stay cached in memory after first use, so switching is free.

## Measured on this machine (Windows 11, CPU)

| Phase | Quality (server) | Fast (mobile) |
|---|---|---|
| One-time model download | ~1.3 GB, ~5 min | ~30 MB, seconds |
| Pipeline load (cached models) | ~20 s (at startup) | ~1–5 s (first use) |
| First request (Paddle graph compilation) | ~7.5 min | ~25 s |
| Steady-state inference, 1-page receipt | **~93 s** | **~25 s** |
| Post-processing (markdown + overlays) | <1 s | <1 s |

Two caveats that make this a *worst-case* number:

1. **oneDNN is disabled on Windows only** to work around a paddlepaddle 3.3
   bug that crashes the RT-DETR-based layout model on Windows CPU. On Linux
   it stays enabled automatically and CPU inference is several times faster
   (override with `OCR_MKLDNN=0/1`).
2. These are the largest server-grade models on **CPU**. On this machine's
   GPU (RTX 5060 Ti, 16 GB) the same server pipeline loads in ~23 s at startup
   and runs **~0.75 s/page** steady-state (~7 s on the first request, which
   includes one-time graph compilation) — roughly two orders of magnitude
   faster than the CPU figures above. Install the GPU build (see *GPU* under
   Run) and it is used automatically.

## Known limitations (measured, relevant for the evaluation)

- **₹ (rupee sign) is unreliable.** It is in the PP-OCRv5 dictionary, and an
  isolated test shows the models *can* read it ("₹ 360.00" with a space →
  correct), but tight against digits ("₹360.00") it becomes `<`/`¥` or is
  dropped. Training-data gap — the production fix is fine-tuning the rec
  model on receipt data ([rupee_test.py](rupee_test.py) reproduces this).
- **Spaces inside a text line are sometimes dropped** ("MODE -" → "MODE-",
  "TOTAL: 3,769.50" → "TOTAL:3,769.50"). Recognition-model behavior; not
  post-correctable without risking invented content.
- Decorative separator lines (dashed rules on receipts) used to appear as
  `+++***+*…` — these are now filtered out of both text views (a box/line
  with 3+ characters and no letter/digit in any script is dropped).

## Production API — seat-limited, deduplicated, multi-worker

`app.py` above is the human eval UI. The **production service** meant to sit
behind a Laravel queue is [service.py](service.py) + [store.py](store.py) +
[serve.py](serve.py). It reuses the same `OCRService` core.

```powershell
.venv\Scripts\python serve.py      # 2 worker processes on http://127.0.0.1:8000
```

- **Two worker processes**, each with its own GPU pipeline. On one RTX 5060 Ti
  this measured **~3.5 images/sec** (vs ~2.2 single-process) with work split
  50/50 across both pipelines. `OCR_WORKERS` sets the count (2 fits a 16 GB
  card; a 3rd server pipeline won't).
- **Seats (admission control):** at most `OCR_SEATS` (default **3**) jobs in
  flight; excess submits get `429`. The GPU is the real ceiling, so 3 seats
  already saturates it — raising it adds latency, not throughput.
- **Shared state via SQLite** (`state.db`, WAL) — no Redis/infra needed on a
  single machine. Seats + job registry + results are coordinated there; the
  store layer is isolated, so swapping in Redis for a multi-machine deploy is a
  small change.
- **Pull model:** whichever worker is free claims the next queued job, so both
  pipelines stay busy regardless of which worker received the HTTP request.

### Endpoints

`POST /api/ocr` — multipart form:

| field | default | notes |
|---|---|---|
| `file` | — | jpg/png/pdf/bmp/webp |
| `key` | — | **idempotency key** — pass your Laravel job/document id. Omit → sha256(file+options). Re-runs of the same key never start a second OCR. |
| `model` | `server` | `server` (quality) or `mobile` (fast) |
| `device` | gpu | `gpu` / `cpu` |
| `enhance` | `0` | `1` = denoise+contrast pre-pass for degraded scans |
| `overlays` | `0` | `1` = save detection overlay PNGs |

Responses:

| Situation | HTTP | Body |
|---|---|---|
| new job admitted | 202 | `{status:"started", process_id}` |
| same job still running | 202 | `{status:"in_progress", process_id}` |
| same job finished | 200 | `{status:"done", process_id, result}` |
| all seats busy | 429 | `{status:"no_seat"}` + `Retry-After` |

`GET /api/ocr/{process_id}` → `200 {status:"done", result}` / `{status:"failed", error}`,
or `202` while still queued/processing. `GET /api/status` → worker id + live seat usage.

### Laravel integration

Each queue job POSTs to `/api/ocr` with a stable `key` (its own id), then polls
`GET /api/ocr/{id}` for the result:

```php
$res = Http::withToken(config('ocr.token'))          // Authorization: Bearer <token>
    ->attach('file', $bytes, 'doc.png')
    ->post('https://ocr.voiceaccountant.com/api/ocr', [
        'key' => (string) $this->job->uuid,   // idempotency key
        'model' => 'server', 'enhance' => '1',
    ]);

if ($res->status() === 429) {
    return $this->release(now()->addSeconds(2)); // no seat -> requeue/backoff
}
$id = $res->json('process_id');
// then poll GET /api/ocr/{$id} until status is done/failed
```

- Set the **queue worker concurrency ≈ seats (3)** so `429`s stay rare; the API
  is the hard cap regardless. Set the job `retry_after`/timeout **> 120 s** so a
  slow job isn't retried while still running.
- Re-runs (Laravel retries) are safe: same `key` → the existing job, never a
  second OCR. A finished job returns its cached result instantly.
- A worker that dies mid-job leaks nothing: its seat is reclaimed after
  `stale_seconds` (120 s) and the job requeues. Done/failed rows are purged
  after `ttl_seconds` (1 h).

The spec is served live at `GET /openapi.yaml` (public), and an interactive
**Swagger UI** (browse + Try-it-out tester, with an Authorize button for the
bearer token) at `GET /docs` — hand either URL to the client.

Env: `OCR_WORKERS` (2), `OCR_SEATS` (3), `OCR_HOST` (127.0.0.1), `OCR_PORT`
(8000), `OCR_DEVICE` (gpu), `OCR_DB` (state.db), `OCR_AUTH` (`on`; set `off` for
local dev), `OCR_KEYS_FILE` (api_keys.json), `OCR_MAX_UPLOAD_MB` (20).

### Auth (bearer tokens)

All `/api/*` and `/files/*` endpoints require `Authorization: Bearer <token>`
(`/healthz` is public). Tokens are managed on the server with [keys.py](keys.py) —
multiple keys, each individually revocable, no restart needed:

```powershell
.venv\Scripts\python keys.py create "laravel-prod"   # prints the token ONCE
.venv\Scripts\python keys.py list                     # id, active/revoked, label
.venv\Scripts\python keys.py revoke <id>              # kill a key in ~seconds
```

Only SHA-256 hashes are stored in `api_keys.json` (git-ignored); the plaintext
token is shown only at creation. With auth on and no keys yet, every request is
`401` until you create one. Pair this with **Cloudflare Access** at the edge for
defense in depth.

### Reusable core

`OCRService` in [app.py](app.py) is framework-agnostic (no FastAPI imports):
`load()` once per process, `process(path, tier=, device=, enhance=)` per
document, returns a JSON-serializable dict. Both the eval UI and the production
service are thin layers over it.
