"""Core sessions through the TestClient cookie jar — no hand-threaded cookies."""

from __future__ import annotations

import pytest

from lauren import (
    InMemoryRevocationStore,
    LaurenFactory,
    Session,
    SessionConfig,
    SignedCookieSessionStore,
    controller,
    get,
    module,
    post,
)
from lauren.testing import AsyncTestClient, TestClient

SECRET = "s" * 32


@controller("/account")
class AccountController:
    @post("/login")
    async def login(self, session: Session) -> dict:
        session.regenerate_id()
        session["user_id"] = "u-42"
        return {"ok": True}

    @get("/visits")
    async def visits(self, session: Session) -> dict:
        session["visits"] = session.get("visits", 0) + 1
        return {"visits": session["visits"], "user": session.get("user_id")}

    @post("/logout")
    async def logout(self, session: Session) -> dict:
        session.invalidate()
        return {"ok": True}


@module(controllers=[AccountController])
class AccountModule:
    pass


def _app(**config):
    config.setdefault("secret", SECRET)
    return LaurenFactory.create(AccountModule, sessions=SessionConfig(**config))


def test_login_read_logout_with_the_default_secure_cookie() -> None:
    with TestClient(_app()) as client:
        client.post("/account/login")
        assert client.get("/account/visits").json() == {"visits": 1, "user": "u-42"}
        assert client.get("/account/visits").json() == {"visits": 2, "user": "u-42"}
        client.post("/account/logout")
        assert "lauren_session" not in client.cookies
        assert client.get("/account/visits").json() == {"visits": 1, "user": None}


@pytest.mark.parametrize("store", [None, SignedCookieSessionStore()])
def test_session_cookie_is_stored_for_both_store_kinds(store) -> None:
    config = {} if store is None else {"store": store}
    with TestClient(_app(**config)) as client:
        client.get("/account/visits")
        assert client.get("/account/visits").json()["visits"] == 2


def test_host_prefixed_cookie_round_trips() -> None:
    with TestClient(_app(cookie_name="__Host-sid")) as client:
        client.post("/account/login")
        assert "__Host-sid" in client.cookies
        assert client.get("/account/visits").json()["user"] == "u-42"


def test_strict_cookies_refuse_secure_session_cookies_over_http() -> None:
    with TestClient(_app(), strict_cookies=True) as client:
        client.get("/account/visits")
        assert client.get("/account/visits").json()["visits"] == 1


def test_strict_cookies_over_https_keep_the_session() -> None:
    with TestClient(_app(), strict_cookies=True, base_url="https://testserver") as client:
        client.get("/account/visits")
        assert client.get("/account/visits").json()["visits"] == 2


def test_two_users_in_one_app_via_new_session() -> None:
    with TestClient(_app()) as alice:
        bob = alice.new_session()
        alice.post("/account/login")
        assert bob.get("/account/visits").json()["user"] is None
        assert alice.get("/account/visits").json()["user"] == "u-42"


def test_per_request_cookie_override_is_not_persisted() -> None:
    with TestClient(_app()) as client:
        client.get("/account/visits")
        tampered = client.get("/account/visits", cookies={"lauren_session": "forged"})
        assert tampered.json()["visits"] == 1
        assert client.get("/account/visits").json()["visits"] == 2


@pytest.mark.parametrize("store", [None, SignedCookieSessionStore()])
def test_set_session_issues_a_valid_signed_cookie(store) -> None:
    config = {} if store is None else {"store": store}
    with TestClient(_app(**config)) as client:
        client.set_session({"user_id": "u-7", "visits": 10})
        assert client.get("/account/visits").json() == {"visits": 11, "user": "u-7"}


def test_set_session_with_revocation_store() -> None:
    app = _app(store=SignedCookieSessionStore(), revocation_store=InMemoryRevocationStore(), max_age=3600)
    with TestClient(app) as client:
        client.set_session({"user_id": "u-9"})
        assert client.get("/account/visits").json()["user"] == "u-9"


def test_set_session_without_sessions_enabled_raises() -> None:
    @controller("/")
    class Plain:
        @get("/")
        async def index(self) -> dict:
            return {}

    @module(controllers=[Plain])
    class PlainModule:
        pass

    with TestClient(LaurenFactory.create(PlainModule)) as client:
        with pytest.raises(RuntimeError, match="sessions"):
            client.set_session({"a": 1})


def test_set_session_with_strict_cookies_over_http_explains_the_failure() -> None:
    with TestClient(_app(), strict_cookies=True) as client:
        with pytest.raises(RuntimeError, match="secure context"):
            client.set_session({"a": 1})


async def test_async_client_keeps_the_session() -> None:
    async with AsyncTestClient(_app()) as client:
        await client.post("/account/login")
        assert (await client.get("/account/visits")).json()["user"] == "u-42"
        await client.set_session({"user_id": "u-1"})
        assert (await client.get("/account/visits")).json()["user"] == "u-1"
