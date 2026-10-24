"""Idempotency keys — the runtime behind ``@idempotent``.

Per request carrying ``Idempotency-Key: K`` (store namespace ``idem:`` inside
the cache's store; ``D = sha256(scope, K)``):

1. ``rec:D`` holds a completed response → replay it when the payload
   fingerprint matches, else ``422``.
2. Otherwise take the lease ``lock:D`` with ``set_if_absent``; when another
   request holds it → ``409`` with ``Retry-After``.
3. Re-check ``rec:D`` (the holder may have finished in between), run the
   handler, store the response when its status is below 500 and it is not
   streamed, release the lease with ``delete_if_equal``.

A handler that raises stores nothing, so a retry runs again.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from typing import Any, Callable

from ..exceptions import BadRequestError, ConflictError, HTTPError
from ..types import CallHandler, ExecutionContext, Request, Response
from ._policy import IdempotencyPolicy
from ._response import decode_response, encode_response
from ._service import Cache

#: Longest accepted key (the IETF draft and Stripe use 255 characters).
MAX_KEY_LENGTH = 255
#: Header added to a replayed response.
REPLAYED_HEADER = "idempotent-replayed"


def request_fingerprint(request: Request, route_template: str, body: bytes) -> str:
    """SHA-256 over the method, route, path, query (order-insensitive) and body."""
    query = sorted(
        ((k, v) for k, values in request.query_params.items() for v in values), key=lambda kv: kv[0]
    )
    head = json.dumps([request.method, route_template, request.path, query], separators=(",", ":"))
    return hashlib.sha256(head.encode("utf-8") + b"\0" + body).hexdigest()


class IdempotencyInterceptor:
    """Serves ``@idempotent`` routes; the factory makes it the route's innermost interceptor."""

    def __init__(self, cache: Cache, policies: Mapping[Callable[..., Any], IdempotencyPolicy]) -> None:
        self._store = cache.store.namespace("idem")
        self._policies = dict(policies)

    async def intercept(self, context: ExecutionContext, call_handler: CallHandler) -> Response:
        policy = self._policies.get(context.handler_func)  # type: ignore[arg-type]
        if policy is None:  # pragma: no cover - only attached to routes with a policy
            return await call_handler.handle()
        request = context.request
        raw_key = request.headers.get(policy.header)
        if not raw_key:
            if policy.required:
                raise BadRequestError(
                    f"the {policy.header} header is required for this request",
                    code="idempotency_key_missing",
                    detail={"header": policy.header},
                )
            return await call_handler.handle()
        if len(raw_key) > MAX_KEY_LENGTH:
            raise BadRequestError(
                f"the {policy.header} header must be at most {MAX_KEY_LENGTH} characters",
                code="idempotency_key_invalid",
                detail={"header": policy.header, "max_length": MAX_KEY_LENGTH},
            )
        scope = policy.scope(context) if policy.scope is not None else None
        digest = hashlib.sha256(f"{'' if scope is None else scope}\0{raw_key}".encode()).hexdigest()
        record_key, lock_key = f"rec:{digest}", f"lock:{digest}"
        fingerprint = request_fingerprint(
            request, context.route_template or request.path, await request.body()
        )

        replay = await self._replay(record_key, fingerprint, policy)
        if replay is not None:
            return replay
        token = secrets.token_hex(16).encode("ascii")
        if not await self._store.set_if_absent(lock_key, token, ttl=policy.lock_ttl):
            raise ConflictError(
                f"a request with this {policy.header} is still being processed; retry later",
                code="idempotency_key_in_use",
                detail={"header": policy.header},
                headers={"retry-after": "1"},
            )
        try:
            replay = await self._replay(record_key, fingerprint, policy)
            if replay is not None:
                return replay
            response = await call_handler.handle()
            if response.status < 500 and response.stream_body is None:
                await self._store.set(
                    record_key, encode_response(response, extra={"f": fingerprint}), ttl=policy.ttl
                )
            return response
        finally:
            await self._store.delete_if_equal(lock_key, token)

    async def _replay(self, record_key: str, fingerprint: str, policy: IdempotencyPolicy) -> Response | None:
        raw = await self._store.get(record_key)
        if raw is None:
            return None
        response, header = decode_response(raw)
        if header.get("f") != fingerprint:
            raise HTTPError(
                f"this {policy.header} was already used with a different request payload",
                status_code=422,
                code="idempotency_key_mismatch",
                detail={"header": policy.header},
            )
        response.headers.set(REPLAYED_HEADER, "true")
        return response


__all__ = ["IdempotencyInterceptor", "MAX_KEY_LENGTH", "REPLAYED_HEADER", "request_fingerprint"]
