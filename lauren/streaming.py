"""Structured streaming primitives — :class:`Stream` and :class:`StreamingResponse`.

Lauren treats streaming as a first-class citizen rather than a string/bytes
escape hatch. Two building blocks live here:

* :class:`Stream` — an **inbound** extractor. Declaring
  ``audio: Stream[AudioChunk]`` turns the ASGI ``receive`` loop into a typed
  async iterator. Each inbound chunk is decoded according to the request's
  ``Content-Type`` (SSE, NDJSON, JSON Lines by default) and validated against
  the inner model. Invalid payloads raise :class:`~lauren.exceptions.ExtractorError`
  with the offending line and variant attached.

* :class:`StreamingResponse` — an **outbound** return-type marker. A handler
  annotated with ``-> StreamingResponse[Transcript]`` returns an
  :class:`AsyncIterable` of ``Transcript`` instances; the runtime negotiates
  the wire format from the request's ``Accept`` header
  (``text/event-stream`` → SSE, ``application/x-ndjson`` → NDJSON,
  ``application/json+stream`` or ``*/*`` → JSON Lines) and serializes each
  item accordingly. The ``response_model`` OpenAPI metadata still feeds
  schema generation, now with an ``x-streaming: true`` extension.

The two primitives are symmetrical on purpose — the same ``Accept``/``Content-
Type`` vocabulary is honoured in both directions, so an LLM-style bidirectional
handler can simply ``async for`` on the inbound ``Stream[T]`` and ``yield``
``U`` instances on the outbound side without any manual framing.
"""

from __future__ import annotations

import inspect as _inspect
import json as jsonlib
from dataclasses import dataclass
from typing import (
    Annotated,
    Any,
    AsyncIterable,
    Generic,
    TypeVar,
    get_args,
    get_origin,
)

from .exceptions import ExtractorError
from .extractors import ExtractionMarker

try:
    import pydantic

    _PYDANTIC_AVAILABLE = True
    _BaseModel = pydantic.BaseModel
    _TypeAdapter = pydantic.TypeAdapter
except ImportError:  # pragma: no cover
    _PYDANTIC_AVAILABLE = False
    _BaseModel = None  # type: ignore[assignment,misc]
    _TypeAdapter = None  # type: ignore[assignment,misc]


T = TypeVar("T")


# ---------------------------------------------------------------------------
# Wire-format vocabulary — shared by the inbound Stream and outbound
# StreamingResponse paths so the two directions speak the same protocol.
# ---------------------------------------------------------------------------


#: Canonical stream wire formats, ordered by preference when the client's
#: ``Accept`` (or ``Content-Type`` for inbound) contains ``*/*`` or nothing.
STREAM_FORMATS = ("jsonlines", "ndjson", "sse")

#: Media types that identify each wire format, used for both negotiation
#: (request-side) and response ``Content-Type`` emission.
FORMAT_TO_MEDIA_TYPE: dict[str, str] = {
    "sse": "text/event-stream",
    "ndjson": "application/x-ndjson",
    "jsonlines": "application/json+stream",
}

#: Reverse lookup — map a media type string to a canonical format token.
#: Several synonyms are accepted so clients don't need to know our exact
#: spelling (e.g. ``application/jsonl`` or ``application/x-jsonlines`` both
#: resolve to ``"jsonlines"``).
MEDIA_TYPE_TO_FORMAT: dict[str, str] = {
    "text/event-stream": "sse",
    "application/x-ndjson": "ndjson",
    "application/ndjson": "ndjson",
    "application/json+stream": "jsonlines",
    "application/jsonl": "jsonlines",
    "application/x-jsonlines": "jsonlines",
}


def negotiate_stream_format(
    accept_or_content_type: str | None,
    *,
    default: str = "jsonlines",
) -> str:
    """Pick a canonical wire format from an ``Accept`` / ``Content-Type`` header.

    The header is parsed as a simple comma-separated list (quality values are
    intentionally ignored — streaming clients rarely use ``q=``, and the
    first match wins is the standard negotiation Rust/Axum-style frameworks
    apply). If no known token is present, ``default`` is returned; an empty
    or missing header is treated as ``*/*`` and also returns the default.
    """
    if not accept_or_content_type:
        return default
    # Simple left-to-right scan: pick the first token whose media type (or
    # an accepted synonym) we know. Parameters (``; charset=utf-8``) are
    # stripped before comparison so we tolerate clients that decorate the
    # type with charset or boundary info.
    for part in accept_or_content_type.split(","):
        media = part.split(";", 1)[0].strip().lower()
        if media in MEDIA_TYPE_TO_FORMAT:
            return MEDIA_TYPE_TO_FORMAT[media]
        if media == "*/*":
            return default
    return default


# ---------------------------------------------------------------------------
# Stream — inbound extractor marker.
# ---------------------------------------------------------------------------


class Stream(ExtractionMarker):
    """Inbound streaming extractor.

    Usage::

        @post("/transcribe")
        async def transcribe(self, audio: Stream[AudioChunk]) -> ...:
            async for chunk in audio:
                ...  # chunk is a validated AudioChunk

    The framework reads the ASGI receive loop directly, so inbound chunks
    are delivered one at a time without the whole body being buffered first.
    Each chunk's payload is decoded according to the request's
    ``Content-Type`` (one of the media types in :data:`MEDIA_TYPE_TO_FORMAT`;
    JSON Lines is the default) and validated against the inner type.

    ``reads_body`` is set because the extractor consumes the ASGI receive
    loop; it is incompatible with :class:`~lauren.Json` / :class:`~lauren.Form`
    / :class:`~lauren.Bytes` on the same handler — the handler signature
    compiler rejects that combination at startup.
    """

    source = "stream"
    reads_body = True

    @classmethod
    async def extract(
        cls,
        request: Any,
        extraction: Any,
        *,
        container: Any = None,
        request_cache: Any = None,
        owning_module: Any = None,
    ) -> "StreamReader[Any]":
        """Build a :class:`StreamReader` bound to the request's receive loop."""
        inner = extraction.inner_type
        content_type = request.headers.get("content-type") or ""
        fmt = negotiate_stream_format(content_type, default="jsonlines")
        return StreamReader(
            request=request,
            inner_type=inner,
            format=fmt,
            field_name=extraction.name,
        )


class StreamReader(Generic[T]):
    """Async iterator producing validated ``T`` values from a streaming body.

    Not directly constructed by user code — lauren creates one for each
    ``Stream[T]`` extractor. It is a thin bridge between the ASGI receive
    callable and the handler's ``async for`` loop: every inbound message is
    buffered into a line accumulator, complete lines are decoded using the
    negotiated wire format, and each decoded payload is validated against
    ``T`` (supporting both plain Pydantic models and ``Annotated[Union[...],
    Field(discriminator=...)]`` tagged unions via :class:`pydantic.TypeAdapter`).
    """

    __slots__ = (
        "_request",
        "_inner_type",
        "_format",
        "_field_name",
        "_buffer",
        "_done",
        "_adapter",
    )

    def __init__(
        self,
        *,
        request: Any,
        inner_type: Any,
        format: str,
        field_name: str,
    ) -> None:
        self._request = request
        self._inner_type = inner_type
        self._format = format
        self._field_name = field_name
        self._buffer = b""
        self._done = False
        # Build a TypeAdapter eagerly so validation errors surface
        # immediately when the first chunk arrives (and so we don't pay the
        # construction cost per-chunk).
        self._adapter = _build_adapter(inner_type) if _PYDANTIC_AVAILABLE else None

    @property
    def format(self) -> str:
        """The canonical wire format negotiated from the request's Content-Type."""
        return self._format

    @property
    def inner_type(self) -> Any:
        return self._inner_type

    def __aiter__(self) -> "StreamReader[T]":
        return self

    async def __anext__(self) -> T:
        while True:
            line = self._pop_line()
            if line is not None:
                return self._decode_and_validate(line)
            if self._done:
                raise StopAsyncIteration
            msg = await self._request._receive()
            mtype = msg.get("type")
            if mtype == "http.disconnect":
                self._done = True
                # Flush any trailing fragment as a final record.
                if self._buffer.strip():
                    line = self._buffer
                    self._buffer = b""
                    return self._decode_and_validate(line)
                raise StopAsyncIteration
            if mtype == "http.request":
                chunk = msg.get("body", b"") or b""
                if chunk:
                    self._buffer += chunk
                if not msg.get("more_body", False):
                    self._done = True
                    # Drain full lines first; any trailing partial line is
                    # still yielded on the next loop iteration via
                    # ``_pop_line`` or the disconnect branch above.
                    continue
                continue
            # Unknown message type — keep waiting; ASGI servers generally
            # only emit http.request / http.disconnect on the request side.

    def _pop_line(self) -> bytes | None:
        """Return the next complete framed record, or ``None`` if incomplete.

        SSE frames end with a blank line (``\\n\\n``); NDJSON and JSON Lines
        both frame on a single ``\\n``. The SSE parser consumes the whole
        block and extracts the ``data:`` payload to preserve back-compat
        with existing SSE clients.
        """
        if self._format == "sse":
            sep = b"\n\n"
            idx = self._buffer.find(sep)
            if idx < 0:
                return None
            block, self._buffer = self._buffer[:idx], self._buffer[idx + len(sep) :]
            return _sse_extract_data(block)
        # NDJSON / JSON Lines — frame on newline.
        idx = self._buffer.find(b"\n")
        if idx < 0:
            return None
        line, self._buffer = self._buffer[:idx], self._buffer[idx + 1 :]
        return line

    def _decode_and_validate(self, line: bytes) -> Any:
        stripped = line.strip()
        if not stripped:
            # Blank line between records is legal in all three formats —
            # just recurse on the next record.
            return self.__anext__().__await__().__next__()  # type: ignore[attr-defined]
        try:
            data = jsonlib.loads(stripped)
        except jsonlib.JSONDecodeError as e:
            raise ExtractorError(
                f"invalid JSON in streaming body: {e}",
                detail={
                    "field": self._field_name,
                    "format": self._format,
                    "fragment": stripped[:120].decode("utf-8", errors="replace"),
                },
            ) from e
        if self._adapter is None:
            return data
        try:
            return self._adapter.validate_python(data)
        except pydantic.ValidationError as e:  # type: ignore[union-attr]
            raise ExtractorError(
                "validation error in streaming body",
                detail={
                    "field": self._field_name,
                    "format": self._format,
                    "errors": e.errors(),
                },
            ) from e


def _sse_extract_data(block: bytes) -> bytes:
    """Extract the concatenated ``data:`` payload from one SSE event block."""
    pieces: list[bytes] = []
    for raw_line in block.split(b"\n"):
        if raw_line.startswith(b"data:"):
            pieces.append(raw_line[5:].lstrip())
        # Ignore ``event:`` / ``id:`` / ``retry:`` / comments — they don't
        # carry the payload. A user who needs that metadata can subscribe
        # to a lower-level API; the typed Stream contract deliberately
        # focuses on the payload only.
    if not pieces:
        return b""
    return b"\n".join(pieces)


# ---------------------------------------------------------------------------
# StreamingResponse — outbound return-type wrapper.
# ---------------------------------------------------------------------------


class _StreamingResponseMeta(type):
    """Metaclass that makes ``StreamingResponse[T]`` return an Annotated alias.

    This mirrors how :class:`ExtractionMarker` produces ``Annotated[T, Marker]``
    so the runtime can detect the return-type marker on a handler without
    requiring a wrapper at call time.
    """

    def __getitem__(cls, item: Any) -> Any:
        return Annotated[AsyncIterable[item], _StreamingMarker(item)]


@dataclass(frozen=True)
class _StreamingMarker:
    """Metadata carried inside ``StreamingResponse[T]`` return annotations.

    Holds the item type ``T`` so the runtime can look up a
    :class:`pydantic.TypeAdapter` to serialize each yielded value and so the
    OpenAPI generator can reference the proper ``$ref`` in the
    ``x-streaming`` response schema.
    """

    item_type: Any


class StreamingResponse(metaclass=_StreamingResponseMeta):
    """Return-type marker for typed streaming responses.

    ``-> StreamingResponse[Transcript]`` tells lauren that the handler will
    return an :class:`AsyncIterable` (typically via ``async def produce():
    ... yield``) of ``Transcript`` values, which the runtime serializes
    according to the request's ``Accept`` header. The negotiation vocabulary
    matches the inbound :class:`Stream` — SSE, NDJSON, and JSON Lines.

    Users should not instantiate this class. ``StreamingResponse[T]`` exists
    solely as a type-annotation alias built by :class:`_StreamingResponseMeta`.
    """


def extract_streaming_item_type(annotation: Any) -> Any | None:
    """Return the ``T`` from a ``StreamingResponse[T]`` annotation, or ``None``.

    Used by both the response-coercion layer (to pick the right serializer)
    and the OpenAPI generator (to emit ``x-streaming`` + the item ``$ref``).
    """
    if annotation is _inspect.Parameter.empty or annotation is None:
        return None
    origin = get_origin(annotation)
    if origin is Annotated or hasattr(annotation, "__metadata__"):
        for extra in get_args(annotation)[1:]:
            if isinstance(extra, _StreamingMarker):
                return extra.item_type
    return None


# ---------------------------------------------------------------------------
# TypeAdapter construction — shared between Stream, Json discriminated
# unions, and response_model serialization so the validation semantics are
# identical in every direction.
# ---------------------------------------------------------------------------


_ADAPTER_CACHE: dict[int, Any] = {}


def _build_adapter(target: Any) -> Any:
    """Return a cached :class:`pydantic.TypeAdapter` for ``target``.

    Caching is keyed on ``id(target)`` because Pydantic type adapters are
    immutable once built and constructing them is non-trivial for
    discriminated unions (Pydantic walks every variant to assemble the tag
    map). The cache is process-wide and never invalidated — safe because
    application types are defined at import time and never change.
    """
    if not _PYDANTIC_AVAILABLE:
        return None
    key = id(target)
    cached = _ADAPTER_CACHE.get(key)
    if cached is not None:
        return cached
    adapter = _TypeAdapter(target)
    _ADAPTER_CACHE[key] = adapter
    return adapter


def is_discriminated_union(target: Any) -> bool:
    """Return True iff ``target`` is ``Annotated[Union[...], Field(discriminator=...)]``.

    Used by the JSON extractor (feature 6) to route validation through a
    :class:`pydantic.TypeAdapter` rather than :meth:`BaseModel.model_validate`,
    and by the OpenAPI generator to emit ``oneOf`` + ``discriminator.mapping``.
    """
    if not _PYDANTIC_AVAILABLE:
        return False
    origin = get_origin(target)
    if origin is not Annotated and not hasattr(target, "__metadata__"):
        return False
    args = get_args(target)
    if not args:
        return False
    inner = args[0]
    inner_origin = get_origin(inner)
    # Accept both typing.Union[...] and PEP 604 ``X | Y`` syntaxes.
    import types as _types

    union_types = {Any}
    try:
        import typing as _typing

        union_types.add(_typing.Union)  # type: ignore[arg-type]
    except Exception:  # pragma: no cover
        pass
    try:
        union_types.add(_types.UnionType)  # type: ignore[arg-type]
    except AttributeError:  # pragma: no cover - py<3.10
        pass
    if inner_origin not in union_types and inner_origin is not getattr(
        _types, "UnionType", None
    ):
        # Try harder — pydantic's tagged unions often come through as
        # Union specialisations whose ``get_origin`` is ``typing.Union``.
        if str(inner_origin) not in ("typing.Union", "types.UnionType"):
            return False
    for extra in args[1:]:
        if _is_discriminator_field(extra):
            return True
    return False


def _is_discriminator_field(obj: Any) -> bool:
    """Heuristically recognize a ``pydantic.Field(discriminator=...)``.

    Pydantic v2 represents this as a :class:`pydantic.fields.FieldInfo`
    whose ``discriminator`` attribute is truthy. We avoid importing
    ``FieldInfo`` at module load so the module stays importable when
    pydantic is missing (the dev-only code path) — hence the attribute
    probe rather than ``isinstance``.
    """
    disc = getattr(obj, "discriminator", None)
    return isinstance(disc, str) and bool(disc)


def discriminator_key(target: Any) -> str | None:
    """Return the tag field name for a discriminated-union ``target``."""
    args = get_args(target)
    for extra in args[1:]:
        if _is_discriminator_field(extra):
            return extra.discriminator  # type: ignore[no-any-return]
    return None


def discriminator_variants(target: Any) -> tuple[type, ...]:
    """Return the variant model classes inside a discriminated union."""
    args = get_args(target)
    if not args:
        return ()
    inner = args[0]
    variants = tuple(a for a in get_args(inner) if isinstance(a, type))
    return variants


__all__ = [
    "Stream",
    "StreamReader",
    "StreamingResponse",
    "STREAM_FORMATS",
    "FORMAT_TO_MEDIA_TYPE",
    "MEDIA_TYPE_TO_FORMAT",
    "negotiate_stream_format",
    "extract_streaming_item_type",
    "is_discriminated_union",
    "discriminator_key",
    "discriminator_variants",
    "_build_adapter",
]
