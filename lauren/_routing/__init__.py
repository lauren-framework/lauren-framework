"""Radix-tree (compressed Patricia trie) router.

The router supports static segments, parameters ``{name}`` /
``{name:converter}`` and the wildcard ``{*name}`` (``{name:path}``).
Lookups are O(depth); a typed parameter costs one ``matches`` call per
candidate. Routes are registered during startup and the tree becomes
immutable thereafter. Semantics — implicit ``HEAD`` / ``OPTIONS``, the
trailing-slash policy, slash merging, raw-path matching and custom
converters — come from a :class:`RoutingConfig`.

Tree shape
----------

A node has static children (a dict), parameter children (a list ordered by
converter specificity, then declaration order) and at most one wildcard
child. Parameter *names* are not part of the tree: they belong to each
:class:`RouteEntry`, so ``/users/{id}`` and ``/users/{user_id}/posts``
share one position. The walk collects positional captures and the matched
entry zips them with its own names once.

Fast-path optimisation
----------------------

When a route consists entirely of static segments the lookup is reducible
to a single flat dict lookup keyed on the path. The router builds this flat
table at :meth:`Router.freeze` time and consults it in :meth:`Router.find`
*before* descending the radix tree, so a static-route lookup avoids
splitting the path, allocating a params dict and the recursive walk.

Host routing
------------

Routes registered with ``host=`` live in one sub-router per host pattern.
:meth:`Router.find_host` tries the exact-host router, then the matching
parameterized host routers (fewer parameters first), then the host-agnostic
tree. A router without host routes never pays for the step.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, unquote, unquote_to_bytes

from ..exceptions import (
    BadRequestError,
    MethodNotAllowedError,
    RouteNotFoundError,
    RouterConflictError,
)
from ._config import TRAILING_SLASH_POLICIES, RoutingConfig
from ._converters import (
    HostPattern,
    PathConverter,
    Segment,
    TemplateError,
    check_template,
    converter_rank,
    normalize_host,
    parse_host,
    parse_template,
)

#: ``RouteEntry.method`` of a method-agnostic route (``@any_method``).
ANY_METHOD = "*"

#: Characters kept verbatim when a decoded path is percent-encoded again
#: (RFC 3986 ``pchar`` minus ``%`` plus ``/``).
_PATH_SAFE = "/:@!$&'()*+,;="
#: The same inside one segment (an encoded ``/`` stays encoded).
_SEGMENT_SAFE = ":@!$&'()*+,;="


@dataclass
class RouteEntry:
    """A concrete registered route."""

    method: str
    path_template: str
    handler: Callable[..., Any]  # the unbound handler function
    handler_class: type | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    param_names: tuple[str, ...] = ()
    #: Parameter name → converter (``{id}`` gets the ``str`` converter,
    #: ``{*rest}`` the ``path`` one). :meth:`build_path` formats with them.
    converters: Mapping[str, PathConverter] = field(default_factory=dict)
    #: The template without converter specs (``/items/{id}``), as OpenAPI spells it.
    path_format: str = ""
    #: Canonical host pattern for host-routed entries (``{tenant}.example.com``).
    host: str | None = None
    #: Host parameter names (``HostParam`` sources) of a host-routed entry.
    host_param_names: tuple[str, ...] = ()
    #: ``(method, host + path_template)`` — the key of the app's compiled handler.
    handler_key: tuple[str, str] = field(init=False)
    _segments: tuple[Segment, ...] = field(default=(), repr=False, compare=False)
    _host_pattern: HostPattern | None = field(default=None, repr=False, compare=False)

    #: Full URL-generation name, assigned at startup (None for internal/ambiguous routes).
    name: str | None = None

    def __post_init__(self) -> None:
        if not self.path_format:
            self.path_format = self.path_template
        self.handler_key = (self.method, (self.host or "") + self.path_template)

    def build_path(self, params: Mapping[str, Any] | None = None, /, **kwargs: Any) -> str:
        """Return this route's percent-encoded path for ``params`` (no root path, no query).

        Each value is formatted by its converter's ``to_url`` and must match
        it; segment values are encoded with ``/`` escaped, the wildcard keeps
        its slashes. Raises ``KeyError`` for a missing parameter and
        ``ValueError`` for a value the converter rejects — the seam URL
        generation (``url_for``) builds on.
        """
        values = {**(params or {}), **kwargs}
        if not self._segments:
            return quote(self.path_template, safe=_PATH_SAFE)
        out: list[str] = []
        for seg in self._segments:
            if seg.kind == "static":
                out.append(quote(seg.text, safe=_SEGMENT_SAFE))
                continue
            if seg.text not in values:
                raise KeyError(f"missing path parameter {seg.text!r} for {self.path_template}")
            converter = seg.converter
            assert converter is not None
            text = converter.to_url(values[seg.text])
            if seg.kind == "wildcard":
                out.append(quote(text.lstrip("/"), safe=_PATH_SAFE))
                continue
            if not text or not converter.matches(text):
                raise ValueError(
                    f"{values[seg.text]!r} does not match {seg.template()} in {self.path_template}"
                )
            out.append(quote(text, safe=_SEGMENT_SAFE))
        return "/" + "/".join(out)

    def build_host(self, params: Mapping[str, Any] | None = None, /, **kwargs: Any) -> str | None:
        """Return the host for ``params`` (``None`` for a host-agnostic route)."""
        if self._host_pattern is None:
            return None
        return self._host_pattern.build({**(params or {}), **kwargs})


NodeKind = str  # "static" | "param" | "wildcard"


@dataclass(eq=False)
class _Node:
    """Radix-tree node."""

    kind: NodeKind = "static"
    #: Static text, or the converter spec of a param node.
    segment: str = ""
    children_static: dict[str, "_Node"] = field(default_factory=dict)
    #: Parameter children, most specific converter first.
    param_children: list["_Node"] = field(default_factory=list)
    child_wildcard: "_Node | None" = None
    handlers: dict[str, RouteEntry] = field(default_factory=dict)
    #: ``@any_method`` handler — used when no explicit method handler matches.
    any_handler: RouteEntry | None = None
    #: Converter of a typed param node; ``None`` for untyped ``{name}``
    #: (any non-empty segment, checked inline).
    converter: PathConverter | None = None
    rank: int = 0

    def has_routes(self) -> bool:
        return bool(self.handlers) or self.any_handler is not None


class RouteRedirect(Exception):
    """Internal routing outcome: the request path is a non-canonical spelling of ``path``.

    Raised by :meth:`Router.find` under ``trailing_slash="redirect"``; the
    dispatcher answers ``308`` with ``Location`` built from ``path``. Never
    reaches user code.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _split_segments(path: str) -> list[str]:
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    # normalize: drop trailing slash for non-root
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if path == "/":
        return []
    return path[1:].split("/")


def _normalize_path(path: str) -> str:
    """Return a canonical form used as the static-table lookup key.

    Matches the normalisation performed by :func:`_split_segments`
    so that ``/users`` and ``/users/`` hit the same entry.
    """
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


_SLASH_RUN = re.compile(r"/{2,}")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _merge_slashes(path: str) -> str:
    return _SLASH_RUN.sub("/", path)


def _toggle_trailing_slash(path: str) -> str | None:
    if path in ("", "/"):
        return None
    return path[:-1] if path.endswith("/") else path + "/"


def decode_segment(segment: str) -> str:
    """Strictly percent-decode one raw path segment (UTF-8); malformed input is a 400."""
    if "%" not in segment:
        return segment
    if _BAD_PERCENT.search(segment) is not None:
        raise BadRequestError(
            "malformed percent-encoding in the request path",
            detail={"reason": "malformed_path_encoding", "segment": segment},
        )
    try:
        data = unquote_to_bytes(segment.encode("latin-1"))
    except UnicodeEncodeError:
        data = unquote_to_bytes(segment)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise BadRequestError(
            "the request path is not valid UTF-8 once percent-decoded",
            detail={"reason": "malformed_path_encoding", "segment": segment},
        ) from None


def raw_routing_path(raw_path: bytes | None, path: str) -> str:
    """The raw (still percent-encoded) form of ``path`` for ``match_raw_path``.

    ``raw_path`` is the ASGI ``scope["raw_path"]`` — the whole request
    target path — while ``path`` is the decoded path routing sees after a
    root-path / mount prefix was stripped. The prefix is removed from the
    raw form segment by segment; without ``raw_path`` (it is optional in
    ASGI) the decoded path is re-encoded instead.
    """
    if raw_path:
        raw = raw_path.decode("latin-1").partition("?")[0]
        if raw.startswith("/"):
            decoded = unquote(raw)
            if decoded == path:
                return raw
            if path.startswith("/") and decoded.endswith(path):
                prefix = decoded[: len(decoded) - len(path)]
                segments = raw[1:].split("/")
                consumed = ""
                for i, seg in enumerate(segments):
                    if consumed == prefix:
                        return "/" + "/".join(segments[i:])
                    consumed += "/" + unquote(seg)
                    if len(consumed) > len(prefix):
                        break
    return quote(path, safe=_PATH_SAFE)


#: Sentinel empty dict returned from every static-fast-path hit.
#: All static routes share this object because the framework never
#: mutates the params dict returned from :meth:`Router.find` — it
#: only reads from it. Sharing avoids one allocation per request on
#: the hot path.
_EMPTY_PARAMS: dict[str, str] = {}


class Router:
    """In-memory radix router with a static-path fast path.

    ``config`` sets the HTTP semantics (default :class:`RoutingConfig()`);
    ``methods`` the accepted method names (an instance property — the
    WebSocket router accepts only ``"WEBSOCKET"``).
    """

    HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})

    def __init__(self, config: RoutingConfig | None = None, *, methods: Iterable[str] | None = None) -> None:
        cfg = config if config is not None else RoutingConfig()
        if not isinstance(cfg, RoutingConfig):
            raise TypeError(f"Router(config=...) expects a RoutingConfig, got {type(cfg).__name__}")
        self._config = cfg
        self._methods: frozenset[str] = (
            frozenset(m.upper() for m in methods) if methods is not None else self.HTTP_METHODS
        )
        self._auto_head = cfg.auto_head
        self._auto_options = cfg.auto_options and "OPTIONS" in self._methods
        assert cfg.trailing_slash in TRAILING_SLASH_POLICIES
        self._keep_trailing = cfg.trailing_slash != "ignore"
        self._redirect = cfg.trailing_slash == "redirect"
        self._merge = cfg.merge_slashes
        self._raw = cfg.match_raw_path
        #: Default path policies — :meth:`find` inlines the static fast path.
        self._simple = not (self._keep_trailing or self._merge or self._raw)
        self._converters: Mapping[str, PathConverter] = cfg.converters
        self._root = _Node()
        self._frozen = False
        self._all_routes: list[RouteEntry] = []
        # Static fast path: canonical path → node. Populated at
        # :meth:`freeze`, consulted first inside :meth:`find`. Dynamic
        # routes are deliberately excluded so a lookup miss cleanly
        # falls through to the full radix walk.
        self._static_table: dict[str, _Node] = {}
        # Flag indicates whether *any* dynamic (param/wildcard) routes
        # exist. When false — which is common for health-check apps,
        # static file servers, and API gateways — a static-table miss
        # skips the radix walk entirely and raises directly.
        self._has_dynamic_routes = False
        # Host routing: one sub-router per host pattern.
        self._host_routers: dict[str, tuple[HostPattern, Router]] = {}
        self._exact_hosts: dict[str, Router] = {}
        self._param_hosts: list[tuple[HostPattern, Router]] = []

    # -- Configuration -------------------------------------------------------

    @property
    def config(self) -> RoutingConfig:
        return self._config

    @property
    def methods(self) -> frozenset[str]:
        """Method names :meth:`add_route` accepts (besides ``"*"``)."""
        return self._methods

    @property
    def has_hosts(self) -> bool:
        """Whether any route was registered with ``host=`` (use :meth:`find_host`)."""
        return bool(self._host_routers)

    # -- Registration ------------------------------------------------------

    def add_route(
        self,
        method: str,
        path: str,
        handler: Callable[..., Any],
        *,
        handler_class: type | None = None,
        metadata: dict[str, Any] | None = None,
        host: str | None = None,
    ) -> RouteEntry:
        """Register ``method path`` (``method="*"`` for a method-agnostic route).

        Raises :class:`RouterConflictError` for a duplicate route (same
        method, same template up to parameter names, same host) and for a
        host parameter that shadows a path parameter; a malformed template
        raises :class:`~lauren.exceptions.DecoratorUsageError`.
        """
        if self._frozen:
            raise RuntimeError("Router is frozen; cannot register new routes.")
        method = method.upper()
        if method != ANY_METHOD and method not in self._methods:
            raise ValueError(f"Unsupported HTTP method: {method}")
        if host is None:
            entry = self._insert(method, path, handler, handler_class, metadata, None)
        else:
            pattern = self._parse_host(host, path)
            sub = self._host_router(pattern)
            entry = sub._insert(method, path, handler, handler_class, metadata, pattern)
        self._all_routes.append(entry)
        return entry

    def _parse_host(self, host: str, path: str) -> HostPattern:
        try:
            return parse_host(host, self._converters)
        except TemplateError as exc:
            from ..exceptions import DecoratorUsageError

            raise DecoratorUsageError(
                f"invalid host pattern {host!r} for {path}: {exc}",
                detail={"host": host, "path": path, "reason": exc.reason},
            ) from None

    def _host_router(self, pattern: HostPattern) -> Router:
        known = self._host_routers.get(pattern.canonical)
        if known is not None:
            return known[1]
        sub = Router(self._config, methods=self._methods)
        self._host_routers[pattern.canonical] = (pattern, sub)
        if pattern.is_exact:
            self._exact_hosts[pattern.canonical] = sub
        else:
            self._param_hosts.append((pattern, sub))
            # Fewer parameters first; ``sort`` is stable, so ties keep
            # declaration order.
            self._param_hosts.sort(key=lambda item: len(item[0].param_names))
        return sub

    def _insert(
        self,
        method: str,
        path: str,
        handler: Callable[..., Any],
        handler_class: type | None,
        metadata: dict[str, Any] | None,
        host: HostPattern | None,
    ) -> RouteEntry:
        try:
            segments = parse_template(path, self._converters, keep_trailing_slash=self._keep_trailing)
        except TemplateError as exc:
            if exc.reason == "wildcard_not_last":
                raise RouterConflictError(
                    "Wildcard must be the last segment", detail={"path": path}
                ) from None
            from ..exceptions import DecoratorUsageError

            raise DecoratorUsageError(
                f"invalid route path {path!r}: {exc}",
                detail={"path": path, "reason": exc.reason},
            ) from None
        param_names = tuple(seg.text for seg in segments if seg.kind != "static")
        if host is not None:
            clash = sorted(set(param_names) & set(host.param_names))
            if clash:
                raise RouterConflictError(
                    f"host parameter {clash[0]!r} of {host.source!r} is also a path parameter of {path}",
                    detail={"host": host.source, "path": path, "param": clash[0]},
                )
        node = self._root
        is_static_route = True
        for seg in segments:
            if seg.kind == "static":
                child = node.children_static.get(seg.text)
                if child is None:
                    child = _Node(kind="static", segment=seg.text)
                    node.children_static[seg.text] = child
                node = child
            elif seg.kind == "param":
                is_static_route = False
                node = self._param_child(node, seg)
            else:
                is_static_route = False
                if node.child_wildcard is None:
                    node.child_wildcard = _Node(kind="wildcard", segment="*")
                node = node.child_wildcard
        template = _canonical_path(segments)
        if method == ANY_METHOD:
            if node.any_handler is not None:
                raise RouterConflictError(
                    f"Route already registered: any method {template}",
                    detail={"method": method, "path": path, "existing": node.any_handler.path_template},
                )
        elif method in node.handlers:
            raise RouterConflictError(
                f"Route already registered: {method} {path}",
                detail={"method": method, "path": path, "existing": node.handlers[method].path_template},
            )
        entry = RouteEntry(
            method=method,
            path_template=template,
            handler=handler,
            handler_class=handler_class,
            metadata=metadata or {},
            param_names=param_names,
            converters={seg.text: seg.converter for seg in segments if seg.converter is not None},
            path_format=_format_path(segments),
            host=host.canonical if host is not None else None,
            host_param_names=host.param_names if host is not None else (),
            _segments=tuple(segments),
            _host_pattern=host,
        )
        if method == ANY_METHOD:
            node.any_handler = entry
        else:
            node.handlers[method] = entry
        if not is_static_route:
            self._has_dynamic_routes = True
        return entry

    def _param_child(self, node: _Node, seg: Segment) -> _Node:
        key = seg.node_key
        for child in node.param_children:
            if child.segment == key:
                return child
        rank = converter_rank(seg.converter)
        # ``{name}`` / ``{name:str}`` (any non-empty segment) is checked inline.
        converter = None if key == "str" else seg.converter
        child = _Node(kind="param", segment=key, converter=converter, rank=rank)
        # Keep the list ordered by rank; declaration order breaks ties.
        index = len(node.param_children)
        for i, other in enumerate(node.param_children):
            if other.rank > rank:
                index = i
                break
        node.param_children.insert(index, child)
        return child

    def freeze(self) -> None:
        """Freeze the router and build the static-route fast table.

        After ``freeze()`` no further routes may be added. The flat
        ``{path: node}`` dict built here is consulted by :meth:`find`
        before the radix walk; a hit skips segment splitting and
        recursion entirely.
        """
        if self._frozen:
            return
        self._static_table = {}
        self._collect_static_routes(self._root, [], self._static_table)
        for _, sub in self._host_routers.values():
            sub.freeze()
        self._frozen = True

    def _collect_static_routes(
        self,
        node: _Node,
        prefix: list[str],
        out: dict[str, _Node],
    ) -> None:
        """Depth-first collect every static-only route into ``out``.

        ``prefix`` accumulates the static segments seen from the root
        so we can form the canonical path when we land on a node that
        has registered handlers. Nodes reached through a param or
        wildcard edge are *not* recursed into — those sub-trees
        describe dynamic routes that the fast path cannot serve.
        """
        if node.has_routes():
            out["/" + "/".join(prefix) if prefix else "/"] = node
        for seg, child in node.children_static.items():
            prefix.append(seg)
            self._collect_static_routes(child, prefix, out)
            prefix.pop()

    @property
    def frozen(self) -> bool:
        return self._frozen

    def routes(self) -> list[RouteEntry]:
        """Every registered entry, host-routed ones included, in registration order."""
        return list(self._all_routes)

    # -- Introspection -----------------------------------------------------

    @property
    def static_route_count(self) -> int:
        """Number of static-only routes registered on this router.

        Counts methods, not paths: ``GET /health`` and ``HEAD /health``
        contribute two. Useful for apps that want to emit a boot-time
        log line reporting how much of their routing table is served
        by the fast path.
        """
        return sum(
            len(node.handlers) + (node.any_handler is not None) for node in self._static_table.values()
        )

    # -- Lookup ------------------------------------------------------------

    def find(self, method: str, path: str) -> tuple[RouteEntry, dict[str, str]]:
        """Find a route entry for (method, path) among the host-agnostic routes.

        Consults the static fast-path table first; falls back to the
        radix walk for routes that contain parameters or wildcards.
        ``HEAD`` falls back to the ``GET`` handler under ``auto_head``.

        Raises :class:`RouteNotFoundError` or :class:`MethodNotAllowedError`
        (whose ``allow`` lists ``HEAD`` / ``OPTIONS`` as configured), a
        :class:`~lauren.exceptions.BadRequestError` for malformed encoding
        under ``match_raw_path``, and :class:`RouteRedirect` under
        ``trailing_slash="redirect"``.
        """
        method = method.upper()
        if self._simple:
            # Default policies: the static fast path, inlined.
            key = _normalize_path(path)
            static_node = self._static_table.get(key)
            if static_node is not None:
                entry = static_node.handlers.get(method)
                if entry is not None:
                    return entry, _EMPTY_PARAMS
            return self._resolve_key(method, path, key, static_node)
        if self._redirect or self._merge:
            return self._find_with_policies(((self, _EMPTY_PARAMS),), method, path)[:2]  # type: ignore[return-value]
        return self._resolve(method, path)

    def find_host(
        self, method: str, path: str, host: str | None
    ) -> tuple[RouteEntry, dict[str, str], dict[str, str]]:
        """Like :meth:`find`, considering host-routed entries for the ``Host`` value ``host``.

        Returns ``(entry, path_params, host_params)``. Candidates are tried
        in order — the exact-host router, matching parameterized host
        routers (fewer parameters first), the host-agnostic routes — and a
        path miss in one falls through to the next.
        """
        method = method.upper()
        return self._find_with_policies(self._candidates(host), method, path)

    def _candidates(self, host: str | None) -> list[tuple[Router, dict[str, str]]]:
        out: list[tuple[Router, dict[str, str]]] = []
        if host and self._host_routers:
            name = normalize_host(host)
            exact = self._exact_hosts.get(name)
            if exact is not None:
                out.append((exact, _EMPTY_PARAMS))
            if self._param_hosts:
                labels = name.split(".")
                for pattern, sub in self._param_hosts:
                    params = pattern.match(labels)
                    if params is not None:
                        out.append((sub, params))
        out.append((self, _EMPTY_PARAMS))
        return out

    def _find_with_policies(
        self,
        candidates: Iterable[tuple[Router, dict[str, str]]],
        method: str,
        path: str,
    ) -> tuple[RouteEntry, dict[str, str], dict[str, str]]:
        candidates = tuple(candidates)
        if self._merge and "//" in path:
            merged = _merge_slashes(path)
            if self._redirect:
                for target in (merged, _toggle_trailing_slash(merged)):
                    if target is not None and any(sub._node_for(target) is not None for sub, _ in candidates):
                        raise RouteRedirect(target)
            else:
                path = merged
        allow: set[str] | None = None
        for sub, host_params in candidates:
            try:
                entry, params = sub._resolve(method, path)
            except RouteNotFoundError:
                continue
            except MethodNotAllowedError as exc:
                allow = (allow or set()) | set(exc.allow)
                continue
            return entry, params, host_params
        if allow is not None:
            raise MethodNotAllowedError(f"{method} not allowed for {path}", allow=sorted(allow))
        if self._redirect:
            target = _toggle_trailing_slash(path)
            if target is not None and any(sub._node_for(target) is not None for sub, _ in candidates):
                raise RouteRedirect(target)
        raise RouteNotFoundError(f"No route matches {method} {path}")

    def _resolve(self, method: str, path: str) -> tuple[RouteEntry, dict[str, str]]:
        """One tree's lookup (no redirects, no host step) — the hot path."""
        if self._raw and "%" in path:
            return self._walk(method, path, self._request_segments(path))
        if self._keep_trailing:
            key = path if path.startswith("/") else "/" + path
        else:
            key = _normalize_path(path)
        return self._resolve_key(method, path, key, self._static_table.get(key))

    def _resolve_key(
        self, method: str, path: str, key: str, static_node: _Node | None
    ) -> tuple[RouteEntry, dict[str, str]]:
        """:meth:`_resolve` once the static-table key and its node are known."""
        if static_node is not None:
            entry = static_node.handlers.get(method) or self._fallback(static_node, method)
            if entry is not None:
                # ``_EMPTY_PARAMS`` is shared across every fast-path
                # hit — safe because no downstream code mutates it.
                return entry, _EMPTY_PARAMS
            # The static path has no handler for this method. Try dynamic
            # siblings (at *any* depth) before 405: POST /items/special
            # can be served by POST /items/{slug} despite a GET-only
            # /items/special. The method-aware walk backtracks from the
            # static leaf instead of skipping its whole top-level prefix.
            if self._has_dynamic_routes:
                segments = [] if key == "/" else key[1:].split("/")
                caught: list[str] = []
                other = self._match(self._root, segments, 0, caught, wanted=method)
                if other is not None:
                    entry = other.handlers.get(method) or self._fallback(other, method)
                    assert entry is not None
                    return entry, dict(zip(entry.param_names, caught)) if caught else _EMPTY_PARAMS
            raise MethodNotAllowedError(f"{method} not allowed for {path}", allow=self._allow(static_node))
        if not self._has_dynamic_routes and self._frozen:
            raise RouteNotFoundError(f"No route matches {method} {path}")
        # The radix walk (``_walk`` inlined). ``key`` is already normalised:
        # split it instead of the raw path.
        return self._walk(method, path, [] if key == "/" else key[1:].split("/"))

    def _walk(self, method: str, path: str, segments: list[str]) -> tuple[RouteEntry, dict[str, str]]:
        captures: list[str] = []
        node = self._match(self._root, segments, 0, captures, wanted=method)
        if node is not None:
            entry = node.handlers.get(method) or self._fallback(node, method)
            assert entry is not None
            return entry, dict(zip(entry.param_names, captures)) if captures else _EMPTY_PARAMS
        # Only method misses need a second walk to distinguish 404 from
        # 405 and produce the Allow list of the highest-priority path.
        node = self._match(self._root, segments, 0, [])
        if node is None:
            raise RouteNotFoundError(f"No route matches {method} {path}")
        raise MethodNotAllowedError(f"{method} not allowed for {path}", allow=self._allow(node))

    def _request_segments(self, path: str) -> list[str]:
        if self._keep_trailing:
            if not path.startswith("/"):
                path = "/" + path
            segments = [] if path == "/" else path[1:].split("/")
        else:
            segments = _split_segments(path)
        if self._raw and "%" in path:
            segments = [decode_segment(seg) for seg in segments]
        return segments

    def _node_for(self, path: str) -> _Node | None:
        """The node ``path`` reaches (any method), or ``None``."""
        if not (self._raw and "%" in path):
            static_node = self._static_table.get(path if self._keep_trailing else _normalize_path(path))
            if static_node is not None:
                return static_node
        try:
            segments = self._request_segments(path)
        except BadRequestError:
            return None
        return self._match(self._root, segments, 0, [])

    def _fallback(self, node: _Node, method: str) -> RouteEntry | None:
        """The entry for ``method`` when the node has no explicit handler: GET for HEAD, then any-method."""
        if method == "HEAD" and self._auto_head:
            entry = node.handlers.get("GET")
            if entry is not None:
                return entry
        return node.any_handler

    def _allow(self, node: _Node) -> list[str]:
        if node.any_handler is not None:
            return sorted(self._methods)
        allowed = set(node.handlers)
        if self._auto_head and "GET" in allowed:
            allowed.add("HEAD")
        if self._auto_options and allowed:
            allowed.add("OPTIONS")
        return sorted(allowed)

    def _match(
        self,
        node: _Node,
        segments: list[str],
        idx: int,
        captures: list[str],
        wanted: str | None = None,
    ) -> _Node | None:
        """Walk the radix tree, appending positional captures to ``captures``.

        When ``wanted`` is set, a path-only match with no handler for that
        method is not a terminal hit: backtrack into the next sibling. When
        ``None``, find the highest-priority path (for 405 / redirect).
        """
        if idx == len(segments):
            if node.has_routes() and (
                wanted is None
                or node.handlers.get(wanted) is not None
                or self._fallback(node, wanted) is not None
            ):
                return node
            # allow falling through to wildcard match with empty remainder
            if node.child_wildcard is not None and (
                wanted is None
                or node.child_wildcard.handlers.get(wanted) is not None
                or self._fallback(node.child_wildcard, wanted) is not None
            ):
                captures.append("")
                return node.child_wildcard
            return None
        seg = segments[idx]
        # 1) static (highest priority), but a method miss in its subtree
        # does not shadow a matching typed / wildcard sibling.
        static_child = node.children_static.get(seg)
        if static_child is not None:
            result = self._match(static_child, segments, idx + 1, captures, wanted)
            if result is not None:
                return result
        # 2) params, most specific converter first; a converter mismatch
        # (or an empty segment) is a non-match and the next one is tried.
        if seg:
            for child in node.param_children:
                converter = child.converter
                if converter is not None and not converter.matches(seg):
                    continue
                captures.append(seg)
                result = self._match(child, segments, idx + 1, captures, wanted)
                if result is not None:
                    return result
                captures.pop()
        # 3) wildcard (captures remainder)
        if node.child_wildcard is not None and (
            wanted is None
            or node.child_wildcard.handlers.get(wanted) is not None
            or self._fallback(node.child_wildcard, wanted) is not None
        ):
            captures.append("/".join(segments[idx:]))
            return node.child_wildcard
        return None

    def allowed_methods(self, path: str) -> list[str]:
        """Methods ``path`` answers (``HEAD`` / ``OPTIONS`` as configured); ``[]`` for no route."""
        node = self._node_for(path)
        return self._allow(node) if node is not None else []


def _canonical_path(segments: Iterable[Segment]) -> str:
    out = [seg.template() for seg in segments]
    return "/" + "/".join(out) if out else "/"


def _format_path(segments: Iterable[Segment]) -> str:
    out = [seg.format() for seg in segments]
    return "/" + "/".join(out) if out else "/"


__all__ = [
    "ANY_METHOD",
    "PathConverter",
    "RouteEntry",
    "RouteRedirect",
    "Router",
    "RoutingConfig",
    "check_template",
    "decode_segment",
    "raw_routing_path",
]
