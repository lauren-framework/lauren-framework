"""In-process ASGI test client.

This client drives a :class:`LaurenApp` using the ASGI protocol directly, with
no socket or server needed. It mirrors a small subset of the ``httpx`` API.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode, urlsplit


@dataclass
class TestResponse:
    __test__ = False  # not a pytest test class

    status_code: int
    headers: list[tuple[str, str]]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return jsonlib.loads(self.body.decode("utf-8"))

    def header(self, name: str) -> str | None:
        name_l = name.lower()
        for k, v in self.headers:
            if k.lower() == name_l:
                return v
        return None

    def headers_all(self, name: str) -> list[str]:
        name_l = name.lower()
        return [v for k, v in self.headers if k.lower() == name_l]


class TestClient:
    """Synchronous-friendly test client for :class:`LaurenApp`."""

    # Tell pytest not to collect this class as a test.
    __test__ = False

    def __init__(self, app: Any) -> None:
        self._app = app

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        json: Any = None,
        content: bytes | str | None = None,
        params: Mapping[str, Any] | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> TestResponse:
        parts = urlsplit(url)
        path = parts.path or "/"
        query = parts.query
        if params:
            extra = urlencode(list(_flatten_params(params)))
            query = f"{query}&{extra}" if query else extra
        req_headers: list[tuple[bytes, bytes]] = []
        if headers:
            if isinstance(headers, Mapping):
                items = list(headers.items())
            else:
                items = list(headers)
            for k, v in items:
                req_headers.append((k.encode("latin-1"), str(v).encode("latin-1")))
        if cookies:
            cookie_val = "; ".join(f"{k}={v}" for k, v in cookies.items())
            req_headers.append((b"cookie", cookie_val.encode("latin-1")))
        body: bytes = b""
        if json is not None:
            body = jsonlib.dumps(json).encode("utf-8")
            req_headers.append((b"content-type", b"application/json"))
        elif content is not None:
            body = content.encode("utf-8") if isinstance(content, str) else content
        if body and not any(k == b"content-length" for k, _ in req_headers):
            req_headers.append((b"content-length", str(len(body)).encode("latin-1")))

        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": query.encode("latin-1"),
            "headers": req_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        sent_body = False

        async def receive() -> dict[str, Any]:
            nonlocal sent_body
            if sent_body:
                return {"type": "http.disconnect"}
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}

        status = 500
        out_headers: list[tuple[str, str]] = []
        out_body = bytearray()
        started = asyncio.Event()

        async def send(msg: dict[str, Any]) -> None:
            nonlocal status, out_headers
            if msg["type"] == "http.response.start":
                status = msg["status"]
                out_headers = [
                    (k.decode("latin-1"), v.decode("latin-1"))
                    for k, v in msg.get("headers", [])
                ]
                started.set()
            elif msg["type"] == "http.response.body":
                out_body.extend(msg.get("body", b""))

        await self._app(scope, receive, send)
        return TestResponse(
            status_code=status, headers=out_headers, body=bytes(out_body)
        )

    async def arequest(self, method: str, url: str, **kwargs: Any) -> TestResponse:
        return await self._request(method, url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> TestResponse:
        try:
            _ = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._request(method, url, **kwargs))
        # When called from inside a running loop, block via a thread.
        import threading

        result: list[Any] = []
        err: list[BaseException] = []

        def runner() -> None:
            try:
                result.append(asyncio.run(self._request(method, url, **kwargs)))
            except BaseException as exc:  # pragma: no cover - re-raised
                err.append(exc)

        t = threading.Thread(target=runner)
        t.start()
        t.join()
        if err:
            raise err[0]
        return result[0]

    def get(self, url: str, **kw: Any) -> TestResponse:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> TestResponse:
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw: Any) -> TestResponse:
        return self.request("PUT", url, **kw)

    def delete(self, url: str, **kw: Any) -> TestResponse:
        return self.request("DELETE", url, **kw)

    def patch(self, url: str, **kw: Any) -> TestResponse:
        return self.request("PATCH", url, **kw)

    def options(self, url: str, **kw: Any) -> TestResponse:
        return self.request("OPTIONS", url, **kw)

    def head(self, url: str, **kw: Any) -> TestResponse:
        return self.request("HEAD", url, **kw)


def _flatten_params(params: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    for k, v in params.items():
        if isinstance(v, (list, tuple)):
            for item in v:
                yield k, str(item)
        else:
            yield k, str(v)


# ---------------------------------------------------------------------------
# WebSocket test client
# ---------------------------------------------------------------------------


class WebSocketTestSession:
    """In-process ASGI WebSocket session.

    The session drives a :class:`~lauren.LaurenApp` through the ASGI
    WebSocket protocol using two :class:`asyncio.Queue` instances as the
    client/server message channels — no sockets, no server, no timing
    flakiness. The public surface mirrors a tiny subset of the httpx /
    starlette clients.

    Typical usage::

        client = WsTestClient(app)
        async with client.connect("/chat/42") as ws:
            await ws.send_json({"event": "chat.send", "data": {"text": "hi"}})
            reply = await ws.receive_json()

    The session context manager guarantees the server task is awaited
    after the block exits, so any unhandled server-side exception
    propagates into the test harness.
    """

    __test__ = False  # pytest: not a test class

    def __init__(
        self,
        app: Any,
        path: str,
        *,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        subprotocols: Iterable[str] | None = None,
        query_string: str = "",
    ) -> None:
        self._app = app
        self._path = path
        self._query_string = query_string
        if headers is None:
            items: list[tuple[bytes, bytes]] = []
        elif isinstance(headers, Mapping):
            items = [
                (k.encode("latin-1"), str(v).encode("latin-1"))
                for k, v in headers.items()
            ]
        else:
            items = [
                (k.encode("latin-1"), str(v).encode("latin-1")) for k, v in headers
            ]
        self._headers = items
        self._subprotocols = tuple(subprotocols or ())
        # Queues: client → server and server → client. Using
        # ``asyncio.Queue`` means we get back-pressure for free and
        # avoid the race conditions a list+event pair would introduce.
        self._to_server: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._to_client: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._server_task: asyncio.Task[None] | None = None
        # State flags — the client side tracks accept / close so API
        # misuse produces clear errors rather than silent hangs.
        self._accepted = False
        self._closed = False
        self.close_code: int | None = None
        self.close_reason: str = ""
        self.accepted_subprotocol: str | None = None

    async def __aenter__(self) -> "WebSocketTestSession":
        scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": self._path,
            "raw_path": self._path.encode("utf-8"),
            "query_string": self._query_string.encode("latin-1"),
            "headers": self._headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "subprotocols": list(self._subprotocols),
        }

        async def receive() -> dict[str, Any]:
            return await self._to_server.get()

        async def send(msg: dict[str, Any]) -> None:
            await self._to_client.put(msg)

        # Kick off the server-side coroutine immediately. ASGI
        # expects the first message the server awaits to be
        # ``websocket.connect``.
        async def run_app() -> None:
            try:
                await self._app(scope, receive, send)
            finally:
                # Ensure the client never blocks on a dead server.
                await self._to_client.put({"type": "__server_exit__"})

        self._server_task = asyncio.create_task(run_app())
        # Send the opening ``websocket.connect`` message and wait for
        # either ``websocket.accept`` or ``websocket.close``.
        await self._to_server.put({"type": "websocket.connect"})
        msg = await self._to_client.get()
        if msg["type"] == "websocket.accept":
            self._accepted = True
            self.accepted_subprotocol = msg.get("subprotocol")
            return self
        if msg["type"] == "websocket.close":
            self._closed = True
            self.close_code = msg.get("code")
            self.close_reason = msg.get("reason", "")
            await self._wait_server()
            return self
        raise RuntimeError(f"unexpected first server message: {msg.get('type')!r}")

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Initiate a client-side close if the connection is still open
        # so the server loop unblocks and the server task can finish.
        if self._accepted and not self._closed:
            try:
                await self._to_server.put(
                    {"type": "websocket.disconnect", "code": 1000}
                )
            except Exception:
                pass
        await self._wait_server()

    # -- Emission ---------------------------------------------------------

    async def send_text(self, text: str) -> None:
        self._require_open()
        await self._to_server.put(
            {"type": "websocket.receive", "text": text, "bytes": None}
        )

    async def send_bytes(self, data: bytes) -> None:
        self._require_open()
        await self._to_server.put(
            {"type": "websocket.receive", "text": None, "bytes": data}
        )

    async def send_json(self, payload: Any) -> None:
        await self.send_text(jsonlib.dumps(payload))

    # -- Reception --------------------------------------------------------

    async def receive(self) -> dict[str, Any]:
        """Pull the next raw server-to-client message.

        Handles ``websocket.close`` by marking the session closed and
        returning the message so callers who care about close codes can
        inspect them without catching exceptions. Further ``receive()``
        calls after a close raise :class:`RuntimeError`.
        """
        if self._closed:
            raise RuntimeError("session is closed")
        msg = await self._to_client.get()
        if msg["type"] == "websocket.close":
            self._closed = True
            self.close_code = msg.get("code")
            self.close_reason = msg.get("reason", "")
        elif msg["type"] == "__server_exit__":
            # The server task ended without sending ``close`` — treat as
            # an abnormal closure so the test fails fast instead of hanging.
            self._closed = True
            self.close_code = 1006
            raise RuntimeError(
                "server coroutine exited without sending websocket.close"
            )
        return msg

    async def receive_text(self) -> str:
        msg = await self.receive()
        if msg["type"] != "websocket.send":
            raise RuntimeError(f"expected websocket.send, got {msg.get('type')!r}")
        if msg.get("text") is None:
            raise RuntimeError("expected text frame, got bytes")
        return msg["text"]

    async def receive_bytes(self) -> bytes:
        msg = await self.receive()
        if msg["type"] != "websocket.send":
            raise RuntimeError(f"expected websocket.send, got {msg.get('type')!r}")
        if msg.get("bytes") is None:
            raise RuntimeError("expected binary frame, got text")
        return msg["bytes"]

    async def receive_json(self) -> Any:
        return jsonlib.loads(await self.receive_text())

    # -- Termination ------------------------------------------------------

    async def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        await self._to_server.put({"type": "websocket.disconnect", "code": code})
        await self._wait_server()

    # -- Internal ---------------------------------------------------------

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("cannot send on a closed session")
        if not self._accepted:
            raise RuntimeError(
                "cannot send before the server has accepted the connection"
            )

    async def _wait_server(self) -> None:
        if self._server_task is None:
            return
        try:
            # Generous timeout so a stuck gateway doesn't deadlock the
            # test forever — one second is plenty for in-process ASGI.
            await asyncio.wait_for(self._server_task, timeout=2.0)
        except asyncio.TimeoutError:
            self._server_task.cancel()
            try:
                await self._server_task
            except Exception:
                pass
            raise RuntimeError("WebSocket server task did not complete in time")
        finally:
            self._server_task = None


class WsTestClient:
    """Factory for :class:`WebSocketTestSession` bound to an ASGI app.

    Mirrors the ergonomic pattern of :class:`TestClient` — one client
    instance per app, one :class:`WebSocketTestSession` per connection.
    Use as::

        client = WsTestClient(app)
        async with client.connect("/chat/42", headers={...}) as ws:
            ...
    """

    __test__ = False

    def __init__(self, app: Any) -> None:
        self._app = app

    def connect(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        subprotocols: Iterable[str] | None = None,
        query_string: str = "",
    ) -> WebSocketTestSession:
        return WebSocketTestSession(
            self._app,
            path,
            headers=headers,
            subprotocols=subprotocols,
            query_string=query_string,
        )


__all__ = ["TestClient", "TestResponse", "WsTestClient", "WebSocketTestSession"]
