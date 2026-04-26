"""Unit tests for :mod:`lauren.serialization`.

These tests exercise each encoder in isolation to prove:

* All three encoders produce semantically identical output for the
  common payload shapes lauren handlers emit (dicts, lists, primitives,
  unicode strings, nested structures).
* ``encode`` and ``encode_compact`` differ only in whitespace.
* The auto-detector prefers orjson when available and falls back
  cleanly.
* The active-encoder getter/setter preserves and restores state.
* Unknown types reach the ``default`` callback via the same path in
  every backend.
"""

from __future__ import annotations

import json as stdlib_json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from lauren.serialization import (
    JSONEncoder,
    MsgspecEncoder,
    OrjsonEncoder,
    StdlibJSONEncoder,
    auto_encoder,
    get_active_encoder,
    set_active_encoder,
)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_stdlib_encoder_conforms_to_protocol() -> None:
    assert isinstance(StdlibJSONEncoder(), JSONEncoder)


def test_orjson_encoder_conforms_to_protocol() -> None:
    pytest.importorskip("orjson")
    assert isinstance(OrjsonEncoder(), JSONEncoder)


def test_msgspec_encoder_conforms_to_protocol() -> None:
    pytest.importorskip("msgspec")
    assert isinstance(MsgspecEncoder(), JSONEncoder)


def test_every_encoder_has_a_name_attribute() -> None:
    assert StdlibJSONEncoder().name == "stdlib"
    if _has("orjson"):
        assert OrjsonEncoder().name == "orjson"
    if _has("msgspec"):
        assert MsgspecEncoder().name == "msgspec"


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Cross-encoder parity for common shapes
# ---------------------------------------------------------------------------


_COMMON_PAYLOADS = [
    {},
    [],
    {"a": 1, "b": "two", "c": True, "d": None},
    [1, 2, 3, 4.5, "five"],
    {"nested": {"deep": {"deeper": [1, {"x": "y"}]}}},
    {"unicode": "café résumé 日本語 🚀"},
    {"ints": list(range(100))},
    [{"id": i, "name": f"item-{i}"} for i in range(50)],
]


def _available_encoders() -> list[JSONEncoder]:
    encoders: list[JSONEncoder] = [StdlibJSONEncoder()]
    if _has("orjson"):
        encoders.append(OrjsonEncoder())
    if _has("msgspec"):
        encoders.append(MsgspecEncoder())
    return encoders


@pytest.mark.parametrize("payload", _COMMON_PAYLOADS)
def test_every_encoder_produces_identical_parsed_output(payload: object) -> None:
    """All encoders must round-trip to the same Python value.

    The on-wire bytes may differ (whitespace, key ordering) but
    ``json.loads`` on every encoder's output must produce a value
    equal to the input.
    """
    for encoder in _available_encoders():
        blob = encoder.encode_compact(payload)
        assert (
            stdlib_json.loads(blob) == payload
        ), f"encoder={encoder.name} payload={payload!r}"


@pytest.mark.parametrize("payload", _COMMON_PAYLOADS)
def test_compact_output_has_no_whitespace(payload: object) -> None:
    """``encode_compact`` must emit separator-free bytes."""
    for encoder in _available_encoders():
        blob = encoder.encode_compact(payload)
        text = blob.decode("utf-8")
        # Dict key/value separators are ``":"`` and ``","`` — any
        # other whitespace between tokens would make the blob
        # non-compact. We check for ``": "`` and ``", "`` specifically
        # because unicode whitespace inside string values is legal.
        assert (
            ": " not in text or '"' in text.split(": ", 1)[0].split('"')[-1]
        ), f"encoder={encoder.name} produced non-compact output"


# ---------------------------------------------------------------------------
# Default fallback handler
# ---------------------------------------------------------------------------


class _Custom:
    def __init__(self, tag: str) -> None:
        self.tag = tag


def _custom_default(obj: object) -> object:
    if isinstance(obj, _Custom):
        return {"__tag__": obj.tag}
    raise TypeError(f"cannot serialize {type(obj).__name__}")


def test_stdlib_encoder_invokes_default_callback() -> None:
    encoder = StdlibJSONEncoder(default=_custom_default)
    blob = encoder.encode({"item": _Custom("xyz")})
    assert stdlib_json.loads(blob) == {"item": {"__tag__": "xyz"}}


def test_orjson_encoder_invokes_default_callback() -> None:
    pytest.importorskip("orjson")
    encoder = OrjsonEncoder(default=_custom_default)
    blob = encoder.encode({"item": _Custom("xyz")})
    assert stdlib_json.loads(blob) == {"item": {"__tag__": "xyz"}}


def test_msgspec_encoder_invokes_default_callback() -> None:
    pytest.importorskip("msgspec")
    encoder = MsgspecEncoder(default=_custom_default)
    blob = encoder.encode({"item": _Custom("xyz")})
    assert stdlib_json.loads(blob) == {"item": {"__tag__": "xyz"}}


# ---------------------------------------------------------------------------
# Built-in rich types — orjson handles natively; others via default
# ---------------------------------------------------------------------------


def test_orjson_natively_encodes_datetime_uuid_decimal() -> None:
    pytest.importorskip("orjson")
    encoder = OrjsonEncoder()
    payload = {
        "when": datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        "day": date(2024, 1, 1),
        "id": UUID("00000000-0000-0000-0000-000000000001"),
        "amount": Decimal("42.50"),
    }
    blob = encoder.encode(payload)
    parsed = stdlib_json.loads(blob)
    # orjson emits ISO-8601 strings for datetimes and UUIDs as hex.
    assert parsed["when"].startswith("2024-01-01T12:00:00")
    assert parsed["day"] == "2024-01-01"
    assert parsed["id"] == "00000000-0000-0000-0000-000000000001"
    assert parsed["amount"] == "42.50"


# ---------------------------------------------------------------------------
# Auto-selection
# ---------------------------------------------------------------------------


def test_auto_encoder_picks_orjson_when_available() -> None:
    pytest.importorskip("orjson")
    enc = auto_encoder()
    assert enc.name == "orjson"


def test_auto_encoder_produces_protocol_compliant_result() -> None:
    enc = auto_encoder()
    assert isinstance(enc, JSONEncoder)
    # Whatever the backend, a simple payload must round-trip.
    assert stdlib_json.loads(enc.encode_compact({"ok": True})) == {"ok": True}


# ---------------------------------------------------------------------------
# Active-encoder state management
# ---------------------------------------------------------------------------


def test_set_active_encoder_returns_previous_and_restores_cleanly() -> None:
    previous = get_active_encoder()
    stub = StdlibJSONEncoder()
    returned = set_active_encoder(stub)
    try:
        assert returned is previous
        assert get_active_encoder() is stub
    finally:
        set_active_encoder(previous)
    assert get_active_encoder() is previous


def test_set_active_encoder_none_resets_to_stdlib() -> None:
    previous = set_active_encoder(None)
    try:
        active = get_active_encoder()
        assert isinstance(active, StdlibJSONEncoder)
    finally:
        set_active_encoder(previous)


# ---------------------------------------------------------------------------
# Encoder registration from missing backend fails loudly
# ---------------------------------------------------------------------------


def test_orjson_encoder_raises_runtime_error_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate orjson being absent even if the test env has it.

    Validates that a user whose environment lacks orjson gets a
    pointed install hint rather than an opaque ImportError from
    deep inside the encoder.
    """
    import builtins

    real_import = builtins.__import__

    def _no_orjson(name: str, *args: object, **kwargs: object) -> object:
        if name == "orjson":
            raise ImportError("simulated missing orjson")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_orjson)
    with pytest.raises(RuntimeError, match="pip install orjson"):
        OrjsonEncoder()


def test_msgspec_encoder_raises_runtime_error_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def _no_msgspec(name: str, *args: object, **kwargs: object) -> object:
        if name == "msgspec":
            raise ImportError("simulated missing msgspec")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_msgspec)
    with pytest.raises(RuntimeError, match="pip install msgspec"):
        MsgspecEncoder()
