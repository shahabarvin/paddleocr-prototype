"""Unit tests for bearer auth + key management (no GPU needed)."""
import pytest

import auth
import keys


@pytest.fixture
def keysfile(tmp_path, monkeypatch):
    f = tmp_path / "api_keys.json"
    monkeypatch.setattr(auth, "KEYS_FILE", f)
    monkeypatch.setattr(keys, "KEYS_FILE", f)
    auth._cache["mtime"] = None
    auth._cache["hashes"] = set()
    monkeypatch.delenv("OCR_AUTH", raising=False)   # auth ON by default
    return f


def _reset():
    auth._cache["mtime"] = None                     # force reload after file change


def test_token_hash_deterministic():
    assert auth.token_hash("abc") == auth.token_hash("abc")
    assert auth.token_hash("abc") != auth.token_hash("abd")


def test_bad_headers_rejected(keysfile):
    keys.create("k")
    _reset()
    assert auth.verify_bearer(None) is False
    assert auth.verify_bearer("") is False
    assert auth.verify_bearer("Token xyz") is False          # wrong scheme
    assert auth.verify_bearer("Bearer ") is False


def test_valid_and_invalid(keysfile):
    tok = keys.create("k")
    _reset()
    assert auth.verify_bearer(f"Bearer {tok}") is True
    assert auth.verify_bearer("Bearer nope") is False


def test_revoke_takes_effect(keysfile):
    tok = keys.create("k")
    kid = keys._load()["keys"][0]["id"]
    _reset()
    assert auth.verify_bearer(f"Bearer {tok}") is True
    keys.revoke(kid)
    _reset()
    assert auth.verify_bearer(f"Bearer {tok}") is False


def test_delete_removes_key(keysfile):
    tok = keys.create("k")
    kid = keys._load()["keys"][0]["id"]
    keys.delete(kid)
    assert keys._load()["keys"] == []
    _reset()
    assert auth.verify_bearer(f"Bearer {tok}") is False


def test_auth_disabled_allows_all(keysfile, monkeypatch):
    monkeypatch.setenv("OCR_AUTH", "off")
    assert auth.verify_bearer(None) is True


def test_has_any_key(keysfile):
    assert auth.has_any_key() is False
    keys.create("k")
    _reset()
    assert auth.has_any_key() is True


def test_multiple_keys_independent(keysfile):
    t1 = keys.create("a")
    t2 = keys.create("b")
    ids = [k["id"] for k in keys._load()["keys"]]
    keys.revoke(ids[0])
    _reset()
    assert auth.verify_bearer(f"Bearer {t1}") is False        # revoked
    assert auth.verify_bearer(f"Bearer {t2}") is True         # still active
