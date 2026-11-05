"""Integration tests for Phase 6a (provider discovery) of ``LaurenFactory.create``.

Each test swaps in a fresh marker registry, as a feature module would populate
it at import time, so the process-wide registry is never touched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import lauren._discovery as discovery
from lauren import Lauren, LaurenFactory, Scope, controller, get, injectable, module, pre_destruct
from lauren._di import _scope
from lauren._discovery import DiscoveryIndex, MarkerRegistry, MarkerSpec
from lauren.exceptions import MetadataInheritanceError, UnresolvableParameterError

JOB = "__lauren_test_job__"
SERVICE = "__lauren_test_service__"


class JobContext:
    def __init__(self, name: str) -> None:
        self.name = name


def job(name: str) -> Any:
    def deco(fn: Any) -> Any:
        setattr(fn, JOB, [*fn.__dict__.get(JOB, []), name])
        return fn

    return deco


def service(cls: type) -> type:
    setattr(cls, SERVICE, {"name": cls.__name__})
    return cls


@pytest.fixture(autouse=True)
def _feature_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the test markers the way a feature module does on import."""
    registry = MarkerRegistry(
        [MarkerSpec(JOB, framework_types=(JobContext,)), MarkerSpec(SERVICE, kind="class")]
    )
    monkeypatch.setattr(discovery, "_REGISTRY", registry)
    monkeypatch.setattr(_scope, "_FRAMEWORK_VALUES", {**_scope._FRAMEWORK_VALUES, JobContext: None})


def test_create_stores_the_index_on_the_app() -> None:
    @injectable()
    class Jobs:
        @job("tick")
        async def tick(self) -> None: ...

    @module(providers=[Jobs])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    assert isinstance(app._discovery, DiscoveryIndex)
    assert [e.meta for e in app._discovery.methods[JOB]] == [["tick"]]
    assert app._discovery.classes[SERVICE] == ()


def test_an_app_without_registered_markers_has_an_empty_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "_REGISTRY", MarkerRegistry())

    @module()
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    assert dict(app._discovery.methods) == {}
    assert dict(app._discovery.classes) == {}


def test_discovery_spans_modules_controllers_and_global_providers() -> None:
    @injectable()
    class Imported:
        @job("imported")
        async def run(self) -> None: ...

    @module(providers=[Imported])
    class FeatureModule:
        pass

    @controller("/c")
    class Ctrl:
        @get("/")
        async def index(self) -> dict:
            return {}

        @job("controller")
        async def run(self) -> None: ...

    @injectable()
    class Global:
        @job("global")
        async def run(self) -> None: ...

    @module(imports=[FeatureModule], controllers=[Ctrl])
    class AppModule:
        pass

    first = LaurenFactory.create(AppModule, global_providers=[Global])
    second = LaurenFactory.create(AppModule, global_providers=[Global])

    names = [e.meta[0] for e in first._discovery.methods[JOB]]
    assert sorted(names) == ["controller", "global", "imported"]
    assert names == [e.meta[0] for e in second._discovery.methods[JOB]]  # deterministic
    owners = {e.meta[0]: e.provider.owning_module for e in first._discovery.methods[JOB]}
    assert owners == {"imported": FeatureModule, "controller": AppModule, "global": None}


def test_hosted_style_class_markers_are_indexed() -> None:
    @service
    @injectable()
    class Relay:
        pass

    @module(providers=[Relay])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    ((provider, meta),) = app._discovery.classes[SERVICE]
    assert (provider.cls, meta) == (Relay, {"name": "Relay"})


def test_inherited_class_marker_fails_create() -> None:
    @service
    @injectable()
    class Relay:
        pass

    @injectable()
    class FastRelay(Relay):
        pass

    @module(providers=[FastRelay])
    class AppModule:
        pass

    with pytest.raises(MetadataInheritanceError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["inherits_from"] == "Relay"


def test_unresolvable_method_parameter_fails_create() -> None:
    @injectable()
    class Jobs:
        @job("bad")
        async def run(self, limit: int) -> None: ...

    @module(providers=[Jobs])
    class AppModule:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail == {"target": "Jobs.run", "param": "limit", "reason": "unresolvable"}


def test_method_dependencies_resolve_in_the_providers_own_module() -> None:
    @injectable()
    class Secret:
        pass

    @module(providers=[Secret])  # not exported
    class Private:
        pass

    @injectable()
    class Jobs:
        @job("leaky")
        async def run(self, secret: Secret) -> None: ...

    @module(imports=[Private], providers=[Jobs])
    class AppModule:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["param"] == "secret"


def test_discovered_method_runs_in_an_operation_scope_after_startup() -> None:
    events: list[str] = []

    @injectable(scope=Scope.REQUEST)
    class DbSession:
        @pre_destruct
        def close(self) -> None:
            events.append("DbSession.closed")

    @injectable()
    class Maintenance:
        @job("purge")
        async def purge(self, ctx: JobContext, db: DbSession) -> str:
            events.append(f"purge:{ctx.name}")
            return "done"

    @module(providers=[DbSession, Maintenance])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)
    (entry,) = app._discovery.methods[JOB]

    async def main() -> Any:
        await app.startup()
        instance = await app.container.resolve(entry.provider.cls)
        ctx = JobContext("nightly")
        assert entry.plan.needs_scope
        async with app.scope(f"job:{ctx.name}", values={JobContext: ctx}) as scope:
            return await entry.invoke(instance, scope)

    assert asyncio.run(main()) == "done"
    assert events == ["purge:nightly", "DbSession.closed"]


def test_imperative_lauren_apps_run_discovery_too() -> None:
    @injectable()
    class Jobs:
        @job("imperative")
        async def run(self) -> None: ...

    app = Lauren()
    app.add_provider(Jobs)

    compiled = asyncio.run(app.startup())

    assert [e.meta for e in compiled._discovery.methods[JOB]] == [["imperative"]]
