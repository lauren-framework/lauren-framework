"""Registry of the frameworks under test and how to start them in-process.

Every framework this benchmark talks to is described here exactly once: the
PyPI distribution its version is read from, the *exact* object that is handed to
``httpx.ASGITransport`` as the ASGI application, and the serialization stack its
handlers end up using.  The feasibility probe (``benches/feasibility.py``) and
the benchmark harness (``benches/run.py``) both read this registry, so a
framework cannot drift between "is it measurable?" and "what did we measure?".

Why a registry instead of ten ad-hoc scripts: the interesting result of a
cross-framework benchmark is not a single number, it is a *comparable* number.
Keeping the entry point and the serialization library explicit in one place is
what makes two rows of the results table fair to compare.

Not every framework in the field can be driven this way.  A framework whose
Python object is only a facade for a different server (Robyn) or that was never
ASGI-compatible (aiohttp) has no in-process request path at all; those entries
carry a ``not_measured_reason`` and no app factories, so the harness reports
them as explicitly excluded instead of quietly dropping them.

Workload contract
-----------------
``Framework.apps`` maps a workload name to a zero-argument factory.  Factories
are called once per measurement and must return a **fresh** application, so no
framework inherits another framework's warm state and no two repeats share a
mutable DI container.
"""

from __future__ import annotations

import importlib.metadata as _metadata
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BY_KEY",
    "FRAMEWORKS",
    "WORKLOADS",
    "Framework",
    "installed_versions",
    "make_app",
]

#: Workload identifiers, in the order they appear in every report.
WORKLOADS: tuple[str, ...] = ("hello", "json", "path_query", "di", "validation")

#: Sanic refuses to register two applications with the same name in one
#: process, and the harness builds a fresh app per measurement.
_sanic_names = itertools.count(1)


@dataclass(frozen=True)
class Framework:
    """One framework under test."""

    key: str
    """Short, stable name used in tables, figures and JSON keys."""

    dist: str
    """PyPI distribution name, used for the pinned-version lookup."""

    entrypoint: str
    """The exact ASGI application object, in prose."""

    serialization: str
    """The JSON library the handlers actually end up using."""

    apps: dict[str, Callable[[], Any]] = field(default_factory=dict)
    """Workload name -> factory returning a fresh ASGI application."""

    not_measured_reason: str = ""
    """Non-empty when the framework cannot be driven in-process."""

    raw_object: Callable[[], Any] | None = None
    """Builds the framework's app object for the negative probe.

    Used only to demonstrate *why* a framework is not measurable: the probe
    calls the object with ASGI arguments and records the resulting error.
    """

    @property
    def measured(self) -> bool:
        """Whether this framework can be driven under ``ASGITransport``."""
        return not self.not_measured_reason

    @property
    def version(self) -> str:
        """Installed version of the distribution, or ``"not installed"``."""
        try:
            return _metadata.version(self.dist)
        except _metadata.PackageNotFoundError:
            return "not installed"


# ---------------------------------------------------------------------------
# App factories
#
# Each factory is deliberately written the way that framework's own docs
# introduce a "hello world" handler, so the measured code is idiomatic for the
# framework rather than a shape we forced on it.
# ---------------------------------------------------------------------------


def _lauren_hello() -> Any:
    from lauren import LaurenFactory, controller, get, module

    @controller("/")
    class HelloController:
        @get("/")
        async def index(self) -> dict:
            return {"ok": True}

    @module(controllers=[HelloController])
    class AppModule: ...

    return LaurenFactory.create(AppModule)


def _fastapi_hello() -> Any:
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/")
    async def index() -> dict:
        return {"ok": True}

    return app


def _starlette_hello() -> Any:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def index(request: Any) -> Any:
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/", index)])


def _litestar_hello() -> Any:
    from litestar import Litestar, get

    @get("/")
    async def index() -> dict:
        return {"ok": True}

    return Litestar(route_handlers=[index])


def _blacksheep_hello() -> Any:
    from blacksheep import Application, json

    app = Application()

    @app.router.get("/")
    async def index(request: Any) -> Any:
        return json({"ok": True})

    return app


def _sanic_hello() -> Any:
    from sanic import Sanic, json

    app = Sanic(f"bench_hello_{next(_sanic_names)}")

    @app.get("/")
    async def index(request: Any) -> Any:
        return json({"ok": True})

    return app


def _falcon_hello() -> Any:
    import falcon.asgi

    class Hello:
        async def on_get(self, req: Any, resp: Any) -> None:
            resp.media = {"ok": True}

    app = falcon.asgi.App()
    app.add_route("/", Hello())
    return app


def _quart_hello() -> Any:
    from quart import Quart, jsonify

    app = Quart(__name__)

    @app.get("/")
    async def index() -> Any:
        return jsonify(ok=True)

    return app


def _aiohttp_object() -> Any:
    from aiohttp import web

    app = web.Application()

    async def index(request: Any) -> Any:
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    return app


def _robyn_object() -> Any:
    from robyn import Robyn

    app = Robyn(__file__)

    @app.get("/")
    async def index(request: Any) -> str:
        return '{"ok":true}'

    return app


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


FRAMEWORKS: tuple[Framework, ...] = (
    Framework(
        key="lauren",
        dist="lauren",
        entrypoint="lauren.LaurenFactory.create(AppModule) -> LaurenApp",
        serialization="stdlib json (lauren's StdlibJSONEncoder, the default)",
        apps={"hello": _lauren_hello},
    ),
    Framework(
        key="fastapi",
        dist="fastapi",
        entrypoint="fastapi.FastAPI()",
        serialization="stdlib json (Starlette JSONResponse; ORJSONResponse is opt-in)",
        apps={"hello": _fastapi_hello},
    ),
    Framework(
        key="starlette",
        dist="starlette",
        entrypoint="starlette.applications.Starlette()",
        serialization="stdlib json (starlette.responses.JSONResponse)",
        apps={"hello": _starlette_hello},
    ),
    Framework(
        key="litestar",
        dist="litestar",
        entrypoint="litestar.Litestar()",
        serialization="msgspec (msgspec.json.Encoder via litestar.serialization)",
        apps={"hello": _litestar_hello},
    ),
    Framework(
        key="blacksheep",
        dist="blacksheep",
        entrypoint="blacksheep.Application()",
        serialization="stdlib json (essentials.json.dumps, an optimised json.dumps)",
        apps={"hello": _blacksheep_hello},
    ),
    Framework(
        key="sanic",
        dist="sanic",
        entrypoint=(
            "sanic.Sanic(name) — Sanic.__call__ is ASGI-native, but the app only "
            "serves after the lifespan startup handshake"
        ),
        serialization="ujson (sanic.helpers prefers ujson when it is installed)",
        apps={"hello": _sanic_hello},
    ),
    Framework(
        key="falcon",
        dist="falcon",
        entrypoint="falcon.asgi.App()",
        serialization="stdlib json (falcon.media.JSONHandler default)",
        apps={"hello": _falcon_hello},
    ),
    Framework(
        key="quart",
        dist="quart",
        entrypoint="quart.Quart(name)",
        serialization="stdlib json (Quart's DefaultJSONProvider)",
        apps={"hello": _quart_hello},
    ),
    Framework(
        key="aiohttp",
        dist="aiohttp",
        entrypoint="none — aiohttp.web.Application is not an ASGI application",
        serialization="stdlib json (aiohttp.web.json_response)",
        not_measured_reason=(
            "aiohttp.web.Application exposes no ASGI callable: its __call__ is a "
            "gunicorn-compatibility stub that takes no arguments and returns self, "
            "and the distribution ships no ASGI adapter. The only PyPI shims "
            "(aiohttp-asgi, aiohttp-asgi-connector) adapt in the opposite "
            "direction — they host an ASGI app on aiohttp's server — so they "
            "cannot put an aiohttp app behind ASGITransport. Hand-writing the "
            "bridge would measure the bridge, not aiohttp."
        ),
        raw_object=_aiohttp_object,
    ),
    Framework(
        key="robyn",
        dist="robyn",
        entrypoint="none — Robyn is a facade over a compiled Rust HTTP server",
        serialization="not applicable (no in-process request path)",
        not_measured_reason=(
            "Robyn's Python object configures a compiled Rust server that owns its "
            "own socket: the object exposes start()/serve(), defines no __call__, "
            "and the package ships no ASGI module (its submodules are router, ws, "
            "responses, ... but nothing ASGI). There is no in-process request path "
            "to drive, so no comparable number can be produced without measuring "
            "Robyn's own server against a socket, which is a different benchmark."
        ),
        raw_object=_robyn_object,
    ),
)

#: Lookup by canonical key.
BY_KEY: dict[str, Framework] = {fw.key: fw for fw in FRAMEWORKS}


def make_app(key: str, workload: str = "hello") -> Any:
    """Build a fresh application for ``key`` and ``workload``.

    Raises:
        KeyError: the framework is unknown or is not measurable in-process.
        NotImplementedError: the framework is measurable but the workload has no
            factory yet.
    """
    framework = BY_KEY[key]
    if not framework.measured:
        raise KeyError(f"{key} is not measurable in-process: {framework.not_measured_reason}")
    try:
        factory = framework.apps[workload]
    except KeyError:  # pragma: no cover - guards typos, not measurement
        raise NotImplementedError(f"no {workload!r} factory for {key!r} yet") from None
    return factory()


def installed_versions() -> dict[str, str]:
    """Map every registry key to its installed distribution version."""
    return {fw.key: fw.version for fw in FRAMEWORKS}
