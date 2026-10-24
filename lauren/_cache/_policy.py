"""``@cache_response`` / ``@idempotent`` — metadata-only route decorators.

Both validate their arguments immediately (``DecoratorUsageError``), store a
frozen policy on the handler function and return the same object. The
factory compiles the policies in Phase 5 and appends the matching framework
interceptor as the route's innermost interceptor.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from ..decorators import _reject_bare_usage
from ..exceptions import DecoratorUsageError
from ..types import ExecutionContext

#: Attribute holding a :class:`CachePolicy` on a handler function.
CACHE_RESPONSE_META = "__lauren_cache_response__"
#: Attribute holding an :class:`IdempotencyPolicy` on a handler function.
IDEMPOTENT_META = "__lauren_idempotent__"

#: Statuses ``@cache_response`` stores by default (RFC 9111 "heuristically
#: cacheable" codes the handler returned, not raised).
DEFAULT_CACHEABLE_STATUSES: frozenset[int] = frozenset({200, 203, 204, 300, 301, 404, 405, 410})
_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

F = TypeVar("F")

KeyFunction = Callable[[ExecutionContext], Any]
TagsFunction = Callable[[ExecutionContext], Iterable[str]]


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """The compiled ``@cache_response(...)`` arguments of one handler."""

    ttl: float
    vary: tuple[str, ...] = ()
    key: KeyFunction | None = None
    tags: tuple[str, ...] | TagsFunction = ()
    methods: frozenset[str] = _SAFE_METHODS
    statuses: frozenset[int] = DEFAULT_CACHEABLE_STATUSES
    debug_header: bool = False
    respect_client_no_cache: bool = False
    #: Query parameters that take part in the key; ``None`` means all of them.
    query: frozenset[str] | None = None
    #: Store / serve requests carrying ``Authorization`` without a per-caller key.
    allow_authenticated: bool = False

    @property
    def per_caller(self) -> bool:
        """``True`` when the key separates callers (``key=`` or ``vary`` on credentials)."""
        return self.key is not None or "authorization" in self.vary or "cookie" in self.vary


@dataclass(frozen=True, slots=True)
class IdempotencyPolicy:
    """The compiled ``@idempotent(...)`` arguments of one handler."""

    ttl: float = 86_400.0
    header: str = "Idempotency-Key"
    required: bool = True
    scope: KeyFunction | None = None
    lock_ttl: float = 60.0


def _fail(decorator: str, message: str, **detail: Any) -> DecoratorUsageError:
    return DecoratorUsageError(f"@{decorator}: {message}", detail={"decorator": decorator, **detail})


def _seconds(decorator: str, name: str, value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise _fail(decorator, f"{name} must be a positive number of seconds; got {value!r}", argument=name)
    return float(value)


def _one_arg_callable(decorator: str, name: str, fn: Any) -> None:
    if not callable(fn):
        raise _fail(
            decorator, f"{name} must be a callable taking the ExecutionContext; got {fn!r}", argument=name
        )
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins without a signature: trust the caller
        return
    try:
        signature.bind(None)
    except TypeError:
        raise _fail(
            decorator,
            f"{name} must accept exactly one argument (the ExecutionContext); got {name}{signature}",
            argument=name,
        ) from None


def _header_names(decorator: str, vary: Any) -> tuple[str, ...]:
    if isinstance(vary, str):
        vary = (vary,)
    names: list[str] = []
    for item in vary:
        if not isinstance(item, str) or not item.strip() or item.strip() == "*":
            raise _fail(decorator, f"vary must list request header names; got {item!r}", argument="vary")
        name = item.strip().lower()
        if name not in names:
            names.append(name)
    return tuple(sorted(names))


def _target_check(decorator: str, target: Any) -> None:
    if isinstance(target, type):
        raise _fail(
            decorator,
            f"applies to route handler methods, not classes ({target.__qualname__})",
            target=target.__qualname__,
        )
    if not (callable(target) or isinstance(target, (staticmethod, classmethod))):
        raise _fail(decorator, f"must decorate a route handler; got {target!r}", target=repr(target))
    if getattr(target, CACHE_RESPONSE_META, None) is not None:
        raise _fail(
            decorator,
            "the handler already has @cache_response; a route has one response-cache policy "
            "(merge the arguments into a single decorator)",
            reason="already_cached",
        )
    if getattr(target, IDEMPOTENT_META, None) is not None:
        raise _fail(
            decorator,
            "the handler already has @idempotent; a route is either cached (GET/HEAD) or "
            "idempotent (unsafe methods), not both — and @idempotent is applied once",
            reason="already_idempotent",
        )


def cache_response(
    *args: Any,
    ttl: float | None = None,
    vary: Iterable[str] | str = (),
    key: KeyFunction | None = None,
    tags: Iterable[str] | TagsFunction = (),
    methods: Iterable[str] = ("GET", "HEAD"),
    statuses: Iterable[int] = DEFAULT_CACHEABLE_STATUSES,
    debug_header: bool = False,
    respect_client_no_cache: bool = False,
    query: Iterable[str] | None = None,
    allow_authenticated: bool = False,
) -> Callable[[F], F]:
    """Cache the route's responses server-side for ``ttl`` seconds.

    The key covers the method (``HEAD`` reads, never writes, the ``GET``
    entry), the route template, the path parameters, the query string
    (order-insensitive; ``query=`` limits it to the named parameters), the
    request headers named in ``vary`` and the result of ``key(ctx)``. ``key``
    must be given for per-user data; returning ``None`` from it bypasses the
    cache for that request. A request carrying ``Authorization`` bypasses the
    cache unless ``key=`` or ``vary`` makes entries per caller, or
    ``allow_authenticated=True`` declares the response identical for every
    caller. ``tags`` (names, or a callable of the context) let
    ``Cache.invalidate_tags`` evict entries.

    A response is stored only when its status is in ``statuses``, it is not
    streamed, it sets no cookie, its ``Cache-Control`` has no ``private`` /
    ``no-store`` / ``no-cache`` and its ``Vary`` names only headers listed in
    ``vary``. Guards run before the lookup. ``debug_header=True`` adds
    ``X-Cache: HIT|MISS``; ``respect_client_no_cache=True`` lets a request's
    ``Cache-Control: no-cache`` force a refresh. Needs ``CacheModule`` (or a
    ``Cache`` provider) — checked at startup.
    """
    name = "cache_response"
    if args:
        _reject_bare_usage(name, args[0])
        raise _fail(name, "takes keyword arguments only: write @cache_response(ttl=60)")
    if ttl is None:
        raise _fail(name, "ttl is required: write @cache_response(ttl=60)", argument="ttl")
    seconds = _seconds(name, "ttl", ttl)
    vary_names = _header_names(name, vary)
    if key is not None:
        _one_arg_callable(name, "key", key)
    tag_value: tuple[str, ...] | TagsFunction
    if callable(tags):
        _one_arg_callable(name, "tags", tags)
        tag_value = tags
    else:
        tag_list: list[str] = []
        for tag in (tags,) if isinstance(tags, str) else tags:
            if not isinstance(tag, str) or not tag:
                raise _fail(name, f"tags must be non-empty strings; got {tag!r}", argument="tags")
            if tag not in tag_list:
                tag_list.append(tag)
        tag_value = tuple(tag_list)
    method_set = frozenset(m.upper() for m in ((methods,) if isinstance(methods, str) else methods))
    if not method_set or not method_set <= _SAFE_METHODS:
        raise _fail(
            name,
            f"methods must be a non-empty subset of GET and HEAD; got {sorted(method_set)!r} "
            "(unsafe methods are never cached — see @idempotent)",
            argument="methods",
        )
    status_set = frozenset(statuses)
    for status in status_set:
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise _fail(name, f"statuses must be HTTP status codes; got {status!r}", argument="statuses")
    query_names: frozenset[str] | None = None
    if query is not None:
        names = (query,) if isinstance(query, str) else tuple(query)
        if not all(isinstance(n, str) and n for n in names):
            raise _fail(name, f"query must list query parameter names; got {query!r}", argument="query")
        query_names = frozenset(names)
    policy = CachePolicy(
        ttl=seconds,
        vary=vary_names,
        key=key,
        tags=tag_value,
        methods=method_set,
        statuses=status_set,
        debug_header=bool(debug_header),
        respect_client_no_cache=bool(respect_client_no_cache),
        query=query_names,
        allow_authenticated=bool(allow_authenticated),
    )

    def decorator(target: F) -> F:
        _target_check(name, target)
        setattr(target, CACHE_RESPONSE_META, policy)
        return target

    return decorator


def idempotent(
    *args: Any,
    ttl: float = 86_400.0,
    header: str = "Idempotency-Key",
    required: bool = True,
    scope: KeyFunction | None = None,
    lock_ttl: float = 60.0,
) -> Callable[[F], F]:
    """Make retries of an unsafe request with the same ``Idempotency-Key`` run the handler once.

    The first request with a key runs the handler and stores its response
    (status < 500, not streamed) for ``ttl`` seconds; a retry with the same
    key and the same payload replays it with ``Idempotent-Replayed: true``.
    The same key while the first request still runs answers ``409``
    (``ConflictError``, ``Retry-After: 1``); with a different payload,
    ``422`` (``code="idempotency_key_mismatch"``). A missing header answers
    ``400`` when ``required``. A handler that raises stores nothing, so the
    retry runs again. ``scope(ctx)`` partitions keys (per user / tenant);
    ``lock_ttl`` bounds how long an in-flight request holds its key. Needs
    ``CacheModule`` (or a ``Cache`` provider) — checked at startup.
    """
    name = "idempotent"
    if args:
        _reject_bare_usage(name, args[0])
        raise _fail(name, "takes keyword arguments only: write @idempotent(ttl=86400)")
    seconds = _seconds(name, "ttl", ttl)
    lease = _seconds(name, "lock_ttl", lock_ttl)
    if not isinstance(header, str) or not header.strip() or any(c in header for c in " :\r\n"):
        raise _fail(name, f"header must be a request header name; got {header!r}", argument="header")
    if not isinstance(required, bool):
        raise _fail(name, f"required must be a bool; got {required!r}", argument="required")
    if scope is not None:
        _one_arg_callable(name, "scope", scope)
    policy = IdempotencyPolicy(
        ttl=seconds, header=header.strip(), required=required, scope=scope, lock_ttl=lease
    )

    def decorator(target: F) -> F:
        _target_check(name, target)
        setattr(target, IDEMPOTENT_META, policy)
        return target

    return decorator


__all__ = [
    "CACHE_RESPONSE_META",
    "DEFAULT_CACHEABLE_STATUSES",
    "IDEMPOTENT_META",
    "CachePolicy",
    "IdempotencyPolicy",
    "cache_response",
    "idempotent",
]
