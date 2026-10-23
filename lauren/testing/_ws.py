"""In-process WebSocket test sessions (async API).

:class:`WsTestClient` / :class:`WebSocketTestSession` drive a WebSocket
connection through the ASGI protocol in the caller's event loop.
:meth:`lauren.testing.TestClient.websocket_connect` wraps the same session in
a synchronous API running on the client's own loop.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from typing import Any, Iterable, Mapping


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
        server_timeout: float = 2.0,
        scheme: str = "ws",
        server: tuple[str, int] = ("testserver", 80),
    ) -> None:
        self._app = app
        self._path = path
        self._query_string = query_string
        #: Seconds the session waits for the server coroutine to finish on exit.
        self._server_timeout = server_timeout
        self._scheme = scheme
        self._server = server
        if headers is None:
            items: list[tuple[bytes, bytes]] = []
        elif isinstance(headers, Mapping):
            items = [(k.encode("latin-1"), str(v).encode("latin-1")) for k, v in headers.items()]
        else:
            items = [(k.encode("latin-1"), str(v).encode("latin-1")) for k, v in headers]
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
            "scheme": getattr(self, "_scheme", "ws"),
            "path": self._path,
            "raw_path": self._path.encode("utf-8"),
            "query_string": self._query_string.encode("latin-1"),
            "headers": self._headers,
            "client": ("127.0.0.1", 12345),
            "server": getattr(self, "_server", ("testserver", 80)),
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
                await self._to_server.put({"type": "websocket.disconnect", "code": 1000})
            except Exception:
                pass
        await self._wait_server()

    # -- Emission ---------------------------------------------------------

    async def send_text(self, text: str) -> None:
        self._require_open()
        await self._to_server.put({"type": "websocket.receive", "text": text, "bytes": None})

    async def send_bytes(self, data: bytes) -> None:
        self._require_open()
        await self._to_server.put({"type": "websocket.receive", "text": None, "bytes": data})

    async def send_json(self, payload: Any) -> None:
        await self.send_text(jsonlib.dumps(payload))

    async def send_event(self, event: str, data: Any = None) -> None:
        """Send Lauren's ``{"event": ..., "data": ...}`` message envelope."""
        await self.send_json({"event": event, "data": data})

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
            raise RuntimeError("server coroutine exited without sending websocket.close")
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

    async def receive_event(self) -> dict[str, Any]:
        """Receive the next JSON frame — Lauren's ``{"event", "data"}`` envelope — as a dict."""
        frame = await self.receive_json()
        if not isinstance(frame, dict):
            raise RuntimeError(f"expected a JSON object frame, got {type(frame).__name__}")
        return frame

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
            raise RuntimeError("cannot send before the server has accepted the connection")

    async def _wait_server(self) -> None:
        if self._server_task is None:
            return
        timeout = getattr(self, "_server_timeout", 2.0)
        try:
            # Bounded so a stuck gateway cannot deadlock the test forever
            # (``server_timeout=`` on ``WsTestClient.connect``).
            await asyncio.wait_for(self._server_task, timeout=timeout)
        except asyncio.TimeoutError:
            # ``wait_for`` already cancelled the task; wait for it without
            # re-raising its CancelledError into the test.
            self._server_task.cancel()
            await asyncio.wait({self._server_task})
            raise RuntimeError(f"WebSocket server task did not complete in time ({timeout}s)") from None
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
        server_timeout: float = 2.0,
    ) -> WebSocketTestSession:
        """Open a session; ``server_timeout`` bounds the server's wind-down on exit."""
        return WebSocketTestSession(
            self._app,
            path,
            headers=headers,
            subprotocols=subprotocols,
            query_string=query_string,
            server_timeout=server_timeout,
        )


__all__ = ["WsTestClient", "WebSocketTestSession"]
