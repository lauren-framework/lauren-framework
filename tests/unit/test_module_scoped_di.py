"""Tests for NestJS-style module-scoped dependency injection.

These exercise the encapsulation contract added to the DI container:

* A provider declared in module *B* is visible in module *A* only if *B*
  exports it **and** *A* imports *B* (transitively).
* ``Depends[X]`` on an endpoint resolves through the controller's declaring
  module, just like constructor-injected dependencies.
* Auto-inferred DI parameters (bare ``svc: SomeService`` on a handler)
  honour the same visibility rules.
"""

# NOTE: we intentionally do NOT use `from __future__ import annotations`.
# Several tests below declare their providers and controllers inside test
# methods; PEP 563 stringified annotations cannot be resolved by
# ``get_type_hints`` in a nested function scope.

import pytest

from lauren import (
    Depends,
    LaurenFactory,
    Request,
    controller,
    get,
    injectable,
    module,
)
from lauren.exceptions import MissingProviderError
from lauren.types import Headers


def _make_request(method: str = "GET", path: str = "/") -> Request:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        method=method,
        path=path,
        raw_query_string=b"",
        headers=Headers([]),
        receive=receive,
    )


# ---------------------------------------------------------------------------
# Positive case: provider exported by an imported module IS visible.
# ---------------------------------------------------------------------------


@injectable()
class SharedService:
    def __init__(self):
        self.name = "shared"


@controller("/feature")
class FeatureController:
    def __init__(self, shared: SharedService):
        self.shared = shared

    @get("/name")
    async def name(self) -> dict:
        return {"name": self.shared.name}


@module(providers=[SharedService], exports=[SharedService])
class SharedModule:
    pass


@module(controllers=[FeatureController], imports=[SharedModule])
class FeatureModule:
    pass


@module(imports=[FeatureModule])
class RootModule:
    pass


class TestExportedProviderVisible:
    @pytest.mark.asyncio
    async def test_controller_can_use_imported_exported_provider(self):
        app = await LaurenFactory.create(RootModule)
        resp = await app.handle(_make_request(path="/feature/name"))
        assert resp.status == 200
        import json

        assert json.loads(resp.body) == {"name": "shared"}


# ---------------------------------------------------------------------------
# Negative case: provider NOT exported is invisible to importers.
# ---------------------------------------------------------------------------


class TestNonExportedProviderHidden:
    @pytest.mark.asyncio
    async def test_controller_cannot_see_non_exported_provider(self):
        @injectable()
        class PrivateSvc:
            def __init__(self):
                self.x = 1

        @controller("/consumer")
        class ConsumerController:
            # Declared in another module and NOT exported \u2014 should fail
            # startup with a clear "not visible from <module>" error.
            def __init__(self, priv: PrivateSvc):
                self.priv = priv

            @get("/")
            async def root(self) -> dict:
                return {"x": self.priv.x}

        @module(providers=[PrivateSvc])  # no exports
        class HiddenModule:
            pass

        @module(controllers=[ConsumerController], imports=[HiddenModule])
        class ConsumerModule:
            pass

        @module(imports=[ConsumerModule])
        class Root:
            pass

        with pytest.raises(MissingProviderError) as ei:
            await LaurenFactory.create(Root)
        assert "visible from module ConsumerModule" in str(ei.value)


# ---------------------------------------------------------------------------
# Depends[X] on an endpoint \u2014 resolution honours module visibility.
# ---------------------------------------------------------------------------


class TestDependsEndpointAnnotation:
    @pytest.mark.asyncio
    async def test_depends_resolves_from_imported_module(self):
        @injectable()
        class Counter:
            def __init__(self):
                self.n = 0

            def inc(self) -> int:
                self.n += 1
                return self.n

        @controller("/api")
        class Ctrl:
            @get("/bump")
            async def bump(self, counter: Depends[Counter]) -> dict:
                return {"value": counter.inc()}

        @module(providers=[Counter], exports=[Counter])
        class SvcMod:
            pass

        @module(controllers=[Ctrl], imports=[SvcMod])
        class FeatMod:
            pass

        @module(imports=[FeatMod])
        class R:
            pass

        app = await LaurenFactory.create(R)
        r1 = await app.handle(_make_request(path="/api/bump"))
        r2 = await app.handle(_make_request(path="/api/bump"))
        import json

        # Counter is singleton \u2014 state persists across requests.
        assert json.loads(r1.body) == {"value": 1}
        assert json.loads(r2.body) == {"value": 2}

    @pytest.mark.asyncio
    async def test_depends_blocked_when_not_exported(self):
        @injectable()
        class Secret:
            def __init__(self):
                self.v = "nope"

        @controller("/leak")
        class Leaker:
            @get("/")
            async def handler(self, s: Depends[Secret]) -> dict:
                return {"v": s.v}

        @module(providers=[Secret])  # not exported
        class Vault:
            pass

        @module(controllers=[Leaker], imports=[Vault])
        class Feature:
            pass

        @module(imports=[Feature])
        class Root:
            pass

        # Auto-promotion is based on `container.has_provider(ann, owning_module=...)`
        # which returns False when the provider isn't visible \u2014 so the bare
        # parameter has no extractor and we get UnresolvableParameterError.
        # But Depends[X] is explicit: it WILL attempt DI resolution and fail
        # with MissingProviderError at request time. Either outcome is a
        # security win; we just assert the app refuses to leak the value.
        app = await LaurenFactory.create(Root)
        resp = await app.handle(_make_request(path="/leak/"))
        assert resp.status >= 400
        import json

        body = json.loads(resp.body)
        # Some variant of "no provider ... visible from module Feature"
        assert "visible from module Feature" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Auto-inferred DI on endpoints honours visibility.
# ---------------------------------------------------------------------------


class TestAutoInferredEndpointDI:
    @pytest.mark.asyncio
    async def test_bare_type_annotation_resolved_via_module(self):
        @injectable()
        class Shared:
            def __init__(self):
                self.x = 42

        @controller("/a")
        class A:
            # Bare `svc: Shared` \u2014 auto-promoted to Depends by the framework
            # when visible from the module.
            @get("/v")
            async def v(self, svc: Shared) -> dict:
                return {"x": svc.x}

        @module(providers=[Shared], exports=[Shared])
        class M1:
            pass

        @module(controllers=[A], imports=[M1])
        class M2:
            pass

        @module(imports=[M2])
        class R:
            pass

        app = await LaurenFactory.create(R)
        resp = await app.handle(_make_request(path="/a/v"))
        assert resp.status == 200
        import json

        assert json.loads(resp.body) == {"x": 42}
