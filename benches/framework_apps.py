"""Registry of the frameworks under test and how to start them in-process.

Every framework this benchmark talks to is described here exactly once: the
PyPI distribution its version is read from, the *exact* object that is handed to
``httpx.ASGITransport`` as the ASGI application, the JSON library its handlers
end up using, and — because they are genuinely not the same thing across this
field — how it does dependency injection and request-body validation.

Why a registry instead of eight ad-hoc scripts: the interesting result of a
cross-framework benchmark is not a single number, it is a *comparable* number.
Keeping the entry point, the serializer, the injection mechanism and the
validation path explicit in one place is what makes two rows of the results
table fair to compare, and what lets the results page tell the reader where the
comparison is not apples-to-apples.

Shape of a measured app
-----------------------
Each framework exposes **one** app factory that registers **all five** routes
from ``benches/workloads.py``.  The runner then measures per route.  This is
deliberate:

* it matches how a real service looks (many routes in one app), and
* it keeps the router table identical across frameworks, so a difference in
  routing cost cannot be an artefact of one framework having one route where
  another has five.

Factories are called once per repeat and must return a **fresh** application, so
no repeat inherits another's warm state.

Not every framework in the field can be driven this way.  A framework whose
Python object is only a facade for a different server (Robyn) or that was never
ASGI-compatible (aiohttp) has no in-process request path at all; those entries
carry a ``not_measured_reason`` and no factory, so the harness reports them as
explicitly excluded instead of quietly dropping them.
"""

from __future__ import annotations

import importlib.metadata as _metadata
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from workloads import DI_RESPONSE, JSON_PAYLOAD

__all__ = [
    "BY_KEY",
    "FRAMEWORKS",
    "Framework",
    "Greeter",
    "PersonIn",
    "PersonOut",
    "installed_versions",
    "make_app",
]

#: Sanic refuses to register two applications with the same name in one
#: process, and the harness builds a fresh app per repeat.
_sanic_names = itertools.count(1)


class PersonIn(BaseModel):
    """Request body model for the ``validation`` workload."""

    name: str
    age: int


class PersonOut(BaseModel):
    """Response model for the ``validation`` workload."""

    name: str
    age: int
    adult: bool


class Greeter:
    """The collaborator the ``di`` workload resolves.

    Deliberately trivial: the workload measures the cost of *reaching* the
    object through the framework's injection mechanism, not of using it.
    """

    def greet(self) -> str:
        """Return a constant greeting."""
        return DI_RESPONSE["greeting"]


def _person_out(person: PersonIn) -> dict[str, Any]:
    """Build the contract response for the ``validation`` workload."""
    return PersonOut(name=person.name, age=person.age, adult=person.age >= 18).model_dump()


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

    di_mechanism: str
    """How the ``di`` workload's collaborator is resolved."""

    validation: str
    """How the ``validation`` workload's body is parsed and checked."""

    app: Callable[[], Any] | None = None
    """Factory returning a fresh app registering all five workloads."""

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
# Each factory is written the way that framework's own documentation introduces
# the equivalent handler, so the measured code is idiomatic for the framework
# rather than a shape this benchmark forced on it.
# ---------------------------------------------------------------------------


def _lauren_app() -> Any:
    from lauren import Json, LaurenFactory, Path, controller, get, injectable, module, post

    @injectable()
    class _Greeter(Greeter):
        pass

    @controller("")
    class Core:
        @get("/")
        async def hello(self) -> dict:
            return {"ok": True}

        @get("/json")
        async def payload(self) -> dict:
            return dict(JSON_PAYLOAD)

        @get("/items/{id}")
        async def item(self, id: Path[int], limit: int, q: str) -> dict:
            return {"id": id, "limit": limit, "q": q}

        @post("/validate")
        async def validate(self, body: Json[PersonIn]) -> PersonOut:
            return PersonOut(name=body.name, age=body.age, adult=body.age >= 18)

    @controller("/di")
    class Di:
        def __init__(self, greeter: _Greeter) -> None:
            self._greeter = greeter

        @get("/")
        async def show(self) -> dict:
            return {"service": "Greeter", "greeting": self._greeter.greet()}

    @module(controllers=[Core, Di], providers=[_Greeter])
    class BenchModule: ...

    return LaurenFactory.create(BenchModule)


def _fastapi_app() -> Any:
    from fastapi import Depends, FastAPI

    greeter = Greeter()

    def get_greeter() -> Greeter:
        return greeter

    app = FastAPI()

    @app.get("/")
    async def hello() -> dict:
        return {"ok": True}

    @app.get("/json")
    async def payload() -> dict:
        return dict(JSON_PAYLOAD)

    @app.get("/items/{id}")
    async def item(id: int, limit: int, q: str) -> dict:
        return {"id": id, "limit": limit, "q": q}

    @app.post("/validate")
    async def validate(body: PersonIn) -> PersonOut:
        return PersonOut(name=body.name, age=body.age, adult=body.age >= 18)

    @app.get("/di")
    async def di(greeter: Greeter = Depends(get_greeter)) -> dict:
        return {"service": "Greeter", "greeting": greeter.greet()}

    return app


def _starlette_app() -> Any:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def hello(request: Any) -> Any:
        return JSONResponse({"ok": True})

    async def payload(request: Any) -> Any:
        return JSONResponse(dict(JSON_PAYLOAD))

    async def item(request: Any) -> Any:
        return JSONResponse(
            {
                "id": int(request.path_params["id"]),
                "limit": int(request.query_params["limit"]),
                "q": request.query_params["q"],
            }
        )

    async def di(request: Any) -> Any:
        greeter: Greeter = request.app.state.greeter
        return JSONResponse({"service": "Greeter", "greeting": greeter.greet()})

    async def validate(request: Any) -> Any:
        return JSONResponse(_person_out(PersonIn.model_validate(await request.json())))

    app = Starlette(
        routes=[
            Route("/", hello),
            Route("/json", payload),
            Route("/items/{id}", item),
            Route("/di", di),
            Route("/validate", validate, methods=["POST"]),
        ]
    )
    app.state.greeter = Greeter()
    return app


def _litestar_app() -> Any:
    from litestar import Litestar, get, post
    from litestar.di import Provide

    @get("/")
    async def hello() -> dict:
        return {"ok": True}

    @get("/json")
    async def payload() -> dict:
        return dict(JSON_PAYLOAD)

    @get("/items/{id:int}")
    async def item(id: int, limit: int, q: str) -> dict:
        return {"id": id, "limit": limit, "q": q}

    # ``status_code=200`` because Litestar defaults POST to 201 and every other
    # framework here answers 200; the contract should not differ by framework.
    # The parameter is called ``data``, not ``body``: Litestar reserves ``body``
    # for the raw request bytes.
    @post("/validate", status_code=200)
    async def validate(data: PersonIn) -> PersonOut:
        return PersonOut(name=data.name, age=data.age, adult=data.age >= 18)

    # ``sync_to_thread=False`` because this provider is trivially non-blocking;
    # leaving it at the default would offload the call to a worker thread and
    # measure a thread hop that the other frameworks do not pay.
    @get("/di", dependencies={"greeter": Provide(Greeter, use_cache=True, sync_to_thread=False)})
    async def di(greeter: Greeter) -> dict:
        return {"service": "Greeter", "greeting": greeter.greet()}

    return Litestar(route_handlers=[hello, payload, item, validate, di])


def _blacksheep_app() -> Any:
    from blacksheep import Application, json

    app = Application()
    app.services.add_singleton(Greeter)

    @app.router.get("/")
    async def hello(request: Any) -> Any:
        return json({"ok": True})

    @app.router.get("/json")
    async def payload(request: Any) -> Any:
        return json(dict(JSON_PAYLOAD))

    @app.router.get("/items/{id}")
    async def item(request: Any, id: int) -> Any:
        # BlackSheep hands query values back as lists; the contract asks for one
        # coerced integer, so take the first and cast.
        return json(
            {
                "id": id,
                "limit": int(request.query.get("limit")[0]),
                "q": request.query.get("q")[0],
            }
        )

    @app.router.get("/di")
    async def di(request: Any, greeter: Greeter) -> Any:
        return json({"service": "Greeter", "greeting": greeter.greet()})

    @app.router.post("/validate")
    async def validate(person: PersonIn) -> Any:
        return json(_person_out(person))

    return app


def _sanic_app() -> Any:
    from sanic import Sanic, json

    app = Sanic(f"bench_{next(_sanic_names)}")

    @app.get("/")
    async def hello(request: Any) -> Any:
        return json({"ok": True})

    @app.get("/json")
    async def payload(request: Any) -> Any:
        return json(dict(JSON_PAYLOAD))

    @app.get("/items/<id:int>")
    async def item(request: Any, id: int) -> Any:
        return json(
            {
                "id": id,
                "limit": int(request.args.get("limit")),
                "q": request.args.get("q"),
            }
        )

    @app.get("/di")
    async def di(request: Any) -> Any:
        greeter: Greeter = request.app.ctx.greeter
        return json({"service": "Greeter", "greeting": greeter.greet()})

    @app.post("/validate")
    async def validate(request: Any) -> Any:
        return json(_person_out(PersonIn.model_validate(request.json)))

    app.ctx.greeter = Greeter()
    return app


def _falcon_app() -> Any:
    import falcon.asgi

    class Hello:
        async def on_get(self, req: Any, resp: Any) -> None:
            resp.media = {"ok": True}

    class Payload:
        async def on_get(self, req: Any, resp: Any) -> None:
            resp.media = dict(JSON_PAYLOAD)

    class Item:
        async def on_get(self, req: Any, resp: Any, id: str) -> None:
            resp.media = {
                "id": int(id),
                "limit": req.get_param_as_int("limit"),
                "q": req.get_param("q"),
            }

    class Di:
        def __init__(self, greeter: Greeter) -> None:
            self._greeter = greeter

        async def on_get(self, req: Any, resp: Any) -> None:
            resp.media = {"service": "Greeter", "greeting": self._greeter.greet()}

    class Validate:
        async def on_post(self, req: Any, resp: Any) -> None:
            resp.media = _person_out(PersonIn.model_validate(await req.get_media()))

    app = falcon.asgi.App()
    app.add_route("/", Hello())
    app.add_route("/json", Payload())
    app.add_route("/items/{id}", Item())
    app.add_route("/di", Di(Greeter()))
    app.add_route("/validate", Validate())
    return app


def _quart_app() -> Any:
    from quart import Quart, jsonify, request

    app = Quart(__name__)

    @app.get("/")
    async def hello() -> Any:
        return jsonify({"ok": True})

    @app.get("/json")
    async def payload() -> Any:
        return jsonify(dict(JSON_PAYLOAD))

    @app.get("/items/<int:id>")
    async def item(id: int) -> Any:
        return jsonify(
            {
                "id": id,
                "limit": request.args.get("limit", type=int),
                "q": request.args.get("q"),
            }
        )

    @app.get("/di")
    async def di() -> Any:
        greeter: Greeter = app.extensions["greeter"]
        return jsonify({"service": "Greeter", "greeting": greeter.greet()})

    @app.post("/validate")
    async def validate() -> Any:
        return jsonify(_person_out(PersonIn.model_validate(await request.get_json())))

    app.extensions["greeter"] = Greeter()
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
        di_mechanism="constructor injection from the @module provider graph",
        validation="built-in Json[PydanticModel] extractor",
        app=_lauren_app,
    ),
    Framework(
        key="fastapi",
        dist="fastapi",
        entrypoint="fastapi.FastAPI()",
        serialization="stdlib json (Starlette JSONResponse; ORJSONResponse is opt-in)",
        di_mechanism="Depends(callable), resolved per request",
        validation="built-in body annotation (pydantic)",
        app=_fastapi_app,
    ),
    Framework(
        key="starlette",
        dist="starlette",
        entrypoint="starlette.applications.Starlette()",
        serialization="stdlib json (starlette.responses.JSONResponse)",
        di_mechanism="none — app.state lookup inside the handler",
        validation="manual — await request.json() then model_validate",
        app=_starlette_app,
    ),
    Framework(
        key="litestar",
        dist="litestar",
        entrypoint="litestar.Litestar()",
        serialization="msgspec (msgspec.json.Encoder via litestar.serialization)",
        di_mechanism="Provide(dependency) declared on the route",
        validation="built-in typed parameter (pydantic)",
        app=_litestar_app,
    ),
    Framework(
        key="blacksheep",
        dist="blacksheep",
        entrypoint="blacksheep.Application()",
        serialization="stdlib json (essentials.json.dumps, an optimised json.dumps)",
        di_mechanism="services.add_singleton + handler-parameter resolution",
        validation="built-in pydantic binding on the handler parameter",
        app=_blacksheep_app,
    ),
    Framework(
        key="sanic",
        dist="sanic",
        entrypoint=(
            "sanic.Sanic(name) — Sanic.__call__ is ASGI-native, but the app only "
            "serves after the lifespan startup handshake"
        ),
        serialization="ujson (sanic.helpers prefers ujson when it is installed)",
        di_mechanism="none — app.ctx lookup inside the handler",
        validation="manual — request.json then model_validate",
        app=_sanic_app,
    ),
    Framework(
        key="falcon",
        dist="falcon",
        entrypoint="falcon.asgi.App()",
        serialization="stdlib json (falcon.media.JSONHandler default)",
        di_mechanism="constructor injection at route-registration time",
        validation="manual — await req.get_media() then model_validate",
        app=_falcon_app,
    ),
    Framework(
        key="quart",
        dist="quart",
        entrypoint="quart.Quart(name)",
        serialization="stdlib json (Quart's DefaultJSONProvider)",
        di_mechanism="none — app.extensions lookup inside the handler",
        validation="manual — await request.get_json() then model_validate",
        app=_quart_app,
    ),
    Framework(
        key="aiohttp",
        dist="aiohttp",
        entrypoint="none — aiohttp.web.Application is not an ASGI application",
        serialization="stdlib json (aiohttp.web.json_response)",
        di_mechanism="n/a",
        validation="n/a",
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
        di_mechanism="n/a",
        validation="n/a",
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


def make_app(key: str) -> Any:
    """Build a fresh application for ``key``, registering all five workloads.

    Raises:
        KeyError: the framework is unknown or is not measurable in-process.
    """
    framework = BY_KEY[key]
    if not framework.measured or framework.app is None:
        raise KeyError(f"{key} is not measurable in-process: {framework.not_measured_reason}")
    return framework.app()


def installed_versions() -> dict[str, str]:
    """Map every registry key to its installed distribution version."""
    return {fw.key: fw.version for fw in FRAMEWORKS}
