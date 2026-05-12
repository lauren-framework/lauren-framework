"""Regression tests for the WebSocket double-close fix.

Prior to the fix, when an ``@on_connect`` hook called ``await ws.close(...)``
and then raised ``WebSocketDisconnect``, the runtime would send a *second*
``websocket.close`` ASGI message via the raw ``send`` callable — bypassing the
WebSocket object's idempotency guard.  On a real Uvicorn process this caused
the ASGI transport to raise (you cannot send two close frames for one
connection), which propagated out of ``_websocket()`` as an unhandled
exception, corrupting concurrent HTTP responses and triggering the Uvicorn log
message "ASGI callable returned without completing response".

These tests verify that:
1. A gateway that calls ``ws.close()`` + raises ``WebSocketDisconnect`` causes
   a clean rejection with the right close code — no exception escapes.
2. A gateway that raises ``WebSocketDisconnect`` WITHOUT an explicit
   ``ws.close()`` still works (the runtime closes the connection).
3. A gateway that calls ``ws.close()`` WITHOUT raising still works.
4. Both patterns behave identically from the client's perspective
   (connection rejected with the custom code).
5. Multiple sequential connections to the same app all succeed, proving no
   per-connection state is leaked by the double-close path.
"""

# NO from __future__ import annotations — ws gateway types need live
# annotations so the framework can build the extraction plan at registration
# time without falling back to raw string annotations.

import asyncio

import pytest

from lauren import (
    LaurenFactory,
    Query,
    WebSocket,
    WebSocketDisconnect,
    module,
    on_connect,
    on_disconnect,
    ws_controller,
)
from lauren.testing import WsTestClient


# ---------------------------------------------------------------------------
# Gateways under test
# ---------------------------------------------------------------------------


@ws_controller("/reject-both")
class CloseAndRaiseGateway:
    """Calls ws.close() AND raises WebSocketDisconnect — the double-close path."""

    disconnected: bool = False

    @on_connect
    async def connect(self, ws: WebSocket, token: Query[str]) -> None:
        if token != "valid":
            await ws.close(code=4401, reason="bad token")
            raise WebSocketDisconnect("unauthorized", close_code=4401)

    @on_disconnect
    async def disconnect(self, ws: WebSocket) -> None:
        CloseAndRaiseGateway.disconnected = True


@ws_controller("/reject-raise-only")
class RaiseOnlyGateway:
    """Raises WebSocketDisconnect without calling ws.close() first."""

    @on_connect
    async def connect(self, ws: WebSocket, token: Query[str]) -> None:
        if token != "valid":
            raise WebSocketDisconnect("unauthorized", close_code=4403)


@ws_controller("/reject-close-only")
class CloseOnlyGateway:
    """Calls ws.close() without raising — just returns from @on_connect."""

    @on_connect
    async def connect(self, ws: WebSocket, token: Query[str]) -> None:
        if token != "valid":
            await ws.close(code=4404, reason="not allowed")


@ws_controller("/accept-ok")
class AcceptGateway:
    """Accepts all connections — used to prove the app still works after rejections."""

    @on_connect
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()


@module(
    controllers=[
        CloseAndRaiseGateway,
        RaiseOnlyGateway,
        CloseOnlyGateway,
        AcceptGateway,
    ]
)
class DoubleCloseModule:
    pass


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    return LaurenFactory.create(DoubleCloseModule)


# ---------------------------------------------------------------------------
# Tests: CloseAndRaiseGateway (the double-close path)
# ---------------------------------------------------------------------------


class TestCloseAndRaise:
    def test_invalid_token_rejected_cleanly(self, app):
        """Connection rejected — no unhandled exception should escape the handler."""

        async def run():
            async with WsTestClient(app).connect("/reject-both", query_string="token=bad") as ws:
                assert ws._closed is True

        asyncio.run(run())

    def test_invalid_token_close_code(self, app):
        """The custom close code (4401) must reach the client."""

        async def run():
            async with WsTestClient(app).connect("/reject-both", query_string="token=bad") as ws:
                assert ws.close_code == 4401

        asyncio.run(run())

    def test_valid_token_accepted(self, app):
        """A valid token should allow the handshake to proceed normally."""

        async def run():
            async with WsTestClient(app).connect("/reject-both", query_string="token=valid") as ws:
                assert ws._accepted is True

        asyncio.run(run())

    def test_multiple_rejections_no_state_leak(self, app):
        """Five sequential rejected connections must all complete cleanly."""

        async def run():
            client = WsTestClient(app)
            for _ in range(5):
                async with client.connect("/reject-both", query_string="token=wrong") as ws:
                    assert ws._closed is True

        asyncio.run(run())

    def test_app_still_works_after_rejections(self, app):
        """After several double-close rejections, the AcceptGateway must still accept."""

        async def run():
            client = WsTestClient(app)
            # Reject a few times
            for _ in range(3):
                async with client.connect("/reject-both", query_string="token=bad") as ws:
                    assert ws._closed is True

            # Then check the app is still healthy
            async with client.connect("/accept-ok") as ws:
                assert ws._accepted is True

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Tests: RaiseOnlyGateway (raise without ws.close — existing path, should
# still work correctly after the double-close fix)
# ---------------------------------------------------------------------------


class TestRaiseOnly:
    def test_raise_only_rejected(self, app):
        async def run():
            async with WsTestClient(app).connect("/reject-raise-only", query_string="token=bad") as ws:
                assert ws._closed is True

        asyncio.run(run())

    def test_raise_only_close_code(self, app):
        async def run():
            async with WsTestClient(app).connect("/reject-raise-only", query_string="token=bad") as ws:
                assert ws.close_code == 4403

        asyncio.run(run())

    def test_raise_only_valid_accepted(self, app):
        async def run():
            async with WsTestClient(app).connect("/reject-raise-only", query_string="token=valid") as ws:
                assert ws._accepted is True

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Tests: CloseOnlyGateway (close without raise)
# ---------------------------------------------------------------------------


class TestCloseOnly:
    def test_close_only_rejected(self, app):
        async def run():
            async with WsTestClient(app).connect("/reject-close-only", query_string="token=bad") as ws:
                assert ws._closed is True

        asyncio.run(run())

    def test_close_only_close_code(self, app):
        async def run():
            async with WsTestClient(app).connect("/reject-close-only", query_string="token=bad") as ws:
                assert ws.close_code == 4404

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Tests: cross-gateway — verify all rejection strategies can coexist
# ---------------------------------------------------------------------------


class TestMixedRejectionStrategies:
    def test_all_strategies_reject_correctly(self, app):
        """All three rejection patterns work in the same app without interfering."""

        async def run():
            client = WsTestClient(app)

            async with client.connect("/reject-both", query_string="token=x") as ws:
                assert ws._closed is True
                assert ws.close_code == 4401

            async with client.connect("/reject-raise-only", query_string="token=x") as ws:
                assert ws._closed is True
                assert ws.close_code == 4403

            async with client.connect("/reject-close-only", query_string="token=x") as ws:
                assert ws._closed is True
                assert ws.close_code == 4404

            async with client.connect("/accept-ok") as ws:
                assert ws._accepted is True

        asyncio.run(run())

    def test_interleaved_accept_reject(self, app):
        """Rejected and accepted connections can be interleaved freely."""

        async def run():
            client = WsTestClient(app)
            for i in range(4):
                if i % 2 == 0:
                    async with client.connect("/reject-both", query_string="token=bad") as ws:
                        assert ws._closed is True
                else:
                    async with client.connect("/accept-ok") as ws:
                        assert ws._accepted is True

        asyncio.run(run())
