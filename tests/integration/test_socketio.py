"""End-to-end integration tests for the Socket.IO adapter.

These tests drive the full ASGI WebSocket pipeline through
:class:`~lauren.testing.WsTestClient`. The aim is to confirm that the
adapter produces wire-compatible bytes for every step a real
``socket.io-client`` performs:

* Engine.IO ``OPEN`` packet immediately after the handshake.
* Socket.IO ``CONNECT`` ack carrying the sid.
* Inbound ``EVENT`` packets routed to the right user handler.
* Auto-ack from the handler return value.
* PING -> PONG keep-alive.
* Graceful client-initiated DISCONNECT.
* DI-resolved dependencies in handler signatures (controllers
  participate in the DI container like any other gateway).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lauren import (
    LaurenFactory,
    injectable,
    module,
)
from lauren._socketio import (
    EIO_MESSAGE,
    EIO_OPEN,
    EIO_PING,
    EIO_PONG,
    SIO_ACK,
    SIO_CONNECT,
    SIO_CONNECT_ERROR,
    SIO_EVENT,
    decode_engineio,
    decode_socketio,
)
from lauren.socketio import (
    SocketIOConnection,
    on_socketio_event,
    socketio_controller,
)
from lauren.testing import WsTestClient


# ---------------------------------------------------------------------------
# Helper: assert_open_then_connect drains the two server-sent handshake
# frames and returns the negotiated sid.
# ---------------------------------------------------------------------------


async def _drain_handshake(ws) -> str:
    """Receive the OPEN + CONNECT frames, return the negotiated sid.

    Every Socket.IO connection starts with these two frames; isolating
    the consumption logic keeps each test focused on the behaviour it's
    actually asserting against.
    """
    open_frame = await ws.receive_text()
    eio = decode_engineio(open_frame)
    assert eio.type == EIO_OPEN
    handshake = json.loads(eio.inner)
    sid = handshake["sid"]
    assert handshake["pingInterval"] > 0
    assert handshake["pingTimeout"] > 0

    connect_frame = await ws.receive_text()
    eio2 = decode_engineio(connect_frame)
    assert eio2.type == EIO_MESSAGE
    sio_pkt = decode_socketio(eio2.inner)
    assert sio_pkt.type == SIO_CONNECT
    assert sio_pkt.data == {"sid": sid}
    return sid


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class TestHandshake:
    """The Engine.IO + Socket.IO handshake produces the right two frames."""

    def test_open_packet_arrives_with_pinginterval(self):
        @socketio_controller("/sio/")
        class Gw:
            pass

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)

        asyncio.run(run())

    def test_handshake_uses_custom_ping_interval(self):
        @socketio_controller("/sio/", ping_interval_ms=10_000, ping_timeout_ms=5_000)
        class Gw:
            pass

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                open_frame = await ws.receive_text()
                handshake = json.loads(decode_engineio(open_frame).inner)
                assert handshake["pingInterval"] == 10_000
                assert handshake["pingTimeout"] == 5_000
                # Drain the connect packet so the session shuts down
                # cleanly.
                await ws.receive_text()

        asyncio.run(run())

    def test_unique_sid_per_connection(self):
        @socketio_controller("/sio/")
        class Gw:
            pass

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            sids: list[str] = []
            for _ in range(3):
                async with client.connect("/sio/") as ws:
                    sid = await _drain_handshake(ws)
                    sids.append(sid)
            # Sids are random tokens — collisions across three opens
            # are astronomically unlikely.
            assert len(set(sids)) == 3

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Lifecycle hooks (connect / disconnect)
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    """``connect`` and ``disconnect`` are reserved event names."""

    def test_connect_handler_runs_after_handshake(self):
        seen: list[str] = []

        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("connect")
            async def hello(self, conn: SocketIOConnection) -> None:
                seen.append(conn.sid)
                await conn.emit("welcome", {"sid": conn.sid})

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                sid = await _drain_handshake(ws)
                # The welcome emit shows up immediately after the
                # handshake.
                welcome = await ws.receive_text()
                pkt = decode_socketio(decode_engineio(welcome).inner)
                assert pkt.type == SIO_EVENT
                assert pkt.data == ["welcome", {"sid": sid}]
            # Connect handler ran exactly once.
            assert seen == [sid]

        asyncio.run(run())

    def test_disconnect_handler_runs_on_client_disconnect(self):
        farewell_called = asyncio.Event()
        captured_sid: list[str] = []

        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("disconnect")
            async def farewell(self, conn: SocketIOConnection) -> None:
                captured_sid.append(conn.sid)
                farewell_called.set()

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                sid = await _drain_handshake(ws)
                # Send a Socket.IO DISCONNECT (41 = EIO MESSAGE + SIO
                # DISCONNECT). The server should run the disconnect
                # handler, then close.
                await ws.send_text("41")
            await asyncio.wait_for(farewell_called.wait(), timeout=2.0)
            assert captured_sid == [sid]

        asyncio.run(run())

    def test_connect_handler_can_reject_the_connection(self):
        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("connect")
            async def reject(self, conn: SocketIOConnection) -> None:
                raise RuntimeError("not authorized")

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                # OPEN + CONNECT arrive normally.
                await ws.receive_text()  # OPEN
                await ws.receive_text()  # CONNECT
                # Then a CONNECT_ERROR carrying the rejection message.
                err_frame = await ws.receive_text()
                pkt = decode_socketio(decode_engineio(err_frame).inner)
                assert pkt.type == SIO_CONNECT_ERROR
                assert pkt.data == {"message": "not authorized"}

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Event dispatch + ack flow
# ---------------------------------------------------------------------------


class TestEventDispatch:
    """Inbound EVENT packets reach the right ``@on_socketio_event``."""

    def test_event_routes_to_named_handler(self):
        captured: list[dict] = []

        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("chat:msg")
            async def chat(self, conn: SocketIOConnection, payload: dict) -> None:
                captured.append(payload)

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                await ws.send_text('42["chat:msg",{"text":"hello"}]')
                # Round-trip the connection to make sure the handler
                # ran before we close.
                await ws.send_text("41")
            assert captured == [{"text": "hello"}]

        asyncio.run(run())

    def test_unknown_event_is_silently_ignored(self):
        # Unknown events match the JS client semantics: the listener
        # simply doesn't fire. Sending an error frame would be wrong.
        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("known")
            async def known(self, conn: SocketIOConnection, payload: dict) -> None:
                return None

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                # Unknown event — server says nothing.
                await ws.send_text('42["nonexistent",{}]')
                # Now send a known event with an ack so we can confirm
                # the connection still works.
                await ws.send_text('421["known",{"x":1}]')
                ack_frame = await ws.receive_text()
                pkt = decode_socketio(decode_engineio(ack_frame).inner)
                assert pkt.type == SIO_ACK
                assert pkt.ack_id == 1

        asyncio.run(run())

    def test_handler_return_value_becomes_ack(self):
        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("compute")
            async def compute(self, conn: SocketIOConnection, payload: dict) -> dict:
                return {"result": payload["a"] + payload["b"]}

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                # Send EVENT with ack id 7.
                await ws.send_text('427["compute",{"a":2,"b":3}]')
                ack = await ws.receive_text()
                pkt = decode_socketio(decode_engineio(ack).inner)
                assert pkt.type == SIO_ACK
                assert pkt.ack_id == 7
                assert pkt.data == [{"result": 5}]

        asyncio.run(run())

    def test_event_without_ack_id_does_not_send_ack(self):
        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("note")
            async def note(self, conn: SocketIOConnection, payload: dict) -> dict:
                return {"received": True}

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                # No ack id ⇒ fire-and-forget.
                await ws.send_text('42["note",{"x":1}]')
                # Verify nothing comes back — we use a tiny timeout to
                # avoid the test hanging on a missing assertion.
                try:
                    extra = await asyncio.wait_for(ws.receive_text(), timeout=0.2)
                except asyncio.TimeoutError:
                    return  # success: no extra frame
                pytest.fail(f"unexpected frame: {extra!r}")

        asyncio.run(run())

    def test_handler_returning_tuple_sends_multiple_ack_args(self):
        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("multi")
            async def multi(self, conn: SocketIOConnection, payload: dict) -> tuple:
                return ("ok", {"count": 1}, [1, 2, 3])

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                await ws.send_text('421["multi",{}]')
                ack = await ws.receive_text()
                pkt = decode_socketio(decode_engineio(ack).inner)
                assert pkt.type == SIO_ACK
                assert pkt.ack_id == 1
                assert pkt.data == ["ok", {"count": 1}, [1, 2, 3]]

        asyncio.run(run())

    def test_event_with_no_payload_args_passes_none(self):
        captured: list = []

        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("ping")
            async def ping(self, conn: SocketIOConnection, payload) -> None:
                captured.append(payload)

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                # JS client emitting ``socket.emit("ping")`` with no
                # data array element. The handler still has a
                # ``payload`` parameter — we pad it with ``None``.
                await ws.send_text('42["ping"]')
                await ws.send_text("41")
            assert captured == [None]

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Engine.IO heartbeats
# ---------------------------------------------------------------------------


class TestHeartbeats:
    """Engine.IO PING / PONG keep-alive frames."""

    def test_client_ping_gets_server_pong(self):
        @socketio_controller("/sio/")
        class Gw:
            pass

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                # Engine.IO PING is the literal "2".
                await ws.send_text(EIO_PING)
                pong = await ws.receive_text()
                assert pong == EIO_PONG

        asyncio.run(run())


# ---------------------------------------------------------------------------
# DI integration — handlers can take @injectable collaborators.
# ---------------------------------------------------------------------------


class TestSocketIODI:
    """The SIO controller is a regular @ws_controller for DI purposes."""

    def test_injectable_dep_is_resolved_in_event_handler(self):
        @injectable()
        class Greeter:
            def greet(self, who: str) -> str:
                return f"hello {who}"

        @socketio_controller("/sio/")
        class Gw:
            def __init__(self, greeter: Greeter) -> None:
                self._greeter = greeter

            @on_socketio_event("hello")
            async def hello(self, conn: SocketIOConnection, name: str) -> str:
                return self._greeter.greet(name)

        @module(controllers=[Gw], providers=[Greeter])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                await ws.send_text('421["hello","world"]')
                ack = await ws.receive_text()
                pkt = decode_socketio(decode_engineio(ack).inner)
                assert pkt.data == ["hello world"]

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Error frames
# ---------------------------------------------------------------------------


class TestErrorRecovery:
    """Malformed frames produce CONNECT_ERROR but keep the connection alive."""

    def test_malformed_frame_does_not_kill_connection(self):
        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("ok")
            async def ok(self, conn: SocketIOConnection, payload) -> str:
                return "still here"

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                # Malformed Socket.IO payload (broken JSON inside the
                # MESSAGE frame).
                await ws.send_text("42[broken")
                err = await ws.receive_text()
                pkt = decode_socketio(decode_engineio(err).inner)
                assert pkt.type == SIO_CONNECT_ERROR
                # Connection is still live — send a valid event.
                await ws.send_text('421["ok",{}]')
                ack = await ws.receive_text()
                ack_pkt = decode_socketio(decode_engineio(ack).inner)
                assert ack_pkt.type == SIO_ACK
                assert ack_pkt.data == ["still here"]

        asyncio.run(run())

    def test_handler_raising_emits_error_in_ack(self):
        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("crash")
            async def crash(self, conn: SocketIOConnection, payload) -> None:
                raise ValueError("boom")

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                await ws.send_text('421["crash",{}]')
                ack = await ws.receive_text()
                pkt = decode_socketio(decode_engineio(ack).inner)
                # The ack carries a single ``{"error": "boom"}`` arg
                # so the JS client's callback can branch.
                assert pkt.type == SIO_ACK
                assert pkt.data == [{"error": "boom"}]

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Server-initiated emits + concurrent clients
# ---------------------------------------------------------------------------


class TestServerInitiatedEmits:
    def test_server_can_emit_inside_handler(self):
        @socketio_controller("/sio/")
        class Gw:
            @on_socketio_event("trigger")
            async def trigger(self, conn: SocketIOConnection, payload) -> None:
                # Three separate emits — they should arrive in order
                # because of the per-connection lock.
                await conn.emit("a")
                await conn.emit("b", 1)
                await conn.emit("c", "x", "y")

        @module(controllers=[Gw])
        class App:
            pass

        async def run():
            app = LaurenFactory.create(App)
            client = WsTestClient(app)
            async with client.connect("/sio/") as ws:
                await _drain_handshake(ws)
                await ws.send_text('42["trigger",{}]')
                events: list[list] = []
                for _ in range(3):
                    frame = await ws.receive_text()
                    pkt = decode_socketio(decode_engineio(frame).inner)
                    events.append(pkt.data)
                assert events == [
                    ["a"],
                    ["b", 1],
                    ["c", "x", "y"],
                ]

        asyncio.run(run())
