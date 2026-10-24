"""Cache codecs and typed decoding.

A :class:`CacheCodec` turns values into the ``bytes`` a store holds and
back. The default :class:`JsonCacheCodec` encodes with the app's JSON
encoder (the one responses use, so models, dataclasses, datetimes … encode
exactly as they would in a response) and decodes with :mod:`json`.

Decoding yields JSON data (``dict`` / ``list`` / scalars). ``type=`` on the
:class:`~lauren.cache.Cache` methods turns that data back into the declared
type through the framework's validation layer (pydantic, msgspec,
dataclass, TypedDict — whichever ``T`` is), including ``list[T]``,
``tuple[T, ...]``, ``dict[str, T]`` and ``T | None``. Data that no longer
validates (the model changed since the entry was written) is a miss.
"""

from __future__ import annotations

import json
import types
import typing
from typing import Any, Callable, Protocol, runtime_checkable

from ..serialization import JSONEncoder, get_active_encoder


@runtime_checkable
class CacheCodec(Protocol):
    """Encode cache values to ``bytes`` and back."""

    def encode(self, value: Any) -> bytes: ...

    def decode(self, data: bytes) -> Any: ...


class JsonCacheCodec:
    """JSON codec: encodes with a :class:`~lauren.serialization.JSONEncoder`, decodes with :mod:`json`.

    ``encoder=None`` uses the app's encoder when ``CacheModule`` builds the
    cache, and the process-wide default encoder otherwise.
    """

    __slots__ = ("_encoder",)

    def __init__(self, encoder: JSONEncoder | None = None) -> None:
        self._encoder = encoder

    @property
    def encoder(self) -> JSONEncoder | None:
        return self._encoder

    def encode(self, value: Any) -> bytes:
        encoder = self._encoder if self._encoder is not None else get_active_encoder()
        return encoder.encode_compact(value)

    def decode(self, data: bytes) -> Any:
        return json.loads(data)


class TypeMismatch(Exception):
    """Cached data does not validate as the requested type (treated as a miss)."""


def _identity(data: Any) -> Any:
    return data


def compile_decoder(tp: Any) -> Callable[[Any], Any]:
    """Build a function that turns decoded JSON data into ``tp`` (raising on mismatch)."""
    if tp is None or tp is Any or tp is object:
        return _identity
    from .._validation import is_json_body_type, validate_as

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        # Before the model check: ``Model | None`` must keep a cached ``None``.
        members = [a for a in args if a is not type(None)]
        inner = compile_decoder(members[0]) if len(members) == 1 else _identity
        allows_none = len(members) != len(args)

        def _union(data: Any) -> Any:
            if data is None and allows_none:
                return None
            return inner(data)

        return _union
    if is_json_body_type(tp):

        def _model(data: Any) -> Any:
            if not isinstance(data, dict):
                raise TypeMismatch(f"expected an object for {tp!r}")
            return validate_as(tp, data, field="cache")

        return _model
    if origin in (list, set, frozenset) and len(args) == 1:
        item = compile_decoder(args[0])
        container = origin

        def _sequence(data: Any) -> Any:
            if not isinstance(data, list):
                raise TypeMismatch(f"expected an array for {tp!r}")
            return container(item(x) for x in data)

        return _sequence
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        item = compile_decoder(args[0])

        def _tuple(data: Any) -> Any:
            if not isinstance(data, list):
                raise TypeMismatch(f"expected an array for {tp!r}")
            return tuple(item(x) for x in data)

        return _tuple
    if origin is dict and len(args) == 2:
        value = compile_decoder(args[1])

        def _mapping(data: Any) -> Any:
            if not isinstance(data, dict):
                raise TypeMismatch(f"expected an object for {tp!r}")
            return {k: value(v) for k, v in data.items()}

        return _mapping
    if tp in (str, int, float, bool):

        def _scalar(data: Any) -> Any:
            if tp is float and isinstance(data, int) and not isinstance(data, bool):
                return float(data)
            if not isinstance(data, tp) or (tp is int and isinstance(data, bool)):
                raise TypeMismatch(f"expected {tp.__name__}, got {type(data).__name__}")
            return data

        return _scalar
    return _identity


__all__ = ["CacheCodec", "JsonCacheCodec", "TypeMismatch", "compile_decoder"]
