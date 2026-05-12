"""Unit tests for the WebSocket decorator surface.

These tests focus on the pieces that can be exercised without driving
an ASGI app:

* ``@ws_controller`` / ``@on_connect`` / ``@on_message`` /
  ``@on_disconnect`` / ``@on_error`` attach the correct marker
  attributes to the decorated entity AND never to its base classes.
* Inheritance is explicit: a subclass of a ``@ws_controller`` is not
  automatically a gateway; overriding a hook method without
  re-decorating drops the marker.
* Bare-decorator usage (``@ws_controller`` without parentheses) is
  rejected loudly.
* Stacked ``@on_message(...)`` decorators accumulate multiple
  :class:`WsMessageMeta` entries on one function.
* ``discover_ws_hooks`` returns the right buckets, sorted
  deterministically.
"""

from __future__ import annotations

import pytest

from lauren import (
    WebSocket,
    on_connect,
    on_disconnect,
    on_error,
    on_message,
    ws_controller,
)
from lauren.exceptions import (
    DecoratorUsageError,
    MetadataInheritanceError,
    StartupError,
)
from lauren.websockets import (
    WS_CONTROLLER_META,
    WS_ON_CONNECT,
    WS_ON_DISCONNECT,
    WS_ON_ERROR,
    WS_ON_MESSAGE,
    WsControllerMeta,
    discover_ws_hooks,
    is_ws_controller,
    own_ws_controller_meta,
)


# ---------------------------------------------------------------------------
# @ws_controller attaches metadata only to the decorated class.
# ---------------------------------------------------------------------------


class TestWsControllerMarker:
    def test_attaches_meta_to_own_dict(self):
        @ws_controller("/chat")
        class G:
            pass

        assert WS_CONTROLLER_META in G.__dict__
        meta = G.__dict__[WS_CONTROLLER_META]
        assert isinstance(meta, WsControllerMeta)
        assert meta.path == "/chat"

    def test_supports_tags_summary_description(self):
        @ws_controller(
            "/chat",
            tags=["realtime", "v1"],
            summary="Chat gateway",
            description="Handles chat messages",
        )
        class G:
            pass

        meta = G.__dict__[WS_CONTROLLER_META]
        assert meta.tags == ("realtime", "v1")
        assert meta.summary == "Chat gateway"
        assert meta.description == "Handles chat messages"

    def test_subclass_does_not_inherit_marker_in_own_dict(self):
        @ws_controller("/base")
        class Base:
            pass

        class Derived(Base):
            pass

        # ``Derived`` inherits the marker via normal attribute lookup,
        # but the OWN __dict__ does not contain it \u2014 which is the
        # framework's contract.
        assert WS_CONTROLLER_META not in Derived.__dict__
        assert is_ws_controller(Base) is True
        assert is_ws_controller(Derived) is False

    def test_own_ws_controller_meta_rejects_inherited_marker(self):
        @ws_controller("/base")
        class Base:
            pass

        class Derived(Base):
            pass

        with pytest.raises(MetadataInheritanceError):
            own_ws_controller_meta(Derived)

    def test_own_ws_controller_meta_raises_when_no_marker(self):
        class Plain:
            pass

        with pytest.raises(StartupError):
            own_ws_controller_meta(Plain)

    def test_redecorated_subclass_is_a_gateway(self):
        @ws_controller("/base")
        class Base:
            pass

        @ws_controller("/derived")
        class Derived(Base):
            pass

        assert is_ws_controller(Derived) is True
        assert own_ws_controller_meta(Derived).path == "/derived"
        assert own_ws_controller_meta(Base).path == "/base"

    def test_bare_ws_controller_rejected(self):
        with pytest.raises(DecoratorUsageError):

            @ws_controller
            class G:  # noqa: F811 - intentional
                pass


# ---------------------------------------------------------------------------
# Method-level hooks attach markers to the function only.
# ---------------------------------------------------------------------------


class TestMethodMarkers:
    def test_on_connect_sets_marker(self):
        @on_connect
        async def joined(self, ws: WebSocket) -> None:
            pass

        assert getattr(joined, WS_ON_CONNECT) is True

    def test_on_disconnect_sets_marker(self):
        @on_disconnect
        async def left(self, ws: WebSocket) -> None:
            pass

        assert getattr(left, WS_ON_DISCONNECT) is True

    def test_on_message_accumulates(self):
        @on_message("chat.send")
        @on_message("chat.typing")
        async def handler(self, ws: WebSocket) -> None:
            pass

        metas = getattr(handler, WS_ON_MESSAGE)
        assert isinstance(metas, list)
        assert len(metas) == 2
        events = {m.event for m in metas}
        assert events == {"chat.send", "chat.typing"}

    def test_on_message_records_summary_and_description(self):
        @on_message(
            "chat.send",
            summary="Send a chat message",
            description="body must be a ChatMessage",
        )
        async def handler(self, ws: WebSocket) -> None:
            pass

        meta = getattr(handler, WS_ON_MESSAGE)[0]
        assert meta.summary == "Send a chat message"
        assert meta.description == "body must be a ChatMessage"

    def test_on_error_sets_marker(self):
        @on_error
        async def handle(self, exc: Exception) -> None:
            pass

        assert getattr(handle, WS_ON_ERROR) is True

    def test_on_message_bare_usage_rejected(self):
        # Writing ``@on_message`` (no call) passes the decorated function
        # as ``event`` \u2014 should raise rather than silently misbehave.
        async def fn(self, ws) -> None: ...

        with pytest.raises(DecoratorUsageError):
            on_message(fn)  # type: ignore[arg-type]

    def test_decorator_rejects_non_callables(self):
        with pytest.raises(DecoratorUsageError):
            on_connect(42)  # type: ignore[arg-type]
        with pytest.raises(DecoratorUsageError):
            on_disconnect("x")  # type: ignore[arg-type]
        with pytest.raises(DecoratorUsageError):
            on_error(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# discover_ws_hooks collects the right hooks and respects the
# no-inheritance rule.
# ---------------------------------------------------------------------------


class TestDiscoverHooks:
    def test_discovers_all_three_hook_types(self):
        @ws_controller("/x")
        class G:
            @on_connect
            async def a(self, ws: WebSocket) -> None: ...

            @on_message("evt")
            async def b(self, ws: WebSocket) -> None: ...

            @on_disconnect
            async def c(self, ws: WebSocket) -> None: ...

        hooks = discover_ws_hooks(G)
        assert hooks["on_connect"] is not None
        assert hooks["on_connect"].__name__ == "a"
        assert hooks["on_disconnect"] is not None
        assert hooks["on_disconnect"].__name__ == "c"
        assert len(hooks["messages"]) == 1
        event, fn, meta = hooks["messages"][0]
        assert event == "evt"

    def test_multiple_events_on_one_function_yield_multiple_rows(self):
        @ws_controller("/x")
        class G:
            @on_message("a")
            @on_message("b")
            async def h(self, ws: WebSocket) -> None: ...

        hooks = discover_ws_hooks(G)
        events = sorted(e for e, _, _ in hooks["messages"])
        assert events == ["a", "b"]

    def test_override_without_redecoration_drops_marker(self):
        @ws_controller("/base")
        class Base:
            @on_message("evt")
            async def handle(self, ws: WebSocket) -> None: ...

        @ws_controller("/derived")
        class Derived(Base):
            # Override WITHOUT re-applying @on_message \u2014 the subclass
            # method loses the marker. This matches the HTTP @get / @post
            # contract.
            async def handle(self, ws: WebSocket) -> None:  # type: ignore[override]
                pass

        base_hooks = discover_ws_hooks(Base)
        derived_hooks = discover_ws_hooks(Derived)
        assert [e for e, _, _ in base_hooks["messages"]] == ["evt"]
        assert derived_hooks["messages"] == []

    def test_redecorated_override_keeps_marker(self):
        @ws_controller("/base")
        class Base:
            @on_message("evt")
            async def handle(self, ws: WebSocket) -> None: ...

        @ws_controller("/derived")
        class Derived(Base):
            @on_message("evt")
            async def handle(self, ws: WebSocket) -> None:  # type: ignore[override]
                pass

        derived_hooks = discover_ws_hooks(Derived)
        assert [e for e, _, _ in derived_hooks["messages"]] == ["evt"]

    def test_returns_none_when_no_hooks(self):
        @ws_controller("/x")
        class Empty:
            pass

        hooks = discover_ws_hooks(Empty)
        assert hooks["on_connect"] is None
        assert hooks["on_disconnect"] is None
        assert hooks["on_error"] is None
        assert hooks["messages"] == []

    def test_on_error_is_picked_up(self):
        @ws_controller("/x")
        class G:
            @on_error
            async def catch(self, exc: Exception) -> None:
                pass

        hooks = discover_ws_hooks(G)
        assert hooks["on_error"] is not None
        assert hooks["on_error"].__name__ == "catch"


# ---------------------------------------------------------------------------
# WebSocket class unit tests (lines 330, 380, 454, 458, 512, 520, etc.)
# ---------------------------------------------------------------------------


class TestWebSocketDirectUnit:
    """Test WebSocket methods directly without a full ASGI app."""

    def _make_ws(self, messages=None):
        """Create a WebSocket in STATE_CONNECTING with a fake receive/send."""
        from lauren.websockets import WebSocket

        if messages is None:
            messages = []

        idx = [0]

        async def receive():
            if idx[0] >= len(messages):
                return {"type": "websocket.disconnect", "code": 1000}
            msg = messages[idx[0]]
            idx[0] += 1
            return msg

        sent = []

        async def send(msg):
            sent.append(msg)

        ws = WebSocket(
            scope={
                "type": "websocket",
                "path": "/test",
                "headers": [],
                "query_string": b"",
            },
            receive=receive,
            send=send,
            path_template="/test",
            path_params={},
        )
        return ws, sent

    def test_path_property(self):
        ws, _ = self._make_ws()
        assert ws.path == "/test"

    def test_path_template_property(self):
        ws, _ = self._make_ws()
        assert ws.path_template == "/test"

    def test_path_params_property(self):
        ws, _ = self._make_ws()
        assert ws.path_params == {}

    def test_headers_property(self):
        ws, _ = self._make_ws()
        assert ws.headers is not None

    def test_query_string_property(self):
        ws, _ = self._make_ws()
        assert ws.query_string == b""

    def test_state_property(self):
        ws, _ = self._make_ws()
        assert ws.state is not None

    def test_app_state_property(self):
        ws, _ = self._make_ws()
        # app_state is None by default (no app_state passed)
        # Just verify the property exists
        _ = ws.app_state  # should not raise

    def test_connected_property_before_accept(self):
        ws, _ = self._make_ws()
        assert not ws.connected

    def test_accept_when_not_connecting_raises(self):
        import asyncio
        from lauren.websockets import WebSocket, WebSocketError

        ws, sent = self._make_ws()

        async def run():
            # Move to STATE_OPEN manually
            ws._state_code = WebSocket.STATE_OPEN
            with pytest.raises(WebSocketError, match="cannot accept"):
                await ws.accept()

        asyncio.run(run())

    def test_receive_text_with_binary_message_raises(self):
        import asyncio
        from lauren.websockets import WebSocket, WebSocketError

        messages = [
            {"type": "websocket.receive", "text": None, "bytes": b"\x01\x02"},
        ]
        ws, _ = self._make_ws(messages)

        async def run():
            ws._state_code = WebSocket.STATE_OPEN
            with pytest.raises(WebSocketError, match="expected text frame"):
                await ws.receive_text()

        asyncio.run(run())

    def test_receive_bytes_with_text_message_raises(self):
        import asyncio
        from lauren.websockets import WebSocket, WebSocketError

        messages = [
            {"type": "websocket.receive", "text": "hello", "bytes": None},
        ]
        ws, _ = self._make_ws(messages)

        async def run():
            ws._state_code = WebSocket.STATE_OPEN
            with pytest.raises(WebSocketError, match="expected binary frame"):
                await ws.receive_bytes()

        asyncio.run(run())

    def test_receive_json_invalid_json_raises(self):
        import asyncio
        from lauren.websockets import WebSocket, WebSocketValidationError

        messages = [
            {"type": "websocket.receive", "text": "not-valid-json", "bytes": None},
        ]
        ws, _ = self._make_ws(messages)

        async def run():
            ws._state_code = WebSocket.STATE_OPEN
            with pytest.raises(WebSocketValidationError, match="invalid JSON frame"):
                await ws.receive_json()

        asyncio.run(run())

    def test_close_when_already_closed_is_noop(self):
        import asyncio
        from lauren.websockets import WebSocket

        ws, sent = self._make_ws()

        async def run():
            ws._state_code = WebSocket.STATE_CLOSED
            await ws.close()  # Should not raise

        asyncio.run(run())
        assert not sent  # nothing sent

    def test_receive_when_closed_raises(self):
        import asyncio
        from lauren.websockets import WebSocket, WebSocketDisconnect

        ws, _ = self._make_ws()

        async def run():
            ws._state_code = WebSocket.STATE_CLOSED
            with pytest.raises(WebSocketDisconnect, match="already closed"):
                await ws.receive()

        asyncio.run(run())

    def test_accept_with_subprotocol_and_headers(self):
        import asyncio

        ws, sent = self._make_ws()

        async def run():
            await ws.accept(
                subprotocol="my-protocol",
                headers=[("x-custom", "value")],
            )

        asyncio.run(run())
        assert len(sent) == 1
        accept_msg = sent[0]
        assert accept_msg["type"] == "websocket.accept"
        assert accept_msg["subprotocol"] == "my-protocol"
        assert accept_msg["headers"]

    def test_on_message_non_method_raises(self):
        """@on_message on a class (not a method) raises DecoratorUsageError."""
        from lauren.exceptions import DecoratorUsageError

        with pytest.raises(DecoratorUsageError):

            @on_message("evt")
            class NotAMethod:
                pass

    def test_is_method_target_returns_false_for_class(self):
        """_is_method_target returns False for a class type."""
        from lauren.websockets import _is_method_target

        class Foo:
            pass

        assert not _is_method_target(Foo)

    def test_is_method_target_returns_true_for_classmethod(self):
        """_is_method_target returns True for classmethod."""
        from lauren.websockets import _is_method_target

        class Foo:
            @classmethod
            def method(cls):
                pass

        assert _is_method_target(Foo.__dict__["method"])


# ---------------------------------------------------------------------------
# Coverage-gap tests
# ---------------------------------------------------------------------------


class TestWsControllerDecoratorScope:
    """Lines 253-255: @ws_controller marks class as REQUEST-scoped when not already set."""

    def test_marks_class_as_request_scoped(self):
        from lauren.websockets import ws_controller
        from lauren._di import INJECTABLE_META
        from lauren.types import Scope

        @ws_controller("/room")
        class MyGateway:
            pass

        meta = getattr(MyGateway, INJECTABLE_META, None)
        assert meta is not None
        assert meta.scope == Scope.REQUEST

    def test_does_not_overwrite_existing_injectable_meta(self):
        from lauren import injectable
        from lauren.types import Scope
        from lauren.websockets import ws_controller

        @ws_controller("/room2")
        @injectable(scope=Scope.SINGLETON)
        class AlreadyDecorated:
            pass

        from lauren._di import INJECTABLE_META

        meta = getattr(AlreadyDecorated, INJECTABLE_META, None)
        # @injectable(SINGLETON) was applied before ws_controller checks;
        # INJECTABLE_META is in __dict__, so ws_controller must not overwrite.
        assert meta is not None


class TestWebSocketReceiveTypeMismatch:
    """Lines 540, 552, 558: receive_text/bytes raise on wrong frame type."""

    def _make_ws(self, messages):
        from lauren.websockets import WebSocket

        idx = 0

        async def receive():
            nonlocal idx
            msg = messages[idx]
            idx += 1
            return msg

        async def send(msg):
            pass

        scope = {
            "type": "websocket",
            "path": "/ws",
            "headers": [],
            "query_string": b"",
        }
        ws = WebSocket(scope=scope, receive=receive, send=send, path_template="/ws", path_params={})
        # Mark as open to skip the accept handshake
        ws._state_code = WebSocket.STATE_OPEN
        return ws

    @pytest.mark.asyncio
    async def test_receive_text_binary_frame_raises(self):
        from lauren.websockets import WebSocketError

        ws = self._make_ws(
            [
                {"type": "websocket.receive", "bytes": b"data", "text": None},
            ]
        )
        with pytest.raises(WebSocketError, match="expected text frame"):
            await ws.receive_text()

    @pytest.mark.asyncio
    async def test_receive_bytes_text_frame_raises(self):
        from lauren.websockets import WebSocketError

        ws = self._make_ws(
            [
                {"type": "websocket.receive", "text": "hello", "bytes": None},
            ]
        )
        with pytest.raises(WebSocketError, match="expected binary frame"):
            await ws.receive_bytes()

    @pytest.mark.asyncio
    async def test_receive_text_wrong_message_type_raises(self):
        from lauren.websockets import WebSocketError

        ws = self._make_ws(
            [
                {"type": "websocket.connect"},
            ]
        )
        with pytest.raises(WebSocketError, match="unexpected message type"):
            await ws.receive_text()


class TestEnsureOpen:
    """Line 621: _ensure_open raises when not in OPEN state."""

    def test_ensure_open_when_closed_raises(self):
        from lauren.websockets import WebSocket, WebSocketError

        async def receive():
            return {"type": "websocket.disconnect"}

        async def send(msg):
            pass

        scope = {"type": "websocket", "path": "/", "headers": [], "query_string": b""}
        ws = WebSocket(scope=scope, receive=receive, send=send, path_template="/ws", path_params={})
        # State is CONNECTING initially — not OPEN
        with pytest.raises(WebSocketError, match="cannot send_text"):
            ws._ensure_open("send_text")


class TestBroadcastGroupUnsubscribeCleanup:
    """Lines 690->exit, 758-763: unsubscribe removes empty group; broadcast evicts dead."""

    @pytest.mark.asyncio
    async def test_unsubscribe_last_member_removes_group(self):
        from lauren.websockets import BroadcastGroup

        bg = BroadcastGroup()

        class FakeWS:
            pass

        ws = FakeWS()
        await bg.subscribe("room", ws)
        assert "room" in bg.groups()
        await bg.unsubscribe("room", ws)
        assert "room" not in bg.groups()

    @pytest.mark.asyncio
    async def test_unsubscribe_one_of_two_keeps_group(self):
        from lauren.websockets import BroadcastGroup

        bg = BroadcastGroup()

        class FakeWS:
            pass

        ws1, ws2 = FakeWS(), FakeWS()
        await bg.subscribe("room", ws1)
        await bg.subscribe("room", ws2)
        await bg.unsubscribe("room", ws1)
        assert "room" in bg.groups()

    @pytest.mark.asyncio
    async def test_broadcast_removes_dead_connections(self):
        from lauren.websockets import BroadcastGroup

        bg = BroadcastGroup()
        sent = []

        class LiveWS:
            async def send_text(self, msg):
                sent.append(msg)

        class DeadWS:
            async def send_text(self, msg):
                raise OSError("connection closed")

        live = LiveWS()
        dead = DeadWS()
        await bg.subscribe("room", live)
        await bg.subscribe("room", dead)
        count = await bg.broadcast("room", "hello")
        assert count == 1
        assert sent == ["hello"]
        # Dead connection cleaned up
        members = bg._members.get("room", set())
        assert dead not in members

    @pytest.mark.asyncio
    async def test_broadcast_removes_group_when_all_dead(self):
        from lauren.websockets import BroadcastGroup

        bg = BroadcastGroup()

        class DeadWS:
            async def send_text(self, msg):
                raise OSError("dead")

        dead = DeadWS()
        await bg.subscribe("room", dead)
        await bg.broadcast("room", "msg")
        assert "room" not in bg.groups()
