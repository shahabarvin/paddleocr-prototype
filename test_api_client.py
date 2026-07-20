"""Realistic client test (what Laravel does): submit a file with a bearer token,
poll by process_id, print the result. Token + base URL come from env:

    OCR_TOKEN=<token> OCR_BASE=https://ocr.voiceaccountant.com python test_api_client.py
"""
import os
import sys
import time

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("OCR_BASE", "https://ocr.voiceaccountant.com").rstrip("/")
TOKEN = os.environ.get("OCR_TOKEN", "")
C = httpx.Client(timeout=120, headers={"Authorization": f"Bearer {TOKEN}"})

print(f"target: {BASE}\n")

# 1) health (public, no auth needed)
print("1) GET /healthz         ->", C.get(f"{BASE}/healthz").json())

# 2) auth sanity: no token must be rejected
r_noauth = httpx.get(f"{BASE}/api/status", timeout=30)
print(f"2) /api/status no token -> HTTP {r_noauth.status_code} (expect 401)")

# 3) status with token
print("3) GET /api/status      ->", C.get(f"{BASE}/api/status").json())

# 4) submit a real document
with open("samples/receipt.png", "rb") as f:
    blob = f.read()
r = C.post(f"{BASE}/api/ocr",
           files={"file": ("receipt.png", blob, "image/png")},
           data={"key": "api-test-001", "model": "server", "enhance": "1"})
print(f"4) POST /api/ocr        -> HTTP {r.status_code} {r.json()}")
pid = r.json()["process_id"]

# 5) poll until done
t0 = time.time()
while True:
    j = C.get(f"{BASE}/api/ocr/{pid}").json()
    if j["status"] in ("done", "failed"):
        break
    time.sleep(0.3)
dt = time.time() - t0
print(f"5) polled to finish     -> status={j['status']} in {dt:.1f}s")

res = j.get("result", {})
print(f"   device={res.get('device')} enhanced={res.get('enhanced')} "
      f"worker={res.get('worker')} timings={res.get('timings')}")
print("\n---- MARKDOWN (first 20 lines) ----")
print("\n".join((res.get("markdown", "") or "").splitlines()[:20]))
