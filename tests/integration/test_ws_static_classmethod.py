"""Integration tests for static/classmethod WebSocket hooks.

Mirrors the HTTP static/classmethod integration suite, verifying that
``@on_connect`` / ``@on_message`` / ``@on_disconnect`` stacked with
``@staticmethod`` or ``@classmethod`` register and dispatch correctly
in either decorator order.
"""

from __future__ import annotations

import asyncio


from lauren import (
    LaurenFactory,
    WebSocket,
    module,
    on_connect,
    on_disconnect,
    on_message,
    ws_controller,
)
from lauren.testing import WsTestClient


class TestWsStaticMethod:
    def test_static_on_message(self):
        @ws_controller("/ws")
        class Gateway:
            @on_message("ping")
            @staticmethod
            async def ping(ws: WebSocket) -> None:
                await ws.send_json({"pong": True, "static": True})

        @module(controllers=[Gateway])
        class M:
            pass

        app = LaurenFactory.create(M)

        async def run() -> None:
            async with WsTestClient(app).connect("/ws") as ws:
                await ws.send_json({"event": "ping"})
                reply = await ws.receive_json()
                assert reply == {"pong": True, "static": True}

        asyncio.run(run())

    def test_static_on_connect(self):
        captured: list[str] = []

        @ws_controller("/ws")
        class Gateway:
            @on_connect
            @staticmethod
            async def greet(ws: WebSocket) -> None:
                captured.append("connected")
                await ws.accept()
                await ws.send_json({"greeting": "hi"})

            @on_message("ping")
            @staticmethod
            async def ping(ws: WebSocket) -> None:
                await ws.send_json({"pong": True})

        @module(controllers=[Gateway])
        class M:
            pass

        app = LaurenFactory.create(M)

        async def run() -> None:
            async with WsTestClient(app).connect("/ws") as ws:
                # @on_connect emitted a greeting frame before any client
                # send \u2014 proving the hook ran.
                msg = await ws.receive_json()
                assert msg == {"greeting": "hi"}

        asyncio.run(run())
        assert captured == ["connected"]

    def test_decorator_order_above_or_below(self):
        @ws_controller("/ws")
        class Gateway:
            @on_message("above")
            @staticmethod
            async def above(ws: WebSocket) -> None:
                await ws.send_json({"order": "above"})

            @staticmethod
            @on_message("below")
            async def below(ws: WebSocket) -> None:
                await ws.send_json({"order": "below"})

        @module(controllers=[Gateway])
        class M:
            pass

        app = LaurenFactory.create(M)

        async def run() -> None:
            async with WsTestClient(app).connect("/ws") as ws:
                await ws.send_json({"event": "above"})
                assert await ws.receive_json() == {"order": "above"}
                await ws.send_json({"event": "below"})
                assert await ws.receive_json() == {"order": "below"}

        asyncio.run(run())


class TestWsClassmethod:
    def test_classmethod_receives_cls(self):
        @ws_controller("/ws")
        class Gateway:
            @on_message("whoami")
            @classmethod
            async def whoami(cls, ws: WebSocket) -> None:
                await ws.send_json({"cls": cls.__name__})

        @module(controllers=[Gateway])
        class M:
            pass

        app = LaurenFactory.create(M)

        async def run() -> None:
            async with WsTestClient(app).connect("/ws") as ws:
                await ws.send_json({"event": "whoami"})
                reply = await ws.receive_json()
                assert reply == {"cls": "Gateway"}

        asyncio.run(run())

    def test_mixed_instance_static_classmethod_hooks(self):
        # A realistic gateway that uses all three binding styles.
        events: list[str] = []

        @ws_controller("/mix")
        class Gateway:
            @on_connect
            async def joined(self, ws: WebSocket) -> None:
                events.append("instance:connect")

            @on_message("cls")
            @classmethod
            async def handle_cls(cls, ws: WebSocket) -> None:
                events.append(f"cls:{cls.__name__}")
                await ws.send_json({"handler": "classmethod"})

            @on_message("static")
            @staticmethod
            async def handle_static(ws: WebSocket) -> None:
                events.append("static")
                await ws.send_json({"handler": "staticmethod"})

            @on_message("instance")
            async def handle_instance(self, ws: WebSocket) -> None:
                events.append(f"instance:{type(self).__name__}")
                await ws.send_json({"handler": "instance"})

            @on_disconnect
            @staticmethod
            async def left(ws: WebSocket) -> None:
                events.append(f"static:disconnect:{ws.close_code}")

        @module(controllers=[Gateway])
        class M:
            pass

        app = LaurenFactory.create(M)

        async def run() -> None:
            async with WsTestClient(app).connect("/mix") as ws:
                await ws.send_json({"event": "cls"})
                assert await ws.receive_json() == {"handler": "classmethod"}
                await ws.send_json({"event": "static"})
                assert await ws.receive_json() == {"handler": "staticmethod"}
                await ws.send_json({"event": "instance"})
                assert await ws.receive_json() == {"handler": "instance"}

        asyncio.run(run())
        assert events[0] == "instance:connect"
        assert "cls:Gateway" in events
        assert "static" in events
        assert "instance:Gateway" in events
        # Disconnect from the client flushes 1000.
        assert events[-1] == "static:disconnect:1000"
