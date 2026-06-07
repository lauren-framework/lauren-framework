"""Pluggable JSON serialization — swap ``json.dumps`` for ``orjson``
or ``msgspec`` without touching any handler code.

Motivation
----------

The stdlib ``json`` module is pure Python and generally 3-10x slower
than its C-accelerated peers on the shapes lauren handlers emit
(dict / list of Pydantic-dumped models). The serializer is hit once
per successful request and once per streamed item, so the cost is
visible under load — particularly on response-heavy APIs.

Design
------

A :class:`JSONEncoder` is a tiny pluggable adapter with two methods:

* :meth:`encode` — serialize a Python value to ``bytes`` ready to
  ship over the wire. Must produce ``application/json``-compliant
  output.
* :meth:`encode_compact` — identical semantics but required to emit
  separator-free output (no whitespace) so wire-size stays tight.
  Defaults to the same as :meth:`encode` when the backend does not
  distinguish.

The framework ships four encoders:

* :class:`StdlibJSONEncoder` — the zero-dependency default.
* :class:`OrjsonEncoder` — the orjson adapter (fastest for dict /
  list payloads; natively handles ``datetime`` / ``UUID`` / ``Decimal``).
* :class:`MsgspecEncoder` — the msgspec adapter (fastest for
  pre-typed payloads via :class:`msgspec.Struct`; also handles
  common extras out of the box).
* :class:`PydanticEncoder` — uses Pydantic v2's Rust-backed
  ``pydantic-core`` serializer for :class:`pydantic.BaseModel` and
  lists of models, skipping the intermediate ``model_dump()`` dict
  and honouring custom ``@field_serializer`` / ``model_config``
  rules.  Falls back to :class:`StdlibJSONEncoder` for non-Pydantic
  values so mixed payloads are handled transparently.

Encoder threading
-----------------

The encoder is passed once to :meth:`LaurenFactory.create` and then
threaded through **every JSON output path** in the application:

* Handler response coercion (``_coerce_to_response``, ``_coerce_streaming_response``)
* HTTP error responses (``_error_response``)
* SSE events (``EventStream._reframe``, ``Response.sse``)
* WebSocket ``send_json`` (``WebSocket._json_encoder``)

Pass it once at factory time and all output paths honour it automatically::

    app = LaurenFactory.create(AppModule, json_encoder=PydanticEncoder())

The active encoder is installed on a :class:`LaurenApp` at startup
and referenced by value (not name) from the hot path. One module-level
global keeps the stdlib fallback available for code paths that run
before a :class:`LaurenApp` has been constructed — primarily the
``Response.json`` factory used by tests that build responses without
an app.

Auto-detection
--------------

:func:`auto_encoder` picks the best available backend in the order
``orjson > msgspec > stdlib``. Callers that want deterministic
behaviour should construct the desired encoder explicitly and pass
it through ``LaurenFactory.create(json_encoder=...)``.

Non-goals
---------

:class:`StdlibJSONEncoder`, :class:`OrjsonEncoder`, and
:class:`MsgspecEncoder` normalise Pydantic models to plain dicts first
(via ``model_dump(mode="json")``), then encode the dict. The
:class:`PydanticEncoder` is the exception — it calls
``model.model_dump_json()`` / ``TypeAdapter.dump_json`` directly so
``pydantic-core`` serialises model → bytes in a single Rust-backed pass,
preserving all ``@field_serializer`` and ``model_config`` rules.
"""

from __future__ import annotations

import json as _stdlib_json
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Default fallback handler — the same permissive rule the framework
# has always used for the stdlib path.
# ---------------------------------------------------------------------------


def _default_fallback(obj: Any) -> Any:
    """Coerce ``obj`` into a JSON-serialisable stand-in.

    The stdlib encoder drops here for any object it doesn't know how
    to serialise. We mirror the behaviour of ``lauren.types._json_default``
    so the adapter layer is a drop-in replacement.
    """
    from .types import _json_default

    return _json_default(obj)


# ---------------------------------------------------------------------------
# Protocol + public alias
# ---------------------------------------------------------------------------


@runtime_checkable
class JSONEncoder(Protocol):
    """Pluggable JSON encoder.

    Implementations MUST be reentrant — the framework calls them from
    many coroutines concurrently. They must NOT mutate the input
    value. The returned ``bytes`` are handed straight to the ASGI
    ``send`` callable, so they need to be UTF-8 encoded (which is a
    superset of ASCII).
    """

    name: str  #: short identifier, e.g. ``"orjson"``

    def encode(self, value: Any) -> bytes:
        """Serialize ``value`` to JSON bytes."""

    def encode_compact(self, value: Any) -> bytes:
        """Serialize ``value`` with no whitespace (``{"a":1}`` not ``{"a": 1}``)."""


# ---------------------------------------------------------------------------
# Stdlib encoder — the default, no third-party deps.
# ---------------------------------------------------------------------------


class StdlibJSONEncoder:
    """The default :class:`JSONEncoder` built on :mod:`json`.

    This is what lauren has always used; keeping it as a first-class
    encoder means users who cannot (or will not) install a native
    dependency keep exactly the same behaviour they had before.
    """

    name = "stdlib"

    __slots__ = ("_default",)

    def __init__(self, default: Any = None) -> None:
        self._default = default or _default_fallback

    def encode(self, value: Any) -> bytes:
        # ``ensure_ascii=False`` is slightly slower but produces the
        # correct UTF-8 byte stream for multilingual content. Matching
        # the behaviour of orjson / msgspec here means all three
        # encoders are byte-equivalent for typical payloads.
        return _stdlib_json.dumps(value, default=self._default, ensure_ascii=False).encode("utf-8")

    def encode_compact(self, value: Any) -> bytes:
        return _stdlib_json.dumps(
            value,
            default=self._default,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")


# ---------------------------------------------------------------------------
# orjson encoder — C-native, 3-10x faster for dict/list payloads.
# ---------------------------------------------------------------------------


class OrjsonEncoder:
    """Encoder backed by the ``orjson`` C extension.

    orjson:

    * returns ``bytes`` directly (no ``.encode('utf-8')`` overhead),
    * natively supports ``datetime`` / ``date`` / ``UUID`` / ``Decimal``
      / numpy / dataclass without a ``default=`` callback,
    * always emits compact output — no whitespace, ever — so
      :meth:`encode` and :meth:`encode_compact` are identical.

    When a value contains an unknown type orjson raises ``TypeError``;
    we retry once through our ``default`` callback so framework users
    who subclass Pydantic models or emit custom types keep working
    unchanged.
    """

    name = "orjson"

    __slots__ = ("_orjson", "_opts", "_default")

    def __init__(self, default: Any = None) -> None:
        try:
            import orjson
        except ImportError as exc:  # pragma: no cover - import guarded in auto_encoder
            raise RuntimeError(
                "OrjsonEncoder requires the 'orjson' package; install it with `pip install orjson`."
            ) from exc
        self._orjson = orjson
        # OPT_NON_STR_KEYS accepts int / float / UUID dict keys without
        # raising — matches pydantic's ``model_dump(mode='json')``
        # behaviour for enum-keyed maps.
        self._opts = orjson.OPT_NON_STR_KEYS
        self._default = default or _default_fallback

    def encode(self, value: Any) -> bytes:
        try:
            return self._orjson.dumps(value, default=self._default, option=self._opts)
        except TypeError:
            # Mirror the stdlib encoder's permissive fallback. orjson
            # raises TypeError for nested shapes it cannot reach via
            # the ``default`` callback (e.g. a model whose ``__dict__``
            # contains opaque handles). Convert to a dict once through
            # our fallback then retry.
            coerced = self._default(value)
            return self._orjson.dumps(coerced, default=self._default, option=self._opts)

    # orjson is always compact.
    encode_compact = encode


# ---------------------------------------------------------------------------
# msgspec encoder — fastest for pre-typed payloads.
# ---------------------------------------------------------------------------


class MsgspecEncoder:
    """Encoder backed by ``msgspec.json.Encoder``.

    msgspec is roughly at parity with orjson for plain Python
    containers, and significantly faster when the handler returns
    :class:`msgspec.Struct` instances directly. It also produces
    compact output by default (no whitespace), so both methods are
    identical.
    """

    name = "msgspec"

    __slots__ = ("_encoder",)

    def __init__(self, default: Any = None) -> None:
        try:
            import msgspec
        except ImportError as exc:  # pragma: no cover - import guarded in auto_encoder
            raise RuntimeError(
                "MsgspecEncoder requires the 'msgspec' package; install it with `pip install msgspec`."
            ) from exc
        # msgspec accepts a ``default=`` callback for unknown types,
        # wired identically to orjson/stdlib.
        self._encoder = msgspec.json.Encoder(enc_hook=default or _default_fallback)

    def encode(self, value: Any) -> bytes:
        return self._encoder.encode(value)

    encode_compact = encode


# ---------------------------------------------------------------------------
# Pydantic encoder — moved to lauren/_encoders/pydantic.py.
# A lazy __getattr__ shim below preserves the import path:
#   from lauren.serialization import PydanticEncoder  # still works
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auto-selection + active-encoder plumbing
# ---------------------------------------------------------------------------


def auto_encoder(default: Any = None) -> JSONEncoder:
    """Pick the best available encoder at import time.

    The selection order mirrors typical performance characteristics:
    orjson first (fastest for the framework's default payload shapes),
    msgspec second (nearly-equivalent, with excellent typed-struct
    support), stdlib as the universal fallback. Callers that care
    about determinism should construct an encoder explicitly.
    """
    try:
        import orjson  # noqa: F401

        return OrjsonEncoder(default=default)
    except ImportError:
        pass
    try:
        import msgspec  # noqa: F401

        return MsgspecEncoder(default=default)
    except ImportError:
        pass
    return StdlibJSONEncoder(default=default)


#: The process-wide fallback encoder. Used by the ``Response.json``
#: factory when no :class:`LaurenApp` is in scope — tests that build
#: responses directly and third-party tooling that calls into the
#: framework without going through ``LaurenFactory.create`` rely on
#: this for backwards compatibility.
_active_encoder: JSONEncoder = StdlibJSONEncoder()


def get_active_encoder() -> JSONEncoder:
    """Return the process-wide default encoder.

    Framework internals should prefer an encoder reference captured
    at app-build time (e.g. ``app._json_encoder``); this accessor is
    the fallback used by contexts that don't have an app handle.
    """
    return _active_encoder


def set_active_encoder(encoder: JSONEncoder | None) -> JSONEncoder:
    """Install ``encoder`` as the process-wide default.

    Passing ``None`` resets the default to the stdlib encoder. Returns
    the previous encoder so callers can restore it (primarily useful
    in tests).
    """
    global _active_encoder
    previous = _active_encoder
    _active_encoder = encoder if encoder is not None else StdlibJSONEncoder()
    return previous


def __getattr__(name: str) -> Any:
    """Lazy import shim so ``from lauren.serialization import PydanticEncoder`` still works."""
    if name == "PydanticEncoder":
        from lauren._encoders.pydantic import PydanticEncoder  # noqa: PLC0415

        return PydanticEncoder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# PydanticEncoder is provided lazily via __getattr__ above.
__all__ = [
    "JSONEncoder",
    "StdlibJSONEncoder",
    "OrjsonEncoder",
    "MsgspecEncoder",
    "PydanticEncoder",  # noqa: F822 — provided lazily via __getattr__
    "auto_encoder",
    "get_active_encoder",
    "set_active_encoder",
]
