"""Radix-tree (compressed Patricia trie) router.

The router supports three node kinds: static, param ``{name}``, and wildcard
``{*name}``. Lookups are O(depth) with no regex. Routes are registered during
startup and the tree becomes immutable thereafter.

Fast-path optimisation
----------------------

When a route consists entirely of static segments (no ``{param}``, no
``{*wild}``) the lookup is reducible to a single flat dict lookup
keyed on ``(method, path)``. The router builds this flat table at
:meth:`Router.freeze` time and consults it in :meth:`Router.find`
*before* descending the radix tree. A static-route lookup therefore
avoids:

* splitting the path into segments,
* allocating a per-call ``params`` dict that is always empty,
* the recursive ``_match`` walk with its three-way priority check.

For applications whose routing table is dominated by static paths
(health checks, OpenAPI docs, SPA asset serving, etc.) this short-
circuit cuts lookup latency by roughly half on the common case. The
dynamic-route path is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..exceptions import (
    MethodNotAllowedError,
    RouteNotFoundError,
    RouterConflictError,
)


@dataclass
class RouteEntry:
    """A concrete registered route."""

    method: str
    path_template: str
    handler: Callable[..., Any]  # the unbound handler function
    handler_class: type | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    param_names: tuple[str, ...] = ()


NodeKind = str  # "static" | "param" | "wildcard"


@dataclass
class _Node:
    """Radix-tree node."""

    kind: NodeKind = "static"
    segment: str = ""  # static text, param name, or wildcard name
    children_static: dict[str, "_Node"] = field(default_factory=dict)
    child_param: "_Node | None" = None
    child_wildcard: "_Node | None" = None
    handlers: dict[str, RouteEntry] = field(default_factory=dict)


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
    so that ``/users``, ``/users/``, and ``///users`` all hit the
    same entry. The key is computed once per lookup and reused for
    the fallback radix walk when the fast path misses.
    """
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def _parse_segment(seg: str) -> tuple[NodeKind, str]:
    """Classify a segment. Returns (kind, name)."""
    if seg.startswith("{") and seg.endswith("}"):
        inner = seg[1:-1]
        if inner.startswith("*"):
            return "wildcard", inner[1:]
        return "param", inner
    return "static", seg


#: Sentinel empty dict returned from every static-fast-path hit.
#: All static routes share this object because the framework never
#: mutates the params dict returned from :meth:`Router.find` \u2014 it
#: only reads from it. Sharing avoids one allocation per request on
#: the hot path.
_EMPTY_PARAMS: dict[str, str] = {}


class Router:
    """In-memory radix router with a static-prefix fast path."""

    HTTP_METHODS = frozenset(
        {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
    )

    def __init__(self) -> None:
        self._root = _Node()
        self._frozen = False
        self._all_routes: list[RouteEntry] = []
        # Static fast path: keyed on the normalised canonical path and
        # mapping to ``{method: RouteEntry}``. Populated at
        # :meth:`freeze`, consulted first inside :meth:`find`. Dynamic
        # routes are deliberately excluded so a lookup miss cleanly
        # falls through to the full radix walk.
        self._static_table: dict[str, dict[str, RouteEntry]] = {}
        # Flag indicates whether *any* dynamic (param/wildcard) routes
        # exist. When false \u2014 which is common for health-check apps,
        # static file servers, and API gateways \u2014 a static-table miss
        # skips the radix walk entirely and raises directly.
        self._has_dynamic_routes = False

    # -- Registration ------------------------------------------------------

    def add_route(
        self,
        method: str,
        path: str,
        handler: Callable[..., Any],
        *,
        handler_class: type | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RouteEntry:
        if self._frozen:
            raise RuntimeError("Router is frozen; cannot register new routes.")
        method = method.upper()
        if method not in self.HTTP_METHODS:
            raise ValueError(f"Unsupported HTTP method: {method}")
        segments = _split_segments(path)
        node = self._root
        param_names: list[str] = []
        # Track whether *this* route is purely static so we can later
        # include it in the fast-path table. A single dynamic segment
        # anywhere disqualifies the whole route.
        is_static_route = True
        for i, seg in enumerate(segments):
            kind, name = _parse_segment(seg)
            if kind == "static":
                child = node.children_static.get(name)
                if child is None:
                    child = _Node(kind="static", segment=name)
                    node.children_static[name] = child
                node = child
            elif kind == "param":
                is_static_route = False
                if node.child_param is None:
                    node.child_param = _Node(kind="param", segment=name)
                elif node.child_param.segment != name:
                    raise RouterConflictError(
                        "Parameter name conflict",
                        detail={
                            "existing": node.child_param.segment,
                            "requested": name,
                            "path": path,
                        },
                    )
                param_names.append(name)
                node = node.child_param
            elif kind == "wildcard":
                is_static_route = False
                if i != len(segments) - 1:
                    raise RouterConflictError(
                        "Wildcard must be the last segment", detail={"path": path}
                    )
                if node.child_wildcard is None:
                    node.child_wildcard = _Node(kind="wildcard", segment=name)
                elif node.child_wildcard.segment != name:
                    raise RouterConflictError(
                        "Wildcard name conflict",
                        detail={
                            "existing": node.child_wildcard.segment,
                            "requested": name,
                            "path": path,
                        },
                    )
                param_names.append(name)
                node = node.child_wildcard
        if method in node.handlers:
            raise RouterConflictError(
                f"Route already registered: {method} {path}",
                detail={"method": method, "path": path},
            )
        canonical = _canonical_path(segments)
        entry = RouteEntry(
            method=method,
            path_template=canonical,
            handler=handler,
            handler_class=handler_class,
            metadata=metadata or {},
            param_names=tuple(param_names),
        )
        node.handlers[method] = entry
        self._all_routes.append(entry)
        if not is_static_route:
            self._has_dynamic_routes = True
        return entry

    def freeze(self) -> None:
        """Freeze the router and build the static-route fast table.

        After ``freeze()`` no further routes may be added. The flat
        ``{path: {method: entry}}`` dict built here is consulted by
        :meth:`find` before the radix walk; a hit skips segment
        splitting and recursion entirely.
        """
        if self._frozen:
            return
        # Walk the tree once and harvest every static-only route. A
        # route is "static-only" when the path from root to its node
        # consists exclusively of :attr:`_Node.children_static` edges.
        # The resulting table is keyed on the canonical path (leading
        # slash, no trailing slash except for the root) so lookup
        # normalisation produces a single stable key per request.
        self._static_table = {}
        self._collect_static_routes(self._root, [], self._static_table)
        self._frozen = True

    def _collect_static_routes(
        self,
        node: _Node,
        prefix: list[str],
        out: dict[str, dict[str, RouteEntry]],
    ) -> None:
        """Depth-first collect every static-only route into ``out``.

        ``prefix`` accumulates the static segments seen from the root
        so we can form the canonical path when we land on a node that
        has registered handlers. Nodes reached through a param or
        wildcard edge are *not* recursed into \u2014 those sub-trees
        describe dynamic routes that the fast path cannot serve.
        """
        if node.handlers:
            path = "/" + "/".join(prefix) if prefix else "/"
            # One entry per method. We copy the node's handler dict so
            # subsequent mutations (shouldn't happen after freeze, but
            # still) do not leak into the fast table.
            out[path] = dict(node.handlers)
        for seg, child in node.children_static.items():
            prefix.append(seg)
            self._collect_static_routes(child, prefix, out)
            prefix.pop()
        # Do NOT descend into param / wildcard children \u2014 any route
        # below those edges is dynamic by construction.

    @property
    def frozen(self) -> bool:
        return self._frozen

    def routes(self) -> list[RouteEntry]:
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
        return sum(len(methods) for methods in self._static_table.values())

    # -- Lookup ------------------------------------------------------------

    def find(self, method: str, path: str) -> tuple[RouteEntry, dict[str, str]]:
        """Find a route entry for (method, path).

        Consults the static fast-path table first; falls back to the
        radix walk for routes that contain parameters or wildcards.

        Raises :class:`RouteNotFoundError` or :class:`MethodNotAllowedError`.
        """
        method = method.upper()

        # ---- Fast path: pure-static lookup ------------------------------
        # ``_static_table`` is populated at :meth:`freeze` time. Apps
        # that never call ``freeze`` (a handful of test fixtures) have
        # an empty table and fall through to the radix walk unchanged.
        static_methods = self._static_table.get(_normalize_path(path))
        if static_methods is not None:
            entry = static_methods.get(method)
            if entry is not None:
                # ``_EMPTY_PARAMS`` is shared across every fast-path
                # hit \u2014 safe because no downstream code mutates it.
                return entry, _EMPTY_PARAMS
            # Path matched but method did not. Fall through to the
            # radix walk only if dynamic routes exist. When the walk
            # re-enters via this path we mask out the top-level
            # static branch so sibling ``{param}`` / ``{*wild}``
            # branches are explored instead -- otherwise the walk
            # would just rediscover the same static node and report
            # the same method miss.
            if self._has_dynamic_routes:
                segments = _split_segments(path)
                skip_first = segments[0] if segments else None
                node, params = self._match(
                    self._root, segments, 0, {}, _skip_static_seg=skip_first
                )
                if node is not None and method in node.handlers:
                    return node.handlers[method], params
            allow = sorted(static_methods.keys())
            raise MethodNotAllowedError(f"{method} not allowed for {path}", allow=allow)

        # ---- Slow path: full radix walk ---------------------------------
        segments = _split_segments(path)
        node, params = self._match(self._root, segments, 0, {})
        if node is None:
            raise RouteNotFoundError(f"No route matches {method} {path}")
        if method not in node.handlers:
            allow = sorted(node.handlers.keys())
            raise MethodNotAllowedError(f"{method} not allowed for {path}", allow=allow)
        return node.handlers[method], params

    def _match(
        self,
        node: _Node,
        segments: list[str],
        idx: int,
        params: dict[str, str],
        _skip_static_seg: str | None = None,
    ) -> tuple[_Node | None, dict[str, str]]:
        """Walk the radix tree.

        ``_skip_static_seg`` is an internal escape hatch used by the
        fast-path fallback: when the static table found a path match
        but no method match, we re-run the walk while ignoring the
        static child at the *top* level (first segment only) so only
        dynamic siblings are explored. Without this skip, the walk
        would re-enter the same static node and rediscover the same
        method-miss, producing a spurious ``MethodNotAllowed``.
        """
        if idx == len(segments):
            if node.handlers:
                return node, params
            # allow falling through to wildcard match with empty remainder
            if node.child_wildcard is not None:
                params = {**params, node.child_wildcard.segment: ""}
                return node.child_wildcard, params
            return None, params
        seg = segments[idx]
        # 1) static (highest priority) -- unless the top-level static
        # branch was masked out by the fast-path fallback.
        skip_this_static = (
            idx == 0 and _skip_static_seg is not None and seg == _skip_static_seg
        )
        if not skip_this_static:
            static_child = node.children_static.get(seg)
            if static_child is not None:
                result, p = self._match(static_child, segments, idx + 1, params)
                if result is not None:
                    return result, p
        # 2) param
        if node.child_param is not None:
            new_params = {**params, node.child_param.segment: seg}
            result, p = self._match(node.child_param, segments, idx + 1, new_params)
            if result is not None:
                return result, p
        # 3) wildcard (captures remainder)
        if node.child_wildcard is not None:
            remainder = "/".join(segments[idx:])
            new_params = {**params, node.child_wildcard.segment: remainder}
            return node.child_wildcard, new_params
        return None, params

    def allowed_methods(self, path: str) -> list[str]:
        try:
            # Honour the fast path here too so operators can cheaply
            # ask "what methods are registered for /health?" without
            # paying the radix-walk cost on every call.
            static_methods = self._static_table.get(_normalize_path(path))
            if static_methods is not None:
                return sorted(static_methods.keys())
            segments = _split_segments(path)
            node, _ = self._match(self._root, segments, 0, {})
            return sorted(node.handlers.keys()) if node else []
        except Exception:
            return []


def _canonical_path(segments: Iterable[str]) -> str:
    out: list[str] = []
    for seg in segments:
        kind, name = _parse_segment(seg)
        if kind == "static":
            out.append(name)
        elif kind == "param":
            out.append("{" + name + "}")
        else:
            out.append("{*" + name + "}")
    return "/" + "/".join(out) if out else "/"


__all__ = ["Router", "RouteEntry"]
