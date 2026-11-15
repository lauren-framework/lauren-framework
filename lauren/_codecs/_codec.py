"""Body codecs: the :class:`Codec` protocol, :class:`JsonCodec` and :class:`MsgpackCodec`.

A codec turns request bodies of its media types into Python data and
structured return values into response bytes. The JSON codec is always
registered; others are added with ``ContentConfig(codecs=[...])``.
"""

from __future__ import annotations

import json as _stdlib_json
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

#: ``JsonCodec(backend=...)`` values. ``"auto"`` picks msgspec › stdlib.
JSON_BACKENDS: tuple[str, ...] = ("auto", "msgspec", "orjson", "stdlib")


@runtime_checkable
class Codec(Protocol):
    """Decode request bodies and encode responses for a set of media types.

    ``media_types`` lists the concrete media types the codec serves; the first
    one is its canonical response type. ``decode`` raises ``ValueError``
    (or a subclass, e.g. msgspec's ``DecodeError``) on malformed input —
    the framework answers with the malformed-body error (422, or 400 with
    ``ContentConfig(malformed_body_status=400)``). Unexpected exceptions
    propagate as handler failures (500). Implementations must be reentrant:
    one instance serves every request.
    """

    media_types: tuple[str, ...]

    def decode(self, data: bytes) -> Any:
        """Parse a request body into Python data."""

    def encode(self, value: Any) -> bytes:
        """Serialize a JSON-compatible value (dicts, lists, scalars) to bytes."""


def _fast_json_loads(backend: str) -> tuple[str, Callable[[bytes], Any] | None]:
    """``(backend name, loads)`` for a ``JsonCodec`` backend; ``loads`` is ``None`` for stdlib.

    ``"auto"`` never picks orjson: it decodes integers beyond 64 bits as
    floats instead of failing, which the stdlib re-parse cannot catch.
    """
    if backend == "orjson":
        try:
            import orjson
        except ImportError:
            raise RuntimeError(
                "JsonCodec(backend='orjson') requires the 'orjson' package; install it with `pip install orjson`."
            ) from None
        return "orjson", orjson.loads
    if backend in ("auto", "msgspec"):
        try:
            import msgspec

            return "msgspec", msgspec.json.Decoder().decode
        except ImportError:
            if backend == "msgspec":
                raise RuntimeError(
                    "JsonCodec(backend='msgspec') requires the 'msgspec' package; "
                    "install it with `pip install msgspec`."
                ) from None
    return "stdlib", None


class JsonCodec:
    """The JSON codec (``application/json`` and every ``+json`` type).

    ``backend`` picks the decoder: ``"auto"`` (default) uses msgspec when it
    is installed, ``"stdlib"`` always uses :mod:`json`, ``"msgspec"`` /
    ``"orjson"`` require that package. A body the fast parser rejects is
    re-parsed with :mod:`json`, which has the final word — so the accepted
    documents (``NaN``, huge integers, UTF-16 bodies) and the error messages
    do not depend on what is installed. The one exception is opt-in:
    orjson decodes integers beyond 64 bits as floats. With a fast backend,
    pydantic bodies are also validated in one pass (``model_validate_json``).

    ``encode`` uses ``encoder`` (a :class:`~lauren.serialization.JSONEncoder`)
    or the process-wide encoder. Inside an app, JSON responses are always
    written by the app's ``json_encoder`` / ``@use_encoder``.
    """

    media_types: tuple[str, ...] = ("application/json",)

    __slots__ = ("backend", "_loads", "_encoder")

    def __init__(self, *, backend: str = "auto", encoder: Any = None) -> None:
        if backend not in JSON_BACKENDS:
            raise ValueError(
                f"JsonCodec(backend={backend!r}) is invalid; use one of {', '.join(map(repr, JSON_BACKENDS))}"
            )
        #: The resolved backend: ``"orjson"``, ``"msgspec"`` or ``"stdlib"``.
        self.backend, self._loads = _fast_json_loads(backend)
        self._encoder = encoder

    @property
    def fast(self) -> bool:
        """``True`` when a native parser (orjson / msgspec) is in use."""
        return self._loads is not None

    def decode(self, data: bytes) -> Any:
        loads = self._loads
        if loads is not None:
            try:
                return loads(data)
            except ValueError:
                # A native parser may reject JSON the stdlib accepts (NaN,
                # UTF-16, large integers). Only syntax/validation failures
                # fall back; unexpected backend failures must surface.
                pass
        return _stdlib_json.loads(data)

    def encode(self, value: Any) -> bytes:
        encoder = self._encoder
        if encoder is None:
            from ..serialization import get_active_encoder

            encoder = get_active_encoder()
        return bytes(encoder.encode_compact(value))

    def __repr__(self) -> str:
        return f"JsonCodec(backend={self.backend!r})"


class MsgpackCodec:
    """MessagePack (``application/msgpack``), backed by msgspec.

    Requires the ``msgspec`` package. ``msgspec.Struct`` bodies decode and
    validate in one ``msgspec.msgpack.decode``; other targets decode first and
    validate like JSON bodies. Values msgspec cannot encode natively fall back
    to the same rules as JSON responses (pydantic models, dataclasses, …).
    """

    media_types: tuple[str, ...] = ("application/msgpack", "application/x-msgpack", "application/vnd.msgpack")

    __slots__ = ("_msgspec", "_decoder", "_encoder")

    def __init__(self) -> None:
        try:
            import msgspec
        except ImportError as exc:
            raise RuntimeError(
                "MsgpackCodec requires the 'msgspec' package; install it with `pip install msgspec`."
            ) from exc
        from ..serialization import _default_fallback

        self._msgspec = msgspec
        self._decoder = msgspec.msgpack.Decoder()
        self._encoder = msgspec.msgpack.Encoder(enc_hook=_default_fallback)

    def decode(self, data: bytes) -> Any:
        return self._decoder.decode(data)

    def encode(self, value: Any) -> bytes:
        return self._encoder.encode(value)

    def decode_struct(self, data: bytes, struct: type) -> Any:
        """Decode and validate ``data`` as ``struct`` in one pass (raises msgspec errors)."""
        return self._msgspec.msgpack.decode(data, type=struct)

    def __repr__(self) -> str:
        return "MsgpackCodec()"


__all__ = ["JSON_BACKENDS", "Codec", "JsonCodec", "MsgpackCodec"]
