"""Unit tests for :class:`lauren.BroadcastGroup`.

These tests exercise subscribe / unsubscribe / broadcast semantics in
isolation, using a tiny ``_FakeWebSocket`` stand-in so we don't need to
spin up a real ASGI app. Every interesting edge case is covered:

* Idempotent subscribe / unsubscribe.
* ``unsubscribe_all`` cleanly evicts a socket from every group.
* Empty groups are pruned (so ``groups()`` only returns live rooms).
* Broadcast counts reflect only successful sends.
* Broadcast evicts sockets whose ``send_*`` raised so dead members
  don't accumulate.
* ``exclude`` skips the origin socket without affecting the return
  count sent to everyone else.
* ``broadcast(..., as_bytes=True)`` routes to ``send_bytes``.
"""

from __future__ import annotations

import pytest

from lauren import BroadcastGroup


class _FakeWebSocket:
    """Minimal stub matching the subset of :class:`WebSocket` used by
    :class:`BroadcastGroup`. Records sent frames for assertion and can
    be told to fail the next send to simulate a dead connection.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.texts: list[str] = []
        self.bytes_received: list[bytes] = []
        self.json_received: list = []
        self.fail_next = False

    async def send_text(self, text: str) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError(f"simulated dead socket {self.name}")
        self.texts.append(text)

    async def send_bytes(self, data: bytes) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError(f"simulated dead socket {self.name}")
        self.bytes_received.append(data)

    async def send_json(self, payload) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError(f"simulated dead socket {self.name}")
        self.json_received.append(payload)

    def __hash__(self) -> int:
        # Identity-based hashing so sets of sockets deduplicate
        # correctly even when two stubs share the same ``name``.
        return id(self)

    def __eq__(self, other) -> bool:
        return self is other


class TestSubscribeUnsubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_creates_group_on_demand(self):
        bg = BroadcastGroup()
        ws = _FakeWebSocket("a")
        await bg.subscribe("room-1", ws)
        assert bg.groups() == ["room-1"]
        assert bg.member_count("room-1") == 1

    @pytest.mark.asyncio
    async def test_subscribe_is_idempotent(self):
        bg = BroadcastGroup()
        ws = _FakeWebSocket("a")
        await bg.subscribe("room", ws)
        await bg.subscribe("room", ws)
        assert bg.member_count("room") == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_safe_when_not_member(self):
        bg = BroadcastGroup()
        ws = _FakeWebSocket("a")
        # Never joined \u2014 unsubscribe must not raise.
        await bg.unsubscribe("room", ws)
        assert bg.groups() == []

    @pytest.mark.asyncio
    async def test_unsubscribe_prunes_empty_group(self):
        bg = BroadcastGroup()
        ws = _FakeWebSocket("a")
        await bg.subscribe("room", ws)
        await bg.unsubscribe("room", ws)
        assert bg.groups() == []

    @pytest.mark.asyncio
    async def test_unsubscribe_all_removes_from_every_group(self):
        bg = BroadcastGroup()
        ws = _FakeWebSocket("a")
        other = _FakeWebSocket("b")
        await bg.subscribe("r1", ws)
        await bg.subscribe("r2", ws)
        await bg.subscribe("r1", other)
        await bg.unsubscribe_all(ws)
        assert bg.member_count("r1") == 1  # 'other' survives
        assert "r2" not in bg.groups()


class TestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_json_to_all_members(self):
        bg = BroadcastGroup()
        a = _FakeWebSocket("a")
        b = _FakeWebSocket("b")
        c = _FakeWebSocket("c")
        await bg.subscribe("room", a)
        await bg.subscribe("room", b)
        await bg.subscribe("room", c)

        sent = await bg.broadcast("room", {"hello": "world"})
        assert sent == 3
        assert a.json_received == [{"hello": "world"}]
        assert b.json_received == [{"hello": "world"}]
        assert c.json_received == [{"hello": "world"}]

    @pytest.mark.asyncio
    async def test_broadcast_excludes_origin(self):
        bg = BroadcastGroup()
        a = _FakeWebSocket("a")
        b = _FakeWebSocket("b")
        await bg.subscribe("room", a)
        await bg.subscribe("room", b)

        sent = await bg.broadcast("room", {"k": "v"}, exclude=a)
        assert sent == 1
        assert a.json_received == []
        assert b.json_received == [{"k": "v"}]

    @pytest.mark.asyncio
    async def test_broadcast_text_on_string_input(self):
        bg = BroadcastGroup()
        ws = _FakeWebSocket("a")
        await bg.subscribe("room", ws)
        sent = await bg.broadcast("room", "raw string")
        assert sent == 1
        assert ws.texts == ["raw string"]

    @pytest.mark.asyncio
    async def test_broadcast_bytes_mode(self):
        bg = BroadcastGroup()
        ws = _FakeWebSocket("a")
        await bg.subscribe("room", ws)
        sent = await bg.broadcast("room", b"\x00\x01\x02", as_bytes=True)
        assert sent == 1
        assert ws.bytes_received == [b"\x00\x01\x02"]

    @pytest.mark.asyncio
    async def test_broadcast_bytes_requires_bytes_type(self):
        bg = BroadcastGroup()
        ws = _FakeWebSocket("a")
        await bg.subscribe("room", ws)
        # Non-bytes payload with as_bytes=True \u2014 the error is swallowed
        # by the per-socket try/except and the socket is evicted, so
        # the broadcast reports zero successful sends.
        sent = await bg.broadcast("room", {"not": "bytes"}, as_bytes=True)
        assert sent == 0
        assert bg.member_count("room") == 0

    @pytest.mark.asyncio
    async def test_broadcast_evicts_dead_sockets(self):
        bg = BroadcastGroup()
        good = _FakeWebSocket("good")
        bad = _FakeWebSocket("bad")
        bad.fail_next = True
        await bg.subscribe("room", good)
        await bg.subscribe("room", bad)

        sent = await bg.broadcast("room", {"ping": 1})
        assert sent == 1
        # The bad socket is gone; a second broadcast sees only the good one.
        assert bg.member_count("room") == 1
        sent = await bg.broadcast("room", {"ping": 2})
        assert sent == 1
        assert good.json_received == [{"ping": 1}, {"ping": 2}]
        assert bad.json_received == []

    @pytest.mark.asyncio
    async def test_broadcast_zero_members_returns_zero(self):
        bg = BroadcastGroup()
        sent = await bg.broadcast("never-existed", {"x": 1})
        assert sent == 0

    @pytest.mark.asyncio
    async def test_members_snapshot_is_a_copy(self):
        bg = BroadcastGroup()
        ws = _FakeWebSocket("a")
        await bg.subscribe("room", ws)
        snapshot = bg.members("room")
        # Mutating the snapshot must not affect the group.
        snapshot.clear()
        assert bg.member_count("room") == 1
