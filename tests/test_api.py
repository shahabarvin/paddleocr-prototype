"""API integration tests via FastAPI TestClient.

The GPU/model work is mocked (service.load / service.process are patched), so
these run without a GPU. They exercise the real endpoints, auth, seat/async
flow, size limit and public docs.
"""
import json
import time

import pytest


def _fake_process(input_path, viz_dir=None, tier="server", device=None, enhance=False):
    return {
        "pages": 1, "markdown": "# hi", "layout_text": "hi", "model_tier": tier,
        "device": "cpu", "device_note": None, "enhanced": bool(enhance),
        "timings": {"model_load_seconds": 0, "model_loaded_during_request": False,
                    "model_load_included_download": False, "inference_seconds": 0.01,
                    "postprocess_seconds": 0.0},
        "visualizations": [],
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    import auth
    import service
    from store import Store
    from fastapi.testclient import TestClient

    tok = "test-token-xyz"
    keysf = tmp_path / "api_keys.json"
    keysf.write_text(json.dumps({"keys": [
        {"id": "t1", "label": "t", "hash": auth.token_hash(tok),
         "active": True, "created_at": "x"}]}))
    monkeypatch.setattr(auth, "KEYS_FILE", keysf)
    auth._cache["mtime"] = None
    auth._cache["hashes"] = set()

    monkeypatch.setattr(service, "JOBS_DIR", tmp_path / "output")
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 1024 * 1024)          # 1 MB
    monkeypatch.setattr(service, "store", Store(tmp_path / "state.db", seats=service.SEATS))
    monkeypatch.setattr(service.service, "load", lambda *a, **k: 0.0)
    monkeypatch.setattr(service.service, "process", _fake_process)

    with TestClient(service.app) as c:
        service._ready["state"] = "ready"                                 # skip real warmup gate
        c.h = {"Authorization": f"Bearer {tok}"}
        c.jobs = tmp_path / "output"
        yield c


def _png(size=8):
    return ("a.png", b"x" * size, "image/png")


# ---- public endpoints (no auth) ----

def test_healthz_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_openapi_public(client):
    r = client.get("/openapi.yaml")
    assert r.status_code == 200 and "openapi:" in r.text


def test_docs_public(client):
    r = client.get("/docs")
    assert r.status_code == 200 and "swagger-ui" in r.text.lower()


# ---- auth ----

def test_status_requires_auth(client):
    assert client.get("/api/status").status_code == 401


def test_status_with_token(client):
    r = client.get("/api/status", headers=client.h)
    assert r.status_code == 200 and r.json()["state"] == "ready"


def test_submit_requires_auth(client):
    r = client.post("/api/ocr", files={"file": _png()})
    assert r.status_code == 401


# ---- validation ----

def test_size_limit(client):
    big = ("a.png", b"0" * (1024 * 1024 + 10), "image/png")
    r = client.post("/api/ocr", headers=client.h, files={"file": big}, data={"model": "server"})
    assert r.status_code == 413


def test_bad_extension(client):
    r = client.post("/api/ocr", headers=client.h,
                    files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 400


# ---- async flow ----

def test_submit_and_poll(client):
    r = client.post("/api/ocr", headers=client.h, files={"file": _png()},
                    data={"key": "job1", "model": "server"})
    assert r.status_code == 202 and r.json()["status"] == "started"
    pid = r.json()["process_id"]

    job = {"status": "queued"}
    for _ in range(50):
        job = client.get(f"/api/ocr/{pid}", headers=client.h).json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "done"
    assert job["result"]["markdown"] == "# hi"

    # debug artifact: output/<pid>/debug.json with input + output
    dbg = client.jobs / pid / "debug.json"
    assert dbg.exists()
    d = json.loads(dbg.read_text(encoding="utf-8"))
    assert d["input"]["key"] == "job1"
    assert d["output"]["markdown"] == "# hi"


def test_idempotent_duplicate(client):
    client.post("/api/ocr", headers=client.h, files={"file": _png()}, data={"key": "dup1"})
    r2 = client.post("/api/ocr", headers=client.h, files={"file": _png()}, data={"key": "dup1"})
    assert r2.json()["status"] in ("in_progress", "done")


def test_unknown_process_id_404(client):
    assert client.get("/api/ocr/nonexistent", headers=client.h).status_code == 404
