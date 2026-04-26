"""Socket.IO / Engine.IO protocol codec (private).

This module is the wire-level layer for the Socket.IO compatibility
adapter. It deliberately knows nothing about lauren's runtime, the
WebSocket transport, or the user-facing controller API — those live in
:mod:`lauren.socketio`. Keeping the codec pure makes it trivial to
unit-test the protocol against fixtures captured from a real JavaScript
``socket.io-client``.

Protocol scope
--------------

We implement the subset that matches modern (v4+) ``socket.io-client``
running over the WebSocket transport, with one caveat: HTTP long-polling
is intentionally out of scope. The adapter requires clients to use
``transport: ['websocket']``, which is the recommended production
configuration anyway.

Engine.IO packet types (the outer envelope)
-------------------------------------------

==============  =====  =====================================================
``OPEN``        ``0``  Server -> client. Carries handshake JSON
                       (``sid``, ``upgrades``, ``pingInterval``,
                       ``pingTimeout``, ``maxPayload``).
``CLOSE``       ``1``  Either direction. Marks transport-level shutdown.
``PING``        ``2``  Either direction. The client's heartbeat in v4.
``PONG``        ``3``  The reply to a ``PING``.
``MESSAGE``     ``4``  Carries a Socket.IO packet (the inner protocol).
``UPGRADE``     ``5``  Long-polling -> WebSocket transport upgrade.
                       Unused in our adapter.
``NOOP``        ``6``  Filler frame used during long-polling. Unused.
==============  =====  =====================================================

Socket.IO packet types (nested inside ``MESSAGE``)
--------------------------------------------------

================  ====  ==================================================
``CONNECT``       ``0`` Client connect to namespace; server replies with
                        the assigned ``sid``.
``DISCONNECT``    ``1`` Namespace-level disconnect. The transport stays
                        open if other namespaces are alive.
``EVENT``         ``2`` ``[event_name, ...args]``. The bread-and-butter
                        of Socket.IO.
``ACK``           ``3`` ``[...args]`` keyed by an integer ``id`` field
                        that follows the packet type.
``CONNECT_ERROR`` ``4`` Server -> client error during ``CONNECT``.
``BINARY_EVENT``  ``5`` Same shape as ``EVENT`` but payload contains
                        binary placeholders. Not implemented (the JS
                        client gracefully degrades).
``BINARY_ACK``    ``6`` Same shape as ``ACK`` for binary payloads.
                        Not implemented.
================  ====  ==================================================

Wire shape examples
-------------------

================================  =================================
On wire                            Meaning
================================  =================================
``0{"sid":"x","pingInterval":...}``  Engine.IO OPEN
``2``                                Engine.IO PING
``3``                                Engine.IO PONG
``40``                               Socket.IO CONNECT (default ns)
``40/admin,``                        Socket.IO CONNECT to namespace
``40{"sid":"y"}``                    Server CONNECT ack with sid
``42["chat",{"msg":"hi"}]``          EVENT \"chat\" with args
``421["ping"]``                      EVENT \"ping\" expecting ack id 1
``431["pong"]``                      ACK id=1 with args
``41``                               Socket.IO DISCONNECT
================================  =================================

Namespaces
----------

When the namespace is non-default, it appears between the packet type
digits and the JSON payload, terminated by a comma::

    42/admin,["broadcast",{"msg":"hi"}]

The codec parses this but the high-level adapter only registers the
default namespace ``\"/\"``. That covers >95% of real-world apps;
multi-namespace support is a clean extension that we'll add when
needed.

References
----------

* `Engine.IO protocol v4
  <https://github.com/socketio/engine.io-protocol/tree/v4>`_
* `Socket.IO protocol v5
  <https://github.com/socketio/socket.io-protocol/tree/v5>`_
"""

from __future__ import annotations

import json as _jsonlib
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------


# Engine.IO packet types. The numeric value IS the wire byte (in ASCII).
EIO_OPEN = "0"
EIO_CLOSE = "1"
EIO_PING = "2"
EIO_PONG = "3"
EIO_MESSAGE = "4"
EIO_UPGRADE = "5"
EIO_NOOP = "6"


# Socket.IO packet types.
SIO_CONNECT = 0
SIO_DISCONNECT = 1
SIO_EVENT = 2
SIO_ACK = 3
SIO_CONNECT_ERROR = 4
SIO_BINARY_EVENT = 5
SIO_BINARY_ACK = 6


#: Default namespace. When the wire form omits a namespace, this is the
#: one in effect. We stay strict about this so a server-side handler
#: registry indexed by namespace remains predictable.
DEFAULT_NAMESPACE = "/"


#: A specific Engine.IO PING heartbeat used by the v4 client. The server
#: must reply with PONG promptly or the client treats the connection as
#: dead.
ENGINE_IO_PING_PROBE = "probe"


# ---------------------------------------------------------------------------
# Decoded packet types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngineIOPacket:
    """A decoded Engine.IO frame.

    The codec produces one of these from each WebSocket text frame.
    For ``MESSAGE`` packets the ``inner`` field holds the body string
    (the Socket.IO encoded payload); for ``OPEN`` it holds the
    handshake JSON; for the heartbeat / control packets it's empty.
    """

    type: str
    inner: str = ""


@dataclass(frozen=True, slots=True)
class SocketIOPacket:
    """A decoded Socket.IO packet (the inner layer of an Engine.IO MESSAGE).

    * ``namespace`` is always set; the default ``"/"`` is filled in for
      packets that omit it on the wire.
    * ``ack_id`` is ``None`` for fire-and-forget events and an integer
      for events expecting an ack callback.
    * ``data`` is the parsed JSON payload (for EVENT/ACK) or the
      handshake dict (for CONNECT). Empty for DISCONNECT.

    The dataclass is frozen because packets flow through asyncio queues;
    immutability prevents accidental cross-coroutine mutation.
    """

    type: int
    namespace: str = DEFAULT_NAMESPACE
    ack_id: int | None = None
    data: Any = None


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def encode_engineio(packet: EngineIOPacket) -> str:
    """Render an :class:`EngineIOPacket` as an Engine.IO text frame.

    The frame is a single ASCII digit (the packet type) optionally
    followed by the inner payload. The format is the *string* form of
    the v4 protocol — that's what you get over the WebSocket transport.
    """
    if not packet.inner:
        return packet.type
    return f"{packet.type}{packet.inner}"


def encode_socketio(packet: SocketIOPacket) -> str:
    """Render a :class:`SocketIOPacket` as the inner string of an Engine.IO MESSAGE.

    Wire shape: ``<type><namespace,><ack_id>?<json_payload>``.

    Examples::

        SocketIOPacket(type=SIO_CONNECT)                    -> "0"
        SocketIOPacket(type=SIO_CONNECT, data={"sid":"x"})  -> '0{"sid":"x"}'
        SocketIOPacket(type=SIO_EVENT, data=["chat", "hi"]) -> '2["chat","hi"]'
        SocketIOPacket(type=SIO_EVENT, ack_id=7,
                       data=["pong", {"x":1}])              -> '27["pong",{"x":1}]'
        SocketIOPacket(type=SIO_EVENT, namespace="/admin",
                       data=["evt", 1])                     -> '2/admin,["evt",1]'
    """
    parts: list[str] = [str(packet.type)]
    if packet.namespace != DEFAULT_NAMESPACE:
        parts.append(packet.namespace)
        parts.append(",")
    if packet.ack_id is not None:
        parts.append(str(packet.ack_id))
    if packet.data is not None:
        parts.append(_jsonlib.dumps(packet.data, separators=(",", ":")))
    return "".join(parts)


def encode_message(packet: SocketIOPacket) -> str:
    """Convenience: produce the full ``4<sio>`` Engine.IO MESSAGE frame."""
    return EIO_MESSAGE + encode_socketio(packet)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


class SocketIOProtocolError(ValueError):
    """Raised when a frame doesn't match the Engine.IO / Socket.IO grammar.

    The high-level adapter catches this, logs the bad frame, and closes
    the connection with the spec close-code 1003 (\"unsupported data\").
    Surfacing a typed error keeps the dispatch path branching tidy and
    lets unit tests assert on grammar-violation modes precisely.
    """


def decode_engineio(frame: str) -> EngineIOPacket:
    """Parse an Engine.IO text frame.

    Empty frames are rejected — the v4 protocol always has at least
    the type digit. We don't try to be defensive about Engine.IO v3 or
    older; lauren's adapter is documented as v4+ only.
    """
    if not frame:
        raise SocketIOProtocolError("empty Engine.IO frame")
    type_char = frame[0]
    if type_char not in {
        EIO_OPEN,
        EIO_CLOSE,
        EIO_PING,
        EIO_PONG,
        EIO_MESSAGE,
        EIO_UPGRADE,
        EIO_NOOP,
    }:
        raise SocketIOProtocolError(f"unknown Engine.IO packet type: {type_char!r}")
    return EngineIOPacket(type=type_char, inner=frame[1:])


def decode_socketio(payload: str) -> SocketIOPacket:
    """Parse the inner string of an Engine.IO ``MESSAGE`` frame.

    Mirrors :func:`encode_socketio`. The grammar is slightly subtle —
    namespaces, ack ids, and JSON payloads are all optional — so we
    walk the string with a tiny state machine instead of a regex
    (which is hard to make correct for ack ids that can be multi-digit
    and may share characters with the JSON ``[``/``{`` delimiters).
    """
    if not payload:
        raise SocketIOProtocolError("empty Socket.IO payload")

    # 1. Packet type — exactly one digit per spec.
    if not payload[0].isdigit():
        raise SocketIOProtocolError(
            f"first char of Socket.IO payload must be a digit, got {payload[0]!r}"
        )
    packet_type = int(payload[0])
    if packet_type not in {
        SIO_CONNECT,
        SIO_DISCONNECT,
        SIO_EVENT,
        SIO_ACK,
        SIO_CONNECT_ERROR,
        SIO_BINARY_EVENT,
        SIO_BINARY_ACK,
    }:
        raise SocketIOProtocolError(f"unknown Socket.IO packet type: {packet_type}")
    cursor = 1

    # 2. Optional namespace, prefixed with ``/`` and terminated by ``,``.
    namespace = DEFAULT_NAMESPACE
    if cursor < len(payload) and payload[cursor] == "/":
        comma = payload.find(",", cursor)
        if comma == -1:
            # ``42/admin`` with no trailing comma — invalid per spec.
            raise SocketIOProtocolError(
                "namespace not terminated by ',' in Socket.IO payload"
            )
        namespace = payload[cursor:comma]
        cursor = comma + 1

    # 3. Optional ack id — one or more digits before the JSON payload.
    ack_id: int | None = None
    if cursor < len(payload) and payload[cursor].isdigit():
        digit_end = cursor
        while digit_end < len(payload) and payload[digit_end].isdigit():
            digit_end += 1
        ack_id = int(payload[cursor:digit_end])
        cursor = digit_end

    # 4. Optional JSON payload — the rest of the string.
    data: Any = None
    if cursor < len(payload):
        rest = payload[cursor:]
        try:
            data = _jsonlib.loads(rest)
        except _jsonlib.JSONDecodeError as exc:
            raise SocketIOProtocolError(
                f"invalid JSON in Socket.IO payload: {exc}"
            ) from exc

    return SocketIOPacket(
        type=packet_type, namespace=namespace, ack_id=ack_id, data=data
    )


# ---------------------------------------------------------------------------
# Engine.IO handshake helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HandshakeConfig:
    """Server-supplied configuration sent in the Engine.IO OPEN packet.

    Mirrors what the JS client expects, with sensible defaults:

    * ``ping_interval`` — how often the server expects a PING (ms).
      The client uses this to schedule its heartbeat; we surface the
      same value to the user-facing API as :attr:`SocketIOConfig`.
    * ``ping_timeout`` — the grace period a side waits for a PONG
      before declaring the link dead.
    * ``max_payload`` — message-size cap. Both directions enforce it.
    """

    sid: str
    ping_interval: int = 25_000
    ping_timeout: int = 20_000
    max_payload: int = 1_000_000
    upgrades: tuple[str, ...] = ()

    def to_open_payload(self) -> str:
        """Return the JSON body of the Engine.IO OPEN packet."""
        return _jsonlib.dumps(
            {
                "sid": self.sid,
                "upgrades": list(self.upgrades),
                "pingInterval": self.ping_interval,
                "pingTimeout": self.ping_timeout,
                "maxPayload": self.max_payload,
            },
            separators=(",", ":"),
        )


# ---------------------------------------------------------------------------
# Per-connection event registry
# ---------------------------------------------------------------------------


@dataclass
class EventRegistry:
    """Maps event names to handler callables for a single gateway class.

    Built once per gateway during decoration, then frozen-by-convention:
    runtime dispatch is pure dict lookup.
    """

    handlers: dict[str, Any] = field(default_factory=dict)

    def register(self, event: str, fn: Any) -> None:
        if event in self.handlers:
            raise ValueError(
                f"duplicate Socket.IO event handler for {event!r} on "
                f"{getattr(fn, '__qualname__', fn)!r}"
            )
        self.handlers[event] = fn

    def get(self, event: str) -> Any | None:
        return self.handlers.get(event)


__all__ = [
    "DEFAULT_NAMESPACE",
    "EIO_CLOSE",
    "EIO_MESSAGE",
    "EIO_NOOP",
    "EIO_OPEN",
    "EIO_PING",
    "EIO_PONG",
    "EIO_UPGRADE",
    "EngineIOPacket",
    "EventRegistry",
    "HandshakeConfig",
    "SIO_ACK",
    "SIO_BINARY_ACK",
    "SIO_BINARY_EVENT",
    "SIO_CONNECT",
    "SIO_CONNECT_ERROR",
    "SIO_DISCONNECT",
    "SIO_EVENT",
    "SocketIOPacket",
    "SocketIOProtocolError",
    "decode_engineio",
    "decode_socketio",
    "encode_engineio",
    "encode_message",
    "encode_socketio",
]
