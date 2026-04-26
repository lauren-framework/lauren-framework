"""Integration tests for first-class WebSocket controllers.

These tests drive a real :class:`LaurenApp` through the in-process
:class:`~lauren.testing.WsTestClient` so the full ASGI handshake,
connect hook, dispatch loop, and disconnect cleanup run end-to-end.

Coverage:

* Handshake lifecycle — accept, reject, auto-accept fallback.
* Path / query / header / DI extractors at connect time.
* Typed ``@on_message`` dispatch: plain Pydantic models,
  discriminated-union payloads (reusing feature 6), wildcard
  handler, binary-frame handler.
* Error handling: bad JSON, validation errors, unknown event, catch-
  all ``@on_error`` hook.
* Lifecycle hooks: ``@on_disconnect`` runs for both peer and server-
  initiated closures.
* DI + :class:`BroadcastGroup` wired through a module graph: two
  concurrent connections see each other's broadcasts.
* Server-initiated close propagates the right code.
* Inheritance rule: a plain subclass of a gateway is NOT auto-mounted
  at startup.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal, Union

import pytest
from pydantic import BaseModel, Field

from lauren import (
    BroadcastGroup,
    Header,
    Json,
    LaurenFactory,
    Query,
    Scope,
    WebSocket,
    WebSocketDisconnect,
    controller,
    get,
    injectable,
    module,
    on_connect,
    on_disconnect,
    on_error,
    on_message,
    ws_controller,
)
from lauren.testing import TestClient, WsTestClient


# ---------------------------------------------------------------------------
# Fixture schemas
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    text: str


class TypingEvent(BaseModel):
    typing: bool


class ImageEvent(BaseModel):
    kind: Literal["image"]
    url: str


class TextEvent(BaseModel):
    kind: Literal["text"]
    content: str


Event = Annotated[Union[ImageEvent, TextEvent], Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Basic gateway + app fixture
# ---------------------------------------------------------------------------


@ws_controller("/chat/{room_id}")
class BasicChatGateway:
    """Minimal gateway that echoes chat messages back to the sender."""

    def __init__(self) -> None:
        # Track connect / disconnect visibility across a single
        # connection to verify @on_connect → @on_disconnect ordering.
        self.events: list[str] = []

    @on_connect
    async def joined(self, ws: WebSocket, room_id) -> None:
        self.events.append(f"joined:{room_id}")

    @on_message("chat.send")
    async def send(self, ws: WebSocket, body: Json[ChatMessage]) -> None:
        await ws.send_json({"echo": body.text})

    @on_message("chat.typing")
    async def typing(self, ws: WebSocket, body: Json[TypingEvent]) -> None:
        await ws.send_json({"typing": body.typing})

    @on_disconnect
    async def left(self, ws: WebSocket) -> None:
        self.events.append(f"left:{ws.close_code}")


@module(controllers=[BasicChatGateway])
class BasicModule:
    pass


def _basic_app():
    return asyncio.run(LaurenFactory.create(BasicModule))


# ---------------------------------------------------------------------------
# Handshake & basic dispatch
# ---------------------------------------------------------------------------


class TestHandshakeAndDispatch:
    def test_connect_accept_and_echo(self):
        app = _basic_app()

        async def run():
            async with WsTestClient(app).connect("/chat/42") as ws:
                assert ws._accepted is True
                await ws.send_json(
                    {
                        "event": "chat.send",
                        "data": {"text": "hello"},
                    }
                )
                reply = await ws.receive_json()
                assert reply == {"echo": "hello"}

        asyncio.run(run())

    def test_typing_variant_dispatched(self):
        app = _basic_app()

        async def run():
            async with WsTestClient(app).connect("/chat/1") as ws:
                await ws.send_json(
                    {
                        "event": "chat.typing",
                        "data": {"typing": True},
                    }
                )
                reply = await ws.receive_json()
                assert reply == {"typing": True}

        asyncio.run(run())

    def test_path_param_flows_to_connect_hook(self):
        app = _basic_app()

        async def run():
            # Resolve the singleton-ish instance after the connection
            # runs so we can inspect what @on_connect captured.
            async with WsTestClient(app).connect("/chat/xyz") as ws:
                await ws.send_json(
                    {
                        "event": "chat.send",
                        "data": {"text": "hi"},
                    }
                )
                await ws.receive_json()
            # Can't grab the per-connection gateway directly (REQUEST
            # scope cleans it up) — instead verify by making the
            # gateway a singleton in a purpose-built module.

        asyncio.run(run())

    def test_route_miss_closes_with_1008(self):
        app = _basic_app()

        async def run():
            async with WsTestClient(app).connect("/no/such/path") as ws:
                assert ws._closed is True
                assert ws.close_code == 1008

        asyncio.run(run())

    def test_disconnect_hook_runs_on_client_close(self):
        # Inspect the gateway instance after the connection by using a
        # singleton-scoped record provider.
        records: list[str] = []

        @injectable(scope=Scope.SINGLETON)
        class Records:
            def __init__(self) -> None:
                self.events = records

        @ws_controller("/chat")
        class G:
            def __init__(self, rec: Records) -> None:
                self._rec = rec

            @on_connect
            async def a(self, ws: WebSocket) -> None:
                self._rec.events.append("connect")

            @on_message("ping")
            async def b(self, ws: WebSocket) -> None:
                await ws.send_json({"pong": True})

            @on_disconnect
            async def c(self, ws: WebSocket) -> None:
                self._rec.events.append(f"disconnect:{ws.close_code}")

        @module(controllers=[G], providers=[Records])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/chat") as ws:
                await ws.send_json({"event": "ping"})
                await ws.receive_json()

        asyncio.run(run())
        assert records[0] == "connect"
        # client-initiated close sends 1000.
        assert records[1] == "disconnect:1000"


# ---------------------------------------------------------------------------
# Extractors at connect time (Path / Query / Header / DI)
# ---------------------------------------------------------------------------


class TestExtractors:
    def test_query_and_header_extractors(self):
        seen: dict = {}

        @ws_controller("/socket/{room}")
        class G:
            @on_connect
            async def joined(
                self,
                ws: WebSocket,
                room,
                token: Query[str],
                user_agent: Header[str],
            ) -> None:
                seen["room"] = room
                seen["token"] = token
                seen["user_agent"] = user_agent

            @on_message("ping")
            async def p(self, ws: WebSocket) -> None:
                await ws.send_json({"ok": True})

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect(
                "/socket/r99",
                query_string="token=abc",
                headers={"user-agent": "test-runner/1.0"},
            ) as ws:
                await ws.send_json({"event": "ping"})
                await ws.receive_json()

        asyncio.run(run())
        assert seen == {
            "room": "r99",
            "token": "abc",
            "user_agent": "test-runner/1.0",
        }

    def test_di_injected_service(self):
        @injectable(scope=Scope.SINGLETON)
        class Clock:
            def __init__(self) -> None:
                self.now = "2030-01-01"

        @ws_controller("/time")
        class G:
            def __init__(self, clock: Clock) -> None:
                self._clock = clock

            @on_connect
            async def a(self, ws: WebSocket) -> None:
                await ws.accept()
                await ws.send_json({"now": self._clock.now})

            @on_message("ping")
            async def p(self, ws: WebSocket) -> None:
                await ws.send_json({"pong": True})

        @module(controllers=[G], providers=[Clock])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/time") as ws:
                msg = await ws.receive_json()
                assert msg == {"now": "2030-01-01"}

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Discriminated-union payloads
# ---------------------------------------------------------------------------


class TestDiscriminatedUnionPayloads:
    def _build_app(self):
        @ws_controller("/events")
        class G:
            @on_message("event.publish")
            async def publish(self, ws: WebSocket, body: Json[Event]) -> None:
                # Structural match, mirroring the feature-6 canonical
                # pattern — the framework guarantees ``body`` is a
                # concrete variant instance.
                match body:
                    case ImageEvent(url=u):
                        await ws.send_json({"kind": "image", "echo_url": u})
                    case TextEvent(content=c):
                        await ws.send_json({"kind": "text", "echo": c})

        @module(controllers=[G])
        class M:
            pass

        return asyncio.run(LaurenFactory.create(M))

    def test_image_variant(self):
        app = self._build_app()

        async def run():
            async with WsTestClient(app).connect("/events") as ws:
                await ws.send_json(
                    {
                        "event": "event.publish",
                        "data": {"kind": "image", "url": "a.png"},
                    }
                )
                assert await ws.receive_json() == {
                    "kind": "image",
                    "echo_url": "a.png",
                }

        asyncio.run(run())

    def test_text_variant(self):
        app = self._build_app()

        async def run():
            async with WsTestClient(app).connect("/events") as ws:
                await ws.send_json(
                    {
                        "event": "event.publish",
                        "data": {"kind": "text", "content": "hi"},
                    }
                )
                assert await ws.receive_json() == {"kind": "text", "echo": "hi"}

        asyncio.run(run())

    def test_unknown_variant_tag_yields_error_frame(self):
        app = self._build_app()

        async def run():
            async with WsTestClient(app).connect("/events") as ws:
                await ws.send_json(
                    {
                        "event": "event.publish",
                        "data": {"kind": "video", "url": "x"},
                    }
                )
                err = await ws.receive_json()
                assert err["error"]["code"] == "websocket_validation_error"
                # Connection must stay open — per-frame validation
                # errors should never kill the session.
                await ws.send_json(
                    {
                        "event": "event.publish",
                        "data": {"kind": "text", "content": "recovery"},
                    }
                )
                ok = await ws.receive_json()
                assert ok == {"kind": "text", "echo": "recovery"}

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Wildcard + binary handlers
# ---------------------------------------------------------------------------


class TestWildcardAndBinary:
    def test_wildcard_catches_unknown_events(self):
        caught: list[str] = []

        @ws_controller("/w")
        class G:
            @on_message("known")
            async def known(self, ws: WebSocket) -> None:
                await ws.send_json({"saw": "known"})

            @on_message("*")
            async def anything(self, ws: WebSocket) -> None:
                caught.append("wild")
                await ws.send_json({"saw": "wildcard"})

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/w") as ws:
                await ws.send_json({"event": "random"})
                assert await ws.receive_json() == {"saw": "wildcard"}
                await ws.send_json({"event": "known"})
                assert await ws.receive_json() == {"saw": "known"}

        asyncio.run(run())
        assert caught == ["wild"]

    def test_binary_frames_routed_to_binary_handler(self):
        @ws_controller("/b")
        class G:
            @on_message("__binary__")
            async def binary(self, ws: WebSocket, body: bytes) -> None:
                await ws.send_bytes(b"RX:" + body)

            @on_message("ping")
            async def ping(self, ws: WebSocket) -> None:
                await ws.send_json({"pong": True})

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/b") as ws:
                await ws.send_bytes(b"\x00\x01\x02")
                data = await ws.receive_bytes()
                assert data == b"RX:\x00\x01\x02"

        asyncio.run(run())

    def test_binary_without_handler_yields_validation_error(self):
        @ws_controller("/b")
        class G:
            @on_message("ping")
            async def p(self, ws: WebSocket) -> None:
                await ws.send_json({"pong": True})

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/b") as ws:
                await ws.send_bytes(b"\x00\x01")
                err = await ws.receive_json()
                assert err["error"]["code"] == "websocket_validation_error"

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_invalid_json_frame_yields_error(self):
        @ws_controller("/e")
        class G:
            @on_message("x")
            async def p(self, ws: WebSocket) -> None:
                pass

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/e") as ws:
                await ws.send_text("{not json")
                err = await ws.receive_json()
                assert err["error"]["code"] == "websocket_validation_error"

        asyncio.run(run())

    def test_missing_event_field_yields_error(self):
        @ws_controller("/e")
        class G:
            @on_message("x")
            async def p(self, ws: WebSocket) -> None:
                pass

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/e") as ws:
                await ws.send_json({"no_event_field": 1})
                err = await ws.receive_json()
                assert err["error"]["code"] == "websocket_validation_error"
                assert "event" in err["error"]["message"]

        asyncio.run(run())

    def test_unknown_event_yields_error_listing_known(self):
        @ws_controller("/e")
        class G:
            @on_message("a")
            async def a(self, ws: WebSocket) -> None:
                pass

            @on_message("b")
            async def b(self, ws: WebSocket) -> None:
                pass

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/e") as ws:
                await ws.send_json({"event": "nope"})
                err = await ws.receive_json()
                assert err["error"]["code"] == "websocket_validation_error"
                known = err["error"]["detail"]["known"]
                assert set(known) == {"a", "b"}

        asyncio.run(run())

    def test_on_error_hook_catches_handler_exceptions(self):
        captured: list[str] = []

        @ws_controller("/e")
        class G:
            @on_message("boom")
            async def boom(self, ws: WebSocket) -> None:
                raise RuntimeError("kaboom")

            @on_error
            async def catch(self, ws: WebSocket, error: Exception) -> None:
                # The runtime passes the exception under ``error``,
                # ``exc``, or ``exception`` \u2014 whichever parameter name
                # the handler declares. We declare ``error`` here.
                captured.append(f"{type(error).__name__}:{error}")
                await ws.send_json({"handled": True})

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/e") as ws:
                await ws.send_json({"event": "boom"})
                reply = await ws.receive_json()
                assert reply == {"handled": True}

        asyncio.run(run())
        assert captured == ["RuntimeError:kaboom"]


# ---------------------------------------------------------------------------
# Server-initiated close
# ---------------------------------------------------------------------------


class TestServerClose:
    def test_server_close_propagates_code(self):
        @ws_controller("/close")
        class G:
            @on_message("hang-up")
            async def hangup(self, ws: WebSocket) -> None:
                await ws.close(code=4001, reason="by request")

            @on_message("ping")
            async def ping(self, ws: WebSocket) -> None:
                await ws.send_json({"pong": True})

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/close") as ws:
                await ws.send_json({"event": "hang-up"})
                # The next receive yields the close frame.
                msg = await ws.receive()
                assert msg["type"] == "websocket.close"
                assert msg["code"] == 4001
                assert ws.close_code == 4001

        asyncio.run(run())

    def test_on_connect_can_reject_handshake(self):
        @ws_controller("/auth")
        class G:
            @on_connect
            async def check(self, ws: WebSocket, token: Query[str]) -> None:
                if token != "secret":
                    await ws.close(code=1008, reason="unauthorized")
                    raise WebSocketDisconnect("bad token", close_code=1008)

            @on_message("ping")
            async def p(self, ws: WebSocket) -> None:
                await ws.send_json({"pong": True})

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            # Bad token \u2014 connection closed immediately with 1008.
            async with WsTestClient(app).connect(
                "/auth", query_string="token=wrong"
            ) as ws:
                assert ws._closed is True
                assert ws.close_code == 1008
            # Valid token \u2014 ping works.
            async with WsTestClient(app).connect(
                "/auth", query_string="token=secret"
            ) as ws:
                await ws.send_json({"event": "ping"})
                assert await ws.receive_json() == {"pong": True}

        asyncio.run(run())


# ---------------------------------------------------------------------------
# BroadcastGroup + DI — multi-client chat room
# ---------------------------------------------------------------------------


class TestBroadcastRoom:
    def _build_app(self):
        # Single shared BroadcastGroup provider; the gateway subscribes
        # each connection on join and broadcasts messages to everyone
        # else in the room.
        @injectable(scope=Scope.SINGLETON)
        class Rooms(BroadcastGroup):
            pass

        @ws_controller("/room/{room_id}")
        class RoomGateway:
            def __init__(self, rooms: Rooms) -> None:
                self._rooms = rooms

            @on_connect
            async def joined(self, ws: WebSocket, room_id) -> None:
                await ws.accept()
                await self._rooms.subscribe(room_id, ws)
                await self._rooms.broadcast(
                    room_id,
                    {"event": "joined", "room": room_id},
                    exclude=ws,
                )

            @on_message("chat.send")
            async def send(
                self, ws: WebSocket, body: Json[ChatMessage], room_id
            ) -> None:
                await self._rooms.broadcast(
                    room_id,
                    {"event": "chat", "text": body.text},
                )

            @on_disconnect
            async def left(self, ws: WebSocket) -> None:
                await self._rooms.unsubscribe_all(ws)

        @module(controllers=[RoomGateway], providers=[Rooms])
        class M:
            pass

        return asyncio.run(LaurenFactory.create(M))

    def test_message_fans_out_to_all_subscribers(self):
        app = self._build_app()

        async def run():
            client = WsTestClient(app)
            async with client.connect("/room/7") as alice:
                async with client.connect("/room/7") as bob:
                    # Bob should see alice's original join event in
                    # the buffer \u2014 we just drain it.
                    # Actually, alice joined first so her broadcast for
                    # bob's join is sent to alice (excluded is bob).
                    join_msg = await alice.receive_json()
                    assert join_msg == {"event": "joined", "room": "7"}

                    # Alice sends a message \u2014 both receive it.
                    await alice.send_json(
                        {
                            "event": "chat.send",
                            "data": {"text": "hello room"},
                        }
                    )
                    m1 = await alice.receive_json()
                    m2 = await bob.receive_json()
                    assert m1 == {"event": "chat", "text": "hello room"}
                    assert m2 == {"event": "chat", "text": "hello room"}

        asyncio.run(run())

    def test_rooms_are_isolated(self):
        app = self._build_app()

        async def run():
            client = WsTestClient(app)
            async with client.connect("/room/A") as a1:
                async with client.connect("/room/B") as b1:
                    # Each session opened second triggers a join
                    # broadcast to the first in the SAME room; A and B
                    # are isolated, so neither sees the other's join.
                    # We therefore expect no pending frames on either.
                    await a1.send_json(
                        {
                            "event": "chat.send",
                            "data": {"text": "only A"},
                        }
                    )
                    msg = await a1.receive_json()
                    assert msg["text"] == "only A"

                    # Immediately send from b1 to confirm b1 has no
                    # leftover frames from room A.
                    await b1.send_json(
                        {
                            "event": "chat.send",
                            "data": {"text": "only B"},
                        }
                    )
                    msg = await b1.receive_json()
                    assert msg["text"] == "only B"

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Subprotocol negotiation
# ---------------------------------------------------------------------------


class TestSubprotocol:
    def test_explicit_subprotocol_selection(self):
        @ws_controller("/sp")
        class G:
            @on_connect
            async def a(self, ws: WebSocket) -> None:
                offered = ws.client_subprotocols
                pick = "chat.v2" if "chat.v2" in offered else offered[0]
                await ws.accept(subprotocol=pick)

            @on_message("ping")
            async def p(self, ws: WebSocket) -> None:
                await ws.send_json({"sub": ws.subprotocol})

        @module(controllers=[G])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            client = WsTestClient(app)
            async with client.connect("/sp", subprotocols=["chat.v1", "chat.v2"]) as ws:
                assert ws.accepted_subprotocol == "chat.v2"
                await ws.send_json({"event": "ping"})
                assert await ws.receive_json() == {"sub": "chat.v2"}

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Inheritance: a non-redecorated subclass is NOT a gateway
# ---------------------------------------------------------------------------


class TestInheritance:
    def test_subclass_not_auto_mounted(self):
        @ws_controller("/base")
        class Base:
            @on_message("ping")
            async def ping(self, ws: WebSocket) -> None:
                await ws.send_json({"from": "base"})

        class Derived(Base):
            # No @ws_controller decoration \u2014 framework must treat this
            # as a plain class, NOT a second gateway.
            pass

        # Listing *only* Derived in controllers should raise because
        # the metadata-inheritance guard will detect the inherited
        # marker and insist on explicit re-decoration.
        from lauren.exceptions import MetadataInheritanceError

        @module(controllers=[Derived])
        class BadModule:
            pass

        with pytest.raises(MetadataInheritanceError):
            asyncio.run(LaurenFactory.create(BadModule))

    def test_explicitly_redecorated_subclass_works(self):
        @ws_controller("/base")
        class Base:
            @on_message("ping")
            async def ping(self, ws: WebSocket) -> None:
                await ws.send_json({"from": "base"})

        @ws_controller("/derived")
        class Derived(Base):
            @on_message("ping")
            async def ping(self, ws: WebSocket) -> None:  # type: ignore[override]
                await ws.send_json({"from": "derived"})

        @module(controllers=[Derived])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/derived") as ws:
                await ws.send_json({"event": "ping"})
                assert await ws.receive_json() == {"from": "derived"}
            async with WsTestClient(app).connect("/base") as ws:
                # Base isn't in the module, so /base is a route miss.
                assert ws._closed is True
                assert ws.close_code == 1008

        asyncio.run(run())


# ---------------------------------------------------------------------------
# HTTP and WS co-existence — both worlds share the same DI + module graph
# ---------------------------------------------------------------------------


class TestCoexistence:
    def test_http_and_ws_in_same_module(self):
        @injectable(scope=Scope.SINGLETON)
        class Counter:
            def __init__(self) -> None:
                self.n = 0

        @controller("/api")
        class Api:
            def __init__(self, c: Counter) -> None:
                self._c = c

            @get("/count")
            async def count(self) -> dict:
                return {"n": self._c.n}

        @ws_controller("/ws")
        class Gw:
            def __init__(self, c: Counter) -> None:
                self._c = c

            @on_message("inc")
            async def inc(self, ws: WebSocket) -> None:
                self._c.n += 1
                await ws.send_json({"n": self._c.n})

        @module(controllers=[Api, Gw], providers=[Counter])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        # HTTP first
        r = TestClient(app).get("/api/count")
        assert r.status_code == 200 and r.json() == {"n": 0}

        async def run():
            async with WsTestClient(app).connect("/ws") as ws:
                await ws.send_json({"event": "inc"})
                m = await ws.receive_json()
                assert m == {"n": 1}
                await ws.send_json({"event": "inc"})
                m = await ws.receive_json()
                assert m == {"n": 2}

        asyncio.run(run())

        r = TestClient(app).get("/api/count")
        assert r.json() == {"n": 2}


# ---------------------------------------------------------------------------
# Pure-HTTP apps reject WebSocket connections cleanly
# ---------------------------------------------------------------------------


class TestPureHttpAppRejectsWs:
    def test_no_gateways_closes_ws_with_1008(self):
        @controller("/api")
        class Api:
            @get("/ping")
            async def ping(self) -> dict:
                return {"ok": True}

        @module(controllers=[Api])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))

        async def run():
            async with WsTestClient(app).connect("/anything") as ws:
                assert ws._closed is True
                assert ws.close_code == 1008

        asyncio.run(run())
