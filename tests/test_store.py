"""Unit tests for the SQLite shared store (no GPU / no models needed)."""
import time

import pytest

from store import Store


@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "t.db", seats=2, stale_seconds=1.0, ttl_seconds=1.0)
    s.init()
    return s


def opt(**k):
    return {"tier": "server", "input_path": "x.png", **k}


def test_started_then_in_progress(st):
    assert st.submit("a", opt())["status"] == "started"
    assert st.submit("a", opt())["status"] == "in_progress"   # dup while queued


def test_claim_and_complete(st):
    st.submit("a", opt())
    job = st.claim_next("w1")
    assert job["process_id"] == "a"
    assert st.get("a")["status"] == "processing"
    assert st.submit("a", opt())["status"] == "in_progress"   # dup while processing
    st.complete("a", {"markdown": "hi"})
    done = st.submit("a", opt())
    assert done["status"] == "done"
    assert done["result"]["markdown"] == "hi"


def test_seat_cap(st):                    # seats = 2
    assert st.submit("a", opt())["status"] == "started"
    assert st.submit("b", opt())["status"] == "started"
    assert st.submit("c", opt())["status"] == "no_seat"


def test_claim_next_empty(st):
    assert st.claim_next("w1") is None


def test_fail_then_resubmit(st):
    st.submit("a", opt())
    st.fail("a", "boom")
    assert st.get("a")["status"] == "failed"
    assert st.get("a")["error"] == "boom"
    assert st.submit("a", opt())["status"] == "started"        # re-queued


def test_stats(st):
    st.submit("a", opt())
    s = st.stats()
    assert s == {"seats": 2, "in_flight": 1, "available": 1, "by_status": {"queued": 1}}


def test_has_queued(st):
    assert st.has_queued() is False
    st.submit("a", opt())
    assert st.has_queued() is True
    st.claim_next("w1")
    assert st.has_queued() is False


def test_get_unknown(st):
    assert st.get("nope") is None


def test_stale_processing_is_reclaimed(st):   # stale_seconds = 1
    st.submit("a", opt())
    st.claim_next("w1")                        # a -> processing
    st._conn().execute("UPDATE jobs SET claimed_at=? WHERE process_id=?",
                       (time.time() - 10, "a"))
    st.submit("b", opt())                      # any submit triggers reclaim
    assert st.get("a")["status"] == "queued"


def test_cleanup_purges_old(st):              # ttl_seconds = 1
    st.submit("a", opt())
    st.complete("a", {})
    st._conn().execute("UPDATE jobs SET updated_at=? WHERE process_id=?",
                       (time.time() - 10, "a"))
    assert st.cleanup() == 1
    assert st.get("a") is None
