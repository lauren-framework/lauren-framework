"""Unit tests for the Socket.IO decorators and connection facade.

These tests pin down behaviour that doesn't need a transport:

* :func:`on_socketio_event` validates its arguments and attaches the
  expected metadata sentinel.
* :func:`socketio_controller` synthesises the right WebSocket hooks on
  the decorated class and refuses obviously-broken usage.
* :class:`SocketIOConnection` enforces the closed-connection invariant.

Full end-to-end behaviour (handshake, dispatch, ack flow) is covered
in ``tests/integration/test_socketio.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from lauren.exceptions import DecoratorUsageError
from lauren.socketio import (
    RESERVED_EVENT_NAMES,
    SOCKETIO_CONTROLLER_META,
    SOCKETIO_EVENT_META,
    SocketIOConnection,
    SocketIOControllerMeta,
    SocketIOError,
    SocketIOEventMeta,
    on_socketio_event,
    socketio_controller,
)
from lauren.websockets import (
    WS_CONTROLLER_META,
    WS_ON_CONNECT,
    WS_ON_DISCONNECT,
    WS_ON_MESSAGE,
)


# ---------------------------------------------------------------------------
# @on_socketio_event behaviour
# ---------------------------------------------------------------------------


class TestOnSocketioEventDecorator:
    """The marker decorator should be a thin metadata-attacher."""

    def test_attaches_metadata_to_method(self):
        @on_socketio_event("chat:message")
        async def handler(self, conn):
            return None

        meta = getattr(handler, SOCKETIO_EVENT_META, None)
        assert isinstance(meta, SocketIOEventMeta)
        assert meta.event_name == "chat:message"

    def test_returns_original_function_unchanged(self):
        async def handler(self, conn):
            return "x"

        decorated = on_socketio_event("evt")(handler)
        # Same function object: framework rule says decorators NEVER
        # rewrap.
        assert decorated is handler

    def test_summary_metadata_carried_through(self):
        @on_socketio_event("evt", summary="A test event")
        async def handler(self, conn):
            return None

        assert handler.__lauren_socketio_event__.summary == "A test event"

    def test_bare_usage_without_parens_is_rejected(self):
        # ``@on_socketio_event`` (no parens) silently passes the
        # decorated function as ``event``; reject explicitly so the
        # mistake fails loudly.
        async def handler(self, conn):
            return None

        with pytest.raises(DecoratorUsageError, match="must be called"):
            on_socketio_event(handler)  # type: ignore[arg-type]

    def test_decorating_a_class_is_rejected(self):
        with pytest.raises(DecoratorUsageError, match="must decorate a method"):

            @on_socketio_event("evt")
            class NotAFunction:
                pass

    def test_two_events_can_be_distinguished(self):
        # The metadata is unique per method, never per event name.
        @on_socketio_event("a")
        async def ha(self, conn):
            return None

        @on_socketio_event("b")
        async def hb(self, conn):
            return None

        assert ha.__lauren_socketio_event__.event_name == "a"
        assert hb.__lauren_socketio_event__.event_name == "b"


# ---------------------------------------------------------------------------
# @socketio_controller class augmentation
# ---------------------------------------------------------------------------


class TestSocketioControllerDecorator:
    """The class decorator wires up WS hooks + metadata."""

    def test_attaches_controller_metadata(self):
        @socketio_controller("/sio/")
        class Gw:
            pass

        meta = getattr(Gw, SOCKETIO_CONTROLLER_META, None)
        assert isinstance(meta, SocketIOControllerMeta)
        assert meta.path == "/sio/"
        assert meta.ping_interval_ms == 25_000
        assert meta.ping_timeout_ms == 20_000
        assert meta.max_payload_bytes == 1_000_000

    def test_custom_handshake_options_propagate(self):
        @socketio_controller(
            "/sio/",
            ping_interval_ms=10_000,
            ping_timeout_ms=5_000,
            max_payload_bytes=64_000,
        )
        class Gw:
            pass

        meta = Gw.__lauren_socketio_controller__
        assert meta.ping_interval_ms == 10_000
        assert meta.ping_timeout_ms == 5_000
        assert meta.max_payload_bytes == 64_000

    def test_also_marks_as_ws_controller(self):
        # The adapter layers itself on top of @ws_controller; without
        # that, the existing WS runtime would never see the gateway.
        @socketio_controller("/sio/")
        class Gw:
            pass

        assert WS_CONTROLLER_META in Gw.__dict__

    def test_synthesizes_on_connect_on_message_on_disconnect(self):
        @socketio_controller("/sio/")
        class Gw:
            pass

        # The synthetic methods are class-level attributes carrying the
        # framework's WS markers. They fire for every Socket.IO
        # connection regardless of whether the user declared a
        # ``connect`` / ``disconnect`` event.
        connect_method = Gw.__dict__.get("_sio_on_connect")
        message_method = Gw.__dict__.get("_sio_on_message")
        disconnect_method = Gw.__dict__.get("_sio_on_disconnect")
        assert connect_method is not None
        assert message_method is not None
        assert disconnect_method is not None
        assert getattr(connect_method, WS_ON_CONNECT, False)
        assert getattr(message_method, WS_ON_MESSAGE, [])
        assert getattr(disconnect_method, WS_ON_DISCONNECT, False)

    def test_collects_user_event_handlers(self):
        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("chat")
            async def chat(self, conn, payload):
                return None

            @on_socketio_event("ping")
            async def ping(self, conn):
                return None

        # The user methods are still on the class and still carry
        # their event metadata. They're not removed or wrapped.
        assert hasattr(Gw, "chat")
        assert hasattr(Gw, "ping")
        assert Gw.chat.__lauren_socketio_event__.event_name == "chat"
        assert Gw.ping.__lauren_socketio_event__.event_name == "ping"

    def test_duplicate_event_handlers_rejected(self):
        with pytest.raises(DecoratorUsageError, match="Duplicate"):

            @socketio_controller("/sio/")
            class Gw:
                @on_socketio_event("chat")
                async def first(self, conn):
                    return None

                @on_socketio_event("chat")
                async def second(self, conn):
                    return None

    def test_decorating_a_non_class_is_rejected(self):
        with pytest.raises(DecoratorUsageError, match="must decorate a class"):
            socketio_controller("/sio/")(lambda: None)  # type: ignore[arg-type]

    def test_inheritance_does_not_propagate_metadata(self):
        # Per the framework rule, subclasses re-decorate or lose the
        # marker. The parent's class-level attribute must NOT count
        # for ``cls.__dict__`` lookups on the subclass.
        @socketio_controller("/sio/")
        class Parent:
            pass

        class Child(Parent):
            pass

        assert SOCKETIO_CONTROLLER_META in Parent.__dict__
        assert SOCKETIO_CONTROLLER_META not in Child.__dict__


# ---------------------------------------------------------------------------
# RESERVED_EVENT_NAMES
# ---------------------------------------------------------------------------


class TestReservedEventNames:
    """Connect / disconnect are framework-reserved event names."""

    def test_reserved_set_is_exactly_connect_and_disconnect(self):
        # Other names ("error", "open", "close") could be reserved
        # later; the test is a regression guard against accidental
        # widening that would silently change user-visible semantics.
        assert RESERVED_EVENT_NAMES == frozenset({"connect", "disconnect"})


# ---------------------------------------------------------------------------
# SocketIOConnection
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal stand-in for :class:`lauren.WebSocket` used in unit tests.

    Captures every outbound text frame on ``self.frames`` so the test
    can inspect the wire format without a real transport. The
    ``connected`` flag mirrors the production type's API.
    """

    def __init__(self) -> None:
        self.frames: list[str] = []
        self.closed: bool = False
        self.connected: bool = True
        self.app_state = None

    async def send_text(self, data: str) -> None:
        self.frames.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.connected = False


@pytest.mark.asyncio
class TestSocketIOConnection:
    """The per-connection facade is a thin wrapper but has clear rules."""

    async def test_emit_writes_event_packet(self):
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="x")
        await conn.emit("chat", {"text": "hi"})
        # Wire form: 4 (EIO MESSAGE) + 2 (SIO EVENT) + JSON array.
        assert ws.frames == ['42["chat",{"text":"hi"}]']

    async def test_emit_with_multiple_args(self):
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="x")
        await conn.emit("evt", "a", 1, {"k": "v"})
        assert ws.frames == ['42["evt","a",1,{"k":"v"}]']

    async def test_send_ack_writes_ack_packet(self):
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="x")
        await conn.send_ack(7, {"ok": True})
        assert ws.frames == ['437[{"ok":true}]']

    async def test_disconnect_writes_disconnect_then_close(self):
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="x")
        await conn.disconnect()
        # First the SIO DISCONNECT, then the EIO CLOSE, then the
        # underlying WS close call.
        assert ws.frames == ["41", "1"]
        assert ws.closed is True

    async def test_disconnect_is_idempotent(self):
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="x")
        await conn.disconnect()
        # A second call must NOT re-send the DISCONNECT packet — that
        # would trip a runtime error inside the JS client.
        await conn.disconnect()
        assert ws.frames == ["41", "1"]

    async def test_emit_after_disconnect_raises(self):
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="x")
        await conn.disconnect()
        with pytest.raises(SocketIOError, match="closed"):
            await conn.emit("late")

    async def test_send_ack_after_disconnect_raises(self):
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="x")
        await conn.disconnect()
        with pytest.raises(SocketIOError, match="closed"):
            await conn.send_ack(1, {"ok": False})

    async def test_sid_is_exposed(self):
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="abc123")
        assert conn.sid == "abc123"

    async def test_connected_reflects_underlying_ws(self):
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="x")
        assert conn.connected is True
        ws.connected = False
        assert conn.connected is False

    async def test_app_state_is_passed_through(self):
        ws = _FakeWebSocket()
        ws.app_state = {"db": object()}
        conn = SocketIOConnection(ws, sid="x")
        assert conn.app_state is ws.app_state

    async def test_namespace_default_is_root(self):
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="x")
        assert conn.namespace == "/"

    async def test_concurrent_emit_calls_serialize(self):
        # The internal lock keeps emits from interleaving on the wire.
        # We exercise it by firing many emits concurrently and
        # asserting every frame arrived intact.
        ws = _FakeWebSocket()
        conn = SocketIOConnection(ws, sid="x")
        await asyncio.gather(*(conn.emit(f"evt{i}") for i in range(10)))
        # Every emit produced exactly one frame, none corrupted.
        assert len(ws.frames) == 10
        for i, frame in enumerate(ws.frames):
            # Order isn't guaranteed under asyncio.gather, but each
            # frame must be a complete EVENT packet.
            assert frame.startswith("42[")
