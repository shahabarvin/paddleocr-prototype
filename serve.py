"""Launch the OCR service as N worker processes on one endpoint.

    python serve.py

Each worker loads its own GPU pipeline and runs a pull loop; they share seats,
the job queue and results through state.db (SQLite). One card fits ~2 server
pipelines, so 2 workers is the default.

Env: OCR_WORKERS (default 2), OCR_HOST (127.0.0.1), OCR_PORT (8000),
     OCR_SEATS (3), OCR_DEVICE (gpu).
"""
import os

import uvicorn

if __name__ == "__main__":
    workers = int(os.environ.get("OCR_WORKERS", "2"))
    host = os.environ.get("OCR_HOST", "127.0.0.1")
    port = int(os.environ.get("OCR_PORT", "8000"))
    print(f"Starting OCR service: {workers} worker(s) on http://{host}:{port} "
          f"(seats={os.environ.get('OCR_SEATS', '3')})")
    # workers>1 needs the import-string form so uvicorn can spawn processes.
    uvicorn.run("service:app", host=host, port=port, workers=workers)
