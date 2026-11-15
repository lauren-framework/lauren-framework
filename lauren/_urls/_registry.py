"""The per-app route-name registry and :class:`UrlBuilder` (private).

``LaurenFactory.create`` builds one :class:`RouteTable` per app at the end of
Phase 5 (HTTP routes and WebSocket gateways, which are separate namespaces)
and binds it into the app's :class:`UrlBuilder`. Lookups are dict reads; a
URL is a join over the route's precompiled :class:`UrlTemplate`.

Naming rules:

* An explicit name is ``[module name.][controller name.]route name`` — the
  module and controller parts appear when those declare ``name=``.
* A route without ``name=`` gets the derived name ``ControllerClass.method``
  when its handler has exactly one route on that controller (``Lauren`` app
  routes use the function name). Derived names are never namespaced.
* Explicit names must be unique (``RouterConflictError``,
  ``reason="duplicate_route_name"``). An explicit name shadows an equal
  derived one; derived names shared by several routes (same class name in two
  modules) are left ambiguous — looking them up raises — instead of failing
  apps that never build URLs.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..exceptions import RouterConflictError
from ._template import UrlTemplate
from ._url import HTTP_TO_WS, URL, compose_origin, join_root, parse_authority


@dataclass(frozen=True, slots=True)
class NamedRoute:
    """One HTTP route or WebSocket gateway, as the URL builder sees it."""

    #: Full name, or ``None`` when the route has none.
    name: str | None
    #: ``name=`` as declared on the route / gateway decorator.
    local_name: str | None
    method: str
    path_template: str
    template: UrlTemplate
    #: The handler function (HTTP) or the gateway class (WebSocket).
    handler: Any
    #: The controller / gateway class.
    owner: type

    @property
    def label(self) -> str:
        return self.name or f"{self.method} {self.path_template} ({self.owner.__name__})"


@dataclass(frozen=True, slots=True)
class RouteSpec:
    """A compiled route, before naming."""

    method: str
    path_template: str
    handler: Any
    owner: type
    #: Full explicit name, or ``None``.
    explicit: str | None
    local_name: str | None
    #: Derived name candidate, or ``None`` (multi-route handler).
    derived: str | None


def _describe(spec: RouteSpec | NamedRoute) -> str:
    target = getattr(spec.handler, "__qualname__", None) or spec.owner.__name__
    return f"{target} ({spec.method} {spec.path_template})"


def _unwrap_chain(handler: Any) -> list[Any]:
    """The handler plus every object in its ``__wrapped__`` chain."""
    chain: list[Any] = []
    seen: set[int] = set()
    current = handler
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = getattr(current, "__wrapped__", None)
    return chain


class _Namespace:
    """Names → routes for one namespace (HTTP or WebSocket)."""

    __slots__ = ("kind", "by_name", "ambiguous", "routes")

    def __init__(self, kind: str, specs: list[RouteSpec], *, log: Callable[[str], None]) -> None:
        self.kind = kind
        explicit: dict[str, list[RouteSpec]] = {}
        derived: dict[str, list[RouteSpec]] = {}
        for spec in specs:
            if spec.explicit is not None:
                explicit.setdefault(spec.explicit, []).append(spec)
            elif spec.derived is not None:
                derived.setdefault(spec.derived, []).append(spec)
        for name, owners in explicit.items():
            if len(owners) > 1:
                handlers = [_describe(spec) for spec in owners]
                raise RouterConflictError(
                    f"{kind} route name {name!r} is used by {len(owners)} routes: {', '.join(handlers)}",
                    detail={
                        "reason": "duplicate_route_name",
                        "name": name,
                        "kind": kind,
                        "handlers": handlers,
                    },
                )
        names: dict[int, str | None] = {}
        self.ambiguous: dict[str, tuple[str, ...]] = {}
        for spec in specs:
            names[id(spec)] = spec.explicit
        for name, owners in derived.items():
            if name in explicit:
                log(f"derived {kind} route name {name!r} is shadowed by an explicit name= and not assigned")
                continue
            if len(owners) > 1:
                self.ambiguous[name] = tuple(_describe(spec) for spec in owners)
                log(
                    f"derived {kind} route name {name!r} is shared by {len(owners)} routes and not assigned; "
                    "give them explicit name= values to build URLs by name"
                )
                continue
            names[id(owners[0])] = name
        self.routes: tuple[NamedRoute, ...] = tuple(
            NamedRoute(
                name=names[id(spec)],
                local_name=spec.local_name,
                method=spec.method,
                path_template=spec.path_template,
                template=UrlTemplate(spec.path_template),
                handler=spec.handler,
                owner=spec.owner,
            )
            for spec in specs
        )
        self.by_name: dict[str, NamedRoute] = {r.name: r for r in self.routes if r.name is not None}

    def lookup(self, name: str) -> NamedRoute:
        route = self.by_name.get(name)
        if route is not None:
            return route
        if name in self.ambiguous:
            raise LookupError(
                f"{self.kind} route name {name!r} is ambiguous: it is derived for "
                f"{', '.join(self.ambiguous[name])}; give the routes explicit name= values"
            )
        close = difflib.get_close_matches(name, list(self.by_name), n=3)
        hint = f"; did you mean {', '.join(map(repr, close))}?" if close else ""
        raise LookupError(f"no {self.kind} route named {name!r}{hint}")


class RouteTable:
    """Every named route of one app: HTTP routes and WebSocket gateways."""

    __slots__ = ("http", "ws", "_by_handler", "_by_gateway")

    def __init__(
        self,
        http: list[RouteSpec],
        ws: list[RouteSpec],
        *,
        log: Callable[[str], None] = lambda message: None,
    ) -> None:
        self.http = _Namespace("HTTP", http, log=log)
        self.ws = _Namespace("WebSocket", ws, log=log)
        self._by_handler: dict[Any, list[NamedRoute]] = {}
        for route in self.http.routes:
            for key in _unwrap_chain(route.handler):
                self._by_handler.setdefault(key, []).append(route)
        self._by_gateway: dict[Any, NamedRoute] = {route.owner: route for route in self.ws.routes}

    def http_route(self, target: Any, route: str | None = None) -> NamedRoute:
        """Resolve a route name or a handler reference (``route=`` picks among several)."""
        if isinstance(target, str):
            return self.http.lookup(target)
        candidates = self._candidates(target)
        if route is not None:
            chosen = [c for c in candidates if route in (c.name, c.local_name)]
            if len(chosen) == 1:
                return chosen[0]
            raise LookupError(
                f"{_qualname(target)} has no single route named {route!r}; its routes: {_listing(candidates)}"
            )
        if len(candidates) == 1:
            return candidates[0]
        raise LookupError(
            f"{_qualname(target)} handles {len(candidates)} routes; pass route=<name> to choose one of: "
            f"{_listing(candidates)}"
        )

    def _candidates(self, target: Any) -> list[NamedRoute]:
        for key in (target, getattr(target, "__func__", None), *_unwrap_chain(target)[1:]):
            if key is None:
                continue
            try:
                found = self._by_handler.get(key)
            except TypeError:  # unhashable target
                found = None
            if found:
                return found
        if callable(target):
            raise LookupError(f"{_qualname(target)} is not a route handler of this app")
        raise TypeError(f"expected a route name (str) or a handler reference, got {type(target).__name__}")

    def ws_route(self, target: Any) -> NamedRoute:
        """Resolve a gateway name or a gateway class."""
        if isinstance(target, str):
            return self.ws.lookup(target)
        found = self._by_gateway.get(target) if isinstance(target, type) else None
        if found is None:
            raise LookupError(f"{_qualname(target)} is not a WebSocket gateway of this app")
        return found


def _qualname(target: Any) -> str:
    return str(getattr(target, "__qualname__", None) or getattr(target, "__name__", None) or repr(target))


def _listing(routes: list[NamedRoute]) -> str:
    return ", ".join(
        f"{route.name!r} ({route.method} {route.path_template})"
        if route.name
        else f"<unnamed> ({route.method} {route.path_template})"
        for route in routes
    )


def _not_bound() -> RuntimeError:
    return RuntimeError("UrlBuilder is not bound yet: routes are known once LaurenFactory.create returns")


class UrlBuilder:
    """Builds URLs to this app's routes — in services, jobs and background tasks.

    One per app, injectable everywhere (``def __init__(self, urls: UrlBuilder)``).
    Paths include the app's public root path (the path of ``public_base_url=``,
    else ``root_path=``); absolute URLs use ``public_base_url=`` — a request's
    ``Host`` header is never consulted here. Inside a request handler prefer
    ``request.url_for`` / ``request.url_path_for``, which know the request's
    own root path, scheme and host.

    ``target`` is a route name (``"users.show"``) or a handler reference
    (``UserController.show``); ``route=`` picks one route of a handler that
    has several. Unknown targets raise :class:`LookupError`, wrong path
    parameters :class:`ValueError`.
    """

    __slots__ = ("_table", "_origin", "_root_path", "_signer")

    def __init__(
        self, *, public_base_url: str | None = None, root_path: str = "", signer: Any = None
    ) -> None:
        origin, root = "", root_path
        if public_base_url:
            origin, base_path = split_base_url(public_base_url)
            root = base_path or root_path
        self._table: RouteTable | None = None
        self._origin = origin
        self._root_path = root.rstrip("/")
        self._signer = signer

    def _bind(self, table: RouteTable) -> None:
        self._table = table

    @property
    def _routes(self) -> RouteTable:
        table = self._table
        if table is None:
            raise _not_bound()
        return table

    # -- hooks used by Request / WebSocket accessors and LaurenApp ----------------

    def _route_path(self, target: Any, route: str | None, params: Mapping[str, Any]) -> str:
        entry = self._routes.http_route(target, route)
        return entry.template.expand(params, label=entry.label)

    def _gateway_path(self, target: Any, params: Mapping[str, Any]) -> str:
        entry = self._routes.ws_route(target)
        return entry.template.expand(params, label=entry.label)

    def _verify(self, request: Any) -> str | None:
        signer = self._signer
        if signer is None:  # pragma: no cover - the factory refuses ValidSignature without signing
            raise RuntimeError("url_signing is not configured")
        from ._signing import request_origin

        query = getattr(request, "query_string", None)
        if query is None:
            query = request.url.partition("?")[2]
        elif isinstance(query, bytes):
            query = query.decode("latin-1")
        origin = request_origin(request) if signer.bind_host else ""
        return signer.verify(method=request.method, origin=origin, path=request.path, query=query)  # type: ignore[no-any-return]

    # -- public API ----------------------------------------------------------------

    @property
    def public_base_url(self) -> str | None:
        """The configured ``public_base_url`` (origin + root path), or ``None``."""
        return self._origin + self._root_path if self._origin else None

    @property
    def root_path(self) -> str:
        """The public path prefix every path from :meth:`path_for` starts with."""
        return self._root_path

    def path_for(self, target: Any, /, *, route: str | None = None, **params: Any) -> URL:
        """The public path of a route (``/backend/users/7``)."""
        return URL(join_root(self._root_path, self._route_path(target, route, params)))

    def url_for(self, target: Any, /, *, route: str | None = None, **params: Any) -> URL:
        """The absolute URL of a route on ``public_base_url`` (``RuntimeError`` when unset)."""
        return URL(self._require_origin("url_for") + self.path_for(target, route=route, **params))

    def ws_url_for(self, target: Any, /, **params: Any) -> URL:
        """The absolute ``ws://`` / ``wss://`` URL of a gateway on ``public_base_url``."""
        origin = self._require_origin("ws_url_for")
        scheme, _, rest = origin.partition("://")
        path = join_root(self._root_path, self._gateway_path(target, params))
        return URL(f"{HTTP_TO_WS.get(scheme, scheme)}://{rest}{path}")

    def signed_url_for(
        self,
        target: Any,
        /,
        *,
        expires_in: Any = None,
        query: Any = None,
        route: str | None = None,
        **params: Any,
    ) -> URL:
        """A signed link to a route, verified by the :class:`ValidSignature` guard.

        ``expires_in`` (seconds or a ``timedelta``) defaults to
        ``UrlSigningConfig.default_ttl``; ``query`` (a mapping or pairs) adds
        signed query parameters. The URL is absolute when ``public_base_url``
        is configured, otherwise a root-path-aware path. ``RuntimeError`` when
        ``url_signing=`` is not configured.
        """
        entry = self._routes.http_route(target, route)
        path = entry.template.expand(params, label=entry.label)
        return self._signed(entry.method, path, query, expires_in, ws=False)

    def signed_ws_url_for(
        self, target: Any, /, *, expires_in: Any = None, query: Any = None, **params: Any
    ) -> URL:
        """A signed ``ws://`` / ``wss://`` link to a gateway (name or class); needs ``public_base_url``."""
        self._require_origin("signed_ws_url_for")
        entry = self._routes.ws_route(target)
        path = entry.template.expand(params, label=entry.label)
        return self._signed("GET", path, query, expires_in, ws=True)

    def _signed(self, method: str, path: str, query: Any, expires_in: Any, *, ws: bool) -> URL:
        signer = self._signer
        if signer is None:
            raise RuntimeError(
                "url_signing is not configured: pass url_signing=UrlSigningConfig(secret=...) to LaurenFactory.create"
            )
        items: list[tuple[str, Any]] = []
        if query is not None:
            items.extend((str(k), v) for k, v in (query.items() if isinstance(query, Mapping) else query))
        signed_query = signer.sign(
            method=method, origin=self._origin, path=path, query=items, expires_in=expires_in
        )
        full = f"{join_root(self._root_path, path)}?{signed_query}"
        if not self._origin:
            return URL(full)
        if ws:
            scheme, _, rest = self._origin.partition("://")
            return URL(f"{HTTP_TO_WS.get(scheme, scheme)}://{rest}{full}")
        return URL(self._origin + full)

    def _require_origin(self, method: str) -> str:
        if not self._origin:
            raise RuntimeError(
                f"UrlBuilder.{method} needs public_base_url= on LaurenFactory.create "
                "(absolute URLs never come from a request's Host header outside the request)"
            )
        return self._origin


def split_base_url(value: str) -> tuple[str, str]:
    """``"https://api.example.com/backend/"`` → ``("https://api.example.com", "/backend")``.

    Raises :class:`ValueError` when ``value`` is not an absolute ``http(s)``
    URL with a valid host and no query, fragment or credentials.
    """
    from urllib.parse import urlsplit

    if not isinstance(value, str) or any(ch in value for ch in " \t\r\n<>\\"):
        raise ValueError("expected an absolute http(s) URL")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        raise ValueError("expected an absolute http(s) URL without credentials")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise ValueError("a base URL has no query or fragment")
    authority = parse_authority(parts.netloc)
    if authority is None:
        raise ValueError(f"{parts.netloc!r} is not a valid host[:port]")
    host, port = authority
    path = parts.path.rstrip("/")
    if path.startswith("//") or any(segment in (".", "..") for segment in path.split("/")):
        raise ValueError("a public base URL path must have a single leading slash and no dot segments")
    return compose_origin(parts.scheme, host, port), path


__all__ = ["NamedRoute", "RouteSpec", "RouteTable", "UrlBuilder", "split_base_url"]
