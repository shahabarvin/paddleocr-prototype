"""Manage API bearer keys for the OCR service.

    python keys.py create "laravel-prod"   # make a key, prints the token ONCE
    python keys.py list                     # show keys (active/revoked)
    python keys.py revoke <id>              # disable a key (takes effect in seconds)
    python keys.py delete <id>              # remove a key row entirely

Only the SHA-256 hash of each token is stored in api_keys.json; the plaintext
token is shown exactly once at creation. Revocation needs no server restart.
"""
from __future__ import annotations

import datetime
import json
import os
import secrets
import sys
from pathlib import Path

from auth import token_hash

KEYS_FILE = Path(os.environ.get("OCR_KEYS_FILE", Path(__file__).parent / "api_keys.json"))


def _load() -> dict:
    if KEYS_FILE.exists():
        return json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    return {"keys": []}


def _save(data: dict) -> None:
    KEYS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def create(label: str) -> None:
    token = secrets.token_urlsafe(32)
    kid = secrets.token_hex(4)
    data = _load()
    data["keys"].append({
        "id": kid,
        "label": label,
        "hash": token_hash(token),
        "active": True,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    _save(data)
    print("API key created. Store this token now — it is NOT shown again:\n")
    print(f"  id     : {kid}")
    print(f"  label  : {label}")
    print(f"  token  : {token}\n")
    print("Use it as:  Authorization: Bearer " + token)


def list_keys() -> None:
    keys = _load()["keys"]
    if not keys:
        print("(no keys yet — run: python keys.py create \"label\")")
        return
    for k in keys:
        state = "ACTIVE " if k.get("active") else "revoked"
        print(f"{k['id']}  {state}  {k.get('created_at','')}  {k.get('label','')}")


def revoke(kid: str) -> None:
    data = _load()
    hit = False
    for k in data["keys"]:
        if k["id"] == kid:
            k["active"] = False
            k["revoked_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            hit = True
    _save(data)
    print(f"revoked {kid}" if hit else f"id {kid} not found")


def delete(kid: str) -> None:
    data = _load()
    before = len(data["keys"])
    data["keys"] = [k for k in data["keys"] if k["id"] != kid]
    _save(data)
    print(f"deleted {kid}" if len(data["keys"]) < before else f"id {kid} not found")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]
    if cmd == "create" and rest:
        create(" ".join(rest))
    elif cmd == "list":
        list_keys()
    elif cmd == "revoke" and rest:
        revoke(rest[0])
    elif cmd == "delete" and rest:
        delete(rest[0])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
