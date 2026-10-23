"""RFC 8288 ``Link`` URLs built from a request snapshot (private).

The pagination extractor copies the request's public path and raw query
string (immutable ``str`` / ``bytes``) into a :class:`RequestSnapshot` — the
pooled ``Request`` itself is never kept. :func:`build_url` then replaces only
the pagination parameters and keeps every other parameter in its original
order. Query text is handled in "latin-1 space" (each byte is one character),
so parameters are re-encoded byte for byte: ``%C3%A9`` stays ``%C3%A9``,
spaces become ``%20`` and ``&`` / ``=`` inside a value stay escaped.

URLs are path-absolute references (``/users?limit=20&offset=40``) that
include the app's root path; ``PaginationConfig(base_url=...)`` prefixes a
trusted origin to make them absolute.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import quote, unquote_plus

#: Request attribute holding the public path prefix (the ASGI ``root_path``)
#: when the app runs behind one. Set by ``LaurenApp.__call__`` only when
#: non-empty; ``Request.reset`` clears it with the rest of ``__dict__``.
ROOT_PATH_ATTR = "_lauren_root_path"

#: Characters kept verbatim in a path (RFC 3986 ``pchar`` minus ``%`` plus ``/``).
_PATH_SAFE = "/:@!$&'()*+,;="


class RequestSnapshot:
    """The public path and raw query of one request (immutable values only)."""

    __slots__ = ("path", "query")

    def __init__(self, path: str, query: bytes) -> None:
        self.path = path
        self.query = query


def snapshot(request: object) -> RequestSnapshot:
    """Copy what link generation needs out of a (pooled) request."""
    prefix: str = getattr(request, ROOT_PATH_ATTR, "")
    path: str = getattr(request, "path")
    if prefix:
        path = prefix.rstrip("/") + path
    query: bytes = getattr(request, "_raw_query_string", b"") or b""
    return RequestSnapshot(path, query)


def _to_wire(name: str) -> str:
    """A configured (Unicode) parameter name in latin-1 space."""
    return name.encode("utf-8").decode("latin-1")


def _encode(text: str) -> str:
    return quote(text, safe="", encoding="latin-1")


def build_url(
    snap: RequestSnapshot,
    managed: Sequence[str],
    values: Mapping[str, str | None],
    *,
    base_url: str = "",
) -> str:
    """Return the request URL with ``values`` applied to the ``managed`` parameters.

    A managed name mapped to a string is set (in place of its first
    occurrence, or appended in ``managed`` order), mapped to ``None`` is
    removed, and absent from ``values`` is kept as the request sent it.
    Repeated occurrences of a replaced parameter are dropped; every other
    parameter is kept, in order.
    """
    wire = {_to_wire(name): name for name in managed}
    parts: list[str] = []
    seen: set[str] = set()
    text = snap.query.decode("latin-1")
    for segment in text.split("&") if text else ():
        if not segment:
            continue
        raw_key, eq, raw_value = segment.partition("=")
        key = unquote_plus(raw_key, encoding="latin-1")
        name = wire.get(key)
        if name is not None and name in values:
            if name in seen:
                continue
            seen.add(name)
            replacement = values[name]
            if replacement is not None:
                parts.append(f"{_encode(key)}={_encode(_to_wire(replacement))}")
            continue
        value = unquote_plus(raw_value, encoding="latin-1")
        parts.append(f"{_encode(key)}={_encode(value)}" if eq else _encode(key))
    for name in managed:
        replacement = values.get(name)
        if name not in seen and replacement is not None:
            parts.append(f"{_encode(_to_wire(name))}={_encode(_to_wire(replacement))}")
    url = quote(snap.path, safe=_PATH_SAFE)
    if parts:
        url = f"{url}?{'&'.join(parts)}"
    return base_url + url


def link_header(links: Sequence[tuple[str, str]]) -> str:
    """Format ``[(rel, url), ...]`` as an RFC 8288 ``Link`` header value."""
    return ", ".join(f'<{url}>; rel="{rel}"' for rel, url in links)
