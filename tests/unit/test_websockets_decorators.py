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
