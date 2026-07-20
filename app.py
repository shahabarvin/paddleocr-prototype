"""
PaddleOCR PP-StructureV3 local prototype.

A minimal web app to evaluate PaddleOCR (with layout analysis) as an
LLM-preprocessing step: upload a JPG/PNG/PDF, get back layout-aware
markdown plus timing breakdown and detection overlays.

Run:            python app.py
Then open:      http://127.0.0.1:8000

Notes:
- Models are loaded once, in a background thread at server start.
  The very first start also DOWNLOADS the models (~1-2 GB for the
  server-grade models) into ~/.paddlex/official_models; that download
  is part of "model load", never of the reported inference time.
- The `OCRService` class below is framework-agnostic on purpose:
  it is the part meant to be reused by the future production API.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------
# OCR service  (reusable core -- no web-framework imports in this section)
# --------------------------------------------------------------------------

MODELS_CACHE_DIR = Path.home() / ".paddlex" / "official_models"


class OCRService:
    """Thin, thread-safe wrapper around PaddleOCR's PP-StructureV3 pipeline.

    - ``load()`` builds the pipeline (downloads models on first ever run).
    - ``process()`` runs layout analysis + OCR on an image or PDF and
      returns a JSON-serializable dict with markdown, timings and
      visualization image paths.
    """

    TIERS = ("server", "mobile")

    def __init__(self, device: Optional[str] = None):
        # Preferred device for the default (unspecified) request. GPU by
        # default; a request may override per-call, and any gpu request
        # transparently falls back to cpu when no CUDA build is present.
        self.default_device = self._normalize_device(
            device or os.environ.get("OCR_DEVICE", "gpu")
        )
        # Pipelines are cached per (tier, device): the same tier can be held
        # loaded on both gpu and cpu at once, so switching is free after warmup.
        self._pipelines: dict[tuple[str, str], Any] = {}
        self._load_meta: dict[tuple[str, str], dict] = {}  # (tier, device) -> stats
        self._load_lock = threading.Lock()
        self._predict_lock = threading.Lock()  # pipelines are not thread-safe
        self.load_error: Optional[str] = None

    # -- device selection --------------------------------------------------

    VALID_DEVICES = ("gpu", "cpu")

    @classmethod
    def _normalize_device(cls, device: Optional[str]) -> str:
        """Fold user/env input into 'gpu' or 'cpu' (unset/'auto' -> 'gpu')."""
        d = (device or "").strip().lower()
        if d in ("", "auto", "cuda", "gpu:0"):
            return "gpu" if d != "cpu" else "cpu"
        if d.startswith("gpu") or d == "cpu":
            return "gpu" if d.startswith("gpu") else "cpu"
        return "cpu"

    @staticmethod
    def gpu_available() -> bool:
        """True only if PaddlePaddle is a CUDA build with a visible device."""
        try:
            import paddle
            return (paddle.device.is_compiled_with_cuda()
                    and paddle.device.cuda.device_count() > 0)
        except Exception:
            return False

    def _resolve_device(self, device: Optional[str]) -> tuple[str, Optional[str]]:
        """Map a requested device to one we can actually run on.

        Returns (effective_device, note). A gpu request on a machine with no
        CUDA-enabled PaddlePaddle silently falls back to cpu; the note is
        surfaced through the API so the UI can show what really happened.
        """
        want = self._normalize_device(
            device if device is not None else self.default_device
        )
        if want == "gpu" and not self.gpu_available():
            return "cpu", ("GPU requested but unavailable — PaddlePaddle is a "
                           "CPU-only build or no CUDA device is present; running "
                           "on CPU. Install paddlepaddle-gpu to enable GPU.")
        return want, None

    # -- loading -----------------------------------------------------------

    def is_loaded(self, tier: str = "server", device: Optional[str] = None) -> bool:
        eff, _ = self._resolve_device(device)
        return (tier, eff) in self._pipelines

    def load_meta(self) -> dict:
        return self._load_meta

    def _kwarg_attempts(self, tier: str, device: str) -> list[dict]:
        """Constructor kwargs, best config first; unknown kwargs or model
        names fall through to the next attempt (paddleocr minor versions
        differ in what they accept).

        oneDNN (a CPU-only optimization) is disabled on Windows and whenever
        the device is gpu: a paddlepaddle 3.x bug makes its executor fail on
        the RT-DETR-based layout model ("ConvertPirAttribute2RuntimeAttribute
        not support pir::ArrayAttribute"). On Linux CPU it works and is a
        several-fold speedup, so it stays on there. Override with OCR_MKLDNN=0/1.
        """
        mkldnn_default = "0" if sys.platform == "win32" else "1"
        mkldnn = (os.environ.get("OCR_MKLDNN", mkldnn_default) == "1"
                  and device != "gpu")
        common = dict(
            device=device,
            enable_mkldnn=mkldnn,
            cpu_threads=os.cpu_count() or 8,
            use_doc_orientation_classify=True,
            use_doc_unwarping=os.environ.get("OCR_UNWARP", "0") == "1",
            use_textline_orientation=True,
        )
        if tier == "server":
            # Highest quality: server det/rec plus all default sub-pipelines
            # (largest layout, table and formula models).
            names = dict(
                text_detection_model_name="PP-OCRv5_server_det",
                text_recognition_model_name="PP-OCRv5_server_rec",
            )
            return [
                {**common, **names},
                {"device": device, "enable_mkldnn": mkldnn, **names},
                {"device": device, "enable_mkldnn": mkldnn},
                {"device": device},
                {},
            ]
        # Fast profile: mobile det/rec, small layout model, and formula/seal
        # recognition off (they dominate runtime and rarely matter for
        # app documents).
        names = dict(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
        )
        fast_flags = dict(
            use_formula_recognition=False,
            use_seal_recognition=False,
            # Skips the RT-DETR region-grouping pass; only refines reading
            # order on complex multi-column pages.
            use_region_detection=False,
            # Table STRUCTURE recognition triggers RT-DETR cell detection —
            # the single heaviest step on CPU. Table text is still OCR'd;
            # markdown just loses the <table> markup. Use the server tier
            # when table structure matters.
            use_table_recognition=False,
        )
        return [
            {**common, **names, **fast_flags,
             "layout_detection_model_name": "PP-DocLayout-S"},
            {**common, **names,
             "use_formula_recognition": False, "use_seal_recognition": False,
             "use_table_recognition": False,
             "layout_detection_model_name": "PP-DocLayout-S"},
            {**common, **names},
            {"device": device, "enable_mkldnn": mkldnn, **names},
        ]

    def load(self, tier: str = "server", device: Optional[str] = None) -> float:
        """Build the pipeline for a (tier, device). Idempotent; returns load
        seconds. A gpu request with no CUDA build loads on cpu instead."""
        if tier not in self.TIERS:
            raise ValueError(f"Unknown model tier: {tier!r}")
        eff, note = self._resolve_device(device)
        key = (tier, eff)
        with self._load_lock:
            if key in self._pipelines:
                return self._load_meta[key]["load_seconds"]

            cache_populated = MODELS_CACHE_DIR.is_dir() and any(
                MODELS_CACHE_DIR.iterdir()
            )

            from paddleocr import PPStructureV3

            t0 = time.perf_counter()
            pipeline = None
            last_err: Optional[Exception] = None
            for kwargs in self._kwarg_attempts(tier, eff):
                try:
                    pipeline = PPStructureV3(**kwargs)
                    break
                except (TypeError, ValueError, KeyError) as err:
                    last_err = err
                    continue
            if pipeline is None:
                self.load_error = f"{type(last_err).__name__}: {last_err}"
                raise RuntimeError(f"Failed to build PPStructureV3: {last_err}")

            load_seconds = round(time.perf_counter() - t0, 3)
            self._pipelines[key] = pipeline
            self._load_meta[key] = {
                "load_seconds": load_seconds,
                "included_download": not cache_populated,
                "device": eff,
                "device_note": note,
            }
            return load_seconds

    # -- inference ---------------------------------------------------------

    def process(
        self,
        input_path: str | Path,
        viz_dir: Optional[str | Path] = None,
        tier: str = "server",
        device: Optional[str] = None,
        enhance: bool = False,
    ) -> dict[str, Any]:
        """Run PP-StructureV3 on one image/PDF file.

        ``enhance`` runs a contrast/denoise pre-pass (see ``_enhance_image``)
        for faded or noisy images; ignored for PDFs. Returns dict with keys:
        markdown, layout_text, pages, timings, device, enhanced, visualizations.
        """
        eff, note = self._resolve_device(device)
        loaded_before = (tier, eff) in self._pipelines
        load_seconds = self.load(tier, device)  # no-op if already loaded
        pipeline = self._pipelines[(tier, eff)]

        # Optional pre-processing: enhance faded/noisy images and feed the
        # cleaned pixels (ndarray) to the pipeline instead of the raw file.
        predict_input: Any = str(input_path)
        enhanced_applied = False
        if enhance and Path(input_path).suffix.lower() != ".pdf":
            import cv2
            raw = cv2.imread(str(input_path))
            if raw is not None:
                enhanced = self._enhance_image(raw)
                predict_input = enhanced
                enhanced_applied = True
                if viz_dir is not None:
                    Path(viz_dir).mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(Path(viz_dir) / "00_input_enhanced.png"), enhanced)

        t0 = time.perf_counter()
        with self._predict_lock:
            outputs = list(pipeline.predict(predict_input))
        inference_seconds = round(time.perf_counter() - t0, 3)

        t1 = time.perf_counter()
        markdown = self._to_markdown(pipeline, outputs)
        layout_text = self._layout_text(outputs)
        viz_files: list[str] = []
        if viz_dir is not None:
            viz_files = self._save_visualizations(outputs, Path(viz_dir))
        postprocess_seconds = round(time.perf_counter() - t1, 3)

        return {
            "pages": len(outputs),
            "markdown": markdown,
            "layout_text": layout_text,
            "model_tier": tier,
            "device": eff,
            "device_note": note,
            "enhanced": enhanced_applied,
            "timings": {
                "model_load_seconds": load_seconds,
                "model_loaded_during_request": not loaded_before,
                "model_load_included_download":
                    self._load_meta[(tier, eff)]["included_download"],
                "inference_seconds": inference_seconds,
                "postprocess_seconds": postprocess_seconds,
            },
            "visualizations": viz_files,
        }

    # -- helpers -----------------------------------------------------------

    _ALNUM_RE = re.compile(r"[^\W_]")  # any unicode letter or digit

    @classmethod
    def _is_noise(cls, text: str) -> bool:
        """True for decorative separator boxes ('+++***+*... * ******'):
        3+ chars, no letter or digit in any script, and not a markdown
        horizontal rule."""
        s = text.strip()
        return len(s) >= 3 and s != "---" and not cls._ALNUM_RE.search(s)

    @staticmethod
    def _normalize_fullwidth(text: str) -> str:
        """Fold full-width ASCII (U+FF01-FF5E) and the ideographic space to
        their half-width forms. The rec model occasionally emits e.g. a
        full-width colon ('Receipt #：') on CJK-trained glyphs; this is a
        lossless, purely typographic fix that never invents content."""
        out = []
        for ch in text:
            o = ord(ch)
            if 0xFF01 <= o <= 0xFF5E:
                out.append(chr(o - 0xFEE0))
            elif o == 0x3000:
                out.append(" ")
            else:
                out.append(ch)
        return "".join(out)

    @classmethod
    def _strip_noise_lines(cls, text: str) -> str:
        """Normalize full-width punctuation, then drop lines with content but
        no letter/digit in any script (separator fragments); keep blank lines
        and '---' page breaks."""
        text = cls._normalize_fullwidth(text)
        return "\n".join(
            line for line in text.splitlines()
            if not line.strip() or line.strip() == "---"
            or cls._ALNUM_RE.search(line)
        )

    def _to_markdown(self, pipeline, outputs: list) -> str:
        """Combine per-page results into one markdown string, defensively
        across paddleocr 3.x minor-version API differences."""
        md_pages = []
        for res in outputs:
            md = getattr(res, "markdown", None)
            if isinstance(md, dict):
                md_pages.append(md)
            elif isinstance(md, str):
                md_pages.append({"markdown_texts": md})
            else:
                # Last-resort fallback: join raw recognized lines.
                try:
                    texts = res["overall_ocr_res"]["rec_texts"]
                    md_pages.append({"markdown_texts": "\n".join(texts)})
                except Exception:
                    md_pages.append({"markdown_texts": ""})

        concat = getattr(pipeline, "concatenate_markdown_pages", None)
        if concat is not None:
            try:
                combined = concat(md_pages)
                if isinstance(combined, tuple):  # some versions return (text, images)
                    combined = combined[0]
                if isinstance(combined, str):
                    return self._strip_noise_lines(combined)
            except Exception:
                pass
        return self._strip_noise_lines("\n\n---\n\n".join(
            p.get("markdown_texts", "") if isinstance(p, dict) else str(p)
            for p in md_pages
        ))

    def _layout_text(self, outputs: list) -> str:
        """Reconstruct plain text that mirrors the page's spatial layout by
        placing each raw OCR box on a monospace character grid.

        Complements the markdown view: documents with no visible structure
        (receipts, forms, tickets) get flattened into paragraphs by the
        layout model, while their line breaks and column alignment survive
        here.
        """
        pages = []
        for res in outputs:
            try:
                ocr = res["overall_ocr_res"]
                texts = list(ocr["rec_texts"])
                boxes = ocr.get("rec_boxes")
                if boxes is None or len(boxes) == 0:
                    boxes = [self._poly_to_bbox(p) for p in ocr["rec_polys"]]
                items = [
                    (float(b[0]), float(b[1]), float(b[2]), float(b[3]), str(t))
                    for b, t in zip(boxes, texts)
                    if str(t).strip() and not self._is_noise(str(t))
                ]
                pages.append(self._spatial_text(items))
            except Exception:
                pages.append("")
        return "\n\n---\n\n".join(pages)

    @staticmethod
    def _poly_to_bbox(poly) -> tuple[float, float, float, float]:
        xs = [float(pt[0]) for pt in poly]
        ys = [float(pt[1]) for pt in poly]
        return min(xs), min(ys), max(xs), max(ys)

    @classmethod
    def _spatial_text(cls, items: list[tuple[float, float, float, float, str]]) -> str:
        """items: (x1, y1, x2, y2, text) in pixels -> monospace-grid text."""
        import statistics

        if not items:
            return ""
        med_h = statistics.median(y2 - y1 for _, y1, _, y2, _ in items) or 1.0
        char_w = statistics.median(
            (x2 - x1) / max(len(t), 1) for x1, _, x2, _, t in items
        ) or 8.0
        min_x = min(x1 for x1, _, _, _, _ in items)

        # Group boxes into visual lines by vertical center proximity.
        items = sorted(items, key=lambda it: ((it[1] + it[3]) / 2, it[0]))
        lines: list[list] = []  # [y_center, [items]]
        for it in items:
            yc = (it[1] + it[3]) / 2
            if lines and abs(yc - lines[-1][0]) <= med_h * 0.6:
                lines[-1][1].append(it)
            else:
                lines.append([yc, [it]])

        out, prev_y = [], None
        for yc, line_items in lines:
            line_items.sort(key=lambda it: it[0])
            s = ""
            for x1, _, _, _, t in line_items:
                col = int(round((x1 - min_x) / char_w))
                if col > len(s):
                    s += " " * (col - len(s))
                elif s and not s.endswith(" "):
                    s += " "
                s += t
            if prev_y is not None and yc - prev_y > 2.2 * med_h:
                out.append("")  # blank line for large vertical gaps
            out.append(s.rstrip())
            prev_y = yc
        return cls._strip_noise_lines("\n".join(out))

    @staticmethod
    def _save_visualizations(outputs: list, viz_dir: Path) -> list[str]:
        viz_dir.mkdir(parents=True, exist_ok=True)
        for res in outputs:
            try:
                res.save_to_img(str(viz_dir))
            except Exception:
                pass
        return sorted(
            str(p) for p in viz_dir.iterdir()
            if p.suffix.lower() in {".png", ".jpg", ".jpeg"}
        )

    @staticmethod
    def _enhance_image(bgr, binarize: bool = False):
        """Rescue faded / low-contrast / noisy captures (thermal receipts,
        crumpled or smudged paper) *before* OCR.

        Pipeline: grayscale -> edge-preserving denoise (kills fingerprint and
        paper speckle) -> CLAHE local contrast (the big lever for faded thermal
        print) -> upscale small images so thin strokes survive detection.

        Deep recognition models generally read grayscale-contrast better than
        hard black/white, so adaptive binarization is opt-in (``binarize``) for
        only the faintest documents. Returns a 3-channel BGR ndarray.
        """
        import cv2
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.fastNlMeansDenoising(
            gray, None, h=10, templateWindowSize=7, searchWindowSize=21
        )
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        h, w = gray.shape[:2]
        if max(h, w) < 1800:  # upscale small phone captures
            gray = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        if binarize:
            gray = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 15,
            )
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


# --------------------------------------------------------------------------
# Web layer (FastAPI) -- prototype-only; the future API replaces this part
# --------------------------------------------------------------------------

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

app = FastAPI(title="PaddleOCR prototype")
service = OCRService()

JOBS_DIR = Path(__file__).parent / "output"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ".bmp", ".webp"}
_load_state = {"state": "loading", "error": None}


def _warmup() -> None:
    try:
        service.load("server")
        _load_state["state"] = "ready"
    except Exception as err:  # surfaced via /api/status
        _load_state["state"] = "error"
        _load_state["error"] = str(err)


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_warmup, daemon=True).start()


@app.get("/api/status")
def status() -> dict:
    meta = service.load_meta()
    server_meta = next(
        (m for (tier, _dev), m in meta.items() if tier == "server"), {}
    )
    return {
        "state": _load_state["state"],
        "error": _load_state["error"],
        "model_load_seconds": server_meta.get("load_seconds"),
        "model_load_included_download": server_meta.get("included_download"),
        "tiers_loaded": sorted(f"{t}:{d}" for (t, d) in meta.keys()),
        "device": server_meta.get("device", service.default_device),
        "default_device": service.default_device,
        "gpu_available": service.gpu_available(),
        "device_note": server_meta.get("device_note"),
    }


@app.post("/api/ocr")
def run_ocr(
    file: UploadFile = File(...),
    model: str = Form("server"),
    device: str = Form(""),
    overlays: str = Form("1"),
    enhance: str = Form("0"),
) -> JSONResponse:
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
    if _load_state["state"] == "error":
        raise HTTPException(503, f"Model failed to load: {_load_state['error']}")

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{ext}"
    input_path.write_bytes(file.file.read())

    t0 = time.perf_counter()
    try:
        viz_dir = (job_dir / "viz") if overlays == "1" else None
        result = service.process(
            input_path, viz_dir=viz_dir, tier=model, device=(device or None),
            enhance=(enhance == "1"),
        )
    except Exception as err:
        raise HTTPException(500, f"OCR failed: {type(err).__name__}: {err}")
    result["timings"]["server_total_seconds"] = round(time.perf_counter() - t0, 3)

    (job_dir / "result.md").write_text(result["markdown"], encoding="utf-8")
    (job_dir / "result.txt").write_text(result["layout_text"], encoding="utf-8")

    result["job_id"] = job_id
    result["filename"] = file.filename
    result["original_url"] = f"/files/{job_id}/{input_path.name}"
    result["visualizations"] = [
        {"name": Path(p).name, "url": f"/files/{job_id}/viz/{Path(p).name}"}
        for p in result["visualizations"]
    ]
    return JSONResponse(result)


@app.get("/files/{job_id}/{path:path}")
def serve_file(job_id: str, path: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404)
    target = (JOBS_DIR / job_id / path).resolve()
    if not target.is_file() or JOBS_DIR.resolve() not in target.parents:
        raise HTTPException(404)
    return FileResponse(target)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


# --------------------------------------------------------------------------
# Frontend (single embedded page)
# --------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PaddleOCR prototype</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #101418; color: #dde3ea;
         font: 15px/1.5 system-ui, "Segoe UI", sans-serif; }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 24px 20px 80px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #8b98a5; font-size: 13px; margin-bottom: 20px; }
  .card { background: #171d24; border: 1px solid #263039; border-radius: 10px;
          padding: 16px; margin-bottom: 16px; }
  .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  #status { padding: 4px 12px; border-radius: 999px; font-size: 13px;
            background: #3a2f10; color: #eac54f; }
  #status.ready { background: #12351f; color: #56d364; }
  #status.error { background: #3d1519; color: #ff7b72; }
  input[type=file] { color: #8b98a5; }
  select { background: #21262d; color: #dde3ea; border: 1px solid #263039;
           border-radius: 6px; padding: 8px 10px; font-size: 14px; }
  button { background: #1f6feb; color: #fff; border: 0; border-radius: 6px;
           padding: 8px 18px; font-size: 14px; cursor: pointer; }
  button:disabled { background: #30363d; color: #8b98a5; cursor: default; }
  .chips { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
  .chip { background: #0d1117; border: 1px solid #263039; border-radius: 6px;
          padding: 6px 12px; font-size: 13px; }
  .chip b { color: #79c0ff; font-variant-numeric: tabular-nums; }
  .chip small { color: #8b98a5; }
  .tabs { display: flex; gap: 4px; margin-bottom: 10px; }
  .tabs button { background: #21262d; color: #b9c4cf; }
  .tabs button.active { background: #1f6feb; color: #fff; }
  pre { background: #0d1117; border: 1px solid #263039; border-radius: 8px;
        padding: 14px; overflow: auto; max-height: 65vh; white-space: pre-wrap;
        font: 13px/1.45 Consolas, monospace; }
  #ltext { white-space: pre; } /* keep column alignment */
  #preview { background: #0d1117; border: 1px solid #263039; border-radius: 8px;
             padding: 14px 20px; overflow: auto; max-height: 65vh; }
  #preview table { border-collapse: collapse; }
  #preview td, #preview th { border: 1px solid #3a4550; padding: 4px 10px; }
  #preview img { max-width: 100%; }
  .imgs { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
          gap: 14px; }
  .imgs figure { margin: 0; }
  .imgs img { width: 100%; border: 1px solid #263039; border-radius: 6px;
              cursor: zoom-in; }
  .imgs figcaption { font-size: 12px; color: #8b98a5; margin-top: 4px;
                     word-break: break-all; }
  .hidden { display: none; }
  #err { color: #ff7b72; white-space: pre-wrap; }
  a { color: #79c0ff; }
</style>
</head>
<body>
<div class="wrap">
  <h1>PaddleOCR &middot; PP-StructureV3 prototype</h1>
  <div class="sub">Layout-aware OCR &rarr; markdown, as an LLM preprocessing step</div>

  <div class="card row">
    <span id="status">loading models&hellip; <span id="loadTimer">0</span>s</span>
    <span id="loadInfo" class="sub" style="margin:0"></span>
  </div>

  <div class="card row">
    <input type="file" id="file" accept=".jpg,.jpeg,.png,.pdf,.bmp,.webp">
    <select id="tier" title="Model quality/speed trade-off">
      <option value="server">Quality — server models (slow on CPU)</option>
      <option value="mobile">Fast — mobile models, no table/formula structure</option>
    </select>
    <select id="device" title="Compute device">
      <option value="gpu">GPU</option>
      <option value="cpu">CPU</option>
    </select>
    <label style="font-size:13px;color:#8b98a5;cursor:pointer">
      <input type="checkbox" id="viz" checked> overlays
    </label>
    <label style="font-size:13px;color:#8b98a5;cursor:pointer"
           title="Denoise + contrast pre-pass for faded/crumpled/low-contrast scans (images only)">
      <input type="checkbox" id="enhance"> enhance
    </label>
    <button id="run" disabled>Run OCR</button>
    <span id="runTimer" class="sub" style="margin:0"></span>
  </div>

  <div id="err" class="card hidden"></div>

  <div id="results" class="hidden">
    <div class="card">
      <div class="chips" id="chips"></div>
    </div>
    <div class="card">
      <div class="tabs">
        <button data-tab="md" class="active">Markdown (raw)</button>
        <button data-tab="ltext">Layout text</button>
        <button data-tab="preview">Rendered</button>
        <button data-tab="overlays">Overlays</button>
      </div>
      <pre id="md"></pre>
      <pre id="ltext" class="hidden"></pre>
      <div id="preview" class="hidden"></div>
      <div id="overlays" class="hidden">
        <p class="sub">Pipeline visualizations (layout regions, text boxes, preprocessing).
           Click to open full size. <a id="origLink" target="_blank">Open original upload</a></p>
        <div class="imgs" id="imgGrid"></div>
      </div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = s => s == null ? "–" : (s >= 60 ? (s/60).toFixed(1) + " min" : s.toFixed(2) + " s");

// ---- model-load status polling ----
let loadStart = Date.now();
const loadTick = setInterval(() => $("loadTimer").textContent =
    Math.round((Date.now() - loadStart) / 1000), 500);

async function poll() {
  try {
    const s = await (await fetch("/api/status")).json();
    if (s.state === "ready") {
      clearInterval(loadTick);
      $("status").className = "ready";
      $("status").textContent = "models ready";
      $("loadInfo").textContent =
        `load took ${fmt(s.model_load_seconds)} on ${s.device}` +
        (s.model_load_included_download ? " (first run: includes model download)" : "") +
        (s.device_note ? " — " + s.device_note : "");
      // Reflect actual availability: if GPU isn't usable, preselect CPU so the
      // dropdown doesn't imply a GPU run that will silently fall back.
      if (!s.gpu_available) $("device").value = "cpu";
      $("run").disabled = false;
      return;
    }
    if (s.state === "error") {
      clearInterval(loadTick);
      $("status").className = "error";
      $("status").textContent = "model load failed";
      showErr(s.error);
      return;
    }
  } catch (e) { /* server still starting */ }
  setTimeout(poll, 1000);
}
poll();

function showErr(msg) { $("err").textContent = msg; $("err").classList.remove("hidden"); }

// ---- run OCR ----
let runTick = null;
$("run").onclick = async () => {
  const f = $("file").files[0];
  if (!f) { showErr("Choose a file first."); return; }
  $("err").classList.add("hidden");
  $("results").classList.add("hidden");
  $("run").disabled = true;
  const t0 = Date.now();
  runTick = setInterval(() => $("runTimer").textContent =
      "processing… " + ((Date.now() - t0) / 1000).toFixed(1) + " s", 100);

  const fd = new FormData();
  fd.append("file", f);
  fd.append("model", $("tier").value);
  fd.append("device", $("device").value);
  fd.append("overlays", $("viz").checked ? "1" : "0");
  fd.append("enhance", $("enhance").checked ? "1" : "0");
  try {
    const resp = await fetch("/api/ocr", { method: "POST", body: fd });
    const roundTrip = (Date.now() - t0) / 1000;
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || resp.statusText);
    }
    render(await resp.json(), roundTrip);
  } catch (e) {
    showErr("Request failed: " + e.message);
  } finally {
    clearInterval(runTick);
    $("runTimer").textContent = "";
    $("run").disabled = false;
  }
};

function chip(label, value, note) {
  return `<span class="chip">${label}: <b>${value}</b>${note ? ` <small>${note}</small>` : ""}</span>`;
}

function render(r, roundTrip) {
  const t = r.timings;
  $("chips").innerHTML =
    chip("Models", r.model_tier === "mobile" ? "fast (mobile)" : "quality (server)") +
    chip("Device", (r.device || "cpu").toUpperCase(), r.device_note ? "fell back from GPU" : "") +
    (r.enhanced ? chip("Pre-processing", "enhanced", "denoise + contrast") : "") +
    chip("Inference", fmt(t.inference_seconds), r.pages + " page(s)") +
    chip("Post-processing", fmt(t.postprocess_seconds), "markdown + overlays") +
    chip("Server total", fmt(t.server_total_seconds)) +
    chip("Round trip", fmt(roundTrip)) +
    chip("Model load", fmt(t.model_load_seconds),
         (t.model_loaded_during_request ? "loaded during this request" : "cached") +
         (t.model_load_included_download ? ", incl. download" : ""));

  $("md").textContent = r.markdown || "(no text found)";
  $("ltext").textContent = r.layout_text || "(no text found)";
  try { $("preview").innerHTML = marked.parse(r.markdown || ""); }
  catch (e) { $("preview").textContent = r.markdown; }

  $("origLink").href = r.original_url;
  $("imgGrid").innerHTML = r.visualizations.map(v =>
    `<figure><a href="${v.url}" target="_blank"><img src="${v.url}" loading="lazy"></a>
     <figcaption>${v.name}</figcaption></figure>`).join("")
    || "<p class='sub'>No visualization images were produced.</p>";

  $("results").classList.remove("hidden");
}

// ---- tabs ----
document.querySelectorAll(".tabs button").forEach(btn => btn.onclick = () => {
  document.querySelectorAll(".tabs button").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  ["md", "ltext", "preview", "overlays"].forEach(id =>
    $(id).classList.toggle("hidden", id !== btn.dataset.tab));
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("OCR_HOST", "127.0.0.1")  # 0.0.0.0 for remote access
    port = int(os.environ.get("OCR_PORT", "8000"))
    print(f"Starting PaddleOCR prototype at http://{host}:{port}")
    print("(first ever start downloads the models -- watch the console)")
    uvicorn.run(app, host=host, port=port)
