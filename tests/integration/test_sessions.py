"""Integration tests for core session management.

Full-stack round-trips through ``TestClient``. Because Lauren's
``TestClient`` keeps no cookie jar, every cross-request flow threads the
cookie by hand: read it from ``Set-Cookie`` on one response, pass it back
via ``cookies={...}`` on the next.
"""

from __future__ import annotations

import pytest

from lauren import (
    InMemorySessionStore,
    Json,
    LaurenFactory,
    Request,
    Session,
    SessionConfig,
    SessionStore,
    SignedCookieSessionStore,
    controller,
    get,
    module,
    post,
    put,
)
from lauren.exceptions import SessionConfigError
from lauren.testing import TestClient, TestResponse

COOKIE = "lauren_session"


def _cookie(response: TestResponse, name: str = COOKIE) -> str | None:
    """Parse a Set-Cookie value out of a response (no cookie jar in TestClient)."""
    sc = response.header("set-cookie") or ""
    for part in sc.split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part.split("=", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Shared controller + app builders
# ---------------------------------------------------------------------------


@controller("/session")
class SessionController:
    def __init__(self, store: SessionStore) -> None:
        # Direct SessionStore injection (global-provider visibility).
        self._store = store

    @get("/")
    async def read(self, session: Session) -> dict:
        return {"data": session.as_dict(), "new": session.is_new, "id": session.id}

    @get("/visits")
    async def visits(self, session: Session) -> dict:
        session["visits"] = session.get("visits", 0) + 1
        return {"visits": session["visits"]}

    @post("/set")
    async def set_value(self, session: Session, key: str, value: str) -> dict:
        session[key] = value
        return {"ok": True}

    @post("/login")
    async def login(self, session: Session) -> dict:
        session.regenerate_id()
        session["user_id"] = "u-42"
        return {"ok": True, "id": session.id}

    @post("/logout")
    async def logout(self, session: Session) -> dict:
        session.invalidate()
        return {"ok": True}

    @get("/same")
    async def same(self, session: Session, request: Request) -> dict:
        return {"identical": session is request.state.session}

    @post("/admin/revoke")
    async def revoke(self, sid: str) -> dict:
        await self._store.delete(sid)
        return {"revoked": sid}


@module(controllers=[SessionController])
class SessionModule:
    pass


def build_app(**cfg) -> TestClient:
    cfg.setdefault("secret", "x" * 32)
    return TestClient(LaurenFactory.create(SessionModule, sessions=SessionConfig(**cfg)))


# ---------------------------------------------------------------------------
# Basic round-trips
# ---------------------------------------------------------------------------


class TestServerSideSessions:
    def test_first_visit_sets_signed_cookie(self):
        client = build_app()
        r = client.get("/session/visits")
        assert r.json() == {"visits": 1}
        sc = r.header("set-cookie") or ""
        assert f"{COOKIE}=" in sc
        assert "HttpOnly" in sc and "Secure" in sc and "SameSite=Lax" in sc
        # The value is signed: "<id>.<hexsig>".
        assert "." in (_cookie(r) or "")

    def test_new_visitor_has_empty_session(self):
        client = build_app()
        r = client.get("/session/")
        assert r.json()["data"] == {}
        assert r.json()["new"] is True

    def test_write_then_read_across_requests(self):
        client = build_app()
        r1 = client.post("/session/set", params={"key": "name", "value": "alice"})
        sid = _cookie(r1)
        assert sid is not None
        r2 = client.get("/session/", cookies={COOKIE: sid})
        assert r2.json()["data"] == {"name": "alice"}
        assert r2.json()["new"] is False

    def test_accumulates_across_multiple_writes(self):
        client = build_app()
        r1 = client.get("/session/visits")
        sid = _cookie(r1)
        r2 = client.get("/session/visits", cookies={COOKIE: sid})
        r3 = client.get("/session/visits", cookies={COOKIE: sid})
        assert [r1.json()["visits"], r2.json()["visits"], r3.json()["visits"]] == [1, 2, 3]

    def test_no_set_cookie_on_unmodified_read(self):
        client = build_app()
        sid = _cookie(client.get("/session/visits"))
        # /session/ only reads — no mutation, non-rolling → no Set-Cookie.
        r = client.get("/session/", cookies={COOKIE: sid})
        assert r.header("set-cookie") is None

    def test_isolation_between_clients(self):
        client = build_app()
        a = _cookie(client.post("/session/set", params={"key": "who", "value": "a"}))
        b = _cookie(client.post("/session/set", params={"key": "who", "value": "b"}))
        assert a != b
        ra = client.get("/session/", cookies={COOKIE: a})
        rb = client.get("/session/", cookies={COOKIE: b})
        assert ra.json()["data"] == {"who": "a"}
        assert rb.json()["data"] == {"who": "b"}

    def test_session_injection_is_same_object_as_state(self):
        client = build_app()
        r = client.get("/session/same")
        assert r.json() == {"identical": True}


class TestRolling:
    def test_rolling_refreshes_cookie_on_read(self):
        client = build_app(rolling=True)
        sid = _cookie(client.get("/session/visits"))
        # A pure read still re-emits the cookie under rolling.
        r = client.get("/session/", cookies={COOKIE: sid})
        assert r.header("set-cookie") is not None
        assert "Max-Age=1209600" in (r.header("set-cookie") or "")


class TestLoginLogout:
    def test_login_rotates_cookie_value(self):
        client = build_app()
        sid = _cookie(client.get("/session/visits"))
        r = client.post("/session/login", cookies={COOKIE: sid})
        new_sid = _cookie(r)
        assert new_sid is not None and new_sid != sid

    def test_old_id_no_longer_loads_after_regeneration(self):
        client = build_app()
        sid = _cookie(client.post("/session/set", params={"key": "k", "value": "v"}))
        new_sid = _cookie(client.post("/session/login", cookies={COOKIE: sid}))
        # Old cookie → fresh session (its row was deleted on rotation).
        old = client.get("/session/", cookies={COOKIE: sid})
        assert old.json()["new"] is True
        # New cookie carries the merged data.
        new = client.get("/session/", cookies={COOKIE: new_sid})
        assert new.json()["data"].get("user_id") == "u-42"
        assert new.json()["data"].get("k") == "v"

    def test_logout_expires_cookie_and_drops_session(self):
        client = build_app()
        sid = _cookie(client.post("/session/set", params={"key": "k", "value": "v"}))
        r = client.post("/session/logout", cookies={COOKIE: sid})
        assert "Max-Age=0" in (r.header("set-cookie") or "")
        after = client.get("/session/", cookies={COOKIE: sid})
        assert after.json()["new"] is True
        assert after.json()["data"] == {}


class TestTampering:
    def test_tampered_cookie_yields_fresh_session_no_error(self):
        client = build_app()
        sid = _cookie(client.post("/session/set", params={"key": "k", "value": "v"}))
        tampered = sid[:-1] + ("0" if sid[-1] != "0" else "1")
        r = client.get("/session/", cookies={COOKIE: tampered})
        assert r.status_code == 200
        assert r.json()["new"] is True

    def test_garbage_cookie_yields_fresh_session(self):
        client = build_app()
        r = client.get("/session/", cookies={COOKIE: "not-even-signed"})
        assert r.status_code == 200
        assert r.json()["new"] is True

    def test_cookie_signed_with_other_secret_rejected(self):
        a = build_app(secret="secret-A" * 4)
        sid = _cookie(a.post("/session/set", params={"key": "k", "value": "v"}))
        b = build_app(secret="secret-B" * 4)
        r = b.get("/session/", cookies={COOKIE: sid})
        assert r.json()["new"] is True


class TestDirectStoreInjection:
    def test_admin_can_revoke_via_injected_store(self):
        store = InMemorySessionStore()
        client = TestClient(
            LaurenFactory.create(SessionModule, sessions=SessionConfig(secret="x" * 32, store=store))
        )
        login = client.post("/session/login")
        sid = _cookie(login)
        # Session is live.
        assert client.get("/session/", cookies={COOKIE: sid}).json()["new"] is False
        # Decode the server-side id from the signed cookie to revoke it.
        server_id = sid.split(".", 1)[0]
        client.post("/session/admin/revoke", params={"sid": server_id})
        # After revocation the cookie no longer loads.
        assert client.get("/session/", cookies={COOKIE: sid}).json()["new"] is True


class TestCookieStore:
    def test_stateless_roundtrip_in_cookie(self):
        store = SignedCookieSessionStore()
        client = TestClient(
            LaurenFactory.create(SessionModule, sessions=SessionConfig(secret="x" * 32, store=store))
        )
        r1 = client.post("/session/set", params={"key": "cart", "value": "book"})
        cookie = _cookie(r1)
        assert cookie is not None
        # Nothing is stored server-side — the data lives in the cookie.
        r2 = client.get("/session/", cookies={COOKIE: cookie})
        assert r2.json()["data"] == {"cart": "book"}

    def test_tampered_cookie_store_payload_is_fresh(self):
        store = SignedCookieSessionStore()
        client = TestClient(
            LaurenFactory.create(SessionModule, sessions=SessionConfig(secret="x" * 32, store=store))
        )
        cookie = _cookie(client.post("/session/set", params={"key": "k", "value": "v"}))
        tampered = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
        r = client.get("/session/", cookies={COOKIE: tampered})
        assert r.json()["new"] is True


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


@controller("/no-session")
class PlainController:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[PlainController])
class PlainModule:
    pass


@controller("/inject")
class SessionInjectController:
    # Injects Session but NOT SessionStore, isolating the Session-injection
    # startup check from the store-provider check.
    @get("/")
    async def index(self, session: Session) -> dict:
        return {"new": session.is_new}


@module(controllers=[SessionInjectController])
class SessionInjectModule:
    pass


class TestStartupValidation:
    def test_session_injected_but_sessions_disabled(self):
        with pytest.raises(SessionConfigError) as ei:
            LaurenFactory.create(SessionInjectModule)  # no sessions=
        assert ei.value.detail.get("reason") == "sessions_disabled"
        assert "SessionInjectController" in ei.value.detail.get("handler", "")

    def test_session_inject_module_works_when_enabled(self):
        client = TestClient(
            LaurenFactory.create(SessionInjectModule, sessions=SessionConfig(secret="x" * 32))
        )
        assert client.get("/inject/").json() == {"new": True}

    def test_same_site_none_without_secure_rejected(self):
        with pytest.raises(SessionConfigError):
            LaurenFactory.create(
                PlainModule, sessions=SessionConfig(secret="x" * 32, same_site="none", secure=False)
            )

    def test_missing_secret_rejected(self):
        with pytest.raises(SessionConfigError):
            LaurenFactory.create(PlainModule, sessions=SessionConfig(secret=""))

    def test_host_prefix_misuse_rejected(self):
        with pytest.raises(SessionConfigError):
            LaurenFactory.create(
                PlainModule,
                sessions=SessionConfig(secret="x" * 32, cookie_name="__Host-s", domain="x.com"),
            )

    def test_plain_app_without_sessions_is_fine(self):
        client = TestClient(LaurenFactory.create(PlainModule))
        assert client.get("/no-session/").json() == {"ok": True}


# ---------------------------------------------------------------------------
# A failing store is rendered through the error pipeline (D01). The session
# engine is a global middleware, so a store outage used to surface as a bare
# 500 with ``Connection: close`` and no log.
# ---------------------------------------------------------------------------


class FlakyStore(InMemorySessionStore):
    """An in-memory store whose ``load`` can be switched into an outage."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def load(self, session_id: str):
        if self.down:
            raise ConnectionError("session backend unavailable")
        return await super().load(session_id)


class TestStoreOutage:
    def test_store_failure_is_logged_and_rendered_as_internal_error(self):
        from lauren.logging import InMemoryLogger, LogLevel

        store = FlakyStore()
        log = InMemoryLogger(level=LogLevel.DEBUG)
        client = TestClient(
            LaurenFactory.create(
                SessionModule,
                sessions=SessionConfig(secret="x" * 32, store=store),
                logger=log,
            )
        )
        cookie = _cookie(client.get("/session/visits"))
        assert cookie

        store.down = True
        r = client.get("/session/visits", cookies={COOKIE: cookie})
        assert r.status_code == 500
        assert r.json()["error"]["code"] == "internal_error"
        assert r.json()["error"]["detail"] == {"type": "ConnectionError"}
        assert r.header("connection") is None
        logged = [
            rec
            for rec in log.records
            if rec.extra.get("error") == "ConnectionError" and "origin" in rec.extra
        ]
        assert len(logged) == 1
        assert logged[0].extra["origin"] == "global middleware _SessionEngine"


# ---------------------------------------------------------------------------
# Content-type posture for cookie-authenticated JSON routes (D03 §5.3). A
# cross-site HTML form or ``no-cors`` fetch can POST ``text/plain`` without a
# preflight and the browser attaches the session cookie, so session apps get
# "warn" by default and a startup warning until they choose a policy.
# ---------------------------------------------------------------------------


@controller("/profile")
class ProfileController:
    @post("/")
    async def update(self, session: Session, body: Json[dict]) -> dict:
        session["profile"] = body
        return {"ok": True}


@module(controllers=[ProfileController])
class ProfileModule:
    pass


def _profile_app(log, **kwargs) -> TestClient:
    return TestClient(
        LaurenFactory.create(
            ProfileModule,
            sessions=SessionConfig(secret="x" * 32),
            logger=log,
            **kwargs,
        )
    )


def _startup_warnings(log) -> list:
    return [r for r in log.records if r.context == "LaurenFactory" and "cross-site" in r.message]


class TestContentTypePosture:
    def test_text_plain_json_is_accepted_and_warned_once_by_default(self):
        from lauren.logging import InMemoryLogger, LogLevel

        log = InMemoryLogger(level=LogLevel.DEBUG)
        client = _profile_app(log)

        first = client.post("/profile/", content=b'{"name": "eve"}', headers={"content-type": "text/plain"})
        second = client.post("/profile/", content=b'{"name": "eve"}', headers={"content-type": "text/plain"})

        assert (first.status_code, second.status_code) == (200, 200)
        warned = [r for r in log.records if r.context == "ContentPolicy"]
        assert len(warned) == 1
        assert warned[0].level == LogLevel.WARN
        assert warned[0].extra["content_type"] == "text/plain"

    def test_startup_warns_about_state_changing_json_routes(self):
        from lauren.logging import InMemoryLogger, LogLevel

        log = InMemoryLogger(level=LogLevel.DEBUG)
        _profile_app(log)

        warnings = _startup_warnings(log)
        assert len(warnings) == 1
        assert warnings[0].level == LogLevel.WARN
        assert warnings[0].extra["routes"] == ["POST /profile"]
        assert "strict_content_type=True" in warnings[0].message

    @pytest.mark.parametrize("strict", [True, False, "warn"])
    def test_explicit_policy_silences_the_startup_warning(self, strict):
        from lauren import ContentConfig
        from lauren.logging import InMemoryLogger, LogLevel

        log = InMemoryLogger(level=LogLevel.DEBUG)
        _profile_app(log, content=ContentConfig(strict_content_type=strict))

        assert _startup_warnings(log) == []

    def test_no_startup_warning_without_json_write_routes(self):
        from lauren.logging import InMemoryLogger, LogLevel

        log = InMemoryLogger(level=LogLevel.DEBUG)
        LaurenFactory.create(SessionModule, sessions=SessionConfig(secret="x" * 32), logger=log)

        assert _startup_warnings(log) == []

    def test_strict_mode_rejects_cross_site_simple_requests(self):
        from lauren import ContentConfig
        from lauren.logging import InMemoryLogger, LogLevel

        log = InMemoryLogger(level=LogLevel.DEBUG)
        client = _profile_app(log, content=ContentConfig(strict_content_type=True))

        forged = client.post("/profile/", content=b'{"name": "eve"}', headers={"content-type": "text/plain"})
        genuine = client.post("/profile/", json={"name": "alice"})

        assert forged.status_code == 415
        assert forged.json()["error"]["code"] == "unsupported_media_type"
        assert forged.header("set-cookie") is None
        assert genuine.status_code == 200

    def test_explicit_false_disables_the_check(self):
        from lauren import ContentConfig
        from lauren.logging import InMemoryLogger, LogLevel

        log = InMemoryLogger(level=LogLevel.DEBUG)
        client = _profile_app(log, content=ContentConfig(strict_content_type=False))

        response = client.post("/profile/", content=b"{}", headers={"content-type": "text/plain"})

        assert response.status_code == 200
        assert [r for r in log.records if r.context == "ContentPolicy"] == []

    def test_startup_warning_reaches_stdlib_logging_without_a_logger(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="lauren"):
            LaurenFactory.create(ProfileModule, sessions=SessionConfig(secret="x" * 32))

        assert [r.levelno for r in caplog.records if "cross-site" in r.getMessage()] == [logging.WARNING]


@controller("/settings")
class SettingsWriteController:
    @post("/")
    async def create(self, session: Session, body: Json[dict]) -> dict:
        return {"ok": True}

    @put("/")
    async def replace(self, session: Session, body: Json[dict]) -> dict:
        return {"ok": True}


@module(controllers=[SettingsWriteController])
class SettingsWriteModule:
    pass


class TestStartupWarningScope:
    def test_only_post_routes_are_reported(self):
        # Cross-site "simple" requests can only POST a body; PUT/PATCH/DELETE
        # need a CORS preflight, so they are not part of the warning.
        from lauren.logging import InMemoryLogger, LogLevel

        log = InMemoryLogger(level=LogLevel.DEBUG)
        LaurenFactory.create(SettingsWriteModule, sessions=SessionConfig(secret="x" * 32), logger=log)

        (warning,) = _startup_warnings(log)
        assert warning.extra["routes"] == ["POST /settings"]
