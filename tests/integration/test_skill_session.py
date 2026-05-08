"""Integration tests for the session store skill."""

from __future__ import annotations

import secrets


from lauren import (
    CallNext,
    Json,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    middleware,
    module,
    post,
)
from lauren.testing import TestClient, TestResponse
from lauren.types import Request, Response
from pydantic import BaseModel


@injectable(scope=Scope.SINGLETON)
class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}

    def create(self) -> str:
        sid = secrets.token_hex(32)
        self._sessions[sid] = {}
        return sid

    def get(self, sid: str) -> dict:
        return dict(self._sessions.get(sid, {}))

    def set(self, sid: str, data: dict) -> None:
        if sid not in self._sessions:
            self._sessions[sid] = {}
        self._sessions[sid].update(data)

    def has(self, sid: str) -> bool:
        return sid in self._sessions

    def delete(self, sid: str) -> None:
        self._sessions.pop(sid, None)


@middleware()
class SessionMiddleware:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        sid = request.cookies.get("session_id", "")
        if not sid or not self._store.has(sid):
            sid = self._store.create()

        request.state.session_id = sid
        request.state.session = self._store.get(sid)

        response = await call_next(request)

        self._store.set(sid, request.state.session)
        return response.with_cookie("session_id", sid, http_only=True, path="/")


class SetValueBody(BaseModel):
    key: str
    value: str


@controller("/session")
class SessionController:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    @get("/")
    async def read_session(self, request: Request) -> dict:
        return {"session": request.state.session}

    @post("/set")
    async def write_session(self, request: Request, body: Json[SetValueBody]) -> dict:
        session = request.state.session
        session[body.key] = body.value
        request.state.session = session
        return {"ok": True}

    @get("/id")
    async def session_id(self, request: Request) -> dict:
        return {"session_id": request.state.session_id}


@module(
    controllers=[SessionController],
    providers=[SessionStore, SessionMiddleware],
)
class SessionModule:
    pass


def build_app() -> TestClient:
    return TestClient(
        LaurenFactory.create(SessionModule, global_middlewares=[SessionMiddleware])
    )


def _extract_session_cookie(response: TestResponse) -> str | None:
    """Extract session_id value from Set-Cookie header."""
    set_cookie = response.header("set-cookie") or ""
    for part in set_cookie.split(";"):
        part = part.strip()
        if part.startswith("session_id="):
            return part.split("=", 1)[1]
    return None


class TestSessionStore:
    def test_session_cookie_set_on_first_request(self):
        client = build_app()
        r = client.get("/session/")
        assert r.status_code == 200
        assert r.header("set-cookie") is not None
        assert "session_id=" in (r.header("set-cookie") or "")

    def test_empty_session_on_new_visitor(self):
        client = build_app()
        r = client.get("/session/")
        assert r.json()["session"] == {}

    def test_write_and_read_session_value(self):
        client = build_app()
        # Write a value
        r1 = client.post("/session/set", json={"key": "username", "value": "alice"})
        assert r1.status_code == 200

        sid = _extract_session_cookie(r1)
        assert sid is not None, "No session_id cookie returned"

        # Read back with the same session cookie
        r2 = client.get("/session/", cookies={"session_id": sid})
        assert r2.status_code == 200
        assert r2.json()["session"].get("username") == "alice"

    def test_different_sessions_are_isolated(self):
        client = build_app()
        # Session A writes a value
        r1 = client.post("/session/set", json={"key": "user", "value": "alice"})
        _ = _extract_session_cookie(r1)

        # Session B (no cookie) should have empty session
        r2 = client.get("/session/")
        assert r2.json()["session"] == {}

    def test_multiple_writes_accumulate(self):
        client = build_app()
        r1 = client.post("/session/set", json={"key": "a", "value": "1"})
        sid = _extract_session_cookie(r1)
        assert sid is not None

        client.post(
            "/session/set", json={"key": "b", "value": "2"}, cookies={"session_id": sid}
        )
        r3 = client.get("/session/", cookies={"session_id": sid})
        session = r3.json()["session"]
        assert session.get("a") == "1"
        assert session.get("b") == "2"
