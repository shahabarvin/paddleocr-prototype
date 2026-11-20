"""Production OCR service: seat-limited, deduplicated, async, multi-worker.

Meant to sit behind a Laravel queue. Each worker PROCESS runs one copy of this
app with its own GPU pipeline; all workers coordinate through a shared SQLite
store (see store.py). Run 2 of them for ~1.4x GPU throughput on one card
(measured) -- see serve.py.

Flow (matches the agreed design):
  POST /api/ocr  (multipart: file, key, model, device, enhance, overlays)
      -> a seat is free and this is new     : 202 {status:"started",     process_id}
      -> the same job is already running     : 202 {status:"in_progress",  process_id}
      -> the same job already finished        : 200 {status:"done",        process_id, result}
      -> the same job previously failed        : (re-run if a seat is free)
      -> all seats busy                        : 429 {status:"no_seat"}   + Retry-After
  GET  /api/ocr/{process_id}                    : 200/202 {status, [result|error]}
  GET  /api/status                              : worker + seat usage

Deduplication key ("same request"): the client-supplied ``key`` (pass your
Laravel job/document id) or, if omitted, sha256(file bytes + options). Re-runs of
the same key never start a second OCR; they join or return the existing job.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app import OCRService          # side-effect-free import of the reusable core
from auth import auth_enabled, has_any_key, verify_bearer
from store import Store

BASE = Path(__file__).parent
JOBS_DIR = BASE / "output"
DB_PATH = Path(os.environ.get("OCR_DB", BASE / "state.db"))
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ".bmp", ".webp"}
SEATS = int(os.environ.get("OCR_SEATS", "3"))
# How long finished rows (and their dedup/cache entry) live before purge.
OCR_TTL_DAYS = float(os.environ.get("OCR_TTL_DAYS", "7"))
MAX_UPLOAD_BYTES = int(os.environ.get("OCR_MAX_UPLOAD_MB", "20")) * 1024 * 1024
WORKER_ID = str(os.getpid())
# Debug: write output/<process_id>/debug.json (input + output) per request.
DEBUG_ENABLED = os.environ.get("OCR_DEBUG", "1").strip().lower() not in ("0", "off", "false")

app = FastAPI(title="PaddleOCR OCR service",
              docs_url=None, redoc_url=None, openapi_url=None)
service = OCRService()
store = Store(DB_PATH, seats=SEATS, ttl_seconds=OCR_TTL_DAYS * 86400)
_ready = {"state": "loading", "error": None}
_stop = threading.Event()   # set on shutdown so the worker loop exits cleanly


# --------------------------------------------------------------------------
# lifecycle: warm the pipeline, then run this process's pull-worker loop
# --------------------------------------------------------------------------

def require_auth(authorization: str = Header(None)) -> None:
    """FastAPI dependency: reject requests without a valid bearer token."""
    if not verify_bearer(authorization):
        raise HTTPException(
            401, "Missing or invalid API key. Send: Authorization: Bearer <token>"
        )


@app.on_event("startup")
def _startup() -> None:
    _stop.clear()
    store.init()
    if auth_enabled() and not has_any_key():
        print("WARNING: auth is ON but no API keys exist yet. All requests will "
              "be rejected until you run:  python keys.py create \"<label>\"  "
              "(or set OCR_AUTH=off for local dev).")
    threading.Thread(target=_warmup, daemon=True).start()
    threading.Thread(target=_worker_loop, daemon=True).start()


@app.on_event("shutdown")
def _shutdown() -> None:
    _stop.set()


def _warmup() -> None:
    try:
        service.load("server")            # this worker's own GPU pipeline
        _ready["state"] = "ready"
    except Exception as err:
        _ready["state"] = "error"
        _ready["error"] = str(err)


def _worker_loop() -> None:
    """Claim queued jobs and run them on this worker's pipeline, one at a time
    (the pipeline is single-threaded). Two such loops in two processes give the
    two-pipeline parallelism; the shared store balances work between them."""
    while _ready["state"] == "loading" and not _stop.is_set():
        time.sleep(0.2)
    ticks = 0
    while not _stop.is_set():
        # Any transient error (e.g. SQLite busy) must not kill the loop — back
        # off and keep going, or the worker would silently stop processing.
        try:
            if _ready["state"] != "ready":
                time.sleep(0.2)
                continue
            # Cheap read first; only take the write lock when there's work.
            if not store.has_queued():
                time.sleep(0.05)          # idle poll
                ticks += 1
                if ticks % 400 == 0:      # ~every 20s idle: free stuck seats + purge
                    store.reap_stale()
                    store.cleanup()
                continue
            job = store.claim_next(WORKER_ID)
            if job is not None:
                _run(job)
        except Exception:
            time.sleep(0.2)


def _write_debug(job_dir: Path, options: dict, output: dict) -> None:
    """Debug artifact: one JSON per request with its input and output."""
    if not DEBUG_ENABLED:
        return
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "worker": WORKER_ID,
            "input": {k: options.get(k) for k in
                      ("key", "filename", "bytes", "tier", "device",
                       "enhance", "overlays", "input_path")},
            "output": output,
        }
        (job_dir / "debug.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _run(job: dict) -> None:
    pid = job["process_id"]
    opt = job["options"]
    job_dir = Path(opt["input_path"]).parent    # the dated folder submit created
    try:
        job_dir.mkdir(parents=True, exist_ok=True)  # normally made at submit; be robust
        input_path = Path(opt["input_path"])
        viz_dir = (job_dir / "viz") if opt.get("overlays") else None
        t0 = time.perf_counter()
        result = service.process(
            input_path, viz_dir=viz_dir, tier=opt.get("tier", "server"),
            device=opt.get("device"), enhance=bool(opt.get("enhance")),
            with_lines=bool(opt.get("with_lines")),
        )
        result["timings"]["server_total_seconds"] = round(time.perf_counter() - t0, 3)
        result["process_id"] = pid
        result["worker"] = WORKER_ID
        # persist the text artifacts alongside the input (durable, and handy
        # for debugging); the JSON result is the store's source of truth.
        (job_dir / "result.md").write_text(result.get("markdown", ""), encoding="utf-8")
        (job_dir / "result.txt").write_text(result.get("layout_text", ""), encoding="utf-8")
        result["visualizations"] = [
            {"name": Path(p).name, "url": f"/files/{pid}/viz/{Path(p).name}"}
            for p in result.get("visualizations", [])
        ]
        store.complete(pid, result)
        _write_debug(job_dir, opt, result)
    except Exception as err:
        err_out = {"status": "failed", "error": f"{type(err).__name__}: {err}"}
        store.fail(pid, err_out["error"])
        _write_debug(job_dir, opt, err_out)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:64] or "job"


@app.post("/api/ocr", dependencies=[Depends(require_auth)])
def submit(
    file: UploadFile = File(...),
    key: str = Form(""),                 # idempotency key (Laravel job/doc id)
    model: str = Form("server"),
    device: str = Form(""),
    enhance: str = Form("0"),
    overlays: str = Form("0"),
    boxes: str = Form("0"),              # opt-in: per-line text+box+score in result
) -> JSONResponse:
    # NOTE: no readiness gate here. This handler only enqueues the job into the
    # shared store; the OCR runs later in whichever worker's pull loop is ready.
    # Gating on THIS worker's warm-up state used to 503 a client while a sibling
    # worker was already ready (and healthz, hitting the ready one, said 200) --
    # so a job is always accepted and simply waits in the queue until a worker
    # picks it up. A job that can never be processed is failed by the reaper's
    # hard deadline, so the client still gets a definitive answer, never a 503.
    ext = Path(file.filename or "upload").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{ext}'. "
                                 f"Allowed: {sorted(ALLOWED_EXTENSIONS)}")
    if model not in OCRService.TIERS:
        raise HTTPException(400, f"Unknown model tier '{model}'. "
                                 f"Allowed: {list(OCRService.TIERS)}")
    if device and device.strip().lower() not in ("gpu", "cpu", "auto"):
        raise HTTPException(400, f"Unknown device '{device}'. "
                                 f"Allowed: {list(OCRService.VALID_DEVICES)}")

    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    if key.strip():
        process_id = _slug(key.strip())
    else:  # content-addressed: identical file + options -> same id
        h = hashlib.sha256(data)
        h.update(f"|{model}|{device}|{enhance}|{overlays}".encode())
        process_id = h.hexdigest()[:24]

    # Persist input where any worker can read it, before it becomes claimable.
    # Grouped under the day's folder: output/<YYYY-MM-DD>/<process_id>/.
    day = datetime.date.today().isoformat()
    job_dir = JOBS_DIR / day / process_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{ext}"
    if not input_path.exists():
        input_path.write_bytes(data)

    options = {
        "tier": model,
        "device": (device or None),
        "enhance": (enhance == "1"),
        "overlays": (overlays == "1"),
        "with_lines": (boxes == "1"),
        "input_path": str(input_path),
        "key": key.strip() or None,
        "filename": file.filename,
        "bytes": len(data),
    }
    res = store.submit(process_id, options)

    status_code = {"started": 202, "in_progress": 202,
                   "done": 200, "no_seat": 429}.get(res["status"], 200)
    headers = {"Retry-After": "1"} if res["status"] == "no_seat" else {}
    return JSONResponse(res, status_code=status_code, headers=headers)


@app.get("/api/ocr/{process_id}", dependencies=[Depends(require_auth)])
def get_job(process_id: str) -> JSONResponse:
    job = store.get(_slug(process_id))
    if job is None:
        raise HTTPException(404, "unknown process_id")
    code = 200 if job["status"] in ("done", "failed") else 202
    return JSONResponse(job, status_code=code)


def _find_job_dir(pid: str):
    """A job's folder: output/<date>/<pid> (new, grouped by day) or the old
    output/<pid> (legacy jobs). Returns None if nothing matches."""
    legacy = JOBS_DIR / pid
    if legacy.is_dir():
        return legacy
    for d in JOBS_DIR.glob(f"*/{pid}"):
        if d.is_dir():
            return d
    return None


@app.get("/files/{process_id}/{path:path}", dependencies=[Depends(require_auth)])
def serve_file(process_id: str, path: str) -> FileResponse:
    job_dir = _find_job_dir(_slug(process_id))
    if job_dir is None:
        raise HTTPException(404)
    if path == "input":                       # convenience: serve input.<ext>
        matches = sorted(job_dir.glob("input.*"))
        if not matches:
            raise HTTPException(404)
        target = matches[0].resolve()
    else:
        target = (job_dir / path).resolve()
    if not target.is_file() or JOBS_DIR.resolve() not in target.parents:
        raise HTTPException(404)
    return FileResponse(target)


@app.get("/api/status", dependencies=[Depends(require_auth)])
def status() -> dict:
    return {
        "state": _ready["state"],
        "error": _ready["error"],
        "worker": WORKER_ID,
        "device": service.default_device,
        "gpu_available": service.gpu_available(),
        **store.stats(),
    }


@app.get("/api/jobs", dependencies=[Depends(require_auth)])
def jobs(limit: int = 200, since: float = None, until: float = None) -> dict:
    """Recent jobs, newest first (feeds /dashboard). Optional created_at window
    [since, until] in epoch seconds. No result payloads."""
    return {"now": time.time(), "jobs": store.recent(limit, since, until)}


@app.get("/healthz")
def healthz() -> dict:
    """Unauthenticated liveness probe (no seat/worker details leaked)."""
    return {"status": "ok", "ready": _ready["state"] == "ready"}


@app.get("/openapi.yaml", include_in_schema=False)
def openapi_yaml() -> FileResponse:
    """Serve the curated OpenAPI spec (public — it's the API contract, no secrets)."""
    p = BASE / "openapi.yaml"
    if not p.is_file():
        raise HTTPException(404, "openapi.yaml not found")
    return FileResponse(p, media_type="application/yaml")


_SWAGGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>PaddleOCR OCR API</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = function () {
      window.ui = SwaggerUIBundle({
        url: "/openapi.yaml",
        dom_id: "#swagger-ui",
        deepLinking: true,
        tryItOutEnabled: true,
        persistAuthorization: true,
        presets: [SwaggerUIBundle.presets.apis],
        layout: "BaseLayout"
      });
    };
  </script>
</body>
</html>"""


@app.get("/docs", include_in_schema=False)
def docs() -> HTMLResponse:
    """Interactive API docs + tester (Swagger UI) reading /openapi.yaml. Public;
    use the Authorize button to enter a bearer token for Try-it-out calls."""
    return HTMLResponse(_SWAGGER_HTML)


# The page itself is public like /docs (it contains no data); every data call it
# makes hits the bearer-protected endpoints, with the key kept in localStorage.
_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>OCR dashboard</title>
<style>
  body { font: 14px/1.5 system-ui, sans-serif; margin: 0; background: #11161d; color: #d7dde6; }
  header { display: flex; gap: 12px; align-items: center; padding: 10px 16px; background: #1a212b; }
  header h1 { font-size: 16px; margin: 0 auto 0 0; }
  input { background: #11161d; border: 1px solid #39465a; color: #d7dde6; padding: 5px 8px; border-radius: 5px; width: 300px; }
  main { padding: 16px; max-width: 1100px; margin: 0 auto; }
  .cards { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
  .card { background: #1a212b; border-radius: 8px; padding: 10px 16px; min-width: 110px; }
  .card b { display: block; font-size: 22px; }
  .card span { color: #8b98a9; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; background: #1a212b; border-radius: 8px; overflow: hidden; }
  th, td { padding: 7px 10px; text-align: left; border-bottom: 1px solid #242e3b; white-space: nowrap; }
  th { color: #8b98a9; font-weight: 500; font-size: 12px; }
  td.id { max-width: 260px; overflow: hidden; text-overflow: ellipsis; font-family: monospace; font-size: 12px; }
  td.err { max-width: 220px; overflow: hidden; text-overflow: ellipsis; color: #e07a7a; }
  .s { padding: 1px 8px; border-radius: 9px; font-size: 12px; }
  .s.processing { background: #2b4a7a; } .s.queued { background: #6a5420; }
  .s.done { background: #2a5a38; }      .s.failed { background: #6a2a2a; }
  #msg { color: #e0b97a; margin-bottom: 10px; }
  button { background: #39465a; border: 0; color: #d7dde6; padding: 2px 9px; border-radius: 5px; cursor: pointer; font-size: 12px; }
  button:disabled { opacity: .38; cursor: not-allowed; }
  select { background: #11161d; border: 1px solid #39465a; color: #d7dde6; padding: 5px 6px; border-radius: 5px; }
  .test, .range { background: #1a212b; border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .test #f_send { padding: 5px 14px; background: #2a5a38; }
  .test #f_out { color: #8b98a9; font-size: 12px; }
  .range input { width: auto; color-scheme: dark; }
  #boxview { margin-bottom: 14px; }
  .boxwrap { position: relative; display: inline-block; max-width: 520px; border: 1px solid #39465a; border-radius: 6px; overflow: hidden; }
  .boxwrap img { display: block; max-width: 520px; height: auto; }
  .boxsvg { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
  .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.65); z-index: 50; align-items: center; justify-content: center; padding: 20px; }
  .modalcard { background: #1a212b; border: 1px solid #39465a; border-radius: 10px; padding: 14px; max-width: 92vw; max-height: 92vh; overflow: auto; }
  .modalhead { display: flex; gap: 12px; align-items: center; margin-bottom: 10px; }
  .modalhead b { font-family: monospace; font-size: 13px; margin-right: auto; word-break: break-all; }
  .boxwrap.modalwrap, .boxwrap.modalwrap img { max-width: min(80vw, 900px); }
</style>
</head>
<body>
<header>
  <h1>OCR dashboard</h1>
  <span id="upd" style="color:#8b98a9;font-size:12px"></span>
  <select id="filter" title="time window">
    <option value="0">all (last hour)</option>
    <option value="300">last 5 min</option>
    <option value="900">last 15 min</option>
    <option value="1800">last 30 min</option>
  </select>
  <input id="key" type="password" placeholder="API key (Bearer ...)"/>
</header>
<main>
  <div id="msg"></div>
  <div class="test">
    <b>Quick test:</b>
    <input id="f_file" type="file"/>
    <input id="f_key" type="text" placeholder="key (optional, e.g. invoice-42)" style="width:220px"/>
    <label style="font-size:12px"><input id="f_boxes" type="checkbox"/> show boxes</label>
    <button id="f_send">Send</button>
    <span id="f_out"></span>
  </div>
  <div id="boxview"></div>
  <div class="range">
    <b>Date range:</b>
    from <input id="r_from" type="datetime-local"/>
    to <input id="r_to" type="datetime-local"/>
    <button id="r_clear">clear</button>
    <span style="color:#8b98a9;font-size:12px">(overrides the window above; data kept ~7 days)</span>
  </div>
  <div class="cards">
    <div class="card"><b id="c_state">-</b><span>state</span></div>
    <div class="card"><b id="c_seats">-</b><span>seats busy</span></div>
    <div class="card"><b id="c_queued">-</b><span>queued</span></div>
    <div class="card"><b id="c_done">-</b><span>done (kept 7d)</span></div>
    <div class="card"><b id="c_failed">-</b><span>failed (kept 7d)</span></div>
    <div class="card"><b id="c_gpu">-</b><span>device</span></div>
  </div>
  <table>
    <thead><tr><th>process_id</th><th>status</th><th>worker</th><th>waited</th>
      <th>run time</th><th>updated</th><th>error</th><th></th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
</main>
<div id="boxmodal" class="modal" onclick="if(event.target===this)closeBoxes()">
  <div class="modalcard">
    <div class="modalhead"><b id="bm_title">boxes</b><button onclick="closeBoxes()">close</button></div>
    <div id="bm_body"></div>
  </div>
</div>
<script>
  var keyEl = document.getElementById('key');
  keyEl.value = localStorage.getItem('ocr_key') || '';
  keyEl.addEventListener('change', function () { localStorage.setItem('ocr_key', keyEl.value); tick(); });

  function fmtDur(s) {
    if (s == null || isNaN(s) || s < 0) return '-';
    if (s < 60) return s.toFixed(1) + 's';
    return Math.floor(s / 60) + 'm' + Math.round(s % 60) + 's';
  }
  function esc(x) { var d = document.createElement('i'); d.textContent = x == null ? '' : x; return d.innerHTML; }

  // Draw each recognized line's box over the uploaded image (opt-in diagnostic).
  // Uses the already-in-browser File, so it costs the API nothing.
  function scoreColor(s) {
    if (s == null) return '#8b98a9';
    if (s >= 0.8) return '#3ddc84';
    if (s >= 0.6) return '#e0b97a';
    return '#e07a7a';
  }
  // Draw one SVG overlay (a rect per line, colored by score) over an <img>.
  // Shared by the quick-test (in-browser File) and the per-row modal (saved file).
  function paintBoxes(wrap, img, lines) {
    var SVGNS = 'http://www.w3.org/2000/svg';
    img.onload = function () {
      var svg = document.createElementNS(SVGNS, 'svg');
      svg.setAttribute('class', 'boxsvg');
      svg.setAttribute('viewBox', '0 0 ' + img.naturalWidth + ' ' + img.naturalHeight);
      svg.setAttribute('preserveAspectRatio', 'none');
      lines.forEach(function (ln) {
        var b = ln.box; if (!b || b.length < 4) return;
        var r = document.createElementNS(SVGNS, 'rect');
        r.setAttribute('x', b[0]); r.setAttribute('y', b[1]);
        r.setAttribute('width', Math.max(1, b[2] - b[0]));
        r.setAttribute('height', Math.max(1, b[3] - b[1]));
        r.setAttribute('fill', 'none');
        r.setAttribute('stroke', scoreColor(ln.score));
        r.setAttribute('stroke-width', Math.max(1, img.naturalWidth / 400));
        var t = document.createElementNS(SVGNS, 'title');
        t.textContent = (ln.text || '') + '  [score ' +
          (ln.score != null ? ln.score.toFixed(3) : '?') + ']';
        r.appendChild(t); svg.appendChild(r);
      });
      wrap.appendChild(svg);
    };
    wrap.appendChild(img);
  }
  function boxNote(n) {
    var note = document.createElement('div');
    note.style.cssText = 'color:#8b98a9;font-size:12px;margin-top:4px';
    note.textContent = n + ' lines — hover a box for its text + score ' +
      '(green \\u22650.8, amber \\u22650.6, red <0.6)';
    return note;
  }
  function renderBoxes(file, lines) {
    var bv = document.getElementById('boxview');
    bv.innerHTML = '';
    if (!file.type || file.type.indexOf('image') !== 0) {
      bv.textContent = 'box overlay is image-only (PDF pages are not drawn)';
      return;
    }
    if (!lines || !lines.length) return;
    var wrap = document.createElement('div'); wrap.className = 'boxwrap';
    var img = document.createElement('img'); img.src = URL.createObjectURL(file);
    paintBoxes(wrap, img, lines);
    bv.appendChild(wrap);
    bv.appendChild(boxNote(lines.length));
  }

  // Per-row "box": rebuild a saved record's overlay from disk (input image +
  // debug.json lines), so it survives refreshes and never depends on the
  // quick-test render still being on screen. Needs the job to have run boxes=1.
  function closeBoxes() {
    document.getElementById('boxmodal').style.display = 'none';
    document.getElementById('bm_body').innerHTML = '';
  }
  function openBoxes(pid) {
    var H = { headers: { Authorization: 'Bearer ' + keyEl.value } };
    var body = document.getElementById('bm_body');
    document.getElementById('bm_title').textContent = pid;
    body.textContent = 'loading...';
    document.getElementById('boxmodal').style.display = 'flex';
    fetch('/files/' + encodeURIComponent(pid) + '/debug.json', H)
      .then(function (r) { if (!r.ok) throw new Error('debug.json ' + r.status); return r.json(); })
      .then(function (dbg) {
        var lines = dbg && dbg.output && dbg.output.lines && dbg.output.lines[0];
        if (!lines || !lines.length) {
          body.innerHTML = '<div style="color:#e0b97a">No box data saved for this job.<br>' +
            'Re-run it with a <b>new key</b> and \\u201cshow boxes\\u201d ticked to capture boxes.</div>';
          return;
        }
        return fetch('/files/' + encodeURIComponent(pid) + '/input', H)
          .then(function (r) { if (!r.ok) throw new Error('input ' + r.status); return r.blob(); })
          .then(function (blob) {
            if (blob.type && blob.type.indexOf('image') !== 0) {
              body.textContent = 'box overlay is image-only (this input is not an image).';
              return;
            }
            body.innerHTML = '';
            var wrap = document.createElement('div'); wrap.className = 'boxwrap modalwrap';
            var img = document.createElement('img'); img.src = URL.createObjectURL(blob);
            paintBoxes(wrap, img, lines);
            body.appendChild(wrap);
            body.appendChild(boxNote(lines.length));
          });
      })
      .catch(function (e) {
        body.innerHTML = '<div style="color:#e07a7a">could not load boxes: ' + esc(e.message) + '</div>';
      });
  }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeBoxes(); });

  function openFile(pid, path) {
    fetch('/files/' + encodeURIComponent(pid) + '/' + path,
          { headers: { Authorization: 'Bearer ' + keyEl.value } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.blob(); })
      .then(function (b) { window.open(URL.createObjectURL(b), '_blank'); })
      .catch(function (e) { alert(path + ' not available (' + e.message + ')'); });
  }
  function openDebug(pid) { openFile(pid, 'debug.json'); }

  // Quick test: upload a file straight from the dashboard and watch it run.
  var sendBtn = document.getElementById('f_send'), fOut = document.getElementById('f_out');
  sendBtn.addEventListener('click', function () {
    if (!keyEl.value) { fOut.textContent = 'enter the API key first'; return; }
    var file = document.getElementById('f_file').files[0];
    if (!file) { fOut.textContent = 'choose a file first'; return; }
    var key = document.getElementById('f_key').value || ('dash-' + Date.now());
    var wantBoxes = document.getElementById('f_boxes').checked;
    var fd = new FormData();
    fd.append('file', file);
    fd.append('key', key);
    if (wantBoxes) fd.append('boxes', '1');         // opt-in only; never sent by Laravel
    document.getElementById('boxview').innerHTML = '';
    sendBtn.disabled = true; fOut.textContent = 'uploading...';
    fetch('/api/ocr', { method: 'POST', headers: { Authorization: 'Bearer ' + keyEl.value }, body: fd })
      .then(function (r) { return r.json().then(function (j) { return { code: r.status, body: j }; }); })
      .then(function (res) {
        var pid = res.body.process_id || key;
        fOut.textContent = 'HTTP ' + res.code + ' - ' + (res.body.status || '') + ' (' + pid + '), polling...';
        tick();
        var tries = 0;
        var iv = setInterval(function () {
          tries++;
          fetch('/api/ocr/' + encodeURIComponent(pid), { headers: { Authorization: 'Bearer ' + keyEl.value } })
            .then(function (r) { return r.json(); })
            .then(function (j) {
              if (j.status === 'done' || j.status === 'failed' || tries > 30) {
                clearInterval(iv); sendBtn.disabled = false; tick();
                if (j.status === 'done') {
                  var md = (j.result && j.result.markdown) ? j.result.markdown : '';
                  fOut.textContent = 'DONE (' + pid + ') - ' + (md ? md.slice(0, 80).replace(/\\n/g, ' ') + '...' : 'no text');
                  if (wantBoxes && j.result && j.result.lines && j.result.lines[0]) {
                    renderBoxes(file, j.result.lines[0]);
                  }
                } else if (j.status === 'failed') {
                  fOut.textContent = 'FAILED (' + pid + '): ' + (j.error || '');
                } else {
                  fOut.textContent = 'still ' + j.status + ' (' + pid + ')';
                }
              } else {
                fOut.textContent = j.status + '... (' + pid + ')';
              }
            });
        }, 2000);
      })
      .catch(function (e) { sendBtn.disabled = false; fOut.textContent = 'error: ' + e.message; });
  });

  function windowSec() { return parseInt(document.getElementById('filter').value, 10) || 0; }

  function tick() {
    if (!keyEl.value) { document.getElementById('msg').textContent = 'Enter the API key above to connect.'; return; }
    var H = { headers: { Authorization: 'Bearer ' + keyEl.value } };
    var fromV = document.getElementById('r_from').value;
    var toV = document.getElementById('r_to').value;
    var fromT = fromV ? new Date(fromV).getTime() / 1000 : null;
    var toT = toV ? new Date(toV).getTime() / 1000 : null;
    var jurl = '/api/jobs?limit=' + ((fromT || toT) ? 2000 : 200);
    if (fromT) jurl += '&since=' + Math.floor(fromT);
    if (toT) jurl += '&until=' + Math.ceil(toT);
    Promise.all([fetch('/api/status', H), fetch(jurl, H)])
      .then(function (rs) {
        if (rs[0].status === 401 || rs[1].status === 401) throw new Error('invalid API key');
        return Promise.all([rs[0].json(), rs[1].json()]);
      })
      .then(function (d) {
        var st = d[0], jd = d[1], by = st.by_status || {};
        document.getElementById('msg').textContent = '';
        document.getElementById('c_state').textContent = st.state;
        document.getElementById('c_seats').textContent = st.in_flight + ' / ' + st.seats;
        document.getElementById('c_queued').textContent = by.queued || 0;
        document.getElementById('c_done').textContent = by.done || 0;
        document.getElementById('c_failed').textContent = by.failed || 0;
        document.getElementById('c_gpu').textContent = st.device + (st.gpu_available ? ' (gpu)' : '');
        document.getElementById('upd').textContent = 'updated ' + new Date().toLocaleTimeString();
        var now = jd.now, html = '';
        var shown;
        if (fromT || toT) {
          shown = (jd.jobs || []).filter(function (j) {
            return (fromT === null || j.created_at >= fromT) && (toT === null || j.created_at <= toT);
          });
        } else {
          var win = windowSec();
          shown = (jd.jobs || []).filter(function (j) { return win === 0 || (now - j.created_at) <= win; });
        }
        shown.forEach(function (j) {
          var waited = j.claimed_at ? j.claimed_at - j.created_at : now - j.created_at;
          var run = j.claimed_at ? ((j.status === 'processing' ? now : j.updated_at) - j.claimed_at) : null;
          html += '<tr><td class="id" title="' + esc(j.process_id) + '">' + esc(j.process_id) + '</td>'
            + '<td><span class="s ' + esc(j.status) + '">' + esc(j.status) + '</span></td>'
            + '<td>' + esc(j.worker || '-') + '</td>'
            + '<td>' + fmtDur(waited) + '</td><td>' + fmtDur(run) + '</td>'
            + '<td>' + new Date(j.updated_at * 1000).toLocaleTimeString() + '</td>'
            + '<td class="err" title="' + esc(j.error || '') + '">' + esc(j.error || '') + '</td>'
            + '<td>'
            + '<button onclick="openFile(\\'' + esc(j.process_id) + '\\',\\'input\\')">input</button> '
            + '<button onclick="openFile(\\'' + esc(j.process_id) + '\\',\\'result.md\\')">md</button> '
            + '<button onclick="openDebug(\\'' + esc(j.process_id) + '\\')">debug</button> '
            + (j.has_boxes
                ? '<button onclick="openBoxes(\\'' + esc(j.process_id) + '\\')">box</button>'
                : '<button disabled title="no box data \\u2014 re-run with a new key + \\u201cshow boxes\\u201d">box</button>')
            + '</td></tr>';
        });
        document.getElementById('rows').innerHTML =
          html || '<tr><td colspan="8" style="color:#8b98a9">no jobs in this time window</td></tr>';
      })
      .catch(function (e) { document.getElementById('msg').textContent = 'Error: ' + e.message; });
  }
  document.getElementById('filter').addEventListener('change', tick);
  document.getElementById('r_from').addEventListener('change', tick);
  document.getElementById('r_to').addEventListener('change', tick);
  document.getElementById('r_clear').addEventListener('click', function () {
    document.getElementById('r_from').value = '';
    document.getElementById('r_to').value = '';
    tick();
  });
  tick();
  setInterval(tick, 3000);
</script>
</body>
</html>"""


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> HTMLResponse:
    """Lightweight live dashboard: seats, queue, recent jobs. The page is static;
    all data comes from the bearer-protected /api/status + /api/jobs."""
    return HTMLResponse(_DASHBOARD_HTML)
