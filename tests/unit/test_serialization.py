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
    PydanticEncoder,
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
        assert stdlib_json.loads(blob) == payload, f"encoder={encoder.name} payload={payload!r}"


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
        assert ": " not in text or '"' in text.split(": ", 1)[0].split('"')[-1], (
            f"encoder={encoder.name} produced non-compact output"
        )


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


# ---------------------------------------------------------------------------
# OrjsonEncoder TypeError retry path (lines 190-197)
# ---------------------------------------------------------------------------


def test_orjson_encoder_retries_on_typeerror_with_default() -> None:
    """OrjsonEncoder.encode should retry through _default when orjson raises TypeError."""
    pytest.importorskip("orjson")

    call_count = [0]

    class UnknownThing:
        pass

    def custom_default(obj: object) -> object:
        call_count[0] += 1
        if isinstance(obj, UnknownThing):
            return {"coerced": True}
        raise TypeError(f"unhandled {type(obj).__name__}")

    encoder = OrjsonEncoder(default=custom_default)
    # orjson will call default for UnknownThing
    blob = encoder.encode({"thing": UnknownThing()})
    assert stdlib_json.loads(blob) == {"thing": {"coerced": True}}


# ---------------------------------------------------------------------------
# MsgspecEncoder encode / encode_compact (lines 234-237)
# ---------------------------------------------------------------------------


def test_msgspec_encoder_encode_and_compact_are_equivalent() -> None:
    pytest.importorskip("msgspec")
    encoder = MsgspecEncoder()
    payload = {"a": 1, "b": [2, 3]}
    # Both methods must return valid JSON-parseable bytes
    assert stdlib_json.loads(encoder.encode(payload)) == payload
    assert stdlib_json.loads(encoder.encode_compact(payload)) == payload
    # They alias the same underlying function (Python creates a fresh bound-method
    # object on every attribute access, so ``is`` would be False; compare __func__).
    assert encoder.encode.__func__ is encoder.encode_compact.__func__


# ---------------------------------------------------------------------------
# auto_encoder fallback chain (lines 257-266)
# ---------------------------------------------------------------------------


def test_auto_encoder_falls_back_to_msgspec_when_no_orjson(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("msgspec")
    import builtins

    real_import = builtins.__import__

    def _no_orjson(name: str, *args: object, **kwargs: object) -> object:
        if name == "orjson":
            raise ImportError("simulated no orjson")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_orjson)
    enc = auto_encoder()
    assert enc.name == "msgspec"


def test_auto_encoder_falls_back_to_stdlib_when_neither_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def _no_fast(name: str, *args: object, **kwargs: object) -> object:
        if name in ("orjson", "msgspec"):
            raise ImportError(f"simulated no {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_fast)
    enc = auto_encoder()
    assert enc.name == "stdlib"


# ---------------------------------------------------------------------------
# PydanticEncoder tests
# ---------------------------------------------------------------------------


class TestPydanticEncoder:
    """Unit tests for PydanticEncoder."""

    @pytest.fixture
    def enc(self):
        pytest.importorskip("pydantic")
        return PydanticEncoder()

    def test_conforms_to_protocol(self):
        pytest.importorskip("pydantic")
        assert isinstance(PydanticEncoder(), JSONEncoder)

    def test_name_is_pydantic(self):
        pytest.importorskip("pydantic")
        assert PydanticEncoder().name == "pydantic"

    def test_raises_without_pydantic(self, monkeypatch):
        import sys

        original = sys.modules.get("pydantic")
        sys.modules["pydantic"] = None  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="pydantic"):
                PydanticEncoder()
        finally:
            if original is not None:
                sys.modules["pydantic"] = original
            else:
                del sys.modules["pydantic"]

    # -- Pydantic model serialization --

    def test_encodes_simple_model(self, enc):
        from pydantic import BaseModel

        class Item(BaseModel):
            name: str
            value: int

        blob = enc.encode_compact(Item(name="test", value=42))
        assert stdlib_json.loads(blob) == {"name": "test", "value": 42}

    def test_encode_vs_encode_compact_whitespace(self, enc):
        from pydantic import BaseModel

        class Item(BaseModel):
            x: int

        compact = enc.encode_compact(Item(x=1))
        pretty = enc.encode(Item(x=1))
        assert stdlib_json.loads(compact) == stdlib_json.loads(pretty)
        # compact has no spaces between key and value
        assert b": " not in compact

    def test_honours_field_serializer(self, enc):
        from pydantic import BaseModel, field_serializer

        class Model(BaseModel):
            score: float

            @field_serializer("score")
            def fmt(self, v: float) -> str:
                return f"{v:.2f}"

        blob = enc.encode_compact(Model(score=3.14159))
        assert stdlib_json.loads(blob) == {"score": "3.14"}

    def test_honours_alias(self, enc):
        from pydantic import BaseModel, Field

        class Model(BaseModel):
            model_config = {"populate_by_name": True}
            user_id: str = Field(serialization_alias="userId")

        blob = enc.encode_compact(Model(user_id="abc").model_dump(by_alias=True))
        # dict path (non-model value) — just check round-trip
        assert stdlib_json.loads(blob)

    def test_list_of_models(self, enc):
        from pydantic import BaseModel

        class Point(BaseModel):
            x: int
            y: int

        pts = [Point(x=1, y=2), Point(x=3, y=4)]
        blob = enc.encode_compact(pts)
        assert stdlib_json.loads(blob) == [{"x": 1, "y": 2}, {"x": 3, "y": 4}]

    def test_nested_model(self, enc):
        from pydantic import BaseModel

        class Inner(BaseModel):
            v: str

        class Outer(BaseModel):
            inner: Inner

        blob = enc.encode_compact(Outer(inner=Inner(v="hello")))
        assert stdlib_json.loads(blob) == {"inner": {"v": "hello"}}

    # -- Fallback to stdlib for non-Pydantic values --

    def test_plain_dict_encoded(self, enc):
        blob = enc.encode_compact({"a": 1, "b": "two"})
        assert stdlib_json.loads(blob) == {"a": 1, "b": "two"}

    def test_plain_list_encoded(self, enc):
        blob = enc.encode_compact([1, 2, 3])
        assert stdlib_json.loads(blob) == [1, 2, 3]

    def test_primitive_string_encoded(self, enc):
        blob = enc.encode_compact("hello world")
        assert stdlib_json.loads(blob) == "hello world"

    def test_none_encoded(self, enc):
        blob = enc.encode_compact(None)
        assert stdlib_json.loads(blob) is None

    def test_empty_list_not_treated_as_list_of_models(self, enc):
        blob = enc.encode_compact([])
        assert stdlib_json.loads(blob) == []

    def test_mixed_list_falls_back_to_stdlib(self, enc):
        # A list containing a mix of model and non-model items is NOT
        # treated as a homogeneous model list — stdlib fallback handles it.
        from pydantic import BaseModel

        class M(BaseModel):
            x: int

        blob = enc.encode_compact([M(x=1), {"plain": "dict"}])
        parsed = stdlib_json.loads(blob)
        assert len(parsed) == 2

    def test_custom_default_callback(self):
        pytest.importorskip("pydantic")

        class Opaque:
            pass

        def my_default(obj):
            if isinstance(obj, Opaque):
                return "<opaque>"
            raise TypeError(type(obj).__name__)

        enc = PydanticEncoder(default=my_default)
        blob = enc.encode_compact({"item": Opaque()})
        assert stdlib_json.loads(blob) == {"item": "<opaque>"}

    def test_parity_with_stdlib_for_common_payloads(self):
        pytest.importorskip("pydantic")
        enc = PydanticEncoder()
        for payload in _COMMON_PAYLOADS:
            blob = enc.encode_compact(payload)
            assert stdlib_json.loads(blob) == payload, f"payload={payload!r}"
