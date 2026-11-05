"""Benchmarks for response contracts (F01): ``off`` vs ``filter`` vs ``strict``.

Measures, on the machine running the benchmark, the per-response coercion cost
of a nested dataclass body (an internal subclass with an extra field, as a
repository would return it) under the three modes, plus a whole request through
the dispatcher. Wall-clock numbers vary with load; the assertions only guard
against gross regressions.
"""

from __future__ import annotations

import dataclasses
import datetime
import time
from collections.abc import Callable
from typing import Any

import pytest

from lauren import LaurenFactory, controller, get, module
from lauren._asgi import _coerce_to_response
from lauren._asgi._contracts import compile_response_plan, describe_responses
from lauren.decorators import RouteMeta
from lauren.serialization import StdlibJSONEncoder
from lauren.testing import TestClient
from lauren.validation import TypeCompiler

pytestmark = pytest.mark.benchmark


@dataclasses.dataclass
class Line:
    sku: str
    qty: int
    price: float


@dataclasses.dataclass
class Order:
    id: int
    customer: str
    placed: datetime.date
    lines: list[Line]


@dataclasses.dataclass
class StoredOrder(Order):
    internal_notes: str


ORDER = StoredOrder(
    1, "ada", datetime.date(2024, 1, 2), [Line(f"s{i}", i, 1.5) for i in range(10)], "do not ship"
)


def _per_call_us(fn: Callable[[], Any], n: int = 2000) -> float:
    for _ in range(100):
        fn()
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        for _ in range(n):
            fn()
        best = min(best, time.perf_counter() - start)
    return best / n * 1_000_000


class _Owner:
    pass


def _handler() -> None: ...


def _coercer(mode: str) -> Callable[[], Any]:
    encoder = StdlibJSONEncoder()
    if mode == "off":
        return lambda: _coerce_to_response(ORDER, encoder=encoder)
    table = describe_responses(RouteMeta(method="GET", path="/"), Order, fn=_handler)
    plan = compile_response_plan(
        table,
        mode=mode,
        compiler=TypeCompiler(),
        coerce=_coerce_to_response,
        controller_cls=_Owner,
        fn=_handler,
        route="GET /",
    )
    assert plan is not None
    return lambda: plan.coerce(ORDER, encoder)


def test_response_coercion_cost_by_mode() -> None:
    rows = [(mode, _per_call_us(_coercer(mode))) for mode in ("off", "filter", "strict")]
    print("\n=== response coercion, nested dataclass (us/response) ===")
    for label, us in rows:
        print(f"{label:<10} {us:>8.2f}")
    costs = dict(rows)

    assert costs["filter"] / costs["off"] < 5.0
    assert costs["strict"] / costs["off"] < 10.0


@controller("/b")
class _OrderController:
    @get("/order")
    async def order(self) -> Order:
        return ORDER


def _drive(mode: str, n: int = 300) -> float:
    @module(controllers=[_OrderController])
    class _OrderModule:
        pass

    client = TestClient(LaurenFactory.create(_OrderModule, response_validation=mode))
    for _ in range(20):
        client.get("/b/order")
    start = time.perf_counter()
    for _ in range(n):
        client.get("/b/order")
    return (time.perf_counter() - start) / n * 1_000_000


def test_order_route_throughput_by_mode() -> None:
    rows = [(mode, _drive(mode)) for mode in ("off", "filter", "strict")]
    print("\n=== GET /b/order (us/request) ===")
    for label, us in rows:
        print(f"{label:<10} {us:>8.1f}")
    costs = dict(rows)

    assert costs["strict"] / costs["off"] < 3.0
