"""Benchmarks for the native validation engine (F02).

Measures, on the machine running the benchmark:

* native dataclass validation — a flat 10-field model and a three-level
  nested model — against pydantic's ``TypeAdapter`` for the same dataclass
  (when pydantic is installed) and against the bare constructor call the 1.x
  shallow path performs;
* a scalar-heavy query route dispatched under ``stdlib_models="warn"`` (the
  shadow validation of the migration window) and ``"enforce"``.

Wall-clock numbers vary with load; the assertions only guard against gross
regressions.
"""

from __future__ import annotations

import dataclasses
import datetime
import time
from collections.abc import Callable
from typing import Any

import pytest

from lauren import LaurenFactory, controller, get, module
from lauren.testing import TestClient
from lauren.validation import TypeCompiler, ValidationPolicy

pytestmark = pytest.mark.benchmark


@dataclasses.dataclass
class Flat:
    a: int
    b: str
    c: float
    d: bool
    e: int
    f: str
    g: float
    h: bool
    i: int
    j: str


@dataclasses.dataclass
class Leaf:
    x: int
    y: str


@dataclasses.dataclass
class Middle:
    inner: Leaf
    items: list[Leaf]
    n: int


@dataclasses.dataclass
class Top:
    mid: Middle
    name: str
    when: datetime.date


FLAT = {"a": 1, "b": "x", "c": 1.5, "d": True, "e": 2, "f": "y", "g": 2.5, "h": False, "i": 3, "j": "z"}
NESTED = {
    "mid": {"inner": {"x": 1, "y": "a"}, "items": [{"x": 2, "y": "b"}, {"x": 3, "y": "c"}], "n": 4},
    "name": "q",
    "when": "2024-01-02",
}


def _per_call_us(fn: Callable[[], Any], n: int = 5000) -> float:
    for _ in range(200):
        fn()
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        for _ in range(n):
            fn()
        best = min(best, time.perf_counter() - start)
    return best / n * 1_000_000


def test_native_dataclass_validation_speed() -> None:
    compiler = TypeCompiler()
    flat, nested = compiler.compile(Flat), compiler.compile(Top)
    rows = [
        ("constructor Flat(**data)", _per_call_us(lambda: Flat(**FLAT))),
        ("native flat (10 fields)", _per_call_us(lambda: flat.validate(FLAT))),
        ("native nested (x3)", _per_call_us(lambda: nested.validate(NESTED))),
    ]
    try:
        import pydantic

        ta_flat, ta_nested = pydantic.TypeAdapter(Flat), pydantic.TypeAdapter(Top)
        rows += [
            ("pydantic flat", _per_call_us(lambda: ta_flat.validate_python(FLAT))),
            ("pydantic nested", _per_call_us(lambda: ta_nested.validate_python(NESTED))),
        ]
    except ImportError:  # pragma: no cover - pydantic is optional
        pass
    print("\n=== dataclass validation (us/call) ===")
    for label, us in rows:
        print(f"{label:<28} {us:>8.2f}")

    assert dict(rows)["native nested (x3)"] < 1000


@controller("/b")
class _ScalarController:
    @get("/search")
    async def search(
        self, q: str, page: int = 1, size: int = 20, active: bool = True, ratio: float = 1.0
    ) -> dict:
        return {"page": page}


@module(controllers=[_ScalarController])
class _ScalarModule:
    pass


def _drive(app: Any, n: int = 400) -> float:
    client = TestClient(app)
    for _ in range(20):
        client.get("/b/search?q=x&page=2&size=5&active=on&ratio=0.5")
    start = time.perf_counter()
    for _ in range(n):
        client.get("/b/search?q=x&page=2&size=5&active=on&ratio=0.5")
    return (time.perf_counter() - start) / n * 1_000_000


def test_scalar_query_route_throughput() -> None:
    warn = _drive(LaurenFactory.create(_ScalarModule))
    enforce = _drive(
        LaurenFactory.create(_ScalarModule, validation=ValidationPolicy(stdlib_models="enforce"))
    )
    print("\n=== scalar query route (us/request) ===")
    print(f"{'warn (default)':<28} {warn:>8.1f}")
    print(f"{'enforce':<28} {enforce:>8.1f}")

    assert enforce / warn < 2.0
