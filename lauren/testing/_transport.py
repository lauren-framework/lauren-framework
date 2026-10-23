"""ASGI plumbing shared by the sync and async test clients.

Everything here runs on the event loop that drives the app — the client's
portal loop for :class:`~lauren.testing.TestClient`, the caller's loop for
:class:`~lauren.testing.AsyncTestClient`.

Connection semantics follow real servers (and Starlette's client):

* the request body is delivered as one ``http.request`` message, or one per
  chunk for a streamed ``content=`` iterable;
* after the last body chunk, ``receive()`` **blocks** until the response is
  complete (final ``http.response.body`` sent, or the app returned) and only
  then reports ``http.disconnect``;
* a simulated client abort (``disconnect_after=``, closing a stream) wakes
  any blocked ``receive()`` with ``http.disconnect`` at once, drops the rest
  of a streamed request body, and silently discards whatever the app sends
  afterwards — exactly what uvicorn does with a connection that went away.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as jsonlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterable, Awaitable, Callable, Iterable
from urllib.parse import urlencode, urlsplit

from ._encoding import _flatten_params, encode_form, encode_multipart

DISCONNECT: dict[str, Any] = {"type": "http.disconnect"}
#: ``scope["extensions"]`` key the client uses to correlate server errors with a request.
EXTENSION_KEY = "lauren.testing"
#: Event-loop iterations an aborted *stream* gets to stop by itself (e.g. a
#: disconnect watcher cancelling its producer) before the client cancels the
#: app's coroutine the way a server drops a dead connection.
ABORT_GRACE_TICKS = 32
#: Unread chunks a streamed response may buffer before ``send()`` waits for the
#: reader — the in-process analogue of TCP back-pressure.
STREAM_BUFFER_LIMIT = 16
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """Where one request goes: the pieces of an absolute URL."""

    scheme: str
    host: str
    port: int
    netloc: str
    path: str
    query: str

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.netloc}{self.path}" + (f"?{self.query}" if self.query else "")


@dataclass(frozen=True)
class BaseURL:
    """A parsed ``base_url``; relative request URLs are resolved against it."""

    scheme: str
    host: str
    port: int
    netloc: str
    path: str

    @classmethod
    def parse(cls, url: str) -> "BaseURL":
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"base_url must be absolute (e.g. 'http://testserver'), got {url!r}")
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        port = parts.port or _DEFAULT_PORTS.get(scheme, 80)
        return cls(scheme, host, port, parts.netloc, parts.path.rstrip("/"))

    def target(self, url: str, params: Any = None) -> Target:
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            scheme = parts.scheme.lower()
            host = (parts.hostname or "").lower()
            port = parts.port or _DEFAULT_PORTS.get(scheme, 80)
            netloc, path = parts.netloc, parts.path or "/"
        else:
            scheme, host, port, netloc = self.scheme, self.host, self.port, self.netloc
            path = parts.path
            if self.path:
                path = self.path + "/" + path.lstrip("/") if path else self.path
            path = path or "/"
        query = parts.query
        if params:
            if hasattr(params, "items"):
                pairs = list(_flatten_params(params))
            else:
                pairs = [(str(k), str(v)) for k, v in params]
            extra = urlencode(pairs)
            query = f"{query}&{extra}" if query else extra
        return Target(scheme, host, port, netloc, path, query)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


@dataclass
class Body:
    """An encoded request body: bytes, or a sync / async iterable streamed chunk by chunk."""

    payload: bytes | Iterable[Any] | AsyncIterable[Any] = b""
    content_type: str | None = None
    streamed: bool = False

    @property
    def replayable(self) -> bool:
        return not self.streamed


def encode_body(*, json: Any, data: Any, files: Any, content: Any) -> Body:
    """Encode the body arguments of a request (same rules as ``httpx``)."""
    if files is not None:
        if json is not None or content is not None:
            raise TypeError("files= cannot be combined with json= or content=")
        if isinstance(data, (str, bytes, bytearray)):
            raise TypeError("data= must be a mapping or a list of pairs when files= is given")
        payload, content_type = encode_multipart(data, files)
        return Body(payload, content_type)
    if json is not None:
        if data is not None:
            raise TypeError("json= cannot be combined with data=")
        return Body(jsonlib.dumps(json).encode("utf-8"), "application/json")
    if data is not None:
        if isinstance(data, str):
            return Body(data.encode("utf-8"))
        if isinstance(data, (bytes, bytearray)):
            return Body(bytes(data))
        if content is not None:
            raise TypeError("data= cannot be combined with content=")
        return Body(encode_form(data), "application/x-www-form-urlencoded")
    if content is None:
        return Body()
    if isinstance(content, str):
        return Body(content.encode("utf-8"))
    if isinstance(content, (bytes, bytearray, memoryview)):
        return Body(bytes(content))
    if hasattr(content, "__aiter__") or hasattr(content, "__iter__"):
        return Body(content, streamed=True)
    raise TypeError(
        f"content= must be bytes, str or an (async) iterable of bytes, got {type(content).__name__}"
    )


def _as_bytes(chunk: Any) -> bytes:
    if isinstance(chunk, str):
        return chunk.encode("utf-8")
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        return bytes(chunk)
    raise TypeError(f"streamed content must yield bytes or str, got {type(chunk).__name__}")


# ---------------------------------------------------------------------------
# One request/response exchange
# ---------------------------------------------------------------------------


@dataclass
class Exchange:
    """Drive one HTTP request through an ASGI app on the current event loop."""

    app: Callable[..., Awaitable[None]]
    scope: dict[str, Any]
    body: Body
    disconnect_after: float | None = None
    streaming: bool = False
    status: int | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    chunks: list[bytes] = field(default_factory=list)
    aborted: bool = False
    task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self.response_started = asyncio.Event()
        self.response_complete = asyncio.Event()
        self.client_gone = asyncio.Event()
        # Streaming mode: chunks the reader has not consumed yet.
        self._pending: deque[bytes] = deque()
        self._readable = asyncio.Event()
        self._writable = asyncio.Event()
        self._writable.set()
        self._ended = False
        self._body_done = False
        self._iterator: Any = None
        self._timer: asyncio.TimerHandle | None = None

    # -- ASGI callables -----------------------------------------------------

    async def receive(self) -> dict[str, Any]:
        if self.client_gone.is_set():
            return DISCONNECT
        if not self._body_done:
            message = await self._next_body_message()
            return DISCONNECT if message is None else message
        await _first_of(self.response_complete, self.client_gone)
        return DISCONNECT

    async def send(self, message: dict[str, Any]) -> None:
        if self.client_gone.is_set():
            # The client went away: drop the message, as servers do — but
            # yield, so an app that keeps sending can still be cancelled.
            await asyncio.sleep(0)
            return
        kind = message.get("type")
        if kind == "http.response.start":
            if self.response_started.is_set():
                return
            self.status = int(message["status"])
            self.headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in message.get("headers", [])]
            self.response_started.set()
        elif kind == "http.response.body":
            chunk = bytes(message.get("body", b"") or b"")
            if chunk:
                if self.streaming:
                    while len(self._pending) >= STREAM_BUFFER_LIMIT and not self.client_gone.is_set():
                        self._writable.clear()
                        await _first_of(self._writable, self.client_gone)
                    if self.client_gone.is_set():
                        return
                    self._pending.append(chunk)
                    self._readable.set()
                else:
                    self.chunks.append(chunk)
            if not message.get("more_body", False):
                self._complete()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.task = loop.create_task(
            self._run_app(), name=f"lauren-testclient {self.scope.get('method')} {self.scope.get('path')}"
        )
        if self.disconnect_after is not None:
            self._timer = loop.call_later(max(self.disconnect_after, 0.0), self.abort)

    def abort(self) -> None:
        """Simulate the client going away (no-op once the response completed)."""
        if self.response_complete.is_set() or self.client_gone.is_set():
            return
        self.aborted = True
        self.client_gone.set()
        self._end_stream()

    async def wait(self, timeout: float | None, *, what: str) -> None:
        """Wait for the app's coroutine; on timeout cancel it and raise :class:`TimeoutError`."""
        assert self.task is not None
        try:
            done, _ = await asyncio.wait({self.task}, timeout=timeout)
        except asyncio.CancelledError:
            self.task.cancel()
            raise
        if not done:
            self.task.cancel()
            await asyncio.wait({self.task})
            self._cancel_timer()
            raise TimeoutError(f"{what} did not complete within {timeout} seconds")
        self._cancel_timer()
        self.task.result()  # re-raise an exception that escaped the app

    async def wait_started(self, timeout: float | None, *, what: str) -> None:
        """Wait until ``http.response.start`` arrived (or the app finished)."""
        assert self.task is not None
        started = asyncio.ensure_future(self.response_started.wait())
        try:
            done, _ = await asyncio.wait(
                {started, self.task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            started.cancel()
        if not done:
            self.task.cancel()
            await asyncio.wait({self.task})
            self._cancel_timer()
            raise TimeoutError(f"{what} sent no response within {timeout} seconds")
        if self.task.done() and not self.response_started.is_set():
            self.task.result()

    async def next_chunk(self, timeout: float | None, *, what: str) -> bytes | None:
        """The next streamed body chunk, or ``None`` once the body ended (or was aborted)."""
        while not self._pending and not self._ended:
            self._readable.clear()
            try:
                await asyncio.wait_for(self._readable.wait(), timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"{what} sent no body chunk within {timeout} seconds") from None
        if self._pending:
            chunk = self._pending.popleft()
            self._writable.set()
            return chunk
        return None

    async def close(self, timeout: float | None, *, what: str) -> None:
        """Finish a stream: wait for a complete response, or abort an unfinished one."""
        assert self.task is not None
        if not self.response_complete.is_set() and not self.task.done():
            self.abort()
            await self.settle_after_abort(timeout, what=what)
            return
        await self.wait(timeout, what=what)

    async def settle_after_abort(self, timeout: float | None, *, what: str) -> None:
        """Give an aborted app a few loop ticks to stop, then cancel it like a dropped connection."""
        assert self.task is not None
        for _ in range(ABORT_GRACE_TICKS):
            if self.task.done():
                break
            await asyncio.sleep(0)
        if not self.task.done():
            self.task.cancel()
        done, _ = await asyncio.wait({self.task}, timeout=timeout)
        self._cancel_timer()
        if not done:
            raise TimeoutError(f"{what} did not stop within {timeout} seconds after the client disconnected")
        if not self.task.cancelled():
            self.task.result()

    # -- internals -------------------------------------------------------------

    async def _run_app(self) -> None:
        try:
            await self.app(self.scope, self.receive, self.send)
        finally:
            self._complete()

    def _complete(self) -> None:
        if not self.response_complete.is_set():
            self.response_complete.set()
            self._end_stream()

    def _end_stream(self) -> None:
        self._ended = True
        self._readable.set()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def _next_body_message(self) -> dict[str, Any] | None:
        payload = self.body.payload
        if not self.body.streamed:
            self._body_done = True
            return {"type": "http.request", "body": payload, "more_body": False}
        if self._iterator is None:
            if hasattr(payload, "__aiter__"):
                self._iterator = payload.__aiter__()  # type: ignore[union-attr]
            else:
                self._iterator = iter(payload)  # type: ignore[arg-type]
        try:
            if hasattr(self._iterator, "__anext__"):
                chunk = await self._race_client_gone(self._iterator.__anext__())
                if chunk is _GONE:
                    return None
            else:
                chunk = next(self._iterator)
        except (StopAsyncIteration, StopIteration):
            self._body_done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": _as_bytes(chunk), "more_body": True}

    async def _race_client_gone(self, awaitable: Awaitable[Any]) -> Any:
        work = asyncio.ensure_future(awaitable)
        gone = asyncio.ensure_future(self.client_gone.wait())
        try:
            await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            gone.cancel()
        if work.done():
            return work.result()
        work.cancel()
        with contextlib.suppress(BaseException):
            await work
        return _GONE


_GONE = object()


async def _first_of(*events: asyncio.Event) -> None:
    if any(e.is_set() for e in events):
        return
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()


# ---------------------------------------------------------------------------
# Lifespan for foreign ASGI apps
# ---------------------------------------------------------------------------


class ForeignLifespan:
    """Drive the ASGI lifespan protocol of a non-Lauren app in a background task."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self._app = app
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._started = asyncio.Event()
        self._stopped = asyncio.Event()
        self._failure: str | None = None
        self.state: dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None

    async def start(self, *, required: bool, timeout: float | None) -> bool:
        """Send ``lifespan.startup``; ``False`` when the app does not speak lifespan."""
        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": self.state}
        self._inbox.put_nowait({"type": "lifespan.startup"})

        async def _run() -> None:
            await self._app(scope, self._inbox.get, self._send)

        self._task = asyncio.get_running_loop().create_task(_run(), name="lauren-testclient lifespan")
        started = asyncio.ensure_future(self._started.wait())
        try:
            done, _ = await asyncio.wait(
                {started, self._task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            started.cancel()
        if not done:
            self._task.cancel()
            raise TimeoutError(f"ASGI lifespan startup did not complete within {timeout} seconds")
        if self._failure is not None:
            raise RuntimeError(f"ASGI lifespan startup failed: {self._failure}")
        if not self._started.is_set():
            error = self._task.exception() if not self._task.cancelled() else None
            if required:
                raise RuntimeError("the app does not support the ASGI lifespan protocol") from error
            return False
        return True

    async def shutdown(self, *, timeout: float | None) -> None:
        if self._task is None or self._task.done():
            return
        self._inbox.put_nowait({"type": "lifespan.shutdown"})
        stopped = asyncio.ensure_future(self._stopped.wait())
        try:
            await asyncio.wait({stopped, self._task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
        if self._failure is not None:
            raise RuntimeError(f"ASGI lifespan shutdown failed: {self._failure}")
        done, _ = await asyncio.wait({self._task}, timeout=timeout)
        if not done:
            self._task.cancel()
            await asyncio.wait({self._task})

    async def _send(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "lifespan.startup.complete":
            self._started.set()
        elif kind == "lifespan.shutdown.complete":
            self._stopped.set()
        elif kind in ("lifespan.startup.failed", "lifespan.shutdown.failed"):
            self._failure = str(message.get("message", "")) or kind
            self._started.set()
            self._stopped.set()


__all__ = [
    "ABORT_GRACE_TICKS",
    "BaseURL",
    "Body",
    "DISCONNECT",
    "EXTENSION_KEY",
    "Exchange",
    "ForeignLifespan",
    "Target",
    "encode_body",
]
