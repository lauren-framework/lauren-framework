"""End-to-end session tests across validator backends and both stores.

Drives a single app through the complete request/response cycle, storing
session payloads that were validated through the dataclass and TypedDict
backends (and pydantic, when installed), against both the server-side and
the stateless cookie store. Confirms sessions interoperate with the
validation pipeline (including a 422 that still round-trips the cookie)
and the full login → authenticated-read → logout lifecycle.
"""

from __future__ import annotations

import dataclasses
from typing import TypedDict

import pytest

from lauren import (
    InMemorySessionStore,
    Json,
    LaurenFactory,
    Session,
    SessionConfig,
    SignedCookieSessionStore,
    controller,
    get,
    module,
    post,
)
from lauren.testing import TestClient, TestResponse

COOKIE = "lauren_session"


def _cookie(response: TestResponse) -> str | None:
    sc = response.header("set-cookie") or ""
    for part in sc.split(";"):
        part = part.strip()
        if part.startswith(f"{COOKIE}="):
            return part.split("=", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Domain types — two distinct validator backends
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Book:
    title: str
    author: str
    pages: int = 0


class SearchQuery(TypedDict):
    q: str
    limit: int


@controller("/e2e")
class E2EController:
    @post("/book")
    async def save_book(self, session: Session, body: Json[Book]) -> dict:
        session["book"] = dataclasses.asdict(body)
        return {"ok": True}

    @post("/search")
    async def save_search(self, session: Session, body: Json[SearchQuery]) -> dict:
        session["search"] = dict(body)
        return {"ok": True}

    @get("/dump")
    async def dump(self, session: Session) -> dict:
        return {"data": session.as_dict(), "new": session.is_new}

    @post("/login")
    async def login(self, session: Session, body: Json[Book]) -> dict:
        session.regenerate_id()
        session["user"] = body.title
        return {"ok": True}

    @post("/logout")
    async def logout(self, session: Session) -> dict:
        session.invalidate()
        return {"ok": True}


@module(controllers=[E2EController])
class E2EModule:
    pass


@pytest.fixture(params=["memory", "cookie"])
def client(request) -> TestClient:
    store = InMemorySessionStore() if request.param == "memory" else SignedCookieSessionStore()
    app = LaurenFactory.create(
        E2EModule,
        sessions=SessionConfig(secret="e2e-secret" * 4, store=store),
        openapi_url="/openapi.json",
    )
    return TestClient(app)


class TestSessionValidationInterop:
    def test_dataclass_payload_roundtrips(self, client: TestClient):
        r1 = client.post("/e2e/book", json={"title": "Dune", "author": "Herbert", "pages": 412})
        cookie = _cookie(r1)
        r2 = client.get("/e2e/dump", cookies={COOKIE: cookie})
        assert r2.json()["data"]["book"] == {"title": "Dune", "author": "Herbert", "pages": 412}

    def test_typeddict_payload_roundtrips(self, client: TestClient):
        r1 = client.post("/e2e/search", json={"q": "rust", "limit": 10})
        cookie = _cookie(r1)
        r2 = client.get("/e2e/dump", cookies={COOKIE: cookie})
        assert r2.json()["data"]["search"] == {"q": "rust", "limit": 10}

    def test_malformed_body_422_does_not_create_session(self, client: TestClient):
        r = client.post("/e2e/book", json={"title": "NoAuthor"})  # missing 'author'
        assert r.status_code == 422
        # Validation failed before the handler ran, so nothing was stored.
        assert r.header("set-cookie") is None

    def test_full_login_logout_cycle(self, client: TestClient):
        """Shared lifecycle assertions true for both stores."""
        # anonymous activity
        anon = client.post("/e2e/search", json={"q": "x", "limit": 1})
        c0 = _cookie(anon)
        # login records a user and re-issues the cookie
        login = client.post("/e2e/login", json={"title": "alice", "author": "a"}, cookies={COOKIE: c0})
        c1 = _cookie(login)
        assert c1 is not None and c1 != c0
        dumped = client.get("/e2e/dump", cookies={COOKIE: c1})
        assert dumped.json()["data"]["user"] == "alice"
        # logout instructs the browser to drop the cookie (Max-Age=0)
        out = client.post("/e2e/logout", cookies={COOKIE: c1})
        assert "Max-Age=0" in (out.header("set-cookie") or "")

    def test_server_store_logout_revokes_session(self):
        """Server-side stores can truly revoke: the old id stops loading.

        (A stateless cookie store cannot — logout there relies on the
        browser honouring Max-Age=0; a replayed cookie still validates.)
        """
        client = TestClient(
            LaurenFactory.create(
                E2EModule,
                sessions=SessionConfig(secret="e2e-secret" * 4, store=InMemorySessionStore()),
            )
        )
        c0 = _cookie(client.post("/e2e/login", json={"title": "bob", "author": "b"}))
        assert client.get("/e2e/dump", cookies={COOKIE: c0}).json()["new"] is False
        client.post("/e2e/logout", cookies={COOKIE: c0})
        after = client.get("/e2e/dump", cookies={COOKIE: c0})
        assert after.json()["new"] is True
        assert after.json()["data"] == {}


class TestSessionPydanticInterop:
    def test_pydantic_payload_roundtrips(self):
        pytest.importorskip("pydantic")
        from pydantic import BaseModel

        class Profile(BaseModel):
            name: str
            age: int

        @controller("/p")
        class PController:
            @post("/save")
            async def save(self, session: Session, body: Json[Profile]) -> dict:
                session["profile"] = body.model_dump()
                return {"ok": True}

            @get("/dump")
            async def dump(self, session: Session) -> dict:
                return {"data": session.as_dict()}

        @module(controllers=[PController])
        class PModule:
            pass

        client = TestClient(LaurenFactory.create(PModule, sessions=SessionConfig(secret="x" * 16)))
        r1 = client.post("/p/save", json={"name": "bob", "age": 30})
        cookie = _cookie(r1)
        r2 = client.get("/p/dump", cookies={COOKIE: cookie})
        assert r2.json()["data"]["profile"] == {"name": "bob", "age": 30}
