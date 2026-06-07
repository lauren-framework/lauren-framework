"""First-class WebSocket controllers with typed message dispatch.

Lauren extends its metadata-first model to WebSockets so the developer
experience is identical to HTTP: declare a controller class, annotate
methods with protocol-level hooks, and the runtime builds an immutable
dispatch table at startup.

Public surface
--------------

* :func:`ws_controller` — mark a class as a WebSocket gateway mounted at
  a path (supports ``{param}`` segments, same as HTTP).
* :func:`on_connect` — method runs when the handshake completes.
* :func:`on_disconnect` — method runs when the connection closes
  (both server-initiated and client-initiated).
* :func:`on_message` — method dispatches one ``event`` name (or
  variant of a discriminated-union payload).
* :class:`WebSocket` — the live connection object (receive / send /
  close / headers / path_params / state).
* :class:`BroadcastGroup` — room-scoped fan-out provider, resolvable
  via DI like any other :func:`~lauren.injectable` service.

Inheritance model
-----------------

lauren's framework-wide rule holds: decorators **only** attach metadata
to the decorated entity. Subclassing a ``@ws_controller`` does NOT make
the subclass a WebSocket gateway. Method-level markers
(``@on_connect`` / ``@on_message("x")``) are stored on the function
itself, so a subclass that overrides a method *without* re-decorating
it loses the marker. :class:`MetadataInheritanceError` is raised at
startup if a gateway ends up in a module's ``controllers`` list without
its own ``@ws_controller`` decoration.

Wire format
-----------

Each frame is a JSON object with at minimum an ``event`` string; the
remainder of the payload is validated against the handler's declared
Pydantic model (or discriminated-union) via the same
:class:`pydantic.TypeAdapter` path used by :class:`~lauren.Json` and
:class:`~lauren.Stream`. Non-JSON frames are delivered raw to
``on_message("*")`` (binary catch-all) when declared, or dropped with a
structured error sent back to the client.
"""

from __future__ import annotations

import asyncio
import json as _jsonlib
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    TypeVar,
)

from .exceptions import (
    DecoratorUsageError,
    LaurenError,
    MetadataInheritanceError,
    StartupError,
)
from .types import Headers, State

from ._validation import is_pydantic_model as _is_pydantic_model  # noqa: F401


F = TypeVar("F", bound=Callable[..., Any])
C = TypeVar("C", bound=type)


# ---------------------------------------------------------------------------
# Marker attribute names. Consistent with the rest of the framework —
# every decorator attaches a dunder-prefixed attribute that subclasses do
# NOT inherit unless they re-decorate themselves.
# ---------------------------------------------------------------------------


WS_CONTROLLER_META = "__lauren_ws_controller__"
WS_ROUTE_META = "__lauren_ws_route__"
WS_ON_CONNECT = "__lauren_ws_on_connect__"
WS_ON_DISCONNECT = "__lauren_ws_on_disconnect__"
WS_ON_MESSAGE = "__lauren_ws_on_message__"
WS_ON_ERROR = "__lauren_ws_on_error__"


# ---------------------------------------------------------------------------
# WebSocket-specific exceptions. These reuse the structured error envelope
# used by HTTP errors but carry close codes for the wire side.
# ---------------------------------------------------------------------------


class WebSocketError(LaurenError):
    """Base class for WebSocket-layer errors."""

    code = "websocket_error"
    close_code: int = 1011  # "internal error" per RFC 6455

    def __init__(
        self,
        message: str = "",
        *,
        close_code: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        if close_code is not None:
            self.close_code = close_code


class WebSocketDisconnect(WebSocketError):
    """Raised inside a handler when the peer closes the connection.

    Handlers that loop over :meth:`WebSocket.receive_text` or the various
    typed helpers may catch this to release resources; the runtime always
    catches it as the normal end-of-connection signal and runs
    ``@on_disconnect`` hooks.
    """

    code = "websocket_disconnect"
    close_code = 1000  # normal closure


class WebSocketValidationError(WebSocketError):
    """Inbound frame failed validation against a Pydantic model.

    Raised from the typed dispatcher; the runtime catches it, sends a
    structured error frame back to the client, and continues the
    connection (mismatched frames should not terminate the session).
    """

    code = "websocket_validation_error"
    close_code = 1003  # "unsupported data"


class WebSocketRouteNotFoundError(WebSocketError):
    """The handshake path doesn't match any registered ``@ws_controller``."""

    code = "websocket_route_not_found"
    close_code = 1008  # "policy violation"


# ---------------------------------------------------------------------------
# Metadata dataclasses — analogous to ControllerMeta / RouteMeta on the
# HTTP side, kept intentionally minimal so introspection stays cheap.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WsControllerMeta:
    """Metadata attached by :func:`ws_controller`."""

    path: str
    tags: tuple[str, ...] = ()
    summary: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class WsMessageMeta:
    """Metadata attached by :func:`on_message`.

    A single callable may carry multiple :class:`WsMessageMeta` entries —
    the decorator appends rather than overwrites so one method can serve
    several aliases (``@on_message("a")`` and ``@on_message("b")`` on
    the same function is legal).
    """

    event: str
    #: Optional Pydantic model / discriminated union the incoming payload
    #: must validate against. If ``None`` the handler receives the raw
    #: ``dict`` (after JSON decoding) plus the ``event`` string.
    payload_model: Any = None
    summary: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Decorator helpers. Every decorator here attaches metadata to its own
# target and NEVER mutates any base class — subclasses must re-decorate
# explicitly to opt into the marker.
# ---------------------------------------------------------------------------


def _reject_bare_usage(name: str, arg: Any) -> None:
    """Reject ``@ws_controller`` written without parentheses.

    Matches the behaviour of :func:`~lauren.controller` and the rest of
    lauren's configurable decorators so mistakes surface loudly instead
    of silently registering the decorated class as the URL path.
    """
    if isinstance(arg, type) or (callable(arg) and not isinstance(arg, str)):
        raise DecoratorUsageError(
            f"@{name} must be used with parentheses: write "
            f"'@{name}(path)' or '@{name}(path=...)'. The bare form is "
            "rejected because it would silently bind the decorated "
            "object as the URL path argument.",
            detail={
                "decorator": name,
                "target": getattr(arg, "__qualname__", repr(arg)),
            },
        )


def ws_controller(
    path: str = "",
    *,
    tags: list[str] | None = None,
    summary: str | None = None,
    description: str | None = None,
) -> Callable[[C], C]:
    """Mark a class as a WebSocket gateway mounted at ``path``.

    The ``path`` may contain ``{name}`` parameters just like HTTP routes;
    they're parsed out and made available via :attr:`WebSocket.path_params`.

    Per the framework-wide rule, ``@ws_controller`` attaches metadata to
    the decorated class only. Subclasses do **NOT** inherit gateway
    status — they must be re-decorated. This is symmetric with
    :func:`~lauren.controller` and keeps inheritance explicit.

    ``path`` is required in practice; passing an empty string is valid
    and registers the gateway at the module root. The bare form
    ``@ws_controller`` (no parentheses) is rejected with
    :class:`~lauren.exceptions.DecoratorUsageError`.
    """
    _reject_bare_usage("ws_controller", path)

    def decorator(cls: C) -> C:
        meta = WsControllerMeta(
            path=path,
            tags=tuple(tags or []),
            summary=summary,
            description=description,
        )
        # Attach to the class's own __dict__ ONLY — never to a parent.
        setattr(cls, WS_CONTROLLER_META, meta)
        # Gateways double as injectables so they flow through the DI
        # container like HTTP controllers do. Mark as REQUEST-scoped so
        # a new instance is created per connection.
        from ._di import INJECTABLE_META, InjectableMeta
        from .types import Scope

        if INJECTABLE_META not in cls.__dict__:
            setattr(cls, INJECTABLE_META, InjectableMeta(scope=Scope.REQUEST))
        return cls

    return decorator


def on_connect(fn: F) -> F:
    """Mark a method to run after the WebSocket handshake completes.

    The marker lives on the function object itself
    (``fn.__lauren_ws_on_connect__``). A subclass that overrides this
    method without re-applying the decorator will NOT inherit the hook
    — symmetric with how ``@get`` and ``@post`` work on HTTP
    controllers.

    Also accepts :class:`staticmethod` / :class:`classmethod`
    descriptors so users can stack the decorator in either order
    (``@on_connect`` above or below ``@staticmethod``) — the marker
    lands wherever ``setattr`` will accept it.
    """
    if not _is_method_target(fn):
        raise DecoratorUsageError("@on_connect must decorate a method, not a class or non-callable")
    setattr(fn, WS_ON_CONNECT, True)
    return fn


def on_disconnect(fn: F) -> F:
    """Mark a method to run when the connection closes.

    Runs for both peer-initiated and server-initiated closures. The hook
    is best-effort: exceptions raised here are logged but don't affect
    the connection (which is already dead) or the response status of the
    handshake.
    """
    if not _is_method_target(fn):
        raise DecoratorUsageError("@on_disconnect must decorate a method, not a class or non-callable")
    setattr(fn, WS_ON_DISCONNECT, True)
    return fn


def on_message(
    event: str,
    *,
    summary: str | None = None,
    description: str | None = None,
) -> Callable[[F], F]:
    """Route one inbound frame event to this method.

    The handler's signature declares what validated payload it expects::

        @on_message("chat.send")
        async def send(self, ws: WebSocket, body: Json[ChatMessage]) -> None: ...

    During gateway compilation lauren inspects the signature once, picks
    out the ``Json[...]`` / path / query / DI extractors, and builds an
    immutable dispatch plan — request-time dispatch is pure lookup, no
    reflection.

    Multiple ``@on_message(...)`` decorators may stack on the same
    method to handle several event names; each creates its own
    :class:`WsMessageMeta` entry and its own dispatch-table row.

    The wildcard event name ``"*"`` matches any event that has no
    specific handler — useful for a catch-all logger. The special name
    ``"__binary__"`` captures binary frames (``bytes``) rather than
    JSON-decoded text frames.
    """
    if callable(event) and not isinstance(event, str):
        _reject_bare_usage("on_message", event)

    def decorator(fn: F) -> F:
        if not _is_method_target(fn):
            raise DecoratorUsageError("@on_message must decorate a method, not a class or non-callable")
        existing: list[WsMessageMeta] = (
            list(fn.__dict__.get(WS_ON_MESSAGE, []))
            if hasattr(fn, "__dict__")
            else list(getattr(fn, WS_ON_MESSAGE, []))
        )
        existing.append(
            WsMessageMeta(
                event=event,
                summary=summary,
                description=description,
            )
        )
        setattr(fn, WS_ON_MESSAGE, existing)
        return fn

    return decorator


def on_error(fn: F) -> F:
    """Mark a method as the connection's error handler.

    The runtime calls the decorated method with the raised exception
    whenever a per-frame handler throws something other than
    :class:`WebSocketDisconnect`. Returning normally resumes the
    connection; raising closes it. Without ``@on_error`` lauren falls
    back to sending a structured error frame and keeping the connection
    open.
    """
    if not _is_method_target(fn):
        raise DecoratorUsageError("@on_error must decorate a method, not a class or non-callable")
    setattr(fn, WS_ON_ERROR, True)
    return fn


def _is_method_target(fn: Any) -> bool:
    """Return True for values a method-level WS decorator may wrap.

    Accepted shapes: plain functions / coroutine functions, and
    :class:`staticmethod` / :class:`classmethod` descriptors. Classes
    and arbitrary non-callables are rejected so mistakes surface at
    decoration time rather than producing a silently-broken gateway.
    """
    if isinstance(fn, (staticmethod, classmethod)):
        return True
    if isinstance(fn, type):
        return False
    return callable(fn)


# ---------------------------------------------------------------------------
# WebSocket — runtime connection object
# ---------------------------------------------------------------------------


class WebSocket:
    """A live WebSocket connection.

    Instances are constructed by the ASGI runtime and passed to
    ``@on_connect`` / ``@on_message`` / ``@on_disconnect`` handlers that
    declare a ``ws: WebSocket`` parameter. User code never instantiates
    this directly.

    The object is intentionally thin: it wraps the ASGI ``receive`` /
    ``send`` callables and exposes the subset of surface that controller
    authors actually need. Typed frame reception is mediated by the
    dispatcher so handlers normally work with already-validated Pydantic
    models rather than raw frames; the raw :meth:`receive_text` /
    :meth:`receive_bytes` / :meth:`receive_json` helpers remain
    available for advanced use.
    """

    # States that track the ASGI handshake / close lifecycle so the
    # runtime can reject illegal operation orderings loudly rather than
    # letting them deadlock or silently drop frames.
    STATE_CONNECTING = "connecting"
    STATE_OPEN = "open"
    STATE_CLOSED = "closed"

    def __init__(
        self,
        *,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
        path_template: str,
        path_params: dict[str, str],
        app_state: Any = None,
        json_encoder: Any = None,
    ) -> None:
        self._scope = scope
        self._receive = receive
        self._send = send
        self._json_encoder = json_encoder
        self._state_code = self.STATE_CONNECTING
        self._path_template = path_template
        self._path_params = dict(path_params)
        self._headers = Headers(
            [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])]
        )
        self._query_string: bytes = scope.get("query_string", b"") or b""
        self._state = State()
        self._app_state = app_state
        # Subprotocol negotiation. A server can select one of the
        # client-offered protocols during ``accept()``; we track the
        # client list here for introspection.
        self._client_subprotocols: tuple[str, ...] = tuple(scope.get("subprotocols") or ())
        self._selected_subprotocol: str | None = None
        # Close code + reason are filled in by :meth:`close` or by
        # handling of the ``websocket.disconnect`` message.
        self.close_code: int | None = None
        self.close_reason: str = ""

    # -- Introspection ----------------------------------------------------

    @property
    def path(self) -> str:
        return self._scope.get("path", "")

    @property
    def path_template(self) -> str:
        return self._path_template

    @property
    def path_params(self) -> dict[str, str]:
        return self._path_params

    @property
    def headers(self) -> Headers:
        return self._headers

    @property
    def query_string(self) -> bytes:
        return self._query_string

    @property
    def state(self) -> State:
        return self._state

    @property
    def app_state(self) -> Any:
        return self._app_state

    @property
    def client_subprotocols(self) -> tuple[str, ...]:
        return self._client_subprotocols

    @property
    def subprotocol(self) -> str | None:
        return self._selected_subprotocol

    @property
    def connected(self) -> bool:
        return self._state_code == self.STATE_OPEN

    @property
    def connection_state(self) -> str:
        return self._state_code

    # -- Handshake --------------------------------------------------------

    async def accept(
        self,
        *,
        subprotocol: str | None = None,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        """Complete the WebSocket handshake.

        lauren's runtime calls this for you if ``@on_connect`` returns
        normally — controllers therefore only need to call it explicitly
        when they want to reject the connection or negotiate a specific
        subprotocol before any application logic runs.
        """
        if self._state_code != self.STATE_CONNECTING:
            raise WebSocketError(f"cannot accept(): connection is in state {self._state_code!r}")
        msg: dict[str, Any] = {"type": "websocket.accept"}
        if subprotocol is not None:
            msg["subprotocol"] = subprotocol
            self._selected_subprotocol = subprotocol
        if headers:
            msg["headers"] = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]
        await self._send(msg)
        # The first message from the peer after accept is the
        # ``websocket.connect`` frame, which our runtime has already
        # awaited before calling user hooks. Mark the socket open.
        self._state_code = self.STATE_OPEN

    # -- Reception --------------------------------------------------------

    async def receive(self) -> dict[str, Any]:
        """Pull the next raw ASGI message from the peer.

        Handles the ``websocket.disconnect`` message by raising
        :class:`WebSocketDisconnect`, so callers that loop with
        ``while True: await ws.receive()`` automatically terminate on
        peer close.
        """
        if self._state_code == self.STATE_CLOSED:
            raise WebSocketDisconnect(
                "connection is already closed",
                close_code=self.close_code or 1006,
            )
        msg = await self._receive()
        mtype = msg.get("type")
        if mtype == "websocket.disconnect":
            code = msg.get("code", 1005)
            self._state_code = self.STATE_CLOSED
            self.close_code = code
            raise WebSocketDisconnect(f"peer closed: {code}", close_code=code)
        return msg

    async def receive_text(self) -> str:
        """Await the next text frame, returning its string payload."""
        msg = await self.receive()
        if msg.get("type") != "websocket.receive":
            raise WebSocketError(f"unexpected message type {msg.get('type')!r}")
        if "text" not in msg or msg["text"] is None:
            raise WebSocketError(
                "expected text frame, got binary",
                close_code=1003,
            )
        return msg["text"]

    async def receive_bytes(self) -> bytes:
        """Await the next binary frame."""
        msg = await self.receive()
        if msg.get("type") != "websocket.receive":
            raise WebSocketError(f"unexpected message type {msg.get('type')!r}")
        if "bytes" not in msg or msg["bytes"] is None:
            raise WebSocketError(
                "expected binary frame, got text",
                close_code=1003,
            )
        return msg["bytes"]

    async def receive_json(self) -> Any:
        """Await the next text frame and JSON-decode it."""
        text = await self.receive_text()
        try:
            return _jsonlib.loads(text)
        except _jsonlib.JSONDecodeError as e:
            raise WebSocketValidationError(
                f"invalid JSON frame: {e}",
                detail={"fragment": text[:120]},
            ) from e

    # -- Emission ---------------------------------------------------------

    async def send_text(self, data: str) -> None:
        self._ensure_open("send_text")
        await self._send({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        self._ensure_open("send_bytes")
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: Any) -> None:
        """Serialize ``data`` and send it as a text frame.

        Handles Pydantic models (``model_dump(mode="json")``),
        dataclasses, and standard JSON types via a permissive default
        handler so rich domain objects can be sent without manual
        coercion.
        """
        self._ensure_open("send_json")
        payload = _encode_json(data, encoder=self._json_encoder)
        await self._send({"type": "websocket.send", "text": payload})

    # -- Termination ------------------------------------------------------

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Initiate a server-side close.

        Idempotent: calling :meth:`close` after the connection has
        already terminated is a no-op. The close code / reason are
        preserved on the instance for ``@on_disconnect`` hooks.
        """
        if self._state_code == self.STATE_CLOSED:
            return
        self.close_code = code
        self.close_reason = reason
        try:
            await self._send(
                {
                    "type": "websocket.close",
                    "code": code,
                    "reason": reason,
                }
            )
        finally:
            self._state_code = self.STATE_CLOSED

    # -- Internal ---------------------------------------------------------

    def _ensure_open(self, op: str) -> None:
        if self._state_code != self.STATE_OPEN:
            raise WebSocketError(
                f"cannot {op}(): connection is in state {self._state_code!r}",
                close_code=1011,
            )


def _encode_json(data: Any, encoder: Any = None) -> str:
    """JSON-encode ``data`` using the configured encoder.

    Falls back to the process-wide active encoder when *encoder* is ``None``.
    """
    from .serialization import get_active_encoder  # noqa: PLC0415

    _enc = encoder or get_active_encoder()
    return _enc.encode_compact(data).decode("utf-8")


# ---------------------------------------------------------------------------
# BroadcastGroup — room-scoped fan-out primitive
# ---------------------------------------------------------------------------


class BroadcastGroup:
    """A named set of :class:`WebSocket` connections.

    Rooms (chat, presence, realtime dashboards) compose out of
    :class:`BroadcastGroup` instances keyed by opaque group names.
    Because the registry is a plain dict keyed by string, groups are
    auto-created on first reference — no extra ceremony.

    Usage::

        @injectable()
        class BroadcastRegistry(BroadcastGroup):
            pass  # BroadcastGroup is already a subclassable provider

        @ws_controller("/chat/{room_id}")
        class ChatGateway:
            def __init__(self, rooms: BroadcastGroup) -> None:
                self._rooms = rooms

            @on_connect
            async def joined(self, ws: WebSocket) -> None:
                rid = ws.path_params["room_id"]
                await self._rooms.subscribe(rid, ws)

    The default :class:`BroadcastGroup` is safe for single-process
    deployments. Production multi-worker setups should subclass it and
    back it with Redis Pub/Sub (or similar) — the same controller code
    works unchanged because the fan-out surface is just ``subscribe``
    / ``unsubscribe`` / ``broadcast``.
    """

    def __init__(self) -> None:
        # ``_members[name]`` is a set of live WebSocket objects. We use
        # ``set`` (identity-hashed) rather than a list so unsubscribe is
        # O(1) and double-subscribe is naturally idempotent.
        self._members: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    # -- Membership -------------------------------------------------------

    async def subscribe(self, group: str, ws: WebSocket) -> None:
        """Add ``ws`` to ``group``. Idempotent."""
        async with self._lock:
            self._members.setdefault(group, set()).add(ws)

    async def unsubscribe(self, group: str, ws: WebSocket) -> None:
        """Remove ``ws`` from ``group``. Safe to call if not a member."""
        async with self._lock:
            bucket = self._members.get(group)
            if bucket is not None:
                bucket.discard(ws)
                if not bucket:
                    # Free the key so ``groups()`` returns only live rooms.
                    del self._members[group]

    async def unsubscribe_all(self, ws: WebSocket) -> None:
        """Remove ``ws`` from every group it's a member of.

        Called automatically by the runtime on disconnect so leaked
        subscriptions don't accumulate after clients drop off — but
        controllers can also invoke it eagerly during cleanup logic.
        """
        async with self._lock:
            empty: list[str] = []
            for name, bucket in self._members.items():
                bucket.discard(ws)
                if not bucket:
                    empty.append(name)
            for name in empty:
                del self._members[name]

    # -- Fan-out ----------------------------------------------------------

    async def broadcast(
        self,
        group: str,
        message: Any,
        *,
        as_bytes: bool = False,
        exclude: WebSocket | None = None,
    ) -> int:
        """Deliver ``message`` to every subscriber of ``group``.

        Returns the count of frames actually sent — callers can use it
        for basic observability. Dead connections (those whose
        :meth:`WebSocket.send_json` raises) are detected and removed
        from the group automatically so broadcast storms don't repeat
        doomed sends.

        ``exclude`` lets a broadcaster skip echoing the message back to
        the original sender, the common pattern for chat UIs.
        """
        async with self._lock:
            targets = list(self._members.get(group, ()))
        if not targets:
            return 0
        sent = 0
        dead: list[WebSocket] = []
        for ws in targets:
            if ws is exclude:
                continue
            try:
                if as_bytes:
                    if not isinstance(message, (bytes, bytearray)):
                        raise TypeError("as_bytes=True requires bytes or bytearray")
                    await ws.send_bytes(bytes(message))
                elif isinstance(message, str):
                    await ws.send_text(message)
                else:
                    await ws.send_json(message)
                sent += 1
            except Exception:
                # The sender errored — likely a closed socket. Collect
                # for post-loop eviction (so we don't mutate the group
                # while iterating) and continue.
                dead.append(ws)
        if dead:
            async with self._lock:
                bucket = self._members.get(group)
                if bucket is not None:
                    for ws in dead:
                        bucket.discard(ws)
                    if not bucket:
                        del self._members[group]
        return sent

    # -- Introspection ----------------------------------------------------

    def groups(self) -> list[str]:
        return list(self._members.keys())

    def members(self, group: str) -> list[WebSocket]:
        return list(self._members.get(group, ()))

    def member_count(self, group: str) -> int:
        return len(self._members.get(group, ()))


# ---------------------------------------------------------------------------
# Metadata helpers — used by the factory to discover gateways inside a
# compiled module graph.
# ---------------------------------------------------------------------------


def is_ws_controller(cls: type) -> bool:
    """Return True iff ``cls`` has its OWN :class:`WsControllerMeta`.

    Inherited metadata doesn't count — subclasses must re-decorate to
    qualify, matching the framework-wide inheritance rule.
    """
    return WS_CONTROLLER_META in cls.__dict__


def own_ws_controller_meta(cls: type) -> WsControllerMeta:
    """Return the class's OWN :class:`WsControllerMeta` or raise.

    Raises :class:`MetadataInheritanceError` if the class inherits the
    marker from a base but isn't re-decorated (the ambiguous case
    lauren rejects elsewhere for HTTP controllers).
    """
    own = cls.__dict__.get(WS_CONTROLLER_META)
    if own is not None:
        assert isinstance(own, WsControllerMeta)
        return own
    for base in cls.__mro__[1:]:
        if WS_CONTROLLER_META in base.__dict__:
            raise MetadataInheritanceError(
                f"{cls.__name__} inherits @ws_controller metadata from "
                f"{base.__name__} but is not itself decorated with "
                "@ws_controller. Decorate the subclass explicitly to opt "
                "in.",
                detail={
                    "class": cls.__name__,
                    "inherits_from": base.__name__,
                },
            )
    raise StartupError(
        f"{cls.__name__} is not a WebSocket controller (missing @ws_controller)",
        detail={"class": cls.__name__},
    )


def discover_ws_hooks(cls: type) -> dict[str, Any]:
    """Walk a gateway class and collect its connect/message/disconnect hooks.

    Returns a dict with keys ``on_connect``, ``on_disconnect``,
    ``on_error`` (function or None each), ``messages`` (list of
    ``(event, method, WsMessageMeta)`` tuples in deterministic order),
    and ``bindings`` (dict mapping each discovered hook to its binding
    style — one of ``"instance"``, ``"classmethod"``, ``"static"``).

    Walks the class's own ``__dict__`` (and then up the MRO) so
    ``@staticmethod`` / ``@classmethod`` descriptors aren't unwrapped
    before we can read their markers. Overrides in subclasses correctly
    shadow base-class definitions because the walk picks up the first
    definition it encounters; a subclass that overrides a hook without
    re-applying the decorator therefore drops the marker — consistent
    with the HTTP route rule.
    """
    from ._asgi import _unwrap_handler_descriptor  # local import: avoid cycle

    on_connect_fn: Any | None = None
    on_disconnect_fn: Any | None = None
    on_error_fn: Any | None = None
    messages: list[tuple[str, Any, WsMessageMeta]] = []
    bindings: dict[Any, str] = {}
    raw_descriptors: dict[Any, Any] = {}
    # Values: (unwrapped_fn, binding_tag, raw_descriptor)
    resolved: dict[str, tuple[Any, str, Any]] = {}
    for klass in cls.__mro__:
        for attr_name, raw in klass.__dict__.items():
            if attr_name in resolved:
                continue
            fn, binding = _unwrap_handler_descriptor(raw)
            if fn is None:
                continue
            resolved[attr_name] = (fn, binding, raw)
    for _name, (fn, binding, raw) in resolved.items():
        if getattr(fn, WS_ON_CONNECT, False):
            on_connect_fn = fn
            bindings[fn] = binding
            raw_descriptors[fn] = raw
        if getattr(fn, WS_ON_DISCONNECT, False):
            on_disconnect_fn = fn
            bindings[fn] = binding
            raw_descriptors[fn] = raw
        if getattr(fn, WS_ON_ERROR, False):
            on_error_fn = fn
            bindings[fn] = binding
            raw_descriptors[fn] = raw
        metas: list[WsMessageMeta] = list(getattr(fn, WS_ON_MESSAGE, []) or [])
        if metas:
            bindings[fn] = binding
            raw_descriptors[fn] = raw
            for meta in metas:
                messages.append((meta.event, fn, meta))
    # Ordering: use the class's source definition order where available.
    # ``dir()`` sorts alphabetically; for deterministic routing we sort
    # by (event name, function qualname) to break ties reproducibly.
    messages.sort(key=lambda t: (t[0], getattr(t[1], "__qualname__", "")))
    return {
        "on_connect": on_connect_fn,
        "on_disconnect": on_disconnect_fn,
        "on_error": on_error_fn,
        "messages": messages,
        "bindings": bindings,
        "raw_descriptors": raw_descriptors,
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


__all__ = [
    # Decorators
    "ws_controller",
    "on_connect",
    "on_disconnect",
    "on_message",
    "on_error",
    # Runtime
    "WebSocket",
    "BroadcastGroup",
    # Errors
    "WebSocketError",
    "WebSocketDisconnect",
    "WebSocketValidationError",
    "WebSocketRouteNotFoundError",
    # Metadata
    "WsControllerMeta",
    "WsMessageMeta",
    "WS_CONTROLLER_META",
    "WS_ROUTE_META",
    "WS_ON_CONNECT",
    "WS_ON_DISCONNECT",
    "WS_ON_MESSAGE",
    "WS_ON_ERROR",
    # Metadata helpers
    "is_ws_controller",
    "own_ws_controller_meta",
    "discover_ws_hooks",
]
