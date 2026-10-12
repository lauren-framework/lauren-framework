"""The five workloads every framework is measured against, and their contracts.

A cross-framework benchmark is only meaningful if every framework is asked to do
the *same* thing and is held to the *same* answer.  So the request and the exact
expected response for each workload live here, in one place, and the runner
asserts against them.  A framework that returns the wrong body fails loudly
instead of quietly contributing a bogus timing.

The response bodies are compared as parsed JSON, not as strings.  Key order is
not part of the contract (Quart sorts keys; Lauren does not), and neither is
whitespace (Falcon emits ``{"ok": true}`` with a space, everyone else emits
``{"ok":true}``).  Insisting on byte equality would be measuring the serializer's
formatting choices, not the framework's request handling.

Every handler returns HTTP 200 explicitly.  Litestar defaults a ``POST`` to
201, and a benchmark should not hand one framework a different status code than
the others for the same work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "DI_RESPONSE",
    "JSON_PAYLOAD",
    "ITEM_RESPONSE",
    "PERSON_IN",
    "PERSON_OUT",
    "VALIDATE_RESPONSE",
    "WORKLOAD_ORDER",
    "WORKLOADS",
    "Workload",
]

#: Shared response for the ``json`` workload.  Deliberately nested and
#: multi-typed (int, string, list, float, bool) so a serializer cannot be
#: measured on a constant or a flat scalar payload.
JSON_PAYLOAD: dict[str, Any] = {
    "id": 1,
    "name": "ada",
    "tags": ["a", "b", "c"],
    "nested": {"x": 1.5, "flag": True},
}

#: Request body for the ``validation`` workload.
PERSON_IN: dict[str, Any] = {"name": "ada", "age": 36}

#: Expected response for ``validation``: the framework must parse two fields,
#: coerce ``age`` to an int, and have the handler produce a derived field.
PERSON_OUT: dict[str, Any] = {"name": "ada", "age": 36, "adult": True}

#: Expected response for ``path_query``: ``limit`` must come back as a JSON
#: number, so query-string coercion is part of the contract.
ITEM_RESPONSE: dict[str, Any] = {"id": 42, "limit": 10, "q": "abc"}

#: Expected response for ``di``.
DI_RESPONSE: dict[str, Any] = {"service": "Greeter", "greeting": "hello"}

#: Alias kept for readability in the table below.
VALIDATE_RESPONSE: dict[str, Any] = PERSON_OUT


@dataclass(frozen=True)
class Workload:
    """One measured request and the exact response it must produce."""

    name: str
    """Stable identifier used in tables and JSON keys."""

    method: str
    path: str
    json_body: dict[str, Any] | None
    status: int
    body: dict[str, Any]
    describes: str
    """What this workload is actually measuring, for the results page."""


WORKLOADS: tuple[Workload, ...] = (
    Workload(
        name="hello",
        method="GET",
        path="/",
        json_body=None,
        status=200,
        body={"ok": True},
        describes="Route lookup and response construction, nothing else — the floor.",
    ),
    Workload(
        name="json",
        method="GET",
        path="/json",
        json_body=None,
        status=200,
        body=JSON_PAYLOAD,
        describes="Serializing a nested, multi-typed payload.",
    ),
    Workload(
        name="path_query",
        method="GET",
        path="/items/42?limit=10&q=abc",
        json_body=None,
        status=200,
        body=ITEM_RESPONSE,
        describes="Extracting and type-coercing one path and two query parameters.",
    ),
    Workload(
        name="di",
        method="GET",
        path="/di",
        json_body=None,
        status=200,
        body=DI_RESPONSE,
        describes="Resolving a collaborator through each framework's own injection mechanism.",
    ),
    Workload(
        name="validation",
        method="POST",
        path="/validate",
        json_body=PERSON_IN,
        status=200,
        body=VALIDATE_RESPONSE,
        describes="Validating and coercing a request body, then serializing a derived model.",
    ),
)

#: Workload identifiers in the order they appear in every report.
WORKLOAD_ORDER: tuple[str, ...] = tuple(workload.name for workload in WORKLOADS)
