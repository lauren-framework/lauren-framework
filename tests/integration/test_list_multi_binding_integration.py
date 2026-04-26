"""End-to-end tests for ``list[T]`` multi-binding parameters.

These tests drive real HTTP requests against a compiled application to
confirm that declaring a handler parameter as ``list[T]`` collects every
provider registered for ``T`` with ``multi=True``, threads it through
the DI container's request cache, and serializes the aggregate response
correctly.

Every test builds a fresh application so module-level state cannot
leak between scenarios — Lauren is explicitly designed to let multiple
apps coexist in one process (see ``.CLAUDE.md`` §3 / AGENTS.md).
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

import pytest

from lauren import (
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Shared Protocol used across scenarios.
# ---------------------------------------------------------------------------


@runtime_checkable
class Notifier(Protocol):
    """Every implementation exposes a short ``channel()`` name used in
    the integration assertions."""

    def channel(self) -> str: ...


# ---------------------------------------------------------------------------
# Happy path — a handler with ``list[T]`` sees every multi-binding.
# ---------------------------------------------------------------------------


class TestHandlerListParameterHappyPath:
    """``async def(handler, senders: list[T])`` receives all providers."""

    def test_handler_receives_all_multi_bound_instances(self):
        @injectable(provides=[Notifier], multi=True)
        class EmailNotifier:
            def channel(self) -> str:
                return "email"

        @injectable(provides=[Notifier], multi=True)
        class SmsNotifier:
            def channel(self) -> str:
                return "sms"

        @injectable(provides=[Notifier], multi=True)
        class PushNotifier:
            def channel(self) -> str:
                return "push"

        @controller("/api")
        class NotifierController:
            @get("/channels")
            async def list_channels(self, notifiers: list[Notifier]) -> dict:
                return {"channels": sorted(n.channel() for n in notifiers)}

        @module(
            controllers=[NotifierController],
            providers=[EmailNotifier, SmsNotifier, PushNotifier],
        )
        class AppModule:
            pass

        app = asyncio.run(LaurenFactory.create(AppModule))
        client = TestClient(app)
        resp = client.get("/api/channels")
        assert resp.status_code == 200
        assert resp.json() == {"channels": ["email", "push", "sms"]}

    def test_empty_list_type_parameter_is_rejected_at_startup(self):
        """A handler typed ``list[Foo]`` when no ``Foo`` provider exists
        must fail at startup, not on the first request.

        That behaviour is part of Lauren's \"startup validates, runtime
        dispatches\" contract: users learn of the mistake as soon as they
        run ``LaurenFactory.create``.
        """

        @runtime_checkable
        class MissingProto(Protocol):
            def go(self) -> None: ...

        @controller("/")
        class C:
            @get("/x")
            async def x(self, items: list[MissingProto]) -> dict:
                return {}

        @module(controllers=[C])
        class AppModule:
            pass

        with pytest.raises(Exception) as excinfo:
            asyncio.run(LaurenFactory.create(AppModule))
        # Any of these two error types is an acceptable signal for the
        # user — the handler compiler rejects the unresolvable parameter
        # before we ever reach a request.
        from lauren.exceptions import (
            MissingProviderError,
            UnresolvableParameterError,
        )

        assert isinstance(
            excinfo.value, (MissingProviderError, UnresolvableParameterError)
        )


# ---------------------------------------------------------------------------
# Controllers can also ask for ``list[T]`` via their ``__init__`` — this
# is the \"plugin registry\" pattern.
# ---------------------------------------------------------------------------


class TestControllerConstructorList:
    """Controller ``__init__`` can accept a ``list[T]`` of plugins."""

    def test_controller_init_receives_plugin_list(self):
        @runtime_checkable
        class Plugin(Protocol):
            def describe(self) -> str: ...

        @injectable(provides=[Plugin], multi=True)
        class PluginA:
            def describe(self) -> str:
                return "a"

        @injectable(provides=[Plugin], multi=True)
        class PluginB:
            def describe(self) -> str:
                return "b"

        @controller("/plugins")
        class PluginCtl:
            def __init__(self, plugins: list[Plugin]) -> None:
                self._plugins = plugins

            @get("/")
            async def list_all(self) -> dict:
                return {"plugins": sorted(p.describe() for p in self._plugins)}

        @module(controllers=[PluginCtl], providers=[PluginA, PluginB])
        class AppModule:
            pass

        app = asyncio.run(LaurenFactory.create(AppModule))
        client = TestClient(app)
        resp = client.get("/plugins/")
        assert resp.status_code == 200
        assert resp.json() == {"plugins": ["a", "b"]}


# ---------------------------------------------------------------------------
# Request scope: ``list[T]`` with REQUEST-scoped members yields stable
# identity across multiple reads in one request.
# ---------------------------------------------------------------------------


class TestRequestScopedListStability:
    """Within one request, each ``list[T]`` member is constructed once."""

    def test_request_scoped_members_are_identity_stable_within_request(self):
        @runtime_checkable
        class Obs(Protocol):
            @property
            def id(self) -> int: ...

        @injectable(provides=[Obs], multi=True, scope=Scope.REQUEST)
        class ObsA:
            _ctr = [0]

            def __init__(self) -> None:
                ObsA._ctr[0] += 1
                self._id = ObsA._ctr[0]

            @property
            def id(self) -> int:
                return self._id

        @injectable(provides=[Obs], multi=True, scope=Scope.REQUEST)
        class ObsB:
            _ctr = [0]

            def __init__(self) -> None:
                ObsB._ctr[0] += 1
                self._id = ObsB._ctr[0]

            @property
            def id(self) -> int:
                return self._id

        @injectable(scope=Scope.REQUEST)
        class Wrapper:
            def __init__(self, xs: list[Obs]) -> None:
                self.xs = xs

        @controller("/s")
        class C:
            @get("/pair")
            async def pair(
                self,
                xs: list[Obs],
                wrapper: Wrapper,
            ) -> dict:
                # ``xs`` and ``wrapper.xs`` must contain the same
                # instances — that's the REQUEST-scope contract.
                return {
                    "direct": [x.id for x in xs],
                    "via_wrapper": [x.id for x in wrapper.xs],
                    "identity_matches": all(a is b for a, b in zip(xs, wrapper.xs)),
                }

        @module(controllers=[C], providers=[ObsA, ObsB, Wrapper])
        class AppModule:
            pass

        app = asyncio.run(LaurenFactory.create(AppModule))
        client = TestClient(app)
        r = client.get("/s/pair")
        assert r.status_code == 200
        data = r.json()
        assert data["identity_matches"] is True
        assert data["direct"] == data["via_wrapper"]


# ---------------------------------------------------------------------------
# Zero-and-one edge cases — the list-typed parameter must still work
# when only a single provider is registered.
# ---------------------------------------------------------------------------


class TestSingleMultiBinding:
    def test_single_provider_yields_single_element_list(self):
        @runtime_checkable
        class Sole(Protocol):
            def hello(self) -> str: ...

        @injectable(provides=[Sole], multi=True)
        class Only:
            def hello(self) -> str:
                return "hi"

        @controller("/s")
        class C:
            @get("/")
            async def idx(self, xs: list[Sole]) -> dict:
                return {"len": len(xs), "hello": xs[0].hello()}

        @module(controllers=[C], providers=[Only])
        class AppModule:
            pass

        app = asyncio.run(LaurenFactory.create(AppModule))
        client = TestClient(app)
        r = client.get("/s/")
        assert r.status_code == 200
        assert r.json() == {"len": 1, "hello": "hi"}


# ---------------------------------------------------------------------------
# Interaction with other extractors: ``list[T]`` shares the handler
# signature with path parameters, query parameters, request bodies, etc.
# ---------------------------------------------------------------------------


class TestMixedExtractors:
    def test_list_parameter_coexists_with_path_and_query(self):
        from lauren import Path, Query

        @runtime_checkable
        class Tag(Protocol):
            def text(self) -> str: ...

        @injectable(provides=[Tag], multi=True)
        class TagA:
            def text(self) -> str:
                return "alpha"

        @injectable(provides=[Tag], multi=True)
        class TagB:
            def text(self) -> str:
                return "beta"

        @controller("/items")
        class Ctl:
            @get("/{item_id}")
            async def show(
                self,
                item_id: Path[int],
                tags: list[Tag],
                verbose: Query[bool] = False,
            ) -> dict:
                out = {
                    "id": item_id,
                    "tags": sorted(t.text() for t in tags),
                }
                if verbose:
                    out["count"] = len(tags)
                return out

        @module(controllers=[Ctl], providers=[TagA, TagB])
        class AppModule:
            pass

        app = asyncio.run(LaurenFactory.create(AppModule))
        client = TestClient(app)
        r = client.get("/items/42?verbose=true")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == 42
        assert body["tags"] == ["alpha", "beta"]
        assert body["count"] == 2


# ---------------------------------------------------------------------------
# Service layer — a regular @injectable service that consumes ``list[T]``
# behaves the same way when reached through a controller.
# ---------------------------------------------------------------------------


class TestServiceConsumesList:
    def test_service_with_list_dep_is_reachable_via_handler(self):
        @runtime_checkable
        class Step(Protocol):
            def run(self) -> str: ...

        @injectable(provides=[Step], multi=True)
        class StepOne:
            def run(self) -> str:
                return "one"

        @injectable(provides=[Step], multi=True)
        class StepTwo:
            def run(self) -> str:
                return "two"

        @injectable()
        class Pipeline:
            def __init__(self, steps: list[Step]) -> None:
                self._steps = steps

            def execute(self) -> list[str]:
                return [s.run() for s in self._steps]

        @controller("/pipe")
        class Ctl:
            @get("/")
            async def index(self, pipeline: Pipeline) -> dict:
                return {"steps": pipeline.execute()}

        @module(
            controllers=[Ctl],
            providers=[StepOne, StepTwo, Pipeline],
        )
        class AppModule:
            pass

        app = asyncio.run(LaurenFactory.create(AppModule))
        client = TestClient(app)
        r = client.get("/pipe/")
        assert r.status_code == 200
        # Both steps are present; the module compiler's provider
        # registration order is an implementation detail and deliberately
        # not relied upon in this integration-level assertion.
        assert sorted(r.json()["steps"]) == ["one", "two"]


# ---------------------------------------------------------------------------
# Module visibility: a handler can only see multi-bindings exported by
# its module's import graph.
# ---------------------------------------------------------------------------


class TestModuleBoundaryWithList:
    def test_handler_sees_only_bindings_from_imported_modules(self):
        @runtime_checkable
        class Feature(Protocol):
            def name(self) -> str: ...

        @injectable(provides=[Feature], multi=True)
        class Public:
            def name(self) -> str:
                return "public"

        @injectable(provides=[Feature], multi=True)
        class Internal:
            def name(self) -> str:
                return "internal"

        # Lauren requires ``exports`` to reference a provider class that
        # the module either declares or imports — exporting the Protocol
        # itself isn't supported. Exporting the concrete ``Public``
        # class is enough: visibility is carried via the binding the
        # provider installs for its ``provides=[Feature]`` entry.
        @module(providers=[Public], exports=[Public])
        class SharedModule:
            pass

        @module(providers=[Internal])
        class PrivateModule:
            pass

        @controller("/f")
        class Ctl:
            @get("/")
            async def idx(self, items: list[Feature]) -> dict:
                return {"features": sorted(x.name() for x in items)}

        @module(
            controllers=[Ctl],
            imports=[SharedModule],
        )
        class AppModule:
            pass

        app = asyncio.run(LaurenFactory.create(AppModule))
        client = TestClient(app)
        r = client.get("/f/")
        assert r.status_code == 200
        # Only ``Public`` is visible — PrivateModule isn't imported at all.
        assert r.json() == {"features": ["public"]}
