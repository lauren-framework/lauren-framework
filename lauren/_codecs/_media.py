"""Media types: parsing, validation and matching (private).

``consumes=`` / ``produces=`` entries are normalised once, at decoration or
startup, by :func:`normalize_media_range`; the request path only lower-cases
the received ``Content-Type`` and compares strings.
"""

from __future__ import annotations

import re

#: RFC 9110 ``token`` characters, without ``*`` (a wildcard is never part of a
#: concrete type or subtype name).
_TOKEN = re.compile(r"^[!#$%&'+.^_`|~0-9a-z-]+$")


def split_media_type(value: str) -> tuple[str, dict[str, str]]:
    """Split a ``Content-Type`` value into ``("type/subtype", params)``.

    The media type and parameter names are lower-cased; parameter values keep
    their case with surrounding quotes removed. Never raises.
    """
    media, _, rest = value.partition(";")
    params: dict[str, str] = {}
    for piece in rest.split(";"):
        name, sep, raw = piece.partition("=")
        name = name.strip().lower()
        if sep and name:
            params[name] = raw.strip().strip('"')
    return media.strip().lower(), params


def media_type_of(value: str) -> str:
    """The lower-cased ``type/subtype`` of a header value (parameters dropped)."""
    return value.partition(";")[0].strip().lower()


def normalize_media_range(entry: object, *, wildcards: bool) -> str:
    """Validate one ``consumes`` / ``produces`` entry and return it lower-cased.

    ``wildcards`` allows ``*/*``, ``type/*`` and structured-suffix ranges such
    as ``application/*+json``. Parameters are not supported. Raises
    :class:`ValueError` with a message suitable for a usage error.
    """
    if not isinstance(entry, str):
        raise ValueError(f"media types must be strings, got {type(entry).__name__}")
    text = entry.strip().lower()
    if ";" in text:
        raise ValueError(f"{entry!r}: media type parameters are not supported; write the bare type/subtype")
    type_, sep, subtype = text.partition("/")
    if not sep or not type_ or not subtype or "/" in subtype:
        raise ValueError(f"{entry!r} is not a type/subtype media type")
    if "*" in text and not wildcards:
        raise ValueError(f"{entry!r}: a response media type must be concrete (no wildcards)")
    if type_ == "*":
        if subtype != "*":
            raise ValueError(f"{entry!r} is not a valid media range (use */* or type/*)")
        return "*/*"
    if not _TOKEN.match(type_):
        raise ValueError(f"{entry!r} is not a valid media type")
    if subtype == "*":
        return text
    if subtype.startswith("*+"):
        if not _TOKEN.match(subtype[2:]):
            raise ValueError(f"{entry!r} is not a valid structured-suffix range")
        return text
    if not _TOKEN.match(subtype):
        raise ValueError(f"{entry!r} is not a valid media type")
    return text


def is_wildcard(media_range: str) -> bool:
    """Whether a normalised entry is a range (``*/*``, ``type/*``, ``type/*+suffix``)."""
    return "*" in media_range


def matches(media_range: str, media: str) -> bool:
    """Whether a normalised range covers a concrete, lower-cased media type."""
    if media_range == "*/*":
        return True
    rtype, _, rsub = media_range.partition("/")
    mtype, _, msub = media.partition("/")
    if rtype != mtype:
        return False
    if rsub == "*":
        return True
    if rsub.startswith("*+"):
        suffix = rsub[1:]
        return msub.endswith(suffix) and len(msub) > len(suffix)
    return rsub == msub


def is_json_media(media: str) -> bool:
    """``application/json`` or any ``+json`` structured-suffix type (RFC 6839)."""
    return media == "application/json" or (media.endswith("+json") and "/" in media)


__all__ = [
    "is_json_media",
    "is_wildcard",
    "matches",
    "media_type_of",
    "normalize_media_range",
    "split_media_type",
]
