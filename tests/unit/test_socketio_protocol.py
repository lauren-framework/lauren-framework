"""Unit tests for the Engine.IO / Socket.IO codec.

The codec lives in :mod:`lauren._socketio` and is a pure
encoder/decoder pair: no transport, no DI, no asyncio. Testing it in
isolation gives us a tight grammar-level grip on the wire format,
which is the single thing third-party Socket.IO clients depend on.

Round-trip parity is the headline contract: encoding then decoding any
valid value must return an equivalent packet, modulo the obvious
metadata-only differences (e.g. ``EngineIOPacket.inner`` becomes a
:class:`SocketIOPacket` after a second pass).
"""

from __future__ import annotations

import json

import pytest

from lauren._socketio import (
    DEFAULT_NAMESPACE,
    EIO_CLOSE,
    EIO_MESSAGE,
    EIO_OPEN,
    EIO_PING,
    EIO_PONG,
    EngineIOPacket,
    EventRegistry,
    HandshakeConfig,
    SIO_ACK,
    SIO_CONNECT,
    SIO_CONNECT_ERROR,
    SIO_DISCONNECT,
    SIO_EVENT,
    SocketIOPacket,
    SocketIOProtocolError,
    decode_engineio,
    decode_socketio,
    encode_engineio,
    encode_message,
    encode_socketio,
)


# ---------------------------------------------------------------------------
# Engine.IO encoding
# ---------------------------------------------------------------------------


class TestEngineIoEncoding:
    """The outer envelope is just one ASCII digit + optional payload."""

    def test_ping_is_a_single_digit(self):
        # PING/PONG/CLOSE/UPGRADE/NOOP carry no inner payload.
        assert encode_engineio(EngineIOPacket(type=EIO_PING)) == "2"

    def test_pong_is_a_single_digit(self):
        assert encode_engineio(EngineIOPacket(type=EIO_PONG)) == "3"

    def test_close_is_a_single_digit(self):
        assert encode_engineio(EngineIOPacket(type=EIO_CLOSE)) == "1"

    def test_open_carries_handshake_payload(self):
        out = encode_engineio(EngineIOPacket(type=EIO_OPEN, inner='{"sid":"x"}'))
        assert out == '0{"sid":"x"}'

    def test_message_carries_inner_payload(self):
        out = encode_engineio(EngineIOPacket(type=EIO_MESSAGE, inner='2["chat","hi"]'))
        # The inner payload is glued straight on with no separator.
        assert out == '42["chat","hi"]'


# ---------------------------------------------------------------------------
# Engine.IO decoding
# ---------------------------------------------------------------------------


class TestEngineIoDecoding:
    def test_decode_ping(self):
        pkt = decode_engineio("2")
        assert pkt.type == EIO_PING
        assert pkt.inner == ""

    def test_decode_pong(self):
        assert decode_engineio("3").type == EIO_PONG

    def test_decode_close(self):
        assert decode_engineio("1").type == EIO_CLOSE

    def test_decode_open_recovers_payload(self):
        pkt = decode_engineio('0{"sid":"abc","pingInterval":25000}')
        assert pkt.type == EIO_OPEN
        # ``inner`` is the raw JSON string; the high-level adapter
        # parses it via :class:`HandshakeConfig` separately.
        assert json.loads(pkt.inner) == {"sid": "abc", "pingInterval": 25000}

    def test_decode_message_keeps_inner_for_socketio_pass(self):
        pkt = decode_engineio('42["chat",1]')
        assert pkt.type == EIO_MESSAGE
        assert pkt.inner == '2["chat",1]'

    def test_empty_frame_rejected(self):
        # The v4 protocol always has at least the type digit; an empty
        # frame is a sign of transport-layer corruption.
        with pytest.raises(SocketIOProtocolError, match="empty"):
            decode_engineio("")

    def test_unknown_type_rejected(self):
        # Anything outside the ``0..6`` range is unknown.
        with pytest.raises(SocketIOProtocolError, match="unknown"):
            decode_engineio("9foo")


# ---------------------------------------------------------------------------
# Socket.IO encoding
# ---------------------------------------------------------------------------


class TestSocketIoEncoding:
    """The inner payload format covers every realistic packet shape."""

    def test_connect_no_data(self):
        out = encode_socketio(SocketIOPacket(type=SIO_CONNECT))
        assert out == "0"

    def test_connect_with_handshake_data(self):
        out = encode_socketio(SocketIOPacket(type=SIO_CONNECT, data={"sid": "x"}))
        assert out == '0{"sid":"x"}'

    def test_disconnect(self):
        out = encode_socketio(SocketIOPacket(type=SIO_DISCONNECT))
        assert out == "1"

    def test_event_with_args(self):
        out = encode_socketio(SocketIOPacket(type=SIO_EVENT, data=["chat", {"msg": "hi"}]))
        assert out == '2["chat",{"msg":"hi"}]'

    def test_event_with_ack_id(self):
        # Ack ids appear immediately before the JSON payload.
        out = encode_socketio(SocketIOPacket(type=SIO_EVENT, ack_id=7, data=["pong", 1]))
        assert out == '27["pong",1]'

    def test_event_in_namespace(self):
        # Non-default namespaces are wedged in between the type digit
        # and the payload, comma-terminated.
        out = encode_socketio(
            SocketIOPacket(
                type=SIO_EVENT,
                namespace="/admin",
                data=["alert", "fire"],
            )
        )
        assert out == '2/admin,["alert","fire"]'

    def test_ack_packet(self):
        out = encode_socketio(SocketIOPacket(type=SIO_ACK, ack_id=42, data=[{"ok": True}]))
        assert out == '342[{"ok":true}]'

    def test_connect_error(self):
        out = encode_socketio(
            SocketIOPacket(
                type=SIO_CONNECT_ERROR,
                data={"message": "nope"},
            )
        )
        assert out == '4{"message":"nope"}'


# ---------------------------------------------------------------------------
# Socket.IO decoding
# ---------------------------------------------------------------------------


class TestSocketIoDecoding:
    def test_decode_connect_no_data(self):
        pkt = decode_socketio("0")
        assert pkt.type == SIO_CONNECT
        assert pkt.namespace == DEFAULT_NAMESPACE
        assert pkt.ack_id is None
        assert pkt.data is None

    def test_decode_connect_with_data(self):
        pkt = decode_socketio('0{"sid":"x"}')
        assert pkt.type == SIO_CONNECT
        assert pkt.data == {"sid": "x"}

    def test_decode_event(self):
        pkt = decode_socketio('2["chat",{"msg":"hi"}]')
        assert pkt.type == SIO_EVENT
        assert pkt.data == ["chat", {"msg": "hi"}]

    def test_decode_event_with_ack(self):
        pkt = decode_socketio('27["pong",1]')
        assert pkt.type == SIO_EVENT
        assert pkt.ack_id == 7
        assert pkt.data == ["pong", 1]

    def test_decode_event_in_namespace(self):
        pkt = decode_socketio('2/admin,["alert","fire"]')
        assert pkt.namespace == "/admin"
        assert pkt.data == ["alert", "fire"]

    def test_decode_event_namespace_and_ack(self):
        # Namespace + ack id + payload together exercise every cursor
        # transition in the decoder. This is the worst-case shape.
        pkt = decode_socketio('2/admin,5["evt",{"k":1}]')
        assert pkt.namespace == "/admin"
        assert pkt.ack_id == 5
        assert pkt.data == ["evt", {"k": 1}]

    def test_decode_ack(self):
        pkt = decode_socketio('342[{"ok":true}]')
        assert pkt.type == SIO_ACK
        assert pkt.ack_id == 42
        assert pkt.data == [{"ok": True}]

    def test_decode_disconnect_no_data(self):
        pkt = decode_socketio("1")
        assert pkt.type == SIO_DISCONNECT
        assert pkt.data is None

    def test_empty_payload_rejected(self):
        with pytest.raises(SocketIOProtocolError, match="empty"):
            decode_socketio("")

    def test_non_digit_first_char_rejected(self):
        # The grammar is strict about the type byte.
        with pytest.raises(SocketIOProtocolError, match="must be a digit"):
            decode_socketio('"not-a-digit"')

    def test_unknown_type_rejected(self):
        with pytest.raises(SocketIOProtocolError, match="unknown"):
            decode_socketio("9")

    def test_namespace_without_terminator_rejected(self):
        # ``/admin`` with no comma is malformed.
        with pytest.raises(SocketIOProtocolError, match="not terminated"):
            decode_socketio("2/admin")

    def test_invalid_json_rejected(self):
        with pytest.raises(SocketIOProtocolError, match="invalid JSON"):
            decode_socketio("2[broken")


# ---------------------------------------------------------------------------
# encode_message convenience
# ---------------------------------------------------------------------------


class TestEncodeMessageConvenience:
    """``encode_message`` glues the EIO MESSAGE digit on for you."""

    def test_event_via_encode_message(self):
        out = encode_message(SocketIOPacket(type=SIO_EVENT, data=["chat", "hi"]))
        assert out == '42["chat","hi"]'

    def test_event_with_namespace_via_encode_message(self):
        out = encode_message(
            SocketIOPacket(
                type=SIO_EVENT,
                namespace="/admin",
                data=["evt"],
            )
        )
        assert out == '42/admin,["evt"]'


# ---------------------------------------------------------------------------
# Round-trip parity
# ---------------------------------------------------------------------------


class TestRoundTripParity:
    """Encoding then decoding any packet yields an equivalent value."""

    @pytest.mark.parametrize(
        "packet",
        [
            SocketIOPacket(type=SIO_CONNECT),
            SocketIOPacket(type=SIO_CONNECT, data={"sid": "abc"}),
            SocketIOPacket(type=SIO_DISCONNECT),
            SocketIOPacket(type=SIO_EVENT, data=["x"]),
            SocketIOPacket(type=SIO_EVENT, data=["x", 1, 2, {"a": [1, 2]}]),
            SocketIOPacket(type=SIO_EVENT, ack_id=5, data=["evt"]),
            SocketIOPacket(type=SIO_EVENT, namespace="/n", ack_id=12, data=["evt"]),
            SocketIOPacket(type=SIO_ACK, ack_id=1, data=[{"ok": True}]),
            SocketIOPacket(type=SIO_CONNECT_ERROR, data={"message": "denied"}),
        ],
    )
    def test_round_trip(self, packet):
        encoded = encode_socketio(packet)
        decoded = decode_socketio(encoded)
        assert decoded == packet


# ---------------------------------------------------------------------------
# HandshakeConfig
# ---------------------------------------------------------------------------


class TestHandshakeConfig:
    def test_defaults_match_jclient_expectations(self):
        # Defaults are the conventional values shipped by socket.io
        # JS v4 \u2014 changing them would silently change client
        # behaviour on every existing deployment.
        cfg = HandshakeConfig(sid="x")
        assert cfg.ping_interval == 25_000
        assert cfg.ping_timeout == 20_000
        assert cfg.max_payload == 1_000_000

    def test_to_open_payload_round_trips_via_decode(self):
        cfg = HandshakeConfig(
            sid="abc",
            ping_interval=10_000,
            ping_timeout=5_000,
            max_payload=64_000,
        )
        payload = cfg.to_open_payload()
        # The OPEN packet payload is plain JSON; the JS client parses
        # exactly these keys.
        decoded = json.loads(payload)
        assert decoded == {
            "sid": "abc",
            "upgrades": [],
            "pingInterval": 10_000,
            "pingTimeout": 5_000,
            "maxPayload": 64_000,
        }


# ---------------------------------------------------------------------------
# EventRegistry
# ---------------------------------------------------------------------------


class TestEventRegistry:
    """The internal handler-name dispatch table."""

    def test_register_and_lookup(self):
        reg = EventRegistry()

        async def handler():
            return None

        reg.register("chat", handler)
        assert reg.get("chat") is handler

    def test_get_returns_none_for_unknown_event(self):
        # Unknown events are valid: the high-level adapter treats them
        # as no-ops to mirror the JS client's tolerance for unknown
        # listeners.
        assert EventRegistry().get("nope") is None

    def test_duplicate_registration_raises(self):
        reg = EventRegistry()

        async def a():
            return None

        async def b():
            return None

        reg.register("evt", a)
        with pytest.raises(ValueError, match="duplicate"):
            reg.register("evt", b)
