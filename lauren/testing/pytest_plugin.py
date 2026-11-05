"""Opt-in pytest fixtures for Lauren apps.

Enable in a ``conftest.py`` (the plugin is deliberately *not* registered
through a ``pytest11`` entry point, so installing ``lauren`` never injects
fixture names into a project)::

    pytest_plugins = ["lauren.testing.pytest_plugin"]

Then either point the ``lauren_app`` ini option at your app::

    [tool.pytest.ini_options]
    lauren_app = "myapp.main:AppModule"    # module:attribute
    lauren_client_scope = "module"         # optional; default "function"

or define a ``lauren_testing_module`` fixture returning a
:class:`~lauren.testing.TestingModuleBuilder` (or a built app)::

    @pytest.fixture(scope="module")
    def lauren_testing_module():
        return Test.create_testing_module(AppModule).override_provider(Mailer).use_value(FakeMailer())

Fixtures:

* ``lauren_app`` — a fresh app per ``lauren_client_scope``: the
  ``lauren_testing_module`` fixture compiled when it exists, otherwise the
  ini target — a ``@module`` class is built with ``LaurenFactory.create``,
  a ``TestingModuleBuilder`` is compiled. An already-built app object can
  go through only one lifespan, so it needs ``lauren_client_scope =
  session``.
* ``lauren_client`` — a lifespan-managed :class:`~lauren.testing.TestClient`
  (``with TestClient(lauren_app)``), scoped by ``lauren_client_scope``.
* ``lauren_aclient`` — a lifespan-managed
  :class:`~lauren.testing.AsyncTestClient` on the test's loop (needs
  ``pytest-asyncio``; function-scoped, so keep the default client scope).
* ``lauren_ws`` — ``lauren_client.websocket_connect``.
* ``lauren_capture`` — ``lauren_client.capture``.

With a ``module`` / ``session`` client scope, give ``lauren_testing_module``
at least the same scope. Use either ``lauren_client`` or ``lauren_aclient``
in a test, not both (one driver per app).
"""

from __future__ import annotations

import importlib
from typing import Any, Iterator, Literal, cast

import pytest

from ._client import AsyncTestClient, TestClient
from ._module import TestingModuleBuilder

_SCOPES = ("function", "class", "module", "package", "session")
_Scope = Literal["session", "package", "module", "class", "function"]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini("lauren_app", "App for the lauren_app fixture, as 'package.module:attribute'", default="")
    parser.addini(
        "lauren_client_scope", "Scope of the lauren_app / lauren_client fixtures", default="function"
    )


def _client_scope(fixture_name: str, config: pytest.Config) -> _Scope:
    scope = str(config.getini("lauren_client_scope") or "function")
    if scope not in _SCOPES:
        raise pytest.UsageError(f"lauren_client_scope must be one of {', '.join(_SCOPES)}, got {scope!r}")
    return cast(_Scope, scope)


def _import_target(target: str) -> Any:
    module_name, sep, attribute = target.partition(":")
    if not sep or not module_name or not attribute:
        raise pytest.UsageError(f"lauren_app must look like 'package.module:attribute', got {target!r}")
    obj: Any = importlib.import_module(module_name)
    for part in attribute.split("."):
        obj = getattr(obj, part)
    return obj


def _build(obj: Any) -> tuple[Any, bool]:
    """Turn a target into an app; the flag says whether the app is fresh."""
    from .._asgi import LaurenFactory
    from ..decorators import MODULE_META

    if isinstance(obj, TestingModuleBuilder):
        return obj.compile(), True
    if isinstance(obj, type) and MODULE_META in obj.__dict__:
        return LaurenFactory.create(obj), True
    return obj, False


def load_app(target: str) -> Any:
    """Import ``"package.module:attribute"`` and turn it into an app (see the module docstring)."""
    return _build(_import_target(target))[0]


@pytest.fixture(scope=_client_scope)
def lauren_app(request: pytest.FixtureRequest) -> Any:
    """The app under test (``lauren_testing_module`` compiled, or the ``lauren_app`` ini target)."""
    try:
        source = request.getfixturevalue("lauren_testing_module")
    except pytest.FixtureLookupError:
        target = str(request.config.getini("lauren_app") or "")
        if not target:
            raise pytest.UsageError(
                "lauren_app: define a `lauren_testing_module` fixture or set the `lauren_app` ini option "
                "('package.module:attribute')"
            ) from None
        source = _import_target(target)
    app, fresh = _build(source)
    if not fresh and request.scope != "session":
        raise pytest.UsageError(
            "lauren_app resolves to an already-built app, which can be started only once; set "
            "`lauren_client_scope = session`, or point it at the root @module class or a "
            "TestingModuleBuilder so every client gets a fresh app"
        )
    return app


@pytest.fixture(scope=_client_scope)
def lauren_client(lauren_app: Any) -> Iterator[TestClient]:
    """A lifespan-managed :class:`TestClient` for ``lauren_app``."""
    with TestClient(lauren_app) as client:
        yield client


@pytest.fixture
def lauren_ws(lauren_client: TestClient) -> Any:
    """``lauren_client.websocket_connect`` — ``with lauren_ws("/chat") as ws: ...``."""
    return lauren_client.websocket_connect


@pytest.fixture
def lauren_capture(lauren_client: TestClient) -> Any:
    """``lauren_client.capture`` — ``with lauren_capture(RequestComplete) as events: ...``."""
    return lauren_client.capture


async def _async_client(lauren_app: Any) -> Any:
    """A lifespan-managed :class:`AsyncTestClient` for ``lauren_app`` on the test's loop."""
    async with AsyncTestClient(lauren_app) as client:
        yield client


def _needs_pytest_asyncio() -> Any:
    """Needs pytest-asyncio (``pip install pytest-asyncio``)."""
    raise pytest.UsageError("the lauren_aclient fixture needs pytest-asyncio installed")


lauren_aclient: Any
try:
    import pytest_asyncio
except ImportError:  # pragma: no cover - pytest-asyncio is optional
    lauren_aclient = pytest.fixture(name="lauren_aclient")(_needs_pytest_asyncio)
else:
    lauren_aclient = pytest_asyncio.fixture(name="lauren_aclient")(_async_client)


__all__ = ["lauren_aclient", "lauren_app", "lauren_capture", "lauren_client", "lauren_ws", "load_app"]
