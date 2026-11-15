"""Startup-compiled content handling: codec registry, content gate, body decoding, negotiation.

``LaurenFactory.create`` resolves ``ContentConfig`` once
(:func:`resolve_content`) and compiles every route's content rules in Phase 5
(:func:`compile_route_content`):

* a :class:`ContentGate` when the route checks ``Content-Type`` (declared
  ``consumes=``, an ``UploadFile`` parameter, or a JSON / form body under
  ``strict_content_type``);
* a :class:`BodyDecoding` on every JSON-body plan — the codec that decodes
  the body, chosen from ``Content-Type`` only when the route consumes more
  than JSON;
* a :class:`Negotiation` when the route's responses are not plain JSON
  (several ``produces`` entries, a non-JSON one, or ``strict_accept``);
* the effective ``max_body_size``.

Routes that keep the defaults get ``None`` everywhere except the shared JSON
decoding, so the request path pays one attribute check per feature.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from .._forms._config import MultipartConfig, resolve_multipart
from ..exceptions import NotAcceptableError, StartupError
from ..types import ContentConfig, Headers, Response
from ._accept import negotiate
from ._codec import JsonCodec
from ._media import (
    is_json_media,
    is_wildcard,
    matches,
    media_type_of,
    normalize_media_range,
    split_media_type,
)

#: What a JSON body parameter accepts when no ``consumes=`` is declared.
JSON_MEDIA_TYPES: tuple[str, ...] = ("application/json", "application/*+json")
#: The form runtime accepts both URL-encoded fields and multipart text/files.
FORM_MEDIA_TYPES: tuple[str, ...] = ("application/x-www-form-urlencoded", "multipart/form-data")
#: What an ``UploadFile`` parameter accepts.
MULTIPART_MEDIA_TYPES: tuple[str, ...] = ("multipart/form-data",)
#: ``ContentConfig(malformed_body_status=...)`` values.
MALFORMED_BODY_STATUSES: tuple[int, ...] = (422, 400)
#: Extraction sources that read the request body.
_BODY_SOURCES = frozenset({"json", "form", "bytes", "byte_stream", "upload_file", "stream"})


def _invalid(message: str, **detail: Any) -> StartupError:
    return StartupError(message, detail={"reason": "invalid_content_config", **detail})


def _codec_name(codec: Any) -> str:
    return type(codec).__qualname__


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class CodecRegistry:
    """The app's codecs by media type; the JSON codec is always present."""

    __slots__ = ("json", "_by_media")

    def __init__(self, codecs: Iterable[Any]) -> None:
        by_media: dict[str, Any] = {}
        json_codec: JsonCodec | None = None
        for codec in codecs:
            media_types = getattr(codec, "media_types", None)
            if (
                not callable(getattr(codec, "decode", None))
                or not callable(getattr(codec, "encode", None))
                or isinstance(media_types, str)
                or not isinstance(media_types, Sequence)
                or not media_types
            ):
                raise _invalid(
                    f"ContentConfig(codecs=...) entries must implement the Codec protocol "
                    f"(media_types, decode, encode); got {_codec_name(codec)}",
                    codec=_codec_name(codec),
                )
            for raw in media_types:
                try:
                    media = normalize_media_range(raw, wildcards=False)
                except ValueError as exc:
                    raise _invalid(f"codec {_codec_name(codec)}: {exc}", codec=_codec_name(codec)) from None
                claimed = by_media.get(media)
                if claimed is None and media == "application/json" and not isinstance(codec, JsonCodec):
                    claimed = "the built-in JsonCodec"
                if claimed is not None:
                    other = claimed if isinstance(claimed, str) else _codec_name(claimed)
                    raise StartupError(
                        f"media type {media!r} is claimed by both {other} and {_codec_name(codec)}",
                        detail={
                            "reason": "codec_conflict",
                            "media_type": media,
                            "codecs": [other, _codec_name(codec)],
                        },
                    )
                by_media[media] = codec
            if isinstance(codec, JsonCodec):
                json_codec = codec
        if json_codec is None:
            json_codec = JsonCodec()
            by_media["application/json"] = json_codec
        self.json: JsonCodec = json_codec
        self._by_media = by_media

    def lookup(self, media: str) -> Any:
        """The codec serving a concrete media type (``+json`` types fall back to JSON)."""
        codec = self._by_media.get(media)
        if codec is None and is_json_media(media):
            return self.json
        return codec

    def media_types(self) -> tuple[str, ...]:
        return tuple(self._by_media)


# ---------------------------------------------------------------------------
# App-level configuration
# ---------------------------------------------------------------------------


class ContentRuntime:
    """``ContentConfig`` resolved for one app."""

    __slots__ = (
        "strict",
        "declared_strict",
        "malformed_status",
        "registry",
        "default_produces",
        "strict_accept",
        "json_decoding",
        "multipart",
    )

    def __init__(
        self,
        *,
        strict: bool | str,
        declared_strict: bool | str | None,
        malformed_status: int,
        registry: CodecRegistry,
        default_produces: tuple[str, ...],
        strict_accept: bool,
        multipart: MultipartConfig | None = None,
    ) -> None:
        #: Effective ``strict_content_type``: ``True``, ``"warn"`` or ``False``.
        self.strict = strict
        #: The value the app set (``None``: left to the framework default).
        self.declared_strict = declared_strict
        self.malformed_status = malformed_status
        self.registry = registry
        self.default_produces = default_produces
        self.strict_accept = strict_accept
        #: The decoding shared by every route that consumes JSON alone.
        self.json_decoding = BodyDecoding(registry, malformed_status=malformed_status)
        self.multipart = resolve_multipart(multipart)


def resolve_content(content: Any, *, sessions: bool) -> ContentRuntime:
    """Validate ``LaurenFactory.create(content=...)``; raise ``StartupError`` on misuse."""
    config = content if content is not None else ContentConfig()
    if not isinstance(config, ContentConfig):
        raise StartupError(
            f"content= expects a ContentConfig, got {type(content).__name__}",
            detail={"reason": "invalid_content_config", "got": type(content).__name__},
        )
    declared = config.strict_content_type
    if not (declared is None or isinstance(declared, bool) or declared == "warn"):
        raise _invalid(
            f'ContentConfig(strict_content_type={declared!r}) is invalid; use True, False, "warn" or None',
            strict_content_type=repr(declared),
        )
    status = config.malformed_body_status
    if isinstance(status, bool) or status not in MALFORMED_BODY_STATUSES:
        raise _invalid(
            f"ContentConfig(malformed_body_status={status!r}) is invalid; use 422 or 400",
            malformed_body_status=repr(status),
        )
    if not isinstance(config.strict_accept, bool):
        raise _invalid(
            f"ContentConfig(strict_accept={config.strict_accept!r}) must be a bool",
            strict_accept=repr(config.strict_accept),
        )
    if isinstance(config.codecs, (str, bytes)) or not isinstance(config.codecs, Sequence):
        raise _invalid(
            "ContentConfig(codecs=...) must be a list of Codec instances", codecs=type(config.codecs).__name__
        )
    registry = CodecRegistry(config.codecs)
    produces = config.default_produces
    if isinstance(produces, str) or not isinstance(produces, Sequence) or not produces:
        raise _invalid(
            "ContentConfig(default_produces=...) must be a non-empty sequence of media types",
            default_produces=repr(produces),
        )
    normalized: list[str] = []
    for entry in produces:
        try:
            media = normalize_media_range(entry, wildcards=False)
        except ValueError as exc:
            raise _invalid(
                f"ContentConfig(default_produces=...): {exc}", default_produces=repr(produces)
            ) from None
        if registry.lookup(media) is None:
            raise StartupError(
                f"ContentConfig(default_produces=...) names {media!r} but no codec serves it; "
                "add one to ContentConfig(codecs=[...])",
                detail={"reason": "codec_not_registered", "media_type": media, "route": None},
            )
        if media not in normalized:
            normalized.append(media)
    strict: bool | str = declared if declared is not None else ("warn" if sessions else False)
    return ContentRuntime(
        strict=strict,
        declared_strict=declared,
        malformed_status=status,
        registry=registry,
        default_produces=tuple(normalized),
        strict_accept=config.strict_accept,
        multipart=config.multipart,
    )


# ---------------------------------------------------------------------------
# Content-Type gate
# ---------------------------------------------------------------------------


class ContentGate:
    """A route's ``Content-Type`` check, run after guards and before the body is read."""

    __slots__ = (
        "supported",
        "strict",
        "label",
        "allow_header",
        "_exact",
        "_ranges",
        "_registry",
        "_boundary",
    )

    def __init__(
        self,
        supported: tuple[str, ...],
        *,
        strict: bool,
        label: str,
        method: str,
        registry: CodecRegistry | None = None,
        boundary: bool = False,
    ) -> None:
        #: The accepted media types / ranges, as reported in a 415 ``detail``.
        self.supported = supported
        #: ``True``: reject with 415; ``False``: accept and log ("warn").
        self.strict = strict
        self.label = label
        #: ``Accept-Post`` / ``Accept-Patch`` advertised on a 415 (``None``: other methods).
        self.allow_header = {"POST": "accept-post", "PATCH": "accept-patch"}.get(method)
        self._exact = frozenset(m for m in supported if not is_wildcard(m))
        self._ranges = tuple(m for m in supported if is_wildcard(m))
        # A typed body must have a decoder. Wildcard ``consumes`` ranges may
        # cover unregistered types, but they cannot silently fall back to JSON.
        self._registry = registry
        self._boundary = boundary

    def accepts(self, media: str) -> bool:
        return (media in self._exact or any(matches(r, media) for r in self._ranges)) and (
            self._registry is None or self._registry.lookup(media) is not None
        )

    def problem(self, headers: Headers) -> str | None:
        """Why the request may not reach the route's body parameter, or ``None``.

        A missing ``Content-Type`` is only a problem when the request announces
        a body (``Content-Length`` above zero, or ``Transfer-Encoding``); an
        empty body falls through to the parameter's own "missing body" handling.
        JSON types must be UTF-8 (RFC 8259 §8.1) when a charset is given.
        """
        raw = headers.get("content-type")
        if raw is None or not raw.strip():
            if headers.get("transfer-encoding") is not None:
                return "no Content-Type"
            length = headers.get("content-length")
            if length is None:
                return None
            length = length.strip()
            if length.isascii() and length.isdigit():
                return "no Content-Type" if any(char != "0" for char in length) else None
            return "no Content-Type"
        media, params = split_media_type(raw)
        if not self.accepts(media):
            return media
        if self._boundary and not params.get("boundary"):
            return f"{media}; missing boundary"
        charset = params.get("charset")
        if charset is not None and is_json_media(media) and charset.lower() != "utf-8":
            return f"{media}; charset={charset.lower()}"
        return None


# ---------------------------------------------------------------------------
# Body decoding
# ---------------------------------------------------------------------------


class BodyDecoding:
    """Which codec decodes a JSON-body parameter, and how malformed bodies are answered."""

    __slots__ = ("json", "single", "malformed_status", "_by_media", "_registry")

    def __init__(
        self,
        registry: CodecRegistry,
        *,
        malformed_status: int,
        by_media: dict[str, Any] | None = None,
    ) -> None:
        self.json = registry.json
        #: The one codec of a route that consumes only JSON (no header lookup).
        self.single: Any = None if by_media is not None else registry.json
        self.malformed_status = malformed_status
        self._by_media = by_media or {}
        self._registry = registry

    def select(self, headers: Headers) -> Any:
        single = self.single
        if single is not None:
            return single
        raw = headers.get("content-type")
        if not raw:
            return self.json
        media = media_type_of(raw)
        codec = self._by_media.get(media)
        if codec is None:
            codec = self._registry.lookup(media)
        return codec if codec is not None else self.json


# ---------------------------------------------------------------------------
# Response negotiation
# ---------------------------------------------------------------------------


class CodecEncoder:
    """A response codec seen through the ``JSONEncoder`` protocol.

    ``Response.json`` labels the body with :attr:`lauren_media_type`, so every
    coercion path (plain values, typed results, response contracts, pages)
    writes the negotiated format without knowing about codecs.
    """

    __slots__ = ("_encode", "name", "lauren_media_type")

    def __init__(self, encode: Any, media_type: str, name: str) -> None:
        self._encode = encode
        self.name = name
        self.lauren_media_type = media_type

    def encode(self, value: Any) -> bytes:
        return cast(bytes, self._encode(value))

    def encode_compact(self, value: Any) -> bytes:
        return cast(bytes, self._encode(value))


def _labelled_json(encoder: Any, media_type: str) -> CodecEncoder:
    return CodecEncoder(encoder.encode_compact, media_type, getattr(encoder, "name", "json"))


def produces_structured(value: Any) -> bool:
    """Whether a handler result is serialized by a codec (not text, bytes, a ``Response`` or empty)."""
    from ..responses import RESULT_TYPES

    if type(value) is tuple and len(value) in (2, 3) and isinstance(value[1], int):
        value = value[0]
    if isinstance(value, RESULT_TYPES):
        value = getattr(value, "value", None)
    return not (value is None or isinstance(value, (str, bytes, bytearray, Response)))


class Negotiation:
    """A route's ``produces`` list and the encoder of each entry."""

    __slots__ = ("offers", "vary", "strict", "early", "route", "_encoders")

    def __init__(
        self, offers: tuple[str, ...], encoders: dict[str, Any], *, strict: bool, early: bool, route: str
    ) -> None:
        self.offers = offers
        #: Several representations: responses carry ``Vary: Accept``.
        self.vary = len(offers) > 1
        #: ``strict_accept``: nothing acceptable is a 406.
        self.strict = strict
        #: Reject with 406 before the handler runs (its output is known to be structured).
        self.early = early
        self.route = route
        #: media type → codec encoder (``None``: the route's JSON encoder).
        self._encoders = encoders

    def select(self, accept: str | None) -> str | None:
        """The negotiated media type (``None`` only under ``strict_accept``)."""
        if not accept:
            return self.offers[0]
        chosen = negotiate(accept, self.offers)
        if chosen is None and not self.strict:
            return self.offers[0]
        return chosen

    def not_acceptable(self, accept: str | None) -> NotAcceptableError:
        return NotAcceptableError(
            f"{self.route} cannot produce a representation the request accepts ({accept or ''!s})",
            detail={"available": list(self.offers)},
        )

    def encoder(self, media: str, json_encoder: Any) -> Any:
        """The encoder that writes ``media`` (the route's JSON encoder for JSON types)."""
        codec_encoder = self._encoders.get(media)
        if codec_encoder is not None:
            return codec_encoder
        if media == "application/json":
            return json_encoder
        return _labelled_json(json_encoder, media)


def add_vary_accept(response: Response) -> Response:
    """Append ``Accept`` to the response's ``Vary`` (once)."""
    headers = response.headers
    names = [part.strip().lower() for value in headers.getall("vary") for part in value.split(",")]
    if "accept" not in names and "*" not in names:
        headers.append("vary", "Accept")
    return response


# ---------------------------------------------------------------------------
# Per-route compilation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteContent:
    """A route's compiled content rules (see the module docstring)."""

    gate: ContentGate | None
    negotiation: Negotiation | None
    body_limit: int | None
    #: Effective accepted media types (declared, or inferred from body parameters).
    consumes: tuple[str, ...]
    #: Effective response media types (route → controller → ``default_produces``).
    produces: tuple[str, ...]
    #: ``consumes`` was declared with ``consumes=`` (route or controller).
    consumes_declared: bool = False


def _structured_annotation(body: Any) -> bool:
    from .._asgi._contracts import NO_BODY, OPAQUE, UNTYPED

    if body in (NO_BODY, OPAQUE, UNTYPED) or isinstance(body, str):
        return False
    return body not in (str, bytes, bytearray)


def _opaque_table(table: Any) -> bool:
    """The route never returns a value a codec serializes (``Response``, text, bytes, no body)."""
    from .._asgi._contracts import NO_BODY, OPAQUE

    if table is None or not table.members:
        return False
    return all(m.body in (NO_BODY, OPAQUE, str, bytes, bytearray) for m in table.members)


def _not_registered(media: str, route: str, handler: str, option: str) -> StartupError:
    return StartupError(
        f"{route} ({handler}) declares {option}={media!r} but no codec serves it; "
        "add one to ContentConfig(codecs=[...])",
        detail={"reason": "codec_not_registered", "media_type": media, "route": route, "handler": handler},
    )


def compile_route_content(
    runtime: ContentRuntime,
    *,
    method: str,
    route: str,
    handler: str,
    route_meta: Any,
    controller_meta: Any,
    extractions: Sequence[Any],
    table: Any,
    streaming: bool,
    form_plan: Any = None,
) -> RouteContent:
    """Compile one route's gate, decoding, negotiation and body limit."""
    registry = runtime.registry
    sources = {ext.source for ext in extractions if ext.reads_body or ext.source in _BODY_SOURCES}
    route_consumes = getattr(route_meta, "consumes", None)
    ctrl_consumes = getattr(controller_meta, "consumes", None)
    declared = route_consumes or (ctrl_consumes if sources else None)
    if (
        form_plan is not None
        and not form_plan.requires_multipart
        and not form_plan.legacy_urlencoded
        and declared
        and any(media not in FORM_MEDIA_TYPES for media in declared)
    ):
        raise StartupError(
            f"{route} ({handler}) takes a typed form but consumes= names a non-form media type",
            detail={"reason": "invalid_form_consumes", "route": route, "handler": handler},
        )

    # --- consumes + gate ---
    gate: ContentGate | None = None
    if "upload_file" in sources or (form_plan is not None and form_plan.requires_multipart):
        # The extractor parses multipart/form-data; a declaration that would
        # admit anything else would promise a codec the extractor cannot use.
        if declared and tuple(declared) != MULTIPART_MEDIA_TYPES:
            raise StartupError(
                f"{route} ({handler}) takes UploadFile but consumes={tuple(declared)!r} "
                "cannot be parsed as multipart/form-data",
                detail={"reason": "invalid_upload_consumes", "route": route, "handler": handler},
            )
        consumes = MULTIPART_MEDIA_TYPES
        gate = ContentGate(
            consumes,
            strict=True,
            label="a multipart/form-data body with boundary",
            method=method,
            boundary=True,
        )
    elif declared:
        consumes = tuple(declared)
        label = "one of " + ", ".join(consumes)
        gate = ContentGate(
            consumes,
            strict=True,
            label=label,
            method=method,
            registry=registry if "json" in sources else None,
        )
    elif form_plan is not None and not form_plan.legacy_urlencoded:
        consumes = FORM_MEDIA_TYPES
        gate = ContentGate(
            consumes,
            strict=True,
            label="a URL-encoded or multipart form body",
            method=method,
        )
    else:
        inferred: list[str] = []
        if "json" in sources:
            inferred.extend(JSON_MEDIA_TYPES)
        if "form" in sources:
            inferred.extend(FORM_MEDIA_TYPES)
        consumes = tuple(inferred)
        if consumes and runtime.strict is not False:
            label = (
                "a JSON body (application/json)" if "form" not in sources else "one of " + ", ".join(consumes)
            )
            gate = ContentGate(
                consumes,
                strict=runtime.strict is True,
                label=label,
                method=method,
                registry=registry if "json" in sources else None,
            )

    # --- decoding of JSON-body parameters ---
    if "json" in sources:
        decoding = _compile_decoding(runtime, declared, route=route, handler=handler)
        for ext in extractions:
            plan = getattr(ext, "compiled", None)
            if ext.source == "json" and plan is not None and hasattr(plan, "decoding"):
                plan.decoding = decoding

    # --- produces + negotiation ---
    explicit = getattr(route_meta, "produces", None) or getattr(controller_meta, "produces", None)
    produces: tuple[str, ...] = tuple(explicit) if explicit else runtime.default_produces
    negotiation: Negotiation | None = None
    if not streaming and not _opaque_table(table):
        encoders: dict[str, Any] = {}
        for media in produces:
            codec = registry.lookup(media)
            if codec is None:
                raise _not_registered(media, route, handler, "produces")
            if not isinstance(codec, JsonCodec):
                encoders[media] = CodecEncoder(codec.encode, media, _codec_name(codec))
        if produces != ("application/json",) or runtime.strict_accept:
            typed = table is not None and any(_structured_annotation(m.body) for m in table.members)
            negotiation = Negotiation(
                produces,
                encoders,
                strict=runtime.strict_accept,
                early=runtime.strict_accept and (bool(explicit) or typed),
                route=route,
            )

    limit = getattr(route_meta, "max_body_size", None) or getattr(controller_meta, "max_body_size", None)
    return RouteContent(
        gate=gate,
        negotiation=negotiation,
        body_limit=limit,
        consumes=consumes,
        produces=produces,
        consumes_declared=bool(declared),
    )


def _compile_decoding(
    runtime: ContentRuntime, declared: Sequence[str] | None, *, route: str, handler: str
) -> BodyDecoding:
    registry = runtime.registry
    if not declared:
        # A custom codec for a JSON-suffix type must run even when the route
        # accepts the default inferred JSON media ranges.
        suffix_codecs = {
            media: registry.lookup(media)
            for media in registry.media_types()
            if is_json_media(media) and registry.lookup(media) is not registry.json
        }
        if suffix_codecs:
            return BodyDecoding(registry, malformed_status=runtime.malformed_status, by_media=suffix_codecs)
        return runtime.json_decoding
    by_media: dict[str, Any] = {}
    multi = False
    non_json = [m for m in registry.media_types() if registry.lookup(m) is not registry.json]
    for media in declared:
        if is_wildcard(media):
            multi = multi or any(matches(media, other) for other in non_json)
            continue
        codec = registry.lookup(media)
        if codec is None:
            raise _not_registered(media, route, handler, "consumes")
        by_media[media] = codec
        multi = multi or codec is not registry.json
    if not multi:
        return runtime.json_decoding
    return BodyDecoding(registry, malformed_status=runtime.malformed_status, by_media=by_media)


__all__ = [
    "FORM_MEDIA_TYPES",
    "JSON_MEDIA_TYPES",
    "MALFORMED_BODY_STATUSES",
    "MULTIPART_MEDIA_TYPES",
    "BodyDecoding",
    "CodecEncoder",
    "CodecRegistry",
    "ContentGate",
    "ContentRuntime",
    "Negotiation",
    "RouteContent",
    "add_vary_accept",
    "compile_route_content",
    "produces_structured",
    "resolve_content",
]
