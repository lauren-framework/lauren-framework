"""Server-side response caching — keying, store-time rules and header handling.

Everything about *which* responses are stored, *how* they are keyed and
*which* headers survive lives in this module, so HTTP cache-control work
(``Cache-Control``, ``ETag``, conditional requests) has one place to
coordinate with. ``@cache_response`` never emits ``Cache-Control`` itself:
downstream caching is a separate concern, and handler-set validators
(``ETag``, ``Last-Modified``, ``Cache-Control``) are stored and replayed
with the body, so conditional-request handling sees them on hits too.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any, Callable

from ..types import CallHandler, ExecutionContext, Headers, MutableHeaders, Response
from ._entry import Entry
from ._policy import CachePolicy
from ._service import Cache, check_tags

#: RFC 9110 §7.6.1 hop-by-hop headers — never stored, never replayed.
HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
#: Headers dropped from a stored response (recomputed or added on replay).
_UNSTORED_HEADERS: frozenset[str] = HOP_BY_HOP_HEADERS | {"content-length", "age", "x-cache", "set-cookie"}
#: Response ``Cache-Control`` directives that forbid storing the response.
_REFUSING_DIRECTIVES: frozenset[str] = frozenset({"private", "no-store", "no-cache"})


def _directives(values: list[str]) -> set[str]:
    return {
        part.split("=", 1)[0].strip().lower() for value in values for part in value.split(",") if part.strip()
    }


def _vary_names(headers: Headers) -> list[str]:
    return [
        part.strip().lower() for value in headers.getall("vary") for part in value.split(",") if part.strip()
    ]


def response_cache_key(
    *,
    method: str,
    route_template: str,
    path_params: Mapping[str, str],
    query_params: Mapping[str, list[str]],
    headers: Headers,
    vary: tuple[str, ...],
    custom: Any = None,
) -> str:
    """Return the store key of a response: ``resp:<route>:<sha256>``.

    ``HEAD`` shares the ``GET`` entry. The query is order-insensitive across
    parameter names while repeated values keep their order; every ``vary``
    header contributes its values (absent and empty differ).
    """
    canonical_method = "GET" if method == "HEAD" else method
    query = sorted(((k, v) for k, values in query_params.items() for v in values), key=lambda kv: kv[0])
    material = [
        canonical_method,
        route_template,
        sorted(path_params.items()),
        query,
        [[name, headers.getall(name)] for name in vary],
        custom,
    ]
    encoded = json.dumps(material, separators=(",", ":"), ensure_ascii=False, default=repr).encode("utf-8")
    return f"resp:{route_template}:{hashlib.sha256(encoded).hexdigest()}"


def is_storable(response: Response, policy: CachePolicy) -> bool:
    """Apply the correct-by-default store-time rules to a handler's response."""
    if response.status not in policy.statuses or response.stream_body is not None:
        return False
    headers = response.headers
    if "set-cookie" in headers:
        return False
    if _directives(headers.getall("cache-control")) & _REFUSING_DIRECTIVES:
        return False
    for name in _vary_names(headers):
        # ``Vary: *`` or a header the key does not cover: the stored response
        # could be replayed to a request it does not match.
        if name not in policy.vary:
            return False
    return True


def client_requests_no_cache(headers: Headers) -> bool:
    """``True`` when the request carries ``Cache-Control: no-cache`` (or ``Pragma: no-cache``)."""
    if "no-cache" in _directives(headers.getall("cache-control")):
        return True
    return "no-cache" in _directives(headers.getall("pragma"))


def encode_response(response: Response, *, extra: Mapping[str, Any] | None = None) -> bytes:
    """Serialise status, storable headers and body (``extra`` joins the header)."""
    header: dict[str, Any] = {
        "s": response.status,
        "h": [[k, v] for k, v in response.headers.raw() if k not in _UNSTORED_HEADERS],
    }
    if extra:
        header.update(extra)
    return json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + response.body


def decode_response(payload: bytes) -> tuple[Response, dict[str, Any]]:
    """Rebuild a :class:`Response` from :func:`encode_response` output."""
    head, _, body = payload.partition(b"\n")
    header = json.loads(head)
    headers = MutableHeaders([(str(k), str(v)) for k, v in header["h"]])
    return Response(body, status=int(header["s"]), headers=headers), header


class ResponseCacheInterceptor:
    """Serves ``@cache_response`` routes; the factory makes it the route's innermost interceptor.

    One instance per app, built from the app's :class:`Cache` and the
    compiled policies (handler function → :class:`CachePolicy`). Concurrent
    misses for one key share one handler run when the result is storable.
    """

    def __init__(self, cache: Cache, policies: Mapping[Callable[..., Any], CachePolicy]) -> None:
        self._cache = cache
        self._policies = dict(policies)
        self._inflight: dict[tuple[Any, str], asyncio.Future[Entry | None]] = {}

    async def intercept(self, context: ExecutionContext, call_handler: CallHandler) -> Response:
        policy = self._policies.get(context.handler_func)  # type: ignore[arg-type]
        request = context.request
        if policy is None or request.method not in policy.methods:
            return await call_handler.handle()
        custom = None
        if policy.key is not None:
            custom = policy.key(context)
            if custom is None:
                return await call_handler.handle()
        key = response_cache_key(
            method=request.method,
            route_template=context.route_template or request.path,
            path_params=request.path_params,
            query_params=request.query_params,
            headers=request.headers,
            vary=policy.vary,
            custom=custom,
        )
        if not (policy.respect_client_no_cache and client_requests_no_cache(request.headers)):
            entry = await self._cache.read_entry(key)
            if entry is not None:
                return self._replay(entry, policy)
        if request.method == "HEAD":
            # A HEAD response may carry no body: HEAD reads the GET entry but
            # never writes it.
            response = await call_handler.handle()
            return response.with_header("x-cache", "MISS") if policy.debug_header else response

        loop = asyncio.get_running_loop()
        flight = (loop, key)
        pending = self._inflight.get(flight)
        if pending is not None:
            shared = await asyncio.shield(pending)
            if shared is not None:
                return self._replay(shared, policy)
            # The leader's response was not storable (or it failed): it may be
            # specific to that request, so run the handler for this one.
            response, _ = await self._run(context, call_handler, policy, key)
            return response

        future: asyncio.Future[Entry | None] = loop.create_future()
        self._inflight[flight] = future
        stored: Entry | None = None
        try:
            response, stored = await self._run(context, call_handler, policy, key)
            return response
        finally:
            if self._inflight.get(flight) is future:
                del self._inflight[flight]
            if not future.done():
                future.set_result(stored)

    async def _run(
        self, context: ExecutionContext, call_handler: CallHandler, policy: CachePolicy, key: str
    ) -> tuple[Response, Entry | None]:
        tags = check_tags(policy.tags(context) if callable(policy.tags) else policy.tags)
        gens = await self._cache.snapshot_generations(tags)
        response = await call_handler.handle()
        entry: Entry | None = None
        if is_storable(response, policy):
            entry = await self._cache.write_entry(key, encode_response(response), ttl=policy.ttl, gens=gens)
        if policy.debug_header:
            response = response.with_header("x-cache", "MISS")
        return response, entry

    def _replay(self, entry: Entry, policy: CachePolicy) -> Response:
        response, _ = decode_response(entry.payload)
        age = max(0, int(self._cache.now() - entry.written_at))
        response.headers.set("age", str(age))
        if policy.debug_header:
            response.headers.set("x-cache", "HIT")
        return response


__all__ = [
    "HOP_BY_HOP_HEADERS",
    "ResponseCacheInterceptor",
    "client_requests_no_cache",
    "decode_response",
    "encode_response",
    "is_storable",
    "response_cache_key",
]
