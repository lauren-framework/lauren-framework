"""Typed route metadata across levels, end to end (F23 §5.1).

The factory merges controller and route metadata once at startup — typed
keys by their merge policy, string keys most-specific-wins — and every
request's ``ExecutionContext`` shares the read-only result.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lauren import (
    DecoratorUsageError,
    Lauren,
    LaurenFactory,
    MetadataKey,
    MetadataSetKey,
    WebSocket,
    controller,
    get,
    module,
    on_connect,
    on_message,
    set_metadata,
    use_guards,
    ws_controller,
)
from lauren.testing import TestClient, WsTestClient
from lauren.types import ExecutionContext

Roles = MetadataSetKey[str]("it.roles")
RateLimit = MetadataKey[int | None]("it.rate_limit", default=None)


def _app(*controllers: type, **kwargs: Any) -> Any:
    @module(controllers=list(controllers))
    class AppModule:
        pass

    return LaurenFactory.create(AppModule, **kwargs)


class Recorder:
    """A guard that records what it sees through typed and string reads."""

    seen: list[dict[str, Any]] = []

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        Recorder.seen.append(
            {
                "roles": ctx.get_metadata(Roles),
                "limit": ctx.get_metadata(RateLimit),
                "legacy": ctx.get_metadata("legacy"),
                "metadata": ctx.metadata,
            }
        )
        return True


@pytest.fixture(autouse=True)
def _reset_recorder() -> None:
    Recorder.seen = []


def test_collection_key_accumulates_controller_and_route() -> None:
    @Roles("admin")
    @use_guards(Recorder)
    @controller("/m")
    class Admin:
        @get("/")
        @Roles("auditor")
        async def audit(self) -> dict:
            return {}

    with TestClient(_app(Admin)) as client:
        assert client.get("/m/").status_code == 200

    assert Recorder.seen[0]["roles"] == ("admin", "auditor")


def test_string_keys_keep_override_semantics() -> None:
    @set_metadata("legacy", ["admin"])
    @use_guards(Recorder)
    @controller("/m")
    class Admin:
        @get("/")
        @set_metadata("legacy", ["auditor"])
        async def audit(self) -> dict:
            return {}

    with TestClient(_app(Admin)) as client:
        client.get("/m/")

    assert Recorder.seen[0]["legacy"] == ["auditor"]


def test_scalar_key_default_and_route_override() -> None:
    @RateLimit(100)
    @use_guards(Recorder)
    @controller("/m")
    class Limited:
        @get("/a")
        async def a(self) -> dict:
            return {}

        @get("/b")
        @RateLimit(5)
        async def b(self) -> dict:
            return {}

    @use_guards(Recorder)
    @controller("/n")
    class Unlimited:
        @get("/")
        async def c(self) -> dict:
            return {}

    with TestClient(_app(Limited, Unlimited)) as client:
        for path in ("/m/a", "/m/b", "/n/"):
            client.get(path)

    assert [s["limit"] for s in Recorder.seen] == [100, 5, None]


def test_outer_merge_pins_the_controller_value() -> None:
    pinned = MetadataKey[str]("it.tier", default="free", merge="outer")
    seen: list[str] = []

    class TierGuard:
        async def can_activate(self, ctx: ExecutionContext) -> bool:
            seen.append(ctx.get_metadata(pinned))
            return True

    @pinned("gold")
    @use_guards(TierGuard)
    @controller("/m")
    class Pinned:
        @get("/")
        @pinned("free")
        async def h(self) -> dict:
            return {}

    with TestClient(_app(Pinned)) as client:
        client.get("/m/")

    assert seen == ["gold"]


def test_error_merge_conflict_fails_create() -> None:
    strict = MetadataKey[str]("it.region", default="eu", merge="error")

    @strict("eu")
    @controller("/m")
    class Conflicting:
        @get("/")
        @strict("us")
        async def h(self) -> dict:
            return {}

    with pytest.raises(DecoratorUsageError) as info:
        _app(Conflicting)

    assert info.value.detail["reason"] == "metadata_merge_conflict"
    assert (
        info.value.detail["outer"] == "controller test_error_merge_conflict_fails_create.<locals>.Conflicting"
    )
    assert info.value.detail["inner"].endswith("Conflicting.h")


def test_key_name_collision_across_levels_fails_create() -> None:
    first = MetadataSetKey[str]("it.scopes")
    second = MetadataSetKey[str]("it.scopes")

    @first("read")
    @controller("/m")
    class Colliding:
        @get("/")
        @second("write")
        async def h(self) -> dict:
            return {}

    with pytest.raises(DecoratorUsageError) as info:
        _app(Colliding)

    assert info.value.detail["reason"] == "metadata_key_collision"
    assert info.value.detail["definitions"] == [first.site, second.site]


def test_metadata_is_shared_read_only_across_requests() -> None:
    @Roles("admin")
    @use_guards(Recorder)
    @controller("/m")
    class Admin:
        @get("/")
        async def h(self) -> dict:
            return {}

    with TestClient(_app(Admin)) as client:
        client.get("/m/")
        client.get("/m/")

    first, second = Recorder.seen[0]["metadata"], Recorder.seen[1]["metadata"]
    assert first is second
    with pytest.raises(TypeError):
        first["it.roles"] = ()


def test_compiled_handler_holds_the_merged_mapping() -> None:
    @Roles("admin")
    @controller("/m")
    class Admin:
        @get("/")
        @Roles("auditor")
        async def h(self) -> dict:
            return {}

    app = _app(Admin)
    compiled = app._handlers[("GET", "/m")]

    assert compiled.metadata["it.roles"] == ("admin", "auditor")


def test_lauren_app_routes_keep_typed_keys() -> None:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/x")
    @Roles("viewer")
    async def handler(ctx: ExecutionContext) -> dict:
        return {"roles": list(ctx.get_metadata(Roles))}

    assert TestClient(app).get("/x").json() == {"roles": ["viewer"]}


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------


async def test_ws_gateway_reads_typed_metadata_at_connect_and_per_message() -> None:
    seen: list[tuple[str, tuple[str, ...]]] = []

    class RoleGuard:
        async def can_activate(self, ctx: Any) -> bool:
            seen.append((type(ctx).__name__, ctx.get_metadata(Roles)))
            return True

    @Roles("member")
    @use_guards(RoleGuard)
    @ws_controller("/chat")
    class Chat:
        @on_connect
        async def joined(self, ws: WebSocket) -> None:
            await ws.accept()

        @on_message("admin.kick")
        @use_guards(RoleGuard)
        @Roles("moderator")
        async def kick(self, ws: WebSocket) -> None:
            await ws.send_json({"event": "kicked"})

    async with WsTestClient(_app(Chat)).connect("/chat") as ws:
        await ws.send_json({"event": "admin.kick"})
        assert await asyncio.wait_for(ws.receive_json(), timeout=2) == {"event": "kicked"}

    assert seen == [
        ("WsConnectionContext", ("member",)),
        ("WsMessageContext", ("member", "moderator")),
    ]
