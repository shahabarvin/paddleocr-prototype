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

- **First ever start downloads the models (~1–2 GB)** into
  `~/.paddlex/official_models`. That happens during "model load" at server
  startup and is never counted in the reported inference time.
- Inference runs on CPU by default (`OCR_DEVICE=gpu` to override — the local
  MX450 2 GB is too small for the server models).
- `OCR_UNWARP=1` enables document unwarping (useful for phone photos of
  curved pages; off by default).
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
2. These are the largest server-grade models on laptop CPU. On a GPU server
   the same pipeline typically runs 1–3 s/page. The local MX450 (2 GB) is too
   small to try.

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

## Reuse for the production API

`OCRService` in [app.py](app.py) is framework-agnostic (no FastAPI imports):
`load()` once at process start, `process(path)` per document, returns a
JSON-serializable dict. Lift that class into the real service and put your
API framework of choice around it.
