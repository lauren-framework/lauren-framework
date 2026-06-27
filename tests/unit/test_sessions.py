"""Unit tests for the session primitives (no ASGI app).

Covers the Session mapping + dirty tracking, HMAC signing with rotation,
the JSON serialiser, both built-in stores, SessionConfig validation, and
the engine's cookie-attribute emission. Integration round-trips live in
``tests/integration/test_sessions.py``.
"""

from __future__ import annotations

import time

import pytest

from lauren import (
    InMemorySessionStore,
    Session,
    SessionConfig,
    SignedCookieSessionStore,
)
from lauren.exceptions import SessionConfigError
from lauren.sessions import JSONSessionSerializer
from lauren._sessions._config import resolve_session_config
from lauren._sessions._engine import _SessionEngine, _b64decode, _b64encode
from lauren._sessions._signing import Signer, normalize_secrets
from lauren.types import Response


# ---------------------------------------------------------------------------
# Session object — mapping semantics + dirty tracking
# ---------------------------------------------------------------------------


class TestSessionMapping:
    def test_starts_clean_and_empty(self):
        s = Session()
        assert len(s) == 0
        assert s.is_new is True
        assert s.is_modified is False
        assert s.is_invalidated is False

    def test_set_get_marks_modified(self):
        s = Session()
        s["k"] = "v"
        assert s["k"] == "v"
        assert s.get("k") == "v"
        assert s.get("missing", "d") == "d"
        assert s.is_modified is True

    def test_read_does_not_mark_modified(self):
        s = Session(data={"a": 1}, is_new=False)
        _ = s["a"]
        _ = s.get("a")
        _ = list(s)
        _ = len(s)
        _ = "a" in s
        _ = s.as_dict()
        assert s.is_modified is False

    def test_delete_marks_modified(self):
        s = Session(data={"a": 1}, is_new=False)
        del s["a"]
        assert "a" not in s
        assert s.is_modified is True

    def test_pop_present_and_missing(self):
        s = Session(data={"a": 1}, is_new=False)
        assert s.pop("a") == 1
        assert s.is_modified is True
        with pytest.raises(KeyError):
            s.pop("nope")
        assert s.pop("nope", "default") == "default"

    def test_setdefault_only_marks_when_inserting(self):
        s = Session(data={"a": 1}, is_new=False)
        assert s.setdefault("a", 99) == 1
        assert s.is_modified is False
        assert s.setdefault("b", 2) == 2
        assert s.is_modified is True

    def test_update_marks_modified(self):
        s = Session(is_new=False)
        s.update({"a": 1}, b=2)
        assert s.as_dict() == {"a": 1, "b": 2}
        assert s.is_modified is True

    def test_clear_marks_modified_only_when_nonempty(self):
        empty = Session(is_new=False)
        empty.clear()
        assert empty.is_modified is False
        full = Session(data={"a": 1}, is_new=False)
        full.clear()
        assert len(full) == 0
        assert full.is_modified is True

    def test_iter_and_len(self):
        s = Session(data={"a": 1, "b": 2}, is_new=False)
        assert sorted(s) == ["a", "b"]
        assert len(s) == 2
        assert dict(s) == {"a": 1, "b": 2}

    def test_as_dict_returns_copy(self):
        s = Session(data={"a": 1}, is_new=False)
        d = s.as_dict()
        d["a"] = 999
        assert s["a"] == 1


class TestSessionLifecycle:
    def test_regenerate_id_keeps_data_changes_id(self):
        ids = iter(["new-id-1", "new-id-2"])
        s = Session(data={"a": 1}, id="old", is_new=False, new_id_factory=lambda: next(ids))
        s.regenerate_id()
        assert s.id == "new-id-1"
        assert s["a"] == 1  # data preserved
        assert s.is_modified is True
        assert s.is_new is False

    def test_invalidate_clears_and_flags(self):
        s = Session(data={"a": 1}, id="x", is_new=False)
        s.invalidate()
        assert len(s) == 0
        assert s.is_invalidated is True
        assert s.is_modified is True


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


class TestSigning:
    def test_normalize_secrets_forms(self):
        assert normalize_secrets("abc") == (b"abc",)
        assert normalize_secrets(b"abc") == (b"abc",)
        assert normalize_secrets(["a", b"b"]) == (b"a", b"b")
        assert normalize_secrets(None) == ()
        assert normalize_secrets(["", b""]) == ()  # empties dropped

    def test_sign_then_unsign_roundtrips(self):
        signer = Signer((b"secret-key",))
        token = signer.sign("session-id-123")
        assert signer.unsign(token) == "session-id-123"

    def test_tampered_signature_rejected(self):
        signer = Signer((b"secret-key",))
        token = signer.sign("abc")
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        assert signer.unsign(tampered) is None

    def test_tampered_value_rejected(self):
        signer = Signer((b"secret-key",))
        token = signer.sign("abc")
        value, _, sig = token.partition(".")
        assert signer.unsign("xyz." + sig) is None

    def test_missing_separator_returns_none(self):
        signer = Signer((b"k",))
        assert signer.unsign("no-separator-here") is None

    def test_rotation_signs_new_verifies_all(self):
        old = Signer((b"old",))
        token_old = old.sign("id")
        # New signer prepends a fresh key, keeps the old for verification.
        rotated = Signer((b"new", b"old"))
        assert rotated.unsign(token_old) == "id"  # old cookie still valid
        token_new = rotated.sign("id2")
        assert old.unsign(token_new) is None  # old key alone cannot verify new

    def test_empty_secrets_rejected(self):
        with pytest.raises(ValueError):
            Signer(())


# ---------------------------------------------------------------------------
# Serializer
# ---------------------------------------------------------------------------


class TestSerializer:
    def test_roundtrip(self):
        ser = JSONSessionSerializer()
        data = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
        assert ser.loads(ser.dumps(data)) == data

    def test_loads_non_dict_raises(self):
        ser = JSONSessionSerializer()
        raw = b"[1, 2, 3]"
        with pytest.raises(ValueError):
            ser.loads(raw)

    def test_deterministic_encoding(self):
        ser = JSONSessionSerializer()
        assert ser.dumps({"b": 1, "a": 2}) == ser.dumps({"a": 2, "b": 1})


def test_b64_roundtrip_no_padding():
    raw = b"some arbitrary bytes \x00\xff"
    encoded = _b64encode(raw)
    assert "=" not in encoded
    assert _b64decode(encoded) == raw


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


class TestInMemoryStore:
    async def test_crud(self):
        store = InMemorySessionStore()
        sid = store.new_id()
        assert await store.load(sid) is None
        await store.save(sid, {"a": 1}, max_age=None)
        assert await store.load(sid) == {"a": 1}
        await store.delete(sid)
        assert await store.load(sid) is None

    async def test_new_id_is_unguessable_and_unique(self):
        store = InMemorySessionStore()
        ids = {store.new_id() for _ in range(100)}
        assert len(ids) == 100
        assert all(len(i) >= 32 for i in ids)

    async def test_lazy_ttl_expiry(self):
        store = InMemorySessionStore()
        sid = store.new_id()
        await store.save(sid, {"a": 1}, max_age=1)
        # Force the stored expiry into the past, then load.
        store._expiry[sid] = time.time() - 1
        assert await store.load(sid) is None

    async def test_load_returns_copy(self):
        store = InMemorySessionStore()
        sid = store.new_id()
        await store.save(sid, {"a": 1}, max_age=None)
        loaded = await store.load(sid)
        loaded["a"] = 999
        assert (await store.load(sid)) == {"a": 1}


class TestCookieStore:
    async def test_store_is_stateless(self):
        store = SignedCookieSessionStore()
        assert store.client_side is True
        assert await store.load("anything") is None
        await store.save("x", {"a": 1}, max_age=None)  # no-op
        await store.delete("x")  # no-op
        assert await store.load("x") is None


# ---------------------------------------------------------------------------
# SessionConfig validation
# ---------------------------------------------------------------------------


class TestSessionConfigValidation:
    def test_defaults_resolve(self):
        resolved = resolve_session_config(SessionConfig(secret="x" * 32))
        assert isinstance(resolved.store, InMemorySessionStore)
        assert resolved.same_site == "Lax"
        assert resolved.secure is True
        assert resolved.http_only is True
        assert resolved.client_side is False

    def test_same_site_canonicalised(self):
        for raw, canon in [("lax", "Lax"), ("strict", "Strict"), ("none", "None")]:
            secure = canon == "None"
            resolved = resolve_session_config(
                SessionConfig(secret="x" * 32, same_site=raw, secure=True or secure)
            )
            assert resolved.same_site == canon

    def test_same_site_none_requires_secure(self):
        with pytest.raises(SessionConfigError) as ei:
            resolve_session_config(SessionConfig(secret="x" * 32, same_site="none", secure=False))
        assert ei.value.detail.get("same_site") == "none"

    def test_invalid_same_site(self):
        with pytest.raises(SessionConfigError):
            resolve_session_config(SessionConfig(secret="x" * 32, same_site="banana"))

    def test_missing_secret_rejected(self):
        with pytest.raises(SessionConfigError) as ei:
            resolve_session_config(SessionConfig(secret=""))
        assert "secret" in str(ei.value).lower()

    def test_host_prefix_requires_secure_path_domain(self):
        with pytest.raises(SessionConfigError):
            resolve_session_config(SessionConfig(secret="x" * 32, cookie_name="__Host-sid", secure=False))
        with pytest.raises(SessionConfigError):
            resolve_session_config(SessionConfig(secret="x" * 32, cookie_name="__Host-sid", path="/app"))
        with pytest.raises(SessionConfigError):
            resolve_session_config(
                SessionConfig(secret="x" * 32, cookie_name="__Host-sid", domain="example.com")
            )
        # Valid __Host- config resolves.
        ok = resolve_session_config(SessionConfig(secret="x" * 32, cookie_name="__Host-sid"))
        assert ok.cookie_name == "__Host-sid"

    def test_secure_prefix_requires_secure(self):
        with pytest.raises(SessionConfigError):
            resolve_session_config(SessionConfig(secret="x" * 32, cookie_name="__Secure-sid", secure=False))

    @pytest.mark.parametrize("field,value", [("max_age", 0), ("max_age", -5), ("idle_timeout", 0)])
    def test_non_positive_lifetimes_rejected(self, field, value):
        with pytest.raises(SessionConfigError) as ei:
            resolve_session_config(SessionConfig(secret="x" * 32, **{field: value}))
        assert ei.value.detail.get(field) == value

    def test_empty_cookie_name_rejected(self):
        with pytest.raises(SessionConfigError):
            resolve_session_config(SessionConfig(secret="x" * 32, cookie_name=""))

    def test_non_config_object_rejected(self):
        with pytest.raises(SessionConfigError):
            resolve_session_config({"secret": "x"})  # type: ignore[arg-type]

    def test_cookie_store_max_bytes_propagates(self):
        resolved = resolve_session_config(
            SessionConfig(secret="x" * 32, store=SignedCookieSessionStore(max_bytes=128))
        )
        assert resolved.client_side is True
        assert resolved.max_cookie_bytes == 128


# ---------------------------------------------------------------------------
# Engine cookie-attribute emission
# ---------------------------------------------------------------------------


class TestEngineCookieEmission:
    def _engine(self, **cfg):
        resolved = resolve_session_config(SessionConfig(secret="x" * 32, **cfg))
        return _SessionEngine(resolved)

    def test_secure_defaults_present(self):
        engine = self._engine()
        resp = engine._set_cookie(Response.json({}), "signed-value")
        sc = resp.headers["set-cookie"]
        assert "lauren_session=signed-value" in sc
        assert "HttpOnly" in sc
        assert "Secure" in sc
        assert "SameSite=Lax" in sc
        assert "Path=/" in sc
        assert "Max-Age=1209600" in sc

    def test_expire_cookie_max_age_zero(self):
        engine = self._engine()
        resp = engine._expire_cookie(Response.json({}))
        sc = resp.headers["set-cookie"]
        assert "Max-Age=0" in sc

    def test_strict_same_site_and_domain(self):
        engine = self._engine(same_site="strict", domain="example.com")
        resp = engine._set_cookie(Response.json({}), "v")
        sc = resp.headers["set-cookie"]
        assert "SameSite=Strict" in sc
        assert "Domain=example.com" in sc

    def test_oversize_cookie_payload_raises(self):
        engine = self._engine(store=SignedCookieSessionStore(max_bytes=64))
        session = Session(is_new=True)
        session["blob"] = "x" * 5000  # far exceeds 64 bytes once encoded
        with pytest.raises(ValueError):
            engine._persist_cookie_store(Response.json({}), session, None)
