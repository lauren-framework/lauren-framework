"""``TestClient`` (sync, persistent portal loop) and ``AsyncTestClient`` (caller's loop)."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
import warnings
import weakref
from datetime import timedelta
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Iterator, Literal, Mapping, TypeVar
from urllib.parse import urljoin

from ._cookies import CookieJar
from ._portal import Portal, stop_portal
from ._response import ResponseHeaders, SentRequest, StreamingTestResponse, TestResponse
from ._transport import EXTENSION_KEY, BaseURL, Body, Exchange, ForeignLifespan, Target, encode_body
from ._ws import WebSocketTestSession

T = TypeVar("T")

LifespanMode = Literal["auto", "on", "off"]

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 20
#: Per-request time limit (seconds) unless the client says otherwise.
DEFAULT_TIMEOUT = 30.0
#: Sentinel: "use the client's configured value".
_UNSET: Any = object()
_portal_ids = itertools.count(1)
#: One driver per app (PRD F21 §5.6): app -> weak reference to the driver that
#: owns its event loop and lifecycle. Holds no app data; entries vanish with
#: the app or the driver.
_DRIVERS: "weakref.WeakKeyDictionary[Any, weakref.ref[_Driver]]" = weakref.WeakKeyDictionary()
#: Whether the "sync client inside a running loop" RuntimeWarning was issued.
_loop_warning = {"issued": False}


def _app_kind(app: Any) -> str:
    from .._app import Lauren
    from .._asgi import LaurenApp

    if isinstance(app, LaurenApp):
        return "lauren_app"
    if isinstance(app, Lauren):
        return "lauren"
    return "asgi"


def _describe_app(app: Any) -> str:
    return f"{type(app).__name__} at {id(app):#x}"


def _record_server_error(event: Any) -> None:
    """``RequestComplete`` listener behind ``raise_server_exceptions=True``.

    Stores an unhandled (non-HTTP, 5xx) error in the request's
    ``lauren.testing`` scope extension so the client that sent the request
    can re-raise it. Requests without the extension are ignored.
    """
    from ..exceptions import HTTPError

    error = event.error
    if error is None or isinstance(error, HTTPError) or event.status < 500:
        return
    slot = event.request.extensions.get(EXTENSION_KEY)
    if isinstance(slot, dict):
        slot.setdefault("error", error)


def _loop_factory(
    backend: str, options: Mapping[str, Any] | None
) -> Callable[[], asyncio.AbstractEventLoop] | None:
    if backend != "asyncio":
        raise ValueError(
            f"backend={backend!r} is not supported: Lauren apps run on asyncio; use backend='asyncio'"
        )
    opts = dict(options or {})
    factory = opts.pop("loop_factory", None)
    if opts.pop("use_uvloop", False):
        import uvloop  # optional dependency, imported only on request

        factory = uvloop.new_event_loop
    if opts:
        raise ValueError(f"unknown backend_options: {sorted(opts)}; supported: 'loop_factory', 'use_uvloop'")
    return factory


class _Driver:
    """The event loop and lifecycle of one app, shared by every client attached to it."""

    def __init__(
        self,
        app: Any,
        *,
        portal: Portal | None,
        loop: asyncio.AbstractEventLoop | None,
        label: str,
        lifespan: LifespanMode,
    ) -> None:
        self.app = app
        self.kind = _app_kind(app)
        self.portal = portal
        self.loop = portal.loop if portal is not None else loop
        self.label = label
        self.lifespan = lifespan
        self.started = False
        self.closed = False
        self.foreign: ForeignLifespan | None = None
        self.error_bus: Any = None
        self._finalizer = weakref.finalize(self, stop_portal, portal) if portal is not None else None
        try:
            _DRIVERS[app] = weakref.ref(self)
        except TypeError:  # unhashable / not weak-referenceable app: no registry entry
            pass

    # -- state ----------------------------------------------------------------

    @property
    def alive(self) -> bool:
        if self.closed:
            return False
        if self.portal is not None:
            return self.portal.running
        return self.loop is not None and not self.loop.is_closed()

    def lauren_app(self) -> Any:
        """The underlying ``LaurenApp`` (``None`` for foreign apps / an uncompiled ``Lauren``)."""
        if self.kind == "lauren_app":
            return self.app
        if self.kind == "lauren":
            return self.app._compiled
        return None

    def unregister(self) -> None:
        try:
            ref = _DRIVERS.get(self.app)
        except TypeError:
            return
        if ref is not None and ref() is self:
            del _DRIVERS[self.app]

    # -- lifecycle (run on the driver's loop) ---------------------------------

    async def start(self, *, foreign_lifespan: bool, strict_restart: bool, timeout: float | None) -> None:
        if self.lifespan == "off":
            return
        target = self.lauren_app()
        if strict_restart and target is not None and target._shutdown_running:
            self._refuse_restart()
        if self.started:
            return
        if self.kind == "lauren_app":
            if not self.app._started:
                await self.app.startup()
        elif self.kind == "lauren":
            await self.app._ensure_started()
        elif foreign_lifespan or self.lifespan == "on":
            foreign = ForeignLifespan(self.app)
            if await foreign.start(required=self.lifespan == "on", timeout=timeout):
                self.foreign = foreign
        self.started = True

    def _refuse_restart(self) -> None:
        from ..exceptions import LifecycleViolationError

        raise RuntimeError(
            f"{_describe_app(self.app)} was already shut down and cannot be started again; "
            "build a fresh app (LaurenFactory.create / Test.create_testing_module(...).compile()) "
            "for each lifespan-managed client"
        ) from LifecycleViolationError("startup called twice")

    async def ensure_ready(self) -> None:
        """Per-request hook: compile and start a ``Lauren`` app on first use (1.x behaviour)."""
        if self.kind == "lauren" and self.lifespan != "off":
            await self.app._ensure_started()

    async def shutdown(self, *, timeout: float | None) -> None:
        try:
            if self.lifespan == "off":
                return
            if self.foreign is not None:
                await self.foreign.shutdown(timeout=timeout)
                return
            target = self.lauren_app()
            if target is not None and target._started:
                await self.app.shutdown()
        finally:
            self.release_error_listener()

    def ensure_error_listener(self) -> None:
        if self.error_bus is not None:
            return
        target = self.lauren_app()
        if target is None:
            return
        from ..signals import RequestComplete

        target.signals.on(RequestComplete)(_record_server_error)
        self.error_bus = target.signals

    def release_error_listener(self) -> None:
        if self.error_bus is not None:
            from ..signals import RequestComplete

            self.error_bus.off(RequestComplete, _record_server_error)
            self.error_bus = None

    # -- teardown (sync driver) -----------------------------------------------

    def stop_portal(self) -> list[str]:
        self.closed = True
        self.unregister()
        if self._finalizer is not None:
            self._finalizer.detach()
        return self.portal.stop() if self.portal is not None else []


def _live_driver(app: Any) -> _Driver | None:
    try:
        ref = _DRIVERS.get(app)
    except TypeError:
        return None
    driver = ref() if ref is not None else None
    if driver is None or not driver.alive:
        return None
    return driver


class _Spec:
    """A prepared request: everything except the per-hop cookie header."""

    __slots__ = ("method", "target", "headers", "body", "cookies", "explicit_cookie")

    def __init__(
        self,
        method: str,
        target: Target,
        headers: list[tuple[str, str]],
        body: Body,
        cookies: Mapping[str, str] | None,
    ) -> None:
        self.method = method
        self.target = target
        self.headers = headers
        self.body = body
        self.cookies = dict(cookies) if cookies else None
        self.explicit_cookie = any(k.lower() == "cookie" for k, _ in headers)


def _header_items(headers: Any) -> list[tuple[str, str]]:
    if not headers:
        return []
    items = headers.items() if isinstance(headers, Mapping) else headers
    return [(str(k), str(v)) for k, v in items]


class _ClientBase:
    """Configuration, request preparation and the async request path shared by both clients."""

    __test__ = False  # not a pytest test class

    def __init__(
        self,
        app: Any,
        *,
        base_url: str,
        headers: Mapping[str, str] | None,
        cookies: Mapping[str, str] | None,
        follow_redirects: bool,
        timeout: Any,
        raise_server_exceptions: bool,
        lifespan: LifespanMode,
        root_path: str,
        strict_cookies: bool,
        persist_cookies: bool | None,
    ) -> None:
        if lifespan not in ("auto", "on", "off"):
            raise ValueError(f"lifespan must be 'auto', 'on' or 'off', got {lifespan!r}")
        self.app = app
        #: 1.x attribute name, kept for tests that reach for ``client._app``.
        self._app = app
        self._base = BaseURL.parse(base_url)
        self.base_url = base_url
        #: Default headers sent with every request (per-request ``headers=`` override them).
        self.headers: dict[str, str] = dict(headers or {})
        self.cookies = CookieJar(default_host=self._base.host, strict=strict_cookies)
        for name, value in (cookies or {}).items():
            self.cookies.set(name, value)
        self.follow_redirects = follow_redirects
        self._timeout_setting: Any = timeout
        self.raise_server_exceptions = raise_server_exceptions
        self.root_path = root_path
        self._lifespan: LifespanMode = lifespan
        self._strict_cookies = strict_cookies
        self._persist_cookies = persist_cookies
        #: 1.x compatibility mode — a bare ``TestClient(app)`` until it is
        #: entered with ``with``: no Set-Cookie storage, no request timeout
        #: unless asked for explicitly.
        self._compat = False

    @property
    def timeout(self) -> float | None:
        """Per-request time limit in seconds (``None``: unlimited).

        Defaults to ``DEFAULT_TIMEOUT`` (30 s); a bare ``TestClient(app)`` not
        used as a context manager keeps the 1.x behaviour of no limit.
        """
        if self._timeout_setting is _UNSET:
            return None if self._compat else DEFAULT_TIMEOUT
        return self._timeout_setting  # type: ignore[no-any-return]

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        self._timeout_setting = value

    # -- request preparation ----------------------------------------------------

    def _build(
        self,
        method: str,
        url: str,
        *,
        params: Any,
        headers: Any,
        cookies: Mapping[str, str] | None,
        json: Any,
        data: Any,
        files: Any,
        content: Any,
    ) -> _Spec:
        target = self._base.target(url, params)
        body = encode_body(json=json, data=data, files=files, content=content)
        explicit = _header_items(headers)
        overridden = {k.lower() for k, _ in explicit}
        merged = [(k, v) for k, v in self.headers.items() if k.lower() not in overridden] + explicit
        return _Spec(method.upper(), target, merged, body, cookies)

    def _scope_for(
        self, driver: _Driver, spec: _Spec, method: str, target: Target, body: Body
    ) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        names = {k.lower() for k, _ in spec.headers}
        sent: list[tuple[str, str]] = []
        if "host" not in names:
            sent.append(("host", target.netloc))
        sent.extend(spec.headers)
        if not spec.explicit_cookie:
            cookie = self.cookies.header_value(
                scheme=target.scheme, host=target.host, path=target.path, extra=spec.cookies
            )
            if cookie:
                sent.append(("cookie", cookie))
        elif spec.cookies:
            # 1.x behaviour: an explicit Cookie header plus cookies= sends both.
            sent.append(("cookie", "; ".join(f"{k}={v}" for k, v in spec.cookies.items())))
        if body.content_type and "content-type" not in names:
            sent.append(("content-type", body.content_type))
        if not body.streamed and body.payload and "content-length" not in names:
            sent.append(("content-length", str(len(body.payload))))  # type: ignore[arg-type]
        try:
            query = target.query.encode("latin-1")
        except UnicodeEncodeError:
            query = target.query.encode("utf-8")
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": target.scheme,
            "path": target.path,
            "raw_path": target.path.encode("utf-8"),
            "query_string": query,
            "root_path": self.root_path,
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in sent],
            "client": ("127.0.0.1", 12345),
            "server": (target.host, target.port),
        }
        if driver.foreign is not None:
            scope["state"] = dict(driver.foreign.state)
        return scope, sent

    # -- async request path (runs on the driver's loop) --------------------------

    async def _asend(
        self,
        driver: _Driver,
        spec: _Spec,
        *,
        follow_redirects: bool,
        timeout: float | None,
        disconnect_after: float | None,
    ) -> TestResponse:
        await driver.ensure_ready()
        history: list[TestResponse] = []
        hop = spec
        for _ in range(_MAX_REDIRECTS + 1):
            response = await self._asend_once(driver, hop, timeout=timeout, disconnect_after=disconnect_after)
            if not (follow_redirects and response.is_redirect):
                response.history = history
                return response
            history.append(response)
            hop = self._redirect_hop(hop, response, original_cookies=spec.cookies)
        raise RuntimeError(f"exceeded the maximum of {_MAX_REDIRECTS} redirects (last: {hop.target.url})")

    def _redirect_hop(
        self, hop: _Spec, response: TestResponse, *, original_cookies: Mapping[str, str] | None
    ) -> _Spec:
        """The request a browser would send next for a redirect ``response`` (``httpx`` rules)."""
        location = response.headers.get("location") or ""
        target = self._base.target(urljoin(hop.target.url, location))
        method, body, headers = hop.method, hop.body, hop.headers
        status = response.status_code
        if (status == 303 and method != "HEAD") or (status in (301, 302) and method == "POST"):
            method, body = "GET", Body()
            headers = [(k, v) for k, v in headers if k.lower() not in ("content-type", "content-length")]
        elif not body.replayable:
            raise RuntimeError(
                f"cannot follow a {status} redirect: the streamed request body was already sent"
            )
        if (target.scheme, target.netloc) != (hop.target.scheme, hop.target.netloc):
            headers = [(k, v) for k, v in headers if k.lower() != "authorization"]
        return _Spec(method, target, headers, body, original_cookies)

    async def _asend_once(
        self, driver: _Driver, spec: _Spec, *, timeout: float | None, disconnect_after: float | None
    ) -> TestResponse:
        scope, sent = self._scope_for(driver, spec, spec.method, spec.target, spec.body)
        slot = self._error_slot(driver, scope)
        exchange = Exchange(driver.app, scope, spec.body, disconnect_after=disconnect_after)
        started = time.perf_counter()
        exchange.start()
        await exchange.wait(timeout, what=f"{spec.method} {spec.target.url}")
        status = exchange.status if exchange.status is not None else (None if exchange.aborted else 500)
        response = TestResponse(
            status,
            exchange.headers,
            b"".join(exchange.chunks),
            aborted=exchange.aborted,
            url=spec.target.url,
            request=self._sent_request(spec, sent),
            elapsed=timedelta(seconds=time.perf_counter() - started),
        )
        self._store_cookies(spec.target, response.headers)
        self._raise_recorded(slot)
        return response

    async def _aopen_stream(
        self, driver: _Driver, spec: _Spec, *, timeout: float | None, disconnect_after: float | None
    ) -> tuple[Exchange, dict[str, Any] | None, list[tuple[str, str]]]:
        await driver.ensure_ready()
        scope, sent = self._scope_for(driver, spec, spec.method, spec.target, spec.body)
        slot = self._error_slot(driver, scope)
        exchange = Exchange(driver.app, scope, spec.body, disconnect_after=disconnect_after, streaming=True)
        exchange.start()
        await exchange.wait_started(timeout, what=f"{spec.method} {spec.target.url}")
        self._store_cookies(spec.target, ResponseHeaders(exchange.headers))
        return exchange, slot, sent

    def _stream_response(
        self, spec: _Spec, exchange: Exchange, sent: list[tuple[str, str]], source: Any
    ) -> StreamingTestResponse:
        status = exchange.status if exchange.status is not None else (None if exchange.aborted else 500)
        return StreamingTestResponse(
            status,
            exchange.headers,
            source=source,
            url=spec.target.url,
            request=self._sent_request(spec, sent),
        )

    def _error_slot(self, driver: _Driver, scope: dict[str, Any]) -> dict[str, Any] | None:
        if not self.raise_server_exceptions:
            return None
        driver.ensure_error_listener()
        slot: dict[str, Any] = {}
        scope["extensions"] = {EXTENSION_KEY: slot}
        return slot

    @staticmethod
    def _raise_recorded(slot: dict[str, Any] | None) -> None:
        if slot is not None and slot.get("error") is not None:
            raise slot["error"]

    @staticmethod
    def _sent_request(spec: _Spec, sent: list[tuple[str, str]]) -> SentRequest:
        content = None if spec.body.streamed else spec.body.payload
        return SentRequest(spec.method, spec.target.url, sent, content)  # type: ignore[arg-type]

    @property
    def persists_cookies(self) -> bool:
        """Whether ``Set-Cookie`` responses are stored in :attr:`cookies` (see ``persist_cookies=``)."""
        return (not self._compat) if self._persist_cookies is None else self._persist_cookies

    def _store_cookies(self, target: Target, headers: ResponseHeaders) -> None:
        values = headers.getall("set-cookie")
        if values and self.persists_cookies:
            self.cookies.extract(values, scheme=target.scheme, host=target.host, path=target.path)

    def _ws_setup(
        self, path: str, headers: Any, query_string: str
    ) -> tuple[str, str, list[tuple[str, str]], str, tuple[str, int]]:
        target = self._base.target(path)
        scheme = "wss" if target.scheme in ("https", "wss") else "ws"
        explicit = _header_items(headers)
        overridden = {k.lower() for k, _ in explicit}
        merged = [(k, v) for k, v in self.headers.items() if k.lower() not in overridden] + explicit
        names = {k.lower() for k, _ in merged}
        sent: list[tuple[str, str]] = []
        if "host" not in names:
            sent.append(("host", target.netloc))
        sent.extend(merged)
        if "cookie" not in names:
            cookie = self.cookies.header_value(scheme=scheme, host=target.host, path=target.path)
            if cookie:
                sent.append(("cookie", cookie))
        query = query_string or target.query
        return target.path, scheme, sent, query, (target.host, target.port)

    async def _asession_cookies(self, driver: _Driver, data: Mapping[str, Any]) -> list[str]:
        """Sign ``data`` into session cookies through the app's own session engine."""
        await driver.ensure_ready()
        target = driver.lauren_app()
        if target is None:
            raise TypeError("set_session() needs a Lauren app built with sessions=SessionConfig(...)")
        from .._sessions._engine import _Inbound, _SessionEngine
        from ..exceptions import MissingProviderError
        from ..sessions import Session
        from ..types import Response

        try:
            engine = await target.container.resolve(_SessionEngine)
        except MissingProviderError:
            raise RuntimeError(
                "set_session() needs sessions enabled: pass sessions=SessionConfig(...) to LaurenFactory.create"
            ) from None
        session = Session(data={}, id="", is_new=True, new_id_factory=engine.store.new_id)
        session.update(dict(data))
        response = await engine._persist(Response(b""), session, _Inbound())
        return response.headers.getall("set-cookie")

    def _store_session_cookies(self, values: list[str]) -> None:
        self.cookies.extract(values, scheme=self._base.scheme, host=self._base.host, path="/")
        for raw in values:
            name = raw.split("=", 1)[0].strip()
            if name and name not in self.cookies:
                raise RuntimeError(
                    f"the session cookie {name!r} was not stored: it is Secure but the client's base_url "
                    f"{self.base_url!r} is not a secure context (strict_cookies=True needs https://)"
                )

    async def _aresolve(self, driver: _Driver, token: Any) -> Any:
        await driver.ensure_ready()
        target = driver.lauren_app()
        if target is None:
            raise TypeError(f"resolve() needs a Lauren app, got {_describe_app(self.app)}")
        return await target.container.resolve(token)

    def _capture_bus(self, driver: _Driver) -> Any:
        target = driver.lauren_app()
        if target is None:
            raise TypeError("capture() needs a compiled Lauren app (start the client first)")
        return target.signals


@contextlib.contextmanager
def _capture_on(bus: Any, events: tuple[type, ...]) -> Iterator[list[Any]]:
    from ..signals import LifecycleEvent

    types = events or (LifecycleEvent,)
    captured: list[Any] = []

    def _listener(event: Any) -> None:
        captured.append(event)

    for event_type in types:
        bus.on(event_type)(_listener)
    try:
        yield captured
    finally:
        for event_type in types:
            bus.off(event_type, _listener)


# ---------------------------------------------------------------------------
# Sync client
# ---------------------------------------------------------------------------


class _SyncStreamSource:
    """Feeds a :class:`StreamingTestResponse` from an exchange running on the portal loop."""

    def __init__(
        self,
        portal: Portal,
        exchange: Exchange,
        slot: dict[str, Any] | None,
        timeout: float | None,
        what: str,
    ) -> None:
        self._portal = portal
        self._exchange = exchange
        self._slot = slot
        self._timeout = timeout
        self._what = what
        self._finished = False

    @property
    def aborted(self) -> bool:
        return self._exchange.aborted

    def next_chunk(self) -> bytes | None:
        chunk = self._portal.call(lambda: self._exchange.next_chunk(self._timeout, what=self._what))
        if chunk is None:
            self.close()
        return chunk

    def close(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._portal.call(lambda: self._exchange.close(self._timeout, what=self._what))
        _ClientBase._raise_recorded(self._slot)

    async def anext_chunk(self) -> bytes | None:
        raise RuntimeError("this stream belongs to the sync TestClient; use iter_bytes() / iter_sse()")

    async def aclose(self) -> None:
        raise RuntimeError("this stream belongs to the sync TestClient; use close()")


class WebSocketSession:
    """A WebSocket connection opened by :meth:`TestClient.websocket_connect` (sync API).

    Every call runs on the client's event loop; a receive that gets no
    message within ``timeout`` seconds raises :class:`TimeoutError`.
    """

    __test__ = False

    def __init__(self, portal: Portal, session: WebSocketTestSession, timeout: float | None) -> None:
        self._portal = portal
        self._session = session
        self._timeout = timeout

    def _call(self, fn: Callable[[], Awaitable[T]]) -> T:
        timeout = self._timeout

        async def _bounded() -> T:
            return await asyncio.wait_for(fn(), timeout)

        try:
            return self._portal.call(_bounded)
        except asyncio.TimeoutError:
            raise TimeoutError(f"no WebSocket message within {timeout} seconds") from None

    @property
    def accepted(self) -> bool:
        return self._session._accepted

    @property
    def close_code(self) -> int | None:
        return self._session.close_code

    @property
    def close_reason(self) -> str:
        return self._session.close_reason

    @property
    def accepted_subprotocol(self) -> str | None:
        return self._session.accepted_subprotocol

    def send_text(self, text: str) -> None:
        self._call(lambda: self._session.send_text(text))

    def send_bytes(self, data: bytes) -> None:
        self._call(lambda: self._session.send_bytes(data))

    def send_json(self, payload: Any) -> None:
        self._call(lambda: self._session.send_json(payload))

    def send_event(self, event: str, data: Any = None) -> None:
        """Send Lauren's ``{"event": ..., "data": ...}`` envelope."""
        self._call(lambda: self._session.send_event(event, data))

    def receive(self) -> dict[str, Any]:
        return self._call(self._session.receive)

    def receive_text(self) -> str:
        return self._call(self._session.receive_text)

    def receive_bytes(self) -> bytes:
        return self._call(self._session.receive_bytes)

    def receive_json(self) -> Any:
        return self._call(self._session.receive_json)

    def receive_event(self) -> dict[str, Any]:
        """Receive the next JSON frame (Lauren's ``{"event", "data"}`` envelope) as a dict."""
        return self._call(self._session.receive_event)

    def close(self, code: int = 1000) -> None:
        self._call(lambda: self._session.close(code))


class TestClient(_ClientBase):
    """Synchronous in-process client for a :class:`~lauren.LaurenApp`, a ``Lauren`` app or any ASGI app.

    One persistent event loop (a daemon "portal" thread) per client runs the
    app's startup, every request, background tasks, WebSockets and shutdown,
    so loop-bound resources created at startup keep working.

    * ``with TestClient(app) as client:`` runs ``startup()`` on enter and
      ``shutdown()`` (drain → ``on_shutdown`` → ``@pre_destruct`` → generator
      teardown) on exit; a failing hook raises its original exception.
    * ``TestClient(app)`` without ``with`` keeps the 1.x behaviour: a
      ``LaurenApp`` is started right away, a ``Lauren`` app on its first
      request. ``close()`` shuts it down; a client that is never closed
      stops its loop when garbage-collected (without shutdown hooks).
    * Cookies in :attr:`cookies` are sent with every matching request.
      ``Set-Cookie`` responses are stored there when the client is used as a
      context manager or comes from :meth:`new_session`; a bare
      ``TestClient(app)`` keeps the 1.x behaviour of not storing them unless
      ``persist_cookies=True`` (2.0 stores them for every client).

    Creating a second client for an app that a live client already drives
    attaches it to that client's loop (it shares the lifecycle, like
    :meth:`new_session`); entering such a client with ``with`` raises.
    """

    def __init__(
        self,
        app: Any,
        *,
        base_url: str = "http://testserver",
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        follow_redirects: bool = False,
        timeout: float | None = _UNSET,
        raise_server_exceptions: bool = False,
        lifespan: LifespanMode = "auto",
        backend: Literal["asyncio"] = "asyncio",
        backend_options: Mapping[str, Any] | None = None,
        root_path: str = "",
        strict_cookies: bool = False,
        persist_cookies: bool | None = None,
    ) -> None:
        super().__init__(
            app,
            base_url=base_url,
            headers=headers,
            cookies=cookies,
            follow_redirects=follow_redirects,
            timeout=timeout,
            raise_server_exceptions=raise_server_exceptions,
            lifespan=lifespan,
            root_path=root_path,
            strict_cookies=strict_cookies,
            persist_cookies=persist_cookies,
        )
        # 1.x compatibility: a bare ``TestClient(app)`` neither stored cookies
        # nor limited request time; ``with TestClient(app)`` (and
        # ``new_session()``) switch to the new defaults.
        self._compat = True
        loop_factory = _loop_factory(backend, backend_options)
        self._backend_options = dict(backend_options or {})
        self._closed = False
        self._child = False
        existing = _live_driver(app)
        if existing is not None:
            if existing.portal is None:
                raise RuntimeError(
                    f"{_describe_app(app)} is already driven by {existing.label} on another event loop; "
                    "an app's loop-bound resources cannot be shared across loops — build a separate app"
                )
            self._driver = existing
            self._owner = False
            return
        portal = Portal(name=f"lauren-testclient-{next(_portal_ids)}", loop_factory=loop_factory)
        self._driver = _Driver(
            app, portal=portal, loop=None, label=f"TestClient at {id(self):#x}", lifespan=lifespan
        )
        self._owner = True
        if self._driver.kind == "lauren_app" or (lifespan == "on" and self._driver.kind == "asgi"):
            # 1.x behaviour: a LaurenApp is started as soon as the client exists.
            try:
                self._driver.portal.call(  # type: ignore[union-attr]
                    lambda: self._driver.start(
                        foreign_lifespan=False, strict_restart=False, timeout=self.timeout
                    )
                )
            except BaseException:
                self._driver.stop_portal()
                raise

    # -- plumbing ------------------------------------------------------------------

    @property
    def _portal(self) -> Portal:
        portal = self._driver.portal
        assert portal is not None
        return portal

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("this TestClient is closed")
        if not self._driver.alive:
            raise RuntimeError("the client that owns this app's event loop was closed")

    def _call(self, fn: Callable[[], Awaitable[T]]) -> T:
        self._ensure_open()
        return self._portal.call(fn)

    @staticmethod
    def _warn_if_in_running_loop() -> None:
        if _loop_warning["issued"]:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        _loop_warning["issued"] = True
        warnings.warn(
            "the synchronous TestClient was called from a running event loop; the call blocks that loop "
            "until the app answers — use lauren.testing.AsyncTestClient in async tests",
            RuntimeWarning,
            stacklevel=4,
        )

    # -- lifecycle -------------------------------------------------------------------

    def __enter__(self) -> "TestClient":
        self._ensure_open()
        self._compat = False
        if not self._owner and not self._child:
            raise RuntimeError(
                f"{_describe_app(self.app)} is already driven by another TestClient; "
                "use client.new_session() for a second client"
            )
        if self._owner:
            try:
                self._call(
                    lambda: self._driver.start(
                        foreign_lifespan=True, strict_restart=True, timeout=self.timeout
                    )
                )
            except BaseException:
                self._closed = True
                self._driver.stop_portal()
                raise
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        """Shut the app down (owning client only) and stop the event loop.

        Runs drain → ``on_shutdown`` → ``@pre_destruct`` → generator teardown
        (unless ``lifespan="off"``), then cancels tasks the app left running
        — with a :class:`RuntimeWarning` naming them — and stops the portal
        thread. A client from :meth:`new_session` only detaches.
        """
        if self._closed:
            return
        self._closed = True
        if not self._owner:
            return
        driver = self._driver
        if not driver.alive:
            driver.stop_portal()
            return
        try:
            self._portal.call(lambda: driver.shutdown(timeout=self.timeout))
        finally:
            leftovers = driver.stop_portal()
        if leftovers:
            warnings.warn(
                f"TestClient.close() cancelled {len(leftovers)} task(s) still running after shutdown: "
                + ", ".join(sorted(leftovers)),
                RuntimeWarning,
                stacklevel=2,
            )

    def new_session(
        self, *, headers: Mapping[str, str] | None = None, cookies: Mapping[str, str] | None = None
    ) -> "TestClient":
        """A client for the same app and loop with its own (empty) cookie jar and default headers.

        The new client shares this client's lifecycle: this client keeps
        ownership of startup and shutdown.
        """
        self._ensure_open()
        child = TestClient.__new__(TestClient)
        _ClientBase.__init__(
            child,
            self.app,
            base_url=self.base_url,
            headers=headers,
            cookies=cookies,
            follow_redirects=self.follow_redirects,
            timeout=self._timeout_setting,
            raise_server_exceptions=self.raise_server_exceptions,
            lifespan=self._lifespan,
            root_path=self.root_path,
            strict_cookies=self._strict_cookies,
            persist_cookies=self._persist_cookies,
        )
        child._backend_options = self._backend_options
        child._closed = False
        child._child = True
        child._driver = self._driver
        child._owner = False
        return child

    # -- requests --------------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        cookies: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        content: Any = None,
        follow_redirects: bool | None = None,
        timeout: float | None = _UNSET,
        disconnect_after: float | None = None,
    ) -> TestResponse:
        """Send one request and return the buffered :class:`TestResponse`."""
        self._ensure_open()
        self._warn_if_in_running_loop()
        spec = self._build(
            method,
            url,
            params=params,
            headers=headers,
            cookies=cookies,
            json=json,
            data=data,
            files=files,
            content=content,
        )
        follow = self.follow_redirects if follow_redirects is None else follow_redirects
        limit = self.timeout if timeout is _UNSET else timeout
        driver = self._driver
        return self._call(
            lambda: self._asend(
                driver, spec, follow_redirects=follow, timeout=limit, disconnect_after=disconnect_after
            )
        )

    async def arequest(self, method: str, url: str, **kwargs: Any) -> TestResponse:
        """``await``-able :meth:`request`: runs on the client's loop without blocking the caller's."""
        self._ensure_open()
        follow = kwargs.pop("follow_redirects", None)
        timeout = kwargs.pop("timeout", _UNSET)
        disconnect_after = kwargs.pop("disconnect_after", None)
        spec = self._build(
            method,
            url,
            params=kwargs.pop("params", None),
            headers=kwargs.pop("headers", None),
            cookies=kwargs.pop("cookies", None),
            json=kwargs.pop("json", None),
            data=kwargs.pop("data", None),
            files=kwargs.pop("files", None),
            content=kwargs.pop("content", None),
        )
        if kwargs:
            raise TypeError(f"arequest() got unexpected keyword arguments: {sorted(kwargs)}")
        driver = self._driver
        work = lambda: self._asend(  # noqa: E731
            driver,
            spec,
            follow_redirects=self.follow_redirects if follow is None else follow,
            timeout=self.timeout if timeout is _UNSET else timeout,
            disconnect_after=disconnect_after,
        )
        if self._portal.in_loop():
            return await work()
        return await asyncio.wrap_future(self._portal.submit(work))

    def get(self, url: str, **kw: Any) -> TestResponse:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> TestResponse:
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw: Any) -> TestResponse:
        return self.request("PUT", url, **kw)

    def patch(self, url: str, **kw: Any) -> TestResponse:
        return self.request("PATCH", url, **kw)

    def delete(self, url: str, **kw: Any) -> TestResponse:
        return self.request("DELETE", url, **kw)

    def head(self, url: str, **kw: Any) -> TestResponse:
        return self.request("HEAD", url, **kw)

    def options(self, url: str, **kw: Any) -> TestResponse:
        return self.request("OPTIONS", url, **kw)

    @contextlib.contextmanager
    def stream(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        cookies: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        content: Any = None,
        timeout: float | None = _UNSET,
        disconnect_after: float | None = None,
    ) -> Iterator[StreamingTestResponse]:
        """Send a request and read the response incrementally.

        ``timeout`` bounds the wait for the response start and for each body
        chunk, so an endless stream cannot hang the test. Leaving the block
        (or ``response.close()``) delivers ``http.disconnect`` to the app if
        the response is still in flight. Redirects are not followed.
        """
        self._ensure_open()
        spec = self._build(
            method,
            url,
            params=params,
            headers=headers,
            cookies=cookies,
            json=json,
            data=data,
            files=files,
            content=content,
        )
        limit = self.timeout if timeout is _UNSET else timeout
        driver = self._driver
        exchange, slot, sent = self._call(
            lambda: self._aopen_stream(driver, spec, timeout=limit, disconnect_after=disconnect_after)
        )
        source = _SyncStreamSource(self._portal, exchange, slot, limit, f"{spec.method} {spec.target.url}")
        response = self._stream_response(spec, exchange, sent, source)
        try:
            yield response
        except BaseException:
            with contextlib.suppress(Exception):
                response.close()
            raise
        else:
            response.close()

    @contextlib.contextmanager
    def websocket_connect(
        self,
        path: str,
        *,
        headers: Any = None,
        subprotocols: Iterable[str] | None = None,
        query_string: str = "",
        timeout: float | None = 5.0,
    ) -> Iterator[WebSocketSession]:
        """Open a WebSocket on the client's loop; the jar's cookies ride along.

        ``timeout`` bounds every receive and the server's wind-down after the
        block exits.
        """
        self._ensure_open()
        ws_path, scheme, sent, query, server = self._ws_setup(path, headers, query_string)
        driver = self._driver

        async def _open() -> WebSocketTestSession:
            await driver.ensure_ready()
            session = WebSocketTestSession(
                self.app,
                ws_path,
                headers=sent,
                subprotocols=subprotocols,
                query_string=query,
                server_timeout=timeout if timeout is not None else 2.0,
                scheme=scheme,
                server=server,
            )
            return await session.__aenter__()

        session = self._call(_open)
        try:
            yield WebSocketSession(self._portal, session, timeout)
        finally:
            self._call(lambda: session.__aexit__(None, None, None))

    # -- app access --------------------------------------------------------------------

    def resolve(self, token: Any) -> Any:
        """Resolve ``token`` from the app's DI container, on the client's loop."""
        driver = self._driver
        return self._call(lambda: self._aresolve(driver, token))

    @contextlib.contextmanager
    def capture(self, *events: type) -> Iterator[list[Any]]:
        """Collect the lifecycle events (all of them when none are named) emitted inside the block."""
        driver = self._driver
        self._call(driver.ensure_ready)
        with _capture_on(self._capture_bus(driver), events) as captured:
            yield captured

    def set_session(self, data: Mapping[str, Any]) -> None:
        """Store a valid signed session cookie holding ``data``, issued by the app's session engine."""
        driver = self._driver
        self._store_session_cookies(self._call(lambda: self._asession_cookies(driver, data)))


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------


class _AsyncStreamSource:
    def __init__(
        self, exchange: Exchange, slot: dict[str, Any] | None, timeout: float | None, what: str
    ) -> None:
        self._exchange = exchange
        self._slot = slot
        self._timeout = timeout
        self._what = what
        self._finished = False

    @property
    def aborted(self) -> bool:
        return self._exchange.aborted

    def next_chunk(self) -> bytes | None:
        raise RuntimeError("this stream belongs to the AsyncTestClient; use aiter_bytes() / aiter_sse()")

    def close(self) -> None:
        raise RuntimeError("this stream belongs to the AsyncTestClient; use await response.aclose()")

    async def anext_chunk(self) -> bytes | None:
        chunk = await self._exchange.next_chunk(self._timeout, what=self._what)
        if chunk is None:
            await self.aclose()
        return chunk

    async def aclose(self) -> None:
        if self._finished:
            return
        self._finished = True
        await self._exchange.close(self._timeout, what=self._what)
        _ClientBase._raise_recorded(self._slot)


class AsyncTestClient(_ClientBase):
    """The async twin of :class:`TestClient`: same keywords, run on the caller's event loop.

    ``async with AsyncTestClient(app) as client:`` runs startup and shutdown
    in the running loop; every method is awaited (``await client.get(...)``,
    ``async with client.stream(...)``). Used without ``async with``, the app
    starts on the first request and :meth:`aclose` shuts it down. An app
    already driven by a client on a *different* loop raises
    :class:`RuntimeError` — loop affinity is checked, not assumed.
    """

    def __init__(
        self,
        app: Any,
        *,
        base_url: str = "http://testserver",
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        follow_redirects: bool = False,
        timeout: float | None = DEFAULT_TIMEOUT,
        raise_server_exceptions: bool = False,
        lifespan: LifespanMode = "auto",
        root_path: str = "",
        strict_cookies: bool = False,
        persist_cookies: bool | None = None,
    ) -> None:
        super().__init__(
            app,
            base_url=base_url,
            headers=headers,
            cookies=cookies,
            follow_redirects=follow_redirects,
            timeout=timeout,
            raise_server_exceptions=raise_server_exceptions,
            lifespan=lifespan,
            root_path=root_path,
            strict_cookies=strict_cookies,
            persist_cookies=persist_cookies,
        )
        self._driver: _Driver | None = None
        self._owner = False
        self._child = False
        self._closed = False

    async def _attach(self) -> _Driver:
        if self._closed:
            raise RuntimeError("this AsyncTestClient is closed")
        loop = asyncio.get_running_loop()
        if self._driver is not None:
            if self._driver.loop is not loop:
                raise RuntimeError(
                    f"AsyncTestClient at {id(self):#x} started on another event loop; "
                    "use one client per loop (pytest-asyncio: give session-scoped clients loop_scope='session')"
                )
            if not self._driver.alive:
                raise RuntimeError("the client that owns this app's event loop was closed")
            return self._driver
        existing = _live_driver(self.app)
        if existing is not None:
            if existing.portal is not None or existing.loop is not loop:
                raise RuntimeError(
                    f"{_describe_app(self.app)} is already driven by {existing.label} on another event loop; "
                    f"AsyncTestClient at {id(self):#x} cannot share it — build a separate app"
                )
            self._driver = existing
            return existing
        self._driver = _Driver(
            self.app,
            portal=None,
            loop=loop,
            label=f"AsyncTestClient at {id(self):#x}",
            lifespan=self._lifespan,
        )
        self._owner = True
        return self._driver

    async def __aenter__(self) -> "AsyncTestClient":
        driver = await self._attach()
        if not self._owner and not self._child:
            raise RuntimeError(
                f"{_describe_app(self.app)} is already driven by another client; "
                "use client.new_session() for a second client"
            )
        if self._owner:
            await driver.start(foreign_lifespan=True, strict_restart=True, timeout=self.timeout)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Shut the app down (owning client only); later calls are no-ops."""
        if self._closed:
            return
        self._closed = True
        driver = self._driver
        if not self._owner or driver is None or driver.closed:
            return
        driver.closed = True
        try:
            await driver.shutdown(timeout=self.timeout)
        finally:
            driver.unregister()

    def new_session(
        self, *, headers: Mapping[str, str] | None = None, cookies: Mapping[str, str] | None = None
    ) -> "AsyncTestClient":
        """A client for the same app and loop with its own cookie jar; this client keeps the lifecycle."""
        child = AsyncTestClient(
            self.app,
            base_url=self.base_url,
            headers=headers,
            cookies=cookies,
            follow_redirects=self.follow_redirects,
            timeout=self._timeout_setting,
            raise_server_exceptions=self.raise_server_exceptions,
            lifespan=self._lifespan,
            root_path=self.root_path,
            strict_cookies=self._strict_cookies,
            persist_cookies=self._persist_cookies,
        )
        child._driver = self._driver
        child._child = True
        return child

    async def _ready_driver(self) -> _Driver:
        driver = await self._attach()
        if self._owner and not driver.started and driver.kind == "lauren_app":
            await driver.start(foreign_lifespan=False, strict_restart=False, timeout=self.timeout)
        return driver

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        cookies: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        content: Any = None,
        follow_redirects: bool | None = None,
        timeout: float | None = _UNSET,
        disconnect_after: float | None = None,
    ) -> TestResponse:
        """Send one request and return the buffered :class:`TestResponse`."""
        driver = await self._ready_driver()
        spec = self._build(
            method,
            url,
            params=params,
            headers=headers,
            cookies=cookies,
            json=json,
            data=data,
            files=files,
            content=content,
        )
        return await self._asend(
            driver,
            spec,
            follow_redirects=self.follow_redirects if follow_redirects is None else follow_redirects,
            timeout=self.timeout if timeout is _UNSET else timeout,
            disconnect_after=disconnect_after,
        )

    async def get(self, url: str, **kw: Any) -> TestResponse:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> TestResponse:
        return await self.request("POST", url, **kw)

    async def put(self, url: str, **kw: Any) -> TestResponse:
        return await self.request("PUT", url, **kw)

    async def patch(self, url: str, **kw: Any) -> TestResponse:
        return await self.request("PATCH", url, **kw)

    async def delete(self, url: str, **kw: Any) -> TestResponse:
        return await self.request("DELETE", url, **kw)

    async def head(self, url: str, **kw: Any) -> TestResponse:
        return await self.request("HEAD", url, **kw)

    async def options(self, url: str, **kw: Any) -> TestResponse:
        return await self.request("OPTIONS", url, **kw)

    @contextlib.asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        cookies: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        content: Any = None,
        timeout: float | None = _UNSET,
        disconnect_after: float | None = None,
    ) -> AsyncIterator[StreamingTestResponse]:
        """``async with client.stream(...) as response:`` — see :meth:`TestClient.stream`."""
        driver = await self._ready_driver()
        spec = self._build(
            method,
            url,
            params=params,
            headers=headers,
            cookies=cookies,
            json=json,
            data=data,
            files=files,
            content=content,
        )
        limit = self.timeout if timeout is _UNSET else timeout
        exchange, slot, sent = await self._aopen_stream(
            driver, spec, timeout=limit, disconnect_after=disconnect_after
        )
        source = _AsyncStreamSource(exchange, slot, limit, f"{spec.method} {spec.target.url}")
        response = self._stream_response(spec, exchange, sent, source)
        try:
            yield response
        except BaseException:
            exchange.abort()
            with contextlib.suppress(Exception):
                await response.aclose()
            raise
        else:
            await response.aclose()

    @contextlib.asynccontextmanager
    async def websocket_connect(
        self,
        path: str,
        *,
        headers: Any = None,
        subprotocols: Iterable[str] | None = None,
        query_string: str = "",
        timeout: float | None = 5.0,
    ) -> AsyncIterator[WebSocketTestSession]:
        """``async with client.websocket_connect(path) as ws:`` — the jar's cookies ride along."""
        driver = await self._ready_driver()
        await driver.ensure_ready()
        ws_path, scheme, sent, query, server = self._ws_setup(path, headers, query_string)
        session = WebSocketTestSession(
            self.app,
            ws_path,
            headers=sent,
            subprotocols=subprotocols,
            query_string=query,
            server_timeout=timeout if timeout is not None else 2.0,
            scheme=scheme,
            server=server,
        )
        async with session as ws:
            yield ws

    async def resolve(self, token: Any) -> Any:
        """Resolve ``token`` from the app's DI container."""
        return await self._aresolve(await self._ready_driver(), token)

    @contextlib.contextmanager
    def capture(self, *events: type) -> Iterator[list[Any]]:
        """Collect lifecycle events emitted inside the block (the client must have started)."""
        driver = self._driver
        if driver is None:
            target = self.app if _app_kind(self.app) == "lauren_app" else None
            if target is None:
                raise TypeError(
                    "capture() needs a started client: use `async with AsyncTestClient(app)` first"
                )
            bus = target.signals
        else:
            bus = self._capture_bus(driver)
        with _capture_on(bus, events) as captured:
            yield captured

    async def set_session(self, data: Mapping[str, Any]) -> None:
        """Store a valid signed session cookie holding ``data``, issued by the app's session engine."""
        driver = await self._ready_driver()
        self._store_session_cookies(await self._asession_cookies(driver, data))


# ---------------------------------------------------------------------------
# Lifespan helper for third-party clients (httpx.ASGITransport, …)
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(app: Any, *, timeout: float | None = 30.0) -> AsyncIterator[Any]:
    """Run an app's startup on enter and shutdown on exit, in the running loop.

    For tests that keep a third-party client::

        async with lifespan(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                ...

    A ``LaurenApp`` / ``Lauren`` app is started and shut down directly (the
    original exception propagates); any other ASGI app is driven through the
    lifespan protocol. Yields ``app``.
    """
    kind = _app_kind(app)
    foreign: ForeignLifespan | None = None
    if kind == "lauren_app":
        if not app._started:
            await app.startup()
    elif kind == "lauren":
        await app._ensure_started()
    else:
        foreign = ForeignLifespan(app)
        await foreign.start(required=True, timeout=timeout)
    try:
        yield app
    finally:
        if foreign is not None:
            await foreign.shutdown(timeout=timeout)
        else:
            await app.shutdown()


__all__ = ["AsyncTestClient", "TestClient", "WebSocketSession", "lifespan"]
