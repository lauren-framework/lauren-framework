"""The harness floor: a hand-written ASGI application, with no framework at all.

Every number this benchmark publishes is taken through ``httpx.ASGITransport``,
and the client is not free.  Measuring a framework without measuring the shift
it is standing on produces a table that looks like a comparison of frameworks
when it is largely a comparison of httpx with itself: in this environment the
hand-written application above answers ``hello`` at roughly 3,200-3,400 req/s,
while the frameworks answer it at roughly 2,100-2,700 req/s.  The framework's own
share of the round trip is the gap, not the total.

So this participant is measured exactly like the frameworks — same driver, same
warmup, same repeats, same startup measurement — and reported as a **reference
floor**, never as a competitor.  It does the same *work* as everyone else:

* routes five literal paths (plus one path parameter and two query parameters,
  which it has to coerce itself);
* parses and validates the ``validation`` request body with the same pydantic
  model the other handlers use, and derives ``adult`` rather than echoing it;
* serializes with the standard library's ``json``, which is what most of the
  frameworks in this field do by default.

What it does **not** do is any of the things a framework is bought for:
dependency injection, an OpenAPI schema, response models, middleware, or
extending.  That is the point: it is the floor, not the ceiling.

The expected-value constants are imported from ``benches/workloads.py`` for the
routes where the constant *is* the answer (``json``, ``items/{id}``, ``di``); the
``validation`` response is computed from the parsed body like any other handler,
so the floor cannot pass by echoing the answer back.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

from framework_apps import Framework, PersonIn, PersonOut
from workloads import DI_RESPONSE, JSON_PAYLOAD

__all__ = ["KEY", "REFERENCE", "baseline_app"]

KEY = "baseline"
"""Registry key.  Deliberately not a framework name, and never in the nav tables."""

_LIFESPAN_COMPLETE = {
    "lifespan.startup": "lifespan.startup.complete",
    "lifespan.shutdown": "lifespan.shutdown.complete",
}


async def _read_body(receive: Any) -> bytes:
    """Collect an ``http.request`` body, which may arrive in several messages."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _handle(scope: dict[str, Any], receive: Any) -> tuple[int, Any]:
    """Return the status and payload for one request."""
    path = scope["path"]

    if path == "/":
        return 200, {"ok": True}
    if path == "/json":
        return 200, JSON_PAYLOAD
    if path.startswith("/items/"):
        query = parse_qs(scope["query_string"].decode())
        return 200, {
            "id": int(path.rsplit("/", 1)[-1]),
            "limit": int(query["limit"][0]),
            "q": query["q"][0],
        }
    if path == "/di":
        return 200, DI_RESPONSE
    if path == "/validate":
        person = PersonIn.model_validate(json.loads(await _read_body(receive)))
        # Derived, not echoed: the floor does the same body work as the handlers.
        return 200, PersonOut(name=person.name, age=person.age, adult=person.age >= 18).model_dump()
    return 404, {"detail": "not found"}


async def baseline_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """A complete, dependency-free ASGI application answering all five workloads."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            completion = _LIFESPAN_COMPLETE.get(message["type"])
            if completion is None:
                return
            await send({"type": completion})
            if message["type"] == "lifespan.shutdown":
                return

    if scope["type"] != "http":  # pragma: no cover - nothing here speaks websockets
        return

    status, payload = await _handle(scope, receive)
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _app_factory() -> Any:
    """Return a fresh application for the runner.

    The application is a stateless callable, so "fresh" costs nothing — there is
    no router table or startup state that could be inherited between repeats.
    """
    return baseline_app


REFERENCE = Framework(
    key=KEY,
    dist="",
    entrypoint="hand-written ASGI callable (no framework)",
    serialization="stdlib json (json.dumps)",
    di_mechanism="none — the handler is a literal dict",
    validation="manual — json.loads + pydantic model_validate",
    app=_app_factory,
)
"""The floor, described exactly like a framework so it can be measured like one."""
