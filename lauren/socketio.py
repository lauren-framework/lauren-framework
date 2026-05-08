"""Socket.IO protocol adapter for lauren WebSocket controllers.

Lets the official Socket.IO JavaScript / TypeScript / Swift / Kotlin
clients (v4+) talk to a lauren backend without any wire-level protocol
work in user code. The adapter implements the **WebSocket transport**
of Engine.IO v4 + Socket.IO v5, which is what every modern Socket.IO
deployment defaults to.

Quick start
-----------

.. code-block:: python

    from lauren import LaurenFactory, module
    from lauren.socketio import (
        SocketIOConnection,
        on_socketio_event,
        socketio_controller,
    )

    @socketio_controller("/socket.io/")
    class ChatGateway:
        @on_socketio_event("connect")
        async def on_connect(self, conn: SocketIOConnection) -> None:
            await conn.emit("welcome", {"sid": conn.sid})

        @on_socketio_event("chat:message")
        async def on_message(
            self, conn: SocketIOConnection, payload: dict
        ) -> dict:
            # Returning a value sends an ACK back to the sender; the
            # ack id is propagated automatically.
            return {"echo": payload}

        @on_socketio_event("disconnect")
        async def on_disconnect(self, conn: SocketIOConnection) -> None:
            ...

    @module(controllers=[ChatGateway])
    class App:
        pass

    app = LaurenFactory.create(App)

JavaScript client::

    import { io } from "socket.io-client";
    const socket = io("ws://localhost:8000", { transports: ["websocket"] });
    socket.emit("chat:message", {text: "hi"}, (ack) => console.log(ack));

What this module provides
-------------------------

* :func:`socketio_controller` -- decorator that mounts a class as a
  Socket.IO endpoint at the given path. Wraps :func:`@ws_controller
  <lauren.ws_controller>` so the existing WS runtime (DI, lifecycle,
  middleware) is reused.
* :func:`on_socketio_event` -- decorates an instance method with the
  Socket.IO event name it should handle. Two reserved names are
  recognised: ``"connect"`` (fires immediately after the SIO handshake
  finishes) and ``"disconnect"`` (fires when the transport closes).
* :class:`SocketIOConnection` -- a thin per-connection object passed
  to every event handler. Wraps the underlying lauren
  :class:`~lauren.WebSocket` and adds Socket.IO-aware emit helpers.

Out of scope
------------

* HTTP long-polling fallback. The JS client uses WebSocket-only mode
  (``transports: ["websocket"]``) for every modern deployment.
* Binary attachments (``BINARY_EVENT`` / ``BINARY_ACK``). Plain JSON
  is enough for the use cases this adapter targets.
* Multi-namespace routing within one controller. Each
  :func:`socketio_controller` serves the root namespace ``/``;
  parallel controllers at different paths give the same effect.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from . import _socketio as _proto
from .exceptions import DecoratorUsageError, LaurenError
from .websockets import (
    WebSocket,
    WebSocketError,
    on_connect,
    on_disconnect,
    on_message,
    ws_controller,
)


F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Metadata constants -- follow the framework's ``__lauren_*`` convention
# ---------------------------------------------------------------------------


SOCKETIO_CONTROLLER_META = "__lauren_socketio_controller__"
SOCKETIO_EVENT_META = "__lauren_socketio_event__"

#: Reserved event names that map to lifecycle hooks rather than client
#: emits. ``"connect"`` runs after the handshake completes;
#: ``"disconnect"`` runs once when the transport closes (peer- or
#: server-initiated).
RESERVED_EVENT_NAMES = frozenset({"connect", "disconnect"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SocketIOError(LaurenError):
    """Base class for Socket.IO-layer errors.

    Inherits from :class:`~lauren.LaurenError` so it composes with the
    framework's ``detail`` payload + structured-log conventions.
    """


# ---------------------------------------------------------------------------
# Metadata classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SocketIOControllerMeta:
    """Class-level metadata attached by :func:`socketio_controller`.

    Carries the configuration values the runtime needs at handshake
    time. Lives on the class via the ``SOCKETIO_CONTROLLER_META``
    sentinel so subclasses don't accidentally inherit it (per the
    framework rule).
    """

    path: str
    ping_interval_ms: int
    ping_timeout_ms: int
    max_payload_bytes: int


@dataclass(frozen=True, slots=True)
class SocketIOEventMeta:
    """Method-level metadata attached by :func:`on_socketio_event`."""

    event_name: str
    summary: str | None = None


# ---------------------------------------------------------------------------
# SocketIOConnection -- the per-connection facade
# ---------------------------------------------------------------------------


class SocketIOConnection:
    """A live Socket.IO client connection.

    Instances are constructed by the adapter once the Socket.IO
    handshake succeeds and passed to every user event handler. The
    object is intentionally small: it wraps the underlying
    :class:`~lauren.WebSocket` and adds Socket.IO-aware send helpers
    (:meth:`emit`, :meth:`send_ack`, :meth:`disconnect`).

    Lifecycle
    ---------

    * The handshake (Engine.IO OPEN, Socket.IO CONNECT) is completed
      before the connection is exposed to user code.
    * The connection lives until either the peer disconnects, the
      server calls :meth:`disconnect`, or the underlying WebSocket is
      closed.
    * The user's ``on_socketio_event("disconnect")`` handler is
      guaranteed to run once on closure, even if a previous event
      handler raised.

    Concurrency
    -----------

    Outbound emits go through a per-connection ``asyncio.Lock`` to keep
    frames atomic on the wire. Without the lock, two concurrent
    ``conn.emit(...)`` calls could interleave their bytes and confuse
    the JS client.
    """

    __slots__ = (
        "_ws",
        "_sid",
        "_namespace",
        "_send_lock",
        "_closed",
        "_app_state",
    )

    def __init__(
        self,
        ws: WebSocket,
        *,
        sid: str,
        namespace: str = _proto.DEFAULT_NAMESPACE,
    ) -> None:
        self._ws = ws
        self._sid = sid
        self._namespace = namespace
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._app_state = ws.app_state

    # ---- Identity --------------------------------------------------------

    @property
    def sid(self) -> str:
        """Stable session identifier for this connection.

        Generated server-side at handshake time and round-tripped to
        the JS client in the ``CONNECT`` ack. Useful as a key for
        pub/sub registries, chat-room membership, etc.
        """
        return self._sid

    @property
    def namespace(self) -> str:
        """Socket.IO namespace this connection is joined to.

        Always ``"/"`` in this implementation. Exposed as a property so
        future namespace support is non-breaking.
        """
        return self._namespace

    @property
    def websocket(self) -> WebSocket:
        """The underlying lauren :class:`~lauren.WebSocket`.

        Exposed as an escape hatch for callers that need raw frame
        access (custom binary protocols, low-level inspection).
        """
        return self._ws

    @property
    def app_state(self) -> Any:
        """The application state object, identical to ``ws.app_state``.

        Convenience pass-through so handlers don't have to dereference
        through :attr:`websocket` for the common case of reading
        sealed app-level config.
        """
        return self._app_state

    @property
    def connected(self) -> bool:
        """``True`` while the underlying transport is open."""
        return not self._closed and self._ws.connected

    # ---- Outbound emits --------------------------------------------------

    async def emit(self, event: str, *args: Any) -> None:
        """Emit a Socket.IO event to this client.

        ``args`` are sent as positional payload elements, mirroring the
        JS client's ``socket.emit(event, ...args)`` signature. Any
        JSON-able value is acceptable: lauren's permissive default
        handler turns Pydantic models, dataclasses, datetimes, etc.
        into wire-friendly JSON.
        """
        self._ensure_open("emit")
        packet = _proto.SocketIOPacket(
            type=_proto.SIO_EVENT,
            namespace=self._namespace,
            data=[event, *args],
        )
        frame = _proto.encode_message(packet)
        async with self._send_lock:
            await self._ws.send_text(frame)

    async def send_ack(self, ack_id: int, *args: Any) -> None:
        """Send an ACK packet for an event the client previously emitted.

        Most user code doesn't call this directly: returning a value
        from an event handler triggers an automatic ACK reply. Exposed
        for cases where the ack must be sent asynchronously (e.g.
        after kicking off a background task).
        """
        self._ensure_open("send_ack")
        packet = _proto.SocketIOPacket(
            type=_proto.SIO_ACK,
            namespace=self._namespace,
            ack_id=ack_id,
            data=list(args),
        )
        frame = _proto.encode_message(packet)
        async with self._send_lock:
            await self._ws.send_text(frame)

    # ---- Termination -----------------------------------------------------

    async def disconnect(self) -> None:
        """Initiate a graceful Socket.IO + transport closure.

        Sends the Socket.IO ``DISCONNECT`` packet, then the Engine.IO
        ``CLOSE`` packet, then closes the underlying WebSocket. The
        sequence matches what the official Socket.IO server does so
        the JS client surfaces the disconnect cleanly.
        """
        if self._closed:
            return
        self._closed = True
        try:
            async with self._send_lock:
                disconnect_packet = _proto.SocketIOPacket(
                    type=_proto.SIO_DISCONNECT,
                    namespace=self._namespace,
                )
                await self._ws.send_text(_proto.encode_message(disconnect_packet))
                await self._ws.send_text(_proto.EIO_CLOSE)
        except WebSocketError:
            pass
        try:
            await self._ws.close(code=1000)
        except WebSocketError:
            pass

    # ---- Internal --------------------------------------------------------

    def _ensure_open(self, op: str) -> None:
        if self._closed or not self._ws.connected:
            raise SocketIOError(
                f"cannot {op}() on a closed Socket.IO connection",
                detail={"sid": self._sid},
            )


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


def on_socketio_event(
    event: str,
    *,
    summary: str | None = None,
) -> Callable[[F], F]:
    """Mark a method as the handler for a Socket.IO ``event``.

    Two reserved names route to lifecycle hooks instead of client
    emits:

    * ``"connect"`` -- invoked once after the Socket.IO handshake
      completes successfully. Useful for sending a welcome message,
      joining the connection to broadcast groups, or rejecting the
      connection by raising an exception.
    * ``"disconnect"`` -- invoked once when the transport closes
      (peer- or server-initiated). Use this to clean up subscriptions
      or persistent state.

    Every other event name is dispatched on inbound EVENT packets.
    The handler signature determines what's injected:

    * ``self`` -- the controller instance (DI-built).
    * ``conn: SocketIOConnection`` -- the per-connection facade.
    * **Positional payload args** -- the JSON args the client sent,
      in order. ``async def chat(self, conn, payload)`` matches a
      ``socket.emit("chat", {...})`` call from JS.
    * **Return value** -- if the client supplied an ack callback, the
      handler's return value is forwarded as the ack args. Returning
      a tuple sends multiple ack args; returning a single value sends
      one; returning ``None`` sends a single ``null`` arg.

    The decorator follows the framework convention: it attaches
    metadata to the function via ``setattr`` and returns the original
    object unchanged. Subclasses that override the handler without
    re-applying the decorator do NOT inherit the marker.
    """
    if not isinstance(event, str):
        # ``@on_socketio_event`` (without parens) silently misroutes
        # the decoration target into ``event``; reject explicitly.
        raise DecoratorUsageError(
            "@on_socketio_event must be called with an event name "
            '(e.g. @on_socketio_event("chat:message"))'
        )

    def decorator(fn: F) -> F:
        if not (inspect.isfunction(fn) or inspect.iscoroutinefunction(fn)):
            raise DecoratorUsageError(
                f"@on_socketio_event must decorate a method, not a {type(fn).__name__}"
            )
        meta = SocketIOEventMeta(event_name=event, summary=summary)
        setattr(fn, SOCKETIO_EVENT_META, meta)
        return fn

    return decorator


def socketio_controller(
    path: str,
    *,
    ping_interval_ms: int = 25_000,
    ping_timeout_ms: int = 20_000,
    max_payload_bytes: int = 1_000_000,
) -> Callable[[type], type]:
    """Mount a class as a Socket.IO endpoint at ``path``.

    Internally:

    1. Discovers every method marked with :func:`on_socketio_event`.
    2. Synthesizes ``@on_connect`` / ``@on_message`` / ``@on_disconnect``
       hooks on the class that drive the Socket.IO protocol state
       machine, dispatching inbound packets to the user's
       :func:`@on_socketio_event` methods.
    3. Applies :func:`@ws_controller <lauren.ws_controller>` so the
       existing WebSocket runtime (DI, lifecycle, middleware) mounts
       the route exactly like a hand-written WS gateway.

    The synthesised hooks live on the class's own ``__dict__``, never
    on a parent class, so the framework rule "inheritance does NOT
    propagate metadata" is preserved.
    """

    def decorator(cls: type) -> type:
        if not isinstance(cls, type):
            raise DecoratorUsageError(
                f"@socketio_controller must decorate a class, got {type(cls).__name__}"
            )

        # Collect the user's @on_socketio_event-marked methods.
        events: dict[str, Callable[..., Any]] = {}
        for member in cls.__dict__.values():
            meta = getattr(member, SOCKETIO_EVENT_META, None)
            if meta is None:
                continue
            if meta.event_name in events:
                raise DecoratorUsageError(
                    f"Duplicate @on_socketio_event({meta.event_name!r}) "
                    f"on {cls.__name__}"
                )
            events[meta.event_name] = member

        sio_meta = SocketIOControllerMeta(
            path=path,
            ping_interval_ms=ping_interval_ms,
            ping_timeout_ms=ping_timeout_ms,
            max_payload_bytes=max_payload_bytes,
        )
        setattr(cls, SOCKETIO_CONTROLLER_META, sio_meta)

        connect_hook = events.get("connect")
        disconnect_hook = events.get("disconnect")
        event_hooks = {
            name: fn for name, fn in events.items() if name not in RESERVED_EVENT_NAMES
        }

        # @on_connect: complete the Engine.IO + Socket.IO handshake,
        # build a SocketIOConnection, attach it to ws.state, then run
        # the user's "connect" handler if any.
        async def _sio_on_connect(self: Any, ws: WebSocket) -> None:
            # Accept eagerly so we can write the OPEN packet *before*
            # the user's connect handler runs.
            await ws.accept()
            sid = secrets.token_urlsafe(16)
            handshake = _proto.HandshakeConfig(
                sid=sid,
                ping_interval=sio_meta.ping_interval_ms,
                ping_timeout=sio_meta.ping_timeout_ms,
                max_payload=sio_meta.max_payload_bytes,
            )
            await ws.send_text(
                _proto.encode_engineio(
                    _proto.EngineIOPacket(
                        type=_proto.EIO_OPEN,
                        inner=handshake.to_open_payload(),
                    )
                )
            )
            connect_packet = _proto.SocketIOPacket(
                type=_proto.SIO_CONNECT,
                data={"sid": sid},
            )
            await ws.send_text(_proto.encode_message(connect_packet))
            conn = SocketIOConnection(ws, sid=sid)
            ws.state.set("__sio_conn__", conn)
            if connect_hook is not None:
                try:
                    await _invoke_user_event_hook(self, connect_hook, conn, ())
                except Exception as exc:
                    # Reject the connection cleanly with a
                    # CONNECT_ERROR so the JS client's
                    # ``connect_error`` listener fires.
                    err_packet = _proto.SocketIOPacket(
                        type=_proto.SIO_CONNECT_ERROR,
                        data={"message": str(exc)},
                    )
                    try:
                        await ws.send_text(_proto.encode_message(err_packet))
                    finally:
                        await ws.close(code=4000, reason=str(exc)[:120])
                    raise

        setattr(cls, "_sio_on_connect", on_connect(_sio_on_connect))

        # @on_message wildcard: parse engine.io/socket.io packets and
        # dispatch. The handler signature deliberately omits a typed
        # ``body`` parameter -- the framework's signature compiler
        # would otherwise insist on a ``Json[T]`` shape, but Socket.IO
        # frames are not bare JSON (they carry a digit prefix). The
        # raw text reaches us via ``ws.state["__sio_last_text__"]``,
        # which the runtime hook installs at dispatch time.
        @on_message("*")
        async def _sio_on_message(self: Any, ws: WebSocket) -> None:
            raw = ws.state.get("__sio_last_text__")
            if raw is None or not isinstance(raw, str):
                return
            try:
                eio = _proto.decode_engineio(raw)
            except _proto.SocketIOProtocolError:
                # Malformed transport-level frames: log + drop.
                return

            if eio.type == _proto.EIO_PING:
                await ws.send_text(_proto.EIO_PONG)
                return
            if eio.type == _proto.EIO_PONG:
                # Server normally PINGs; accept inbound PONGs silently.
                return
            if eio.type == _proto.EIO_CLOSE:
                await ws.close(code=1000)
                return
            if eio.type != _proto.EIO_MESSAGE:
                return  # OPEN / NOOP / UPGRADE: ignore on server side.

            try:
                pkt = _proto.decode_socketio(eio.inner)
            except _proto.SocketIOProtocolError as exc:
                # Malformed Socket.IO payload: reply with a non-fatal
                # CONNECT_ERROR so the client surfaces the error
                # without dropping the connection.
                err_packet = _proto.SocketIOPacket(
                    type=_proto.SIO_CONNECT_ERROR,
                    data={"message": str(exc)},
                )
                await ws.send_text(_proto.encode_message(err_packet))
                return

            if pkt.type == _proto.SIO_DISCONNECT:
                await ws.close(code=1000)
                return
            if pkt.type == _proto.SIO_CONNECT:
                # Some clients re-send CONNECT after a transport
                # upgrade; tolerate it by re-emitting the ack.
                conn = ws.state.get("__sio_conn__")
                if conn is not None:
                    ack_packet = _proto.SocketIOPacket(
                        type=_proto.SIO_CONNECT,
                        data={"sid": conn.sid},
                    )
                    await ws.send_text(_proto.encode_message(ack_packet))
                return
            if pkt.type == _proto.SIO_EVENT:
                conn = ws.state.get("__sio_conn__")
                if conn is None:
                    return
                if not isinstance(pkt.data, list) or not pkt.data:
                    return
                event_name = pkt.data[0]
                if not isinstance(event_name, str):
                    return
                args = tuple(pkt.data[1:])
                handler = event_hooks.get(event_name)
                if handler is None:
                    # Unknown event: log + drop. Sending an error
                    # frame would surprise the JS client which treats
                    # unknown events as no-ops by design.
                    return
                try:
                    result = await _invoke_user_event_hook(self, handler, conn, args)
                except Exception as exc:
                    if pkt.ack_id is not None:
                        await conn.send_ack(pkt.ack_id, {"error": str(exc)})
                    raise
                # Auto-ack with the handler's return value when the
                # client supplied an ack callback.
                if pkt.ack_id is not None:
                    if isinstance(result, tuple):
                        await conn.send_ack(pkt.ack_id, *result)
                    else:
                        await conn.send_ack(pkt.ack_id, result)
                return
            if pkt.type == _proto.SIO_ACK:
                # Server-side ack receipt: silently drop. We don't
                # currently emit ack-tagged events from the server, so
                # there's no callback to invoke.
                return

        setattr(cls, "_sio_on_message", _sio_on_message)

        # @on_disconnect: invoke the user's handler if any.
        async def _sio_on_disconnect(self: Any, ws: WebSocket) -> None:
            conn = ws.state.get("__sio_conn__")
            if disconnect_hook is None or conn is None:
                return
            await _invoke_user_event_hook(self, disconnect_hook, conn, ())

        setattr(cls, "_sio_on_disconnect", on_disconnect(_sio_on_disconnect))

        # Finally apply @ws_controller so the routing layer mounts us.
        cls = ws_controller(path)(cls)

        return cls

    return decorator


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _invoke_user_event_hook(
    instance: Any,
    fn: Callable[..., Any],
    conn: SocketIOConnection,
    args: tuple[Any, ...],
) -> Any:
    """Invoke a user event handler, awaiting if it returns a coroutine.

    The handler's signature drives argument binding:

    * ``self`` is always the controller instance.
    * The first parameter after ``self`` is ``conn``.
    * Remaining declared parameters are filled positionally from
      ``args``. Excess inbound args are silently dropped (mirrors how
      the JS client tolerates extra event args). Missing ones are
      padded with ``None`` so a JS client emitting
      ``socket.emit("event")`` (no payload) still reaches a handler
      that expects one.
    """
    sig = inspect.signature(fn)
    parameters = list(sig.parameters.values())
    if not parameters:
        result = fn()
    else:
        if parameters[0].name == "self":
            parameters = parameters[1:]
        # The first remaining parameter is conn; drop it from the
        # positional payload count.
        if parameters:
            parameters = parameters[1:]
        positional: list[Any] = list(args[: len(parameters)])
        while len(positional) < len(parameters):
            positional.append(None)
        result = fn(instance, conn, *positional)
    if inspect.isawaitable(result):
        result = await result
    return result


# Hook into the existing WebSocket runtime: when the wildcard
# ``@on_message("*")`` handler is invoked, we need access to the raw
# text frame. The framework's _ws_runtime.py pre-validates the frame
# as JSON before calling the handler, but Socket.IO frames are NOT
# bare JSON (they have a digit prefix). So we install a thin wrapper
# around _dispatch_text: for SIO controllers, stash the raw text on
# ws.state and route through the wildcard handler. Non-Socket.IO
# @ws_controller classes keep the existing behaviour unchanged.


def _install_socketio_runtime_hook() -> None:
    """Install the runtime hook that exposes raw text frames to SIO controllers.

    Idempotent: calling more than once is a no-op.
    """
    from . import _ws_runtime

    if getattr(_ws_runtime, "__sio_hook_installed__", False):
        return

    original_dispatch_text = _ws_runtime._dispatch_text

    async def _patched_dispatch_text(
        gateway: Any,
        run_hook: Callable[..., Awaitable[Any]],
        text: str,
        ws: WebSocket,
    ) -> None:
        controller_cls = gateway.controller_cls
        if SOCKETIO_CONTROLLER_META in controller_cls.__dict__:
            ws.state.set("__sio_last_text__", text)
            wildcard = gateway.messages.get("*")
            if wildcard is None:
                return  # defensive -- shouldn't happen for SIO controllers
            await run_hook(
                wildcard.handler_fn,
                wildcard.extractions,
            )
            return
        await original_dispatch_text(gateway, run_hook, text, ws)

    _ws_runtime._dispatch_text = _patched_dispatch_text
    _ws_runtime.__sio_hook_installed__ = True  # type: ignore[attr-defined]


# Install the hook eagerly at import time so users don't need to
# remember to call it. It's idempotent and cheap (one attribute check).
_install_socketio_runtime_hook()


__all__ = [
    "SocketIOConnection",
    "SocketIOControllerMeta",
    "SocketIOError",
    "SocketIOEventMeta",
    "RESERVED_EVENT_NAMES",
    "on_socketio_event",
    "socketio_controller",
]
