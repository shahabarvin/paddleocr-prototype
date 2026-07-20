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

import hashlib
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
MAX_UPLOAD_BYTES = int(os.environ.get("OCR_MAX_UPLOAD_MB", "20")) * 1024 * 1024
WORKER_ID = str(os.getpid())

app = FastAPI(title="PaddleOCR OCR service",
              docs_url=None, redoc_url=None, openapi_url=None)
service = OCRService()
store = Store(DB_PATH, seats=SEATS)
_ready = {"state": "loading", "error": None}


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
    store.init()
    if auth_enabled() and not has_any_key():
        print("WARNING: auth is ON but no API keys exist yet. All requests will "
              "be rejected until you run:  python keys.py create \"<label>\"  "
              "(or set OCR_AUTH=off for local dev).")
    threading.Thread(target=_warmup, daemon=True).start()
    threading.Thread(target=_worker_loop, daemon=True).start()


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
    while _ready["state"] == "loading":
        time.sleep(0.2)
    if _ready["state"] != "ready":
        return
    ticks = 0
    while True:
        # Cheap read first; only take the write lock when there's work to claim.
        if not store.has_queued():
            time.sleep(0.05)              # idle poll
            ticks += 1
            if ticks % 400 == 0:          # ~every 20s idle, purge expired rows
                try:
                    store.cleanup()
                except Exception:
                    pass
            continue
        job = store.claim_next(WORKER_ID)
        if job is not None:
            _run(job)


def _run(job: dict) -> None:
    pid = job["process_id"]
    opt = job["options"]
    try:
        input_path = Path(opt["input_path"])
        viz_dir = (JOBS_DIR / pid / "viz") if opt.get("overlays") else None
        t0 = time.perf_counter()
        result = service.process(
            input_path, viz_dir=viz_dir, tier=opt.get("tier", "server"),
            device=opt.get("device"), enhance=bool(opt.get("enhance")),
        )
        result["timings"]["server_total_seconds"] = round(time.perf_counter() - t0, 3)
        result["process_id"] = pid
        result["worker"] = WORKER_ID
        # persist the text artifacts alongside the input (durable, and handy
        # for debugging); the JSON result is the store's source of truth.
        job_dir = JOBS_DIR / pid
        (job_dir / "result.md").write_text(result.get("markdown", ""), encoding="utf-8")
        (job_dir / "result.txt").write_text(result.get("layout_text", ""), encoding="utf-8")
        result["visualizations"] = [
            {"name": Path(p).name, "url": f"/files/{pid}/viz/{Path(p).name}"}
            for p in result.get("visualizations", [])
        ]
        store.complete(pid, result)
    except Exception as err:
        store.fail(pid, f"{type(err).__name__}: {err}")


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
) -> JSONResponse:
    if _ready["state"] == "error":
        raise HTTPException(503, f"Model failed to load: {_ready['error']}")
    if _ready["state"] != "ready":
        raise HTTPException(503, "warming up; retry shortly")

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
    job_dir = JOBS_DIR / process_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{ext}"
    if not input_path.exists():
        input_path.write_bytes(data)

    options = {
        "tier": model,
        "device": (device or None),
        "enhance": (enhance == "1"),
        "overlays": (overlays == "1"),
        "input_path": str(input_path),
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


@app.get("/files/{process_id}/{path:path}", dependencies=[Depends(require_auth)])
def serve_file(process_id: str, path: str) -> FileResponse:
    target = (JOBS_DIR / _slug(process_id) / path).resolve()
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
