"""``Lauren.on_startup`` hooks run after ``@post_construct`` (PRD F19 §5.10).

They used to run when the app compiled, before any singleton existed; they
now run inside ``LaurenApp.startup()``, right after the ``@post_construct``
hooks and before hosted services start or a request is served.
"""

from __future__ import annotations

import pytest

from lauren import ApplicationContext, Lauren, injectable, post_construct
from lauren.exceptions import StartupError
from lauren.scheduling import ServiceContext, hosted_service
from lauren.testing import TestClient

EVENTS: list[str] = []


@pytest.fixture(autouse=True)
def _reset() -> None:
    EVENTS.clear()


@injectable()
class Db:
    def __init__(self) -> None:
        self.ready = False

    @post_construct
    async def connect(self) -> None:
        self.ready = True
        EVENTS.append("Db.@post_construct")


@hosted_service()
class Relay:
    async def run(self, ctx: ServiceContext) -> None:
        EVENTS.append("Relay.run")
        await ctx.wait_stopped()


def _app(**kwargs: object) -> Lauren:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None, **kwargs)  # type: ignore[arg-type]
    app.add_provider(Db)

    @app.get("/")
    async def root() -> dict[str, list[str]]:
        EVENTS.append("request")
        return {"events": list(EVENTS)}

    return app


def test_on_startup_runs_after_post_construct_under_the_lifespan() -> None:
    app = _app()

    @app.on_startup
    def boot() -> None:
        EVENTS.append("app.on_startup")

    with TestClient(app) as client:
        client.get("/")

    assert EVENTS == ["Db.@post_construct", "app.on_startup", "request"]


def test_on_startup_can_use_initialised_singletons() -> None:
    app = _app()
    seen: list[bool] = []

    @app.on_startup
    async def warm() -> None:
        seen.append(app.container.singletons()[Db].ready)

    with TestClient(app) as client:
        client.get("/")

    assert seen == [True]


def test_hooks_run_in_registration_order_before_hosted_services() -> None:
    app = _app()
    app.add_provider(Relay)

    @app.on_startup
    def first() -> None:
        EVENTS.append("first")

    @app.on_startup
    async def second() -> None:
        EVENTS.append("second")

    with TestClient(app) as client:
        client.get("/")

    assert EVENTS[:3] == ["Db.@post_construct", "first", "second"]
    assert EVENTS.index("second") < EVENTS.index("Relay.run")


async def test_compiling_does_not_run_hooks() -> None:
    app = _app()

    @app.on_startup
    def boot() -> None:  # pragma: no cover - asserted not to run
        EVENTS.append("app.on_startup")

    compiled = await app.startup()

    assert compiled._started is False
    assert EVENTS == []


async def test_a_failing_hook_aborts_startup_with_startup_error() -> None:
    app = _app()

    @app.on_startup
    def explode() -> None:
        raise ValueError("no config")

    ctx = ApplicationContext.from_lauren(app)
    with pytest.raises(StartupError) as info:
        async with ctx:
            pass  # pragma: no cover - never entered

    assert info.value.detail == {"handler": "explode"}
    assert isinstance(info.value.__cause__, ValueError)
    assert EVENTS == ["Db.@post_construct"]


async def test_hooks_run_when_an_application_context_starts() -> None:
    app = _app()

    @app.on_startup
    async def boot() -> None:
        EVENTS.append("app.on_startup")

    async with ApplicationContext.from_lauren(app):
        assert EVENTS == ["Db.@post_construct", "app.on_startup"]
