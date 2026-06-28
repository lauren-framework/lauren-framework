"""Integration tests for session revocation.

Verifies the two opt-in mechanisms end-to-end through ``TestClient``:

* per-session **deny-list** — logout (and id rotation) make even a
  stateless ``SignedCookieSessionStore`` cookie un-replayable;
* per-user **cutoff** — "log out everywhere" invalidates a user's other
  sessions, for both the cookie and server-side stores.

It also pins the default: with no ``revocation_store`` the cookie store is
*not* revocable (replay still works), so revocation is genuinely opt-in.
"""

from __future__ import annotations

import time

import pytest

from lauren import (
    InMemoryRevocationStore,
    InMemorySessionStore,
    LaurenFactory,
    RevocationStore,
    Session,
    SessionConfig,
    SignedCookieSessionStore,
    controller,
    get,
    module,
    post,
)
from lauren.exceptions import SessionConfigError
from lauren.testing import TestClient, TestResponse

COOKIE = "lauren_session"


def _cookie(response: TestResponse) -> str | None:
    sc = response.header("set-cookie") or ""
    for part in sc.split(";"):
        part = part.strip()
        if part.startswith(f"{COOKIE}="):
            return part.split("=", 1)[1]
    return None


@controller("/auth")
class AuthController:
    @get("/me")
    async def me(self, session: Session) -> dict:
        return {"new": session.is_new, "user": session.get("user_id"), "data": session.as_dict()}

    @post("/visit")
    async def visit(self, session: Session) -> dict:
        # Establish an anonymous session with data (no login / no rotation).
        session["visits"] = session.get("visits", 0) + 1
        return {"visits": session["visits"]}

    @post("/login")
    async def login(self, session: Session) -> dict:
        session.regenerate_id()
        session["user_id"] = "u-1"
        return {"ok": True}

    @post("/logout")
    async def logout(self, session: Session) -> dict:
        session.invalidate()
        return {"ok": True}


@controller("/auth")
class RevocationController:
    # Lives in its own controller so the plain AuthModule (used by the
    # revocation-off test) does not depend on a RevocationStore provider.
    @post("/logout-all")
    async def logout_all(self, session: Session, revocation: RevocationStore) -> dict:
        await revocation.revoke_user(str(session.get("user_id")))
        return {"ok": True}


@module(controllers=[AuthController])
class AuthModule:
    pass


@module(controllers=[AuthController, RevocationController])
class RevAuthModule:
    pass


def build(store, revocation) -> TestClient:
    return TestClient(
        LaurenFactory.create(
            RevAuthModule,
            sessions=SessionConfig(
                secret="revocation-secret" * 2,
                store=store,
                revocation_store=revocation,
                max_age=3600,
            ),
        )
    )


def build_plain(store) -> TestClient:
    return TestClient(
        LaurenFactory.create(
            AuthModule,
            sessions=SessionConfig(secret="revocation-secret" * 2, store=store, max_age=3600),
        )
    )


class TestCookieStoreDenyList:
    def test_logout_makes_cookie_unreplayable(self):
        client = build(SignedCookieSessionStore(), InMemoryRevocationStore())
        sid = _cookie(client.post("/auth/login"))
        assert client.get("/auth/me", cookies={COOKIE: sid}).json()["new"] is False
        client.post("/auth/logout", cookies={COOKIE: sid})
        # The stolen/old cookie is still signed and unexpired — but revoked.
        replay = client.get("/auth/me", cookies={COOKIE: sid})
        assert replay.json()["new"] is True
        assert replay.json()["user"] is None

    def test_regenerate_revokes_prior_cookie(self):
        client = build(SignedCookieSessionStore(), InMemoryRevocationStore())
        # 1. Anonymous session with data → pre-login cookie.
        pre_login = _cookie(client.post("/auth/visit"))
        assert client.get("/auth/me", cookies={COOKIE: pre_login}).json()["data"] == {"visits": 1}
        # 2. Login on that cookie → regenerate_id() rotates + deny-lists it.
        client.post("/auth/login", cookies={COOKIE: pre_login})
        # 3. Replaying the pre-login cookie is now rejected (fixation defence).
        replay = client.get("/auth/me", cookies={COOKIE: pre_login})
        assert replay.json()["new"] is True

    def test_without_revocation_cookie_is_replayable(self):
        # Pins the opt-in default: no revocation store → stateless cookie
        # cannot be revoked server-side (replay still loads).
        client = build_plain(SignedCookieSessionStore())
        sid = _cookie(client.post("/auth/login"))
        client.post("/auth/logout", cookies={COOKIE: sid})
        replay = client.get("/auth/me", cookies={COOKIE: sid})
        assert replay.json()["new"] is False  # replay still works — the known limitation


class TestPerUserLogoutEverywhere:
    @pytest.mark.parametrize("store_factory", [SignedCookieSessionStore, InMemorySessionStore])
    def test_logout_all_invalidates_other_sessions(self, store_factory):
        client = build(store_factory(), InMemoryRevocationStore())
        device_a = _cookie(client.post("/auth/login"))
        device_b = _cookie(client.post("/auth/login"))
        assert client.get("/auth/me", cookies={COOKIE: device_a}).json()["new"] is False
        # device B triggers "log out everywhere"
        client.post("/auth/logout-all", cookies={COOKIE: device_b})
        time.sleep(0.01)
        after = client.get("/auth/me", cookies={COOKIE: device_a})
        assert after.json()["new"] is True


class TestRevocationStartupValidation:
    def test_revocation_without_finite_lifetime_rejected(self):
        with pytest.raises(SessionConfigError):
            LaurenFactory.create(
                AuthModule,
                sessions=SessionConfig(
                    secret="x" * 32,
                    revocation_store=InMemoryRevocationStore(),
                    max_age=None,
                    idle_timeout=None,
                ),
            )

    def test_revocation_store_is_injectable(self):
        # The /auth/logout-all handler injects RevocationStore; if it weren't
        # registered the app would fail to build.
        client = build(InMemorySessionStore(), InMemoryRevocationStore())
        assert client.get("/auth/me").json()["new"] is True
