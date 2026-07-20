"""SQLite-backed shared store for the multi-worker OCR service.

Two worker processes coordinate through one SQLite file in WAL mode:
  * admission control  -- at most ``seats`` jobs in flight (queued+processing),
  * deduplication      -- one row per process_id (idempotency key / content hash),
  * a work queue       -- whichever worker is free claims the next queued job,
  * result storage      -- the finished result JSON, retrievable by process_id,
  * self-healing        -- jobs stuck 'processing' past ``stale_seconds`` (a dead
                           worker) are requeued; done/failed rows past ``ttl_seconds``
                           are purged.

WAL gives concurrent readers + a single fast writer; every mutating step runs in
a BEGIN IMMEDIATE transaction so the seat count and the insert are atomic across
processes. At a few jobs/sec this is far off the hot path (the GPU is), so SQLite
never bottlenecks -- and it needs zero infrastructure.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    process_id TEXT PRIMARY KEY,
    status     TEXT NOT NULL,            -- queued | processing | done | failed
    worker     TEXT,
    options    TEXT NOT NULL,            -- json: tier, device, enhance, overlays, input_path
    result     TEXT,                     -- json (when done)
    error      TEXT,                     -- (when failed)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    claimed_at REAL                      -- when a worker began processing
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
"""

_ACTIVE = ("queued", "processing")


class Store:
    def __init__(self, path: str | Path, seats: int = 3,
                 stale_seconds: float = 120.0, ttl_seconds: float = 3600.0):
        self.path = str(path)
        self.seats = seats
        self.stale = stale_seconds
        self.ttl = ttl_seconds
        self._local = threading.local()  # one sqlite connection per thread

    # -- connection -------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            # isolation_level=None -> autocommit; we open explicit transactions.
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def init(self) -> None:
        self._conn().executescript(_SCHEMA)

    # -- admission (called by the submit endpoint) ------------------------

    def submit(self, process_id: str, options: dict) -> dict:
        """Deduplicate + admit. Returns one of:
        {status: started|in_progress|done|failed|no_seat, process_id, [result]}.
        """
        c = self._conn()
        now = time.time()
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute(
                "SELECT status, result, error FROM jobs WHERE process_id=?",
                (process_id,),
            ).fetchone()
            if row:
                status, result, error = row
                if status in _ACTIVE:
                    c.execute("COMMIT")
                    return {"status": "in_progress", "process_id": process_id}
                if status == "done":
                    c.execute("COMMIT")
                    return {"status": "done", "process_id": process_id,
                            "result": json.loads(result) if result else None}
                # status == 'failed' -> fall through and allow a re-run.

            # Requeue anything abandoned by a dead worker before counting seats.
            c.execute(
                "UPDATE jobs SET status='queued', worker=NULL, claimed_at=NULL, "
                "updated_at=? WHERE status='processing' AND claimed_at < ?",
                (now, now - self.stale),
            )
            in_flight = c.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing')"
            ).fetchone()[0]
            if in_flight >= self.seats:
                c.execute("COMMIT")
                return {"status": "no_seat", "process_id": process_id}

            c.execute(
                "INSERT INTO jobs(process_id,status,options,created_at,updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(process_id) DO UPDATE SET "
                "status='queued', worker=NULL, result=NULL, error=NULL, "
                "claimed_at=NULL, options=excluded.options, updated_at=excluded.updated_at",
                (process_id, "queued", json.dumps(options), now, now),
            )
            c.execute("COMMIT")
            return {"status": "started", "process_id": process_id}
        except Exception:
            c.execute("ROLLBACK")
            raise

    # -- execution (called by each worker's pull loop) --------------------

    def has_queued(self) -> bool:
        """Cheap WAL read (no write lock) so idle pull loops don't churn the
        write lock; only call claim_next when this is true."""
        return self._conn().execute(
            "SELECT 1 FROM jobs WHERE status='queued' LIMIT 1"
        ).fetchone() is not None

    def claim_next(self, worker: str) -> Optional[dict]:
        """Atomically claim the oldest queued job. Returns None if none queued."""
        c = self._conn()
        now = time.time()
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute(
                "SELECT process_id, options FROM jobs WHERE status='queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                c.execute("COMMIT")
                return None
            pid, options = row
            c.execute(
                "UPDATE jobs SET status='processing', worker=?, claimed_at=?, "
                "updated_at=? WHERE process_id=?",
                (worker, now, now, pid),
            )
            c.execute("COMMIT")
            return {"process_id": pid, "options": json.loads(options)}
        except Exception:
            c.execute("ROLLBACK")
            raise

    def complete(self, process_id: str, result: dict) -> None:
        now = time.time()
        self._conn().execute(
            "UPDATE jobs SET status='done', result=?, error=NULL, updated_at=? "
            "WHERE process_id=?",
            (json.dumps(result), now, process_id),
        )

    def fail(self, process_id: str, error: str) -> None:
        now = time.time()
        self._conn().execute(
            "UPDATE jobs SET status='failed', error=?, updated_at=? WHERE process_id=?",
            (str(error), now, process_id),
        )

    # -- retrieval / housekeeping ----------------------------------------

    def get(self, process_id: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT status, result, error, worker, created_at, updated_at "
            "FROM jobs WHERE process_id=?",
            (process_id,),
        ).fetchone()
        if not row:
            return None
        status, result, error, worker, created, updated = row
        out: dict[str, Any] = {"process_id": process_id, "status": status,
                               "worker": worker, "created_at": created,
                               "updated_at": updated}
        if result:
            out["result"] = json.loads(result)
        if error:
            out["error"] = error
        return out

    def stats(self) -> dict:
        counts = dict(self._conn().execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status"
        ).fetchall())
        in_flight = counts.get("queued", 0) + counts.get("processing", 0)
        return {"seats": self.seats, "in_flight": in_flight,
                "available": max(0, self.seats - in_flight), "by_status": counts}

    def cleanup(self) -> int:
        """Purge done/failed rows older than ttl. Returns rows deleted."""
        now = time.time()
        cur = self._conn().execute(
            "DELETE FROM jobs WHERE status IN ('done','failed') AND updated_at < ?",
            (now - self.ttl,),
        )
        return cur.rowcount
