"""Bearer-token auth for the OCR service.

Multiple keys, each individually revocable via keys.py. Only SHA-256 hashes of
the tokens are stored (in api_keys.json), so the file never contains a usable
token. Both worker processes read the same file and pick up revocations within
one request (mtime-based reload) — no restart needed.

Auth is ON by default. Set OCR_AUTH=off to disable it for local development.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path

KEYS_FILE = Path(os.environ.get("OCR_KEYS_FILE", Path(__file__).parent / "api_keys.json"))

_cache = {"mtime": None, "hashes": set()}
_lock = threading.Lock()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def auth_enabled() -> bool:
    return os.environ.get("OCR_AUTH", "on").strip().lower() != "off"


def _reload_if_changed() -> None:
    try:
        mtime = KEYS_FILE.stat().st_mtime
    except FileNotFoundError:
        with _lock:
            _cache["mtime"], _cache["hashes"] = None, set()
        return
    if _cache["mtime"] == mtime:
        return
    with _lock:
        try:
            data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
            _cache["hashes"] = {
                k["hash"] for k in data.get("keys", []) if k.get("active")
            }
        except Exception:
            _cache["hashes"] = set()
        _cache["mtime"] = mtime


def has_any_key() -> bool:
    _reload_if_changed()
    return bool(_cache["hashes"])


def verify_bearer(authorization: str | None) -> bool:
    """True if the Authorization header carries a valid, active bearer token."""
    if not auth_enabled():
        return True
    if not authorization:
        return False
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    incoming = token_hash(parts[1].strip())
    _reload_if_changed()
    # constant-time compare against each active key hash
    return any(hmac.compare_digest(incoming, h) for h in _cache["hashes"])
