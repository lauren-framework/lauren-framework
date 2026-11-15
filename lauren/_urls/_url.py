"""The ``URL`` value type and the ``url_for`` accessors of a connection (private).

Stdlib only: :mod:`lauren.types` imports this module, so it must not import
anything from lauren.

* :class:`URL` — an immutable ``str`` subclass with parsed components and
  ``with_query`` / ``with_fragment`` / ``replace``.
* :func:`parse_authority` — validates a ``host[:port]`` value (``Host`` header,
  forwarded host) and splits it.
* :class:`UrlAccess` — ``base_url`` and ``url_for`` / ``url_path_for`` /
  ``ws_url_for`` for ``Request``, ``WebSocket`` and ``WsUpgradeRequest``. Each
  of them supplies its URL parts through ``_url_parts()``.
"""

from __future__ import annotations

import enum
import ipaddress
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit

#: Default port of every scheme Lauren builds URLs for.
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "ws": 80, "wss": 443}

#: Characters kept verbatim in a URL path: RFC 3986 ``pchar`` without ``%``, plus ``/``.
PATH_SAFE = "/:@!$&'()*+,;="

#: Characters kept verbatim in a fragment (RFC 3986 ``fragment`` without ``%``).
_FRAGMENT_SAFE = "/?:@!$&'()*+,;="

HTTP_TO_WS: dict[str, str] = {"http": "ws", "https": "wss"}
WS_TO_HTTP: dict[str, str] = {"ws": "http", "wss": "https"}

_PORT_RE = re.compile(r"[0-9]{1,5}")
# A DNS name or IPv4 address. Stricter than the RFC 3986 ``reg-name`` on
# purpose: a Host value with sub-delimiters or percent-escapes is never a
# legitimate public host, and refusing it keeps it out of generated URLs.
_REG_NAME_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9_])?")

#: Keyword components of :meth:`URL.replace`.
_COMPONENTS = frozenset({"scheme", "host", "port", "path", "query", "fragment"})


def parse_authority(value: str) -> tuple[str, int | None] | None:
    """Split a ``host[:port]`` authority, or return ``None`` when it is not a valid one.

    IPv6 literals keep their brackets (``"[::1]"``), as in a URL. Ports must be
    in ``1..65535``.
    """
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host, rest = value[: end + 1], value[end + 1 :]
        try:
            if not isinstance(ipaddress.ip_address(host[1:-1]), ipaddress.IPv6Address):
                return None
        except ValueError:
            return None
    else:
        host, colon, port_text = value.partition(":")
        rest = colon + port_text
        labels = host.rstrip(".").split(".")
        if len(host) > 253 or not all(
            1 <= len(label) <= 63 and _REG_NAME_RE.fullmatch(label) for label in labels
        ):
            return None
    if not rest:
        return host, None
    port_text = rest[1:]
    if rest[0] != ":" or not _PORT_RE.fullmatch(port_text):
        return None
    port = int(port_text)
    if not 0 < port < 65536:
        return None
    return host, port


def compose_origin(scheme: str, host: str, port: int | None) -> str:
    """``scheme://host[:port]``, leaving out the scheme's default port."""
    if port is None or port == DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def join_root(root_path: str, path: str) -> str:
    """Prefix an encoded app path (``"/users/7"``) with a (decoded) root path."""
    root = root_path.rstrip("/")
    if not root:
        return path
    return quote(root, safe=PATH_SAFE) + path


def format_value(value: Any) -> str:
    """Render a path or query value as text (booleans lower-case, enums by value)."""
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


def _query_items(params: Any, kwargs: Mapping[str, Any]) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    if params is not None:
        source: Iterable[Any] = params.items() if isinstance(params, Mapping) else params
        items.extend((str(key), value) for key, value in source)
    items.extend(kwargs.items())
    return items


def encode_query(items: Iterable[tuple[str, Any]]) -> list[str]:
    """``key=value`` pairs; a list / tuple value repeats the key, ``None`` is skipped."""
    out: list[str] = []
    for key, value in items:
        values = value if isinstance(value, (list, tuple)) else (value,)
        for item in values:
            if item is not None:
                out.append(f"{quote(key, safe='')}={quote(format_value(item), safe='')}")
    return out


class URL(str):
    """An immutable URL: a ``str`` with parsed components.

    Usable anywhere a string is — ``Response.redirect(url)``, a header value,
    a JSON field. Built by the ``url_for`` family; ``URL("...")`` wraps any
    string. ``host`` keeps IPv6 brackets; ``port`` is ``None`` unless the URL
    names one.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return f"URL({str(self)!r})"

    @property
    def scheme(self) -> str:
        return urlsplit(self).scheme

    @property
    def netloc(self) -> str:
        return urlsplit(self).netloc

    @property
    def host(self) -> str:
        return self._authority()[0]

    @property
    def port(self) -> int | None:
        return self._authority()[1]

    @property
    def path(self) -> str:
        return urlsplit(self).path

    @property
    def query(self) -> str:
        return urlsplit(self).query

    @property
    def fragment(self) -> str:
        return urlsplit(self).fragment

    @property
    def is_absolute(self) -> bool:
        """Whether the URL has a scheme and a host (``https://host/...``)."""
        parts = urlsplit(self)
        return bool(parts.scheme and parts.netloc)

    def _authority(self) -> tuple[str, int | None]:
        netloc = urlsplit(self).netloc.rpartition("@")[2]
        if netloc.startswith("["):
            end = netloc.find("]") + 1
            host, rest = netloc[:end], netloc[end:]
        else:
            host, colon, port_text = netloc.partition(":")
            rest = colon + port_text
        port_text = rest[1:]
        return host, int(port_text) if port_text.isdigit() else None

    def with_query(self, params: Any = None, /, **kwargs: Any) -> URL:
        """Set query parameters: a mapping / pairs and/or keyword arguments.

        Every key given replaces the URL's existing values for it (kept
        parameters stay in order, new ones are appended in argument order); a
        list or tuple repeats the key; ``None`` removes it. Values are
        percent-encoded.
        """
        items = _query_items(params, kwargs)
        names = {key for key, _ in items}
        parts = urlsplit(self)
        kept = [
            segment
            for segment in parts.query.split("&")
            if segment and unquote_plus(segment.partition("=")[0]) not in names
        ]
        kept.extend(encode_query(items))
        return URL(urlunsplit(parts._replace(query="&".join(kept))))

    def with_fragment(self, fragment: str | None) -> URL:
        """Replace the fragment (``None`` or ``""`` removes it)."""
        parts = urlsplit(self)
        return URL(urlunsplit(parts._replace(fragment=quote(fragment or "", safe=_FRAGMENT_SAFE))))

    def replace(self, *args: Any, **components: Any) -> Any:
        """Return a copy with URL components replaced: ``url.replace(scheme="https", port=None)``.

        Keywords: ``scheme``, ``host``, ``port`` (``None`` drops it), ``path``,
        ``query`` and ``fragment`` (the last three taken as already encoded).
        Called with positional arguments it is ``str.replace`` (``URL`` stays
        a drop-in string) and returns a plain ``str``.
        """
        if args:
            if components:
                raise TypeError("URL.replace takes either str.replace arguments or URL components, not both")
            return str(self).replace(*args)
        unknown = set(components).difference(_COMPONENTS)
        if unknown:
            raise TypeError(f"URL.replace got unknown component(s) {', '.join(sorted(unknown))}")
        parts = urlsplit(self)
        netloc = parts.netloc
        if "host" in components or "port" in components:
            current_host, current_port = self._authority()
            host = components.get("host", current_host)
            port = components.get("port", current_port)
            netloc = host if port is None else f"{host}:{port}"
        return URL(
            urlunsplit(
                (
                    components.get("scheme", parts.scheme),
                    netloc,
                    components.get("path", parts.path),
                    components.get("query", parts.query),
                    components.get("fragment", parts.fragment),
                )
            )
        )


def _unbound() -> RuntimeError:
    return RuntimeError(
        "URL building needs a request served by a Lauren app (built by LaurenFactory.create); "
        "outside a request, inject UrlBuilder or call app.url_path_for(...)"
    )


class UrlAccess:
    """``base_url`` and the ``url_for`` family over a connection's URL parts.

    Subclasses implement ``_url_parts() -> (scheme, host, port, root_path,
    builder)``; ``builder`` is the app's :class:`~lauren.urls.UrlBuilder` (or
    ``None`` for an object built outside an app).
    """

    __slots__ = ()

    def _url_parts(self) -> tuple[str, str, int, str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def base_url(self) -> URL:
        """The public URL of the app root: ``scheme://host[:port]{root_path}/``."""
        scheme, host, port, root, _ = self._url_parts()
        return URL(compose_origin(scheme, host, port) + join_root(root, "/"))

    def url_path_for(self, target: Any, /, *, route: str | None = None, **params: Any) -> URL:
        """The path of a route, including this request's root path (mounts, proxies).

        ``target`` is a route name or a handler reference
        (``UserController.show``); ``route=`` picks one route of a handler that
        has several. Raises :class:`LookupError` (unknown target) or
        :class:`ValueError` (missing / unexpected path parameters).
        """
        _, _, _, root, builder = self._url_parts()
        if builder is None:
            raise _unbound()
        return URL(join_root(root, builder._route_path(target, route, params)))

    def url_for(self, target: Any, /, *, route: str | None = None, **params: Any) -> URL:
        """The absolute URL of a route on this request's scheme and host."""
        scheme, host, port, root, builder = self._url_parts()
        if builder is None:
            raise _unbound()
        path = builder._route_path(target, route, params)
        return URL(compose_origin(WS_TO_HTTP.get(scheme, scheme), host, port) + join_root(root, path))

    def ws_url_for(self, target: Any, /, **params: Any) -> URL:
        """The absolute ``ws://`` / ``wss://`` URL of a WebSocket gateway (name or class)."""
        scheme, host, port, root, builder = self._url_parts()
        if builder is None:
            raise _unbound()
        path = builder._gateway_path(target, params)
        return URL(compose_origin(HTTP_TO_WS.get(scheme, scheme), host, port) + join_root(root, path))


__all__ = [
    "DEFAULT_PORTS",
    "HTTP_TO_WS",
    "PATH_SAFE",
    "URL",
    "UrlAccess",
    "WS_TO_HTTP",
    "compose_origin",
    "encode_query",
    "format_value",
    "join_root",
    "parse_authority",
]
