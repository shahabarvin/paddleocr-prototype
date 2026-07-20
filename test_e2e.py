"""End-to-end test of the seat-based multi-worker OCR service."""
import concurrent.futures
import os
import sys
import time
import uuid
from collections import Counter

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
B = "http://127.0.0.1:8000"
BLOB = open("samples/receipt.png", "rb").read()
TOKEN = os.environ.get("OCR_TEST_TOKEN", "")
_headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
C = httpx.Client(timeout=120, headers=_headers)   # pooled, shared across the test


def submit(key, enhance="0"):
    r = C.post(f"{B}/api/ocr",
               files={"file": ("r.png", BLOB, "image/png")},
               data={"key": key, "model": "server", "enhance": enhance, "overlays": "0"})
    return r.status_code, r.json()


def get(pid):
    return C.get(f"{B}/api/ocr/{pid}").json()


def status():
    return C.get(f"{B}/api/status").json()


def poll(pid, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        j = get(pid)
        st = j.get("status")
        if st in ("done", "failed") or st is None:
            return j
        time.sleep(0.1)
    return {"status": "timeout"}


print("== Test 1: dedup + lifecycle ==")
jobA = f"jobA-{uuid.uuid4().hex[:6]}"
c, j = submit(jobA)
print(f"  submit #1        : HTTP {c}  status={j['status']}")
c, j2 = submit(jobA)
print(f"  submit #2 (dup)  : HTTP {c}  status={j2['status']}")
done = poll(jobA)
md = done.get("result", {}).get("markdown", "")
print(f"  polled to finish : status={done['status']}  worker={done.get('worker')}  markdown_chars={len(md)}")
c, j3 = submit(jobA)
print(f"  submit #3 (done) : HTTP {c}  status={j3['status']}  returns_cached_result={'result' in j3}")

print("\n== Test 2: seats / no_seat (fire 8 distinct jobs at once, seats=3) ==")
keys = [f"burst-{uuid.uuid4().hex[:6]}" for _ in range(8)]
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
    res = list(ex.map(lambda k: submit(k), keys))
print(f"  statuses: {dict(Counter(j['status'] for _, j in res))}")
print(f"  HTTP codes: {dict(Counter(c for c, _ in res))}")
for k, (_, j) in zip(keys, res):
    if j.get("status") in ("started", "in_progress"):
        poll(k)

print("\n== Test 3: throughput (30 distinct jobs, seats kept full) ==")
N = 30
keys = [f"tp-{i}-{uuid.uuid4().hex[:4]}" for i in range(N)]
base_done = status()["by_status"].get("done", 0)
t0 = time.time()
# single-threaded submitter: seats pace it (no_seat -> brief backoff)
for k in keys:
    while submit(k)[1]["status"] == "no_seat":
        time.sleep(0.03)
# wait for the batch to drain
while True:
    s = status()
    if s["in_flight"] == 0 and s["by_status"].get("done", 0) >= base_done + N:
        break
    time.sleep(0.1)
wall = time.time() - t0
print(f"  {N} jobs done in {wall:.1f}s -> {N/wall:.2f} img/s")
dist = Counter(get(k).get("worker") for k in keys)
print(f"  worker distribution: {dict(dist)}  (both PIDs => 2 pipelines in use)")
