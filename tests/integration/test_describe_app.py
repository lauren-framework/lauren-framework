"""A compiled application's manifest is available without running its lifecycle."""

from __future__ import annotations

import json
from dataclasses import dataclass

from lauren import (
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    on_connect,
    on_message,
    post_construct,
    set_metadata,
    use_guards,
    ws_controller,
)
from lauren.config import Secret
from lauren.reflect import describe_app, get_all_routes


_events: list[str] = []


@injectable(scope=Scope.SINGLETON)
class _Repository:
    @post_construct
    async def connect(self) -> None:
        _events.append("connected")


@injectable(scope=Scope.SINGLETON)
class _Guard:
    async def can_activate(self, ctx: object) -> bool:
        return True


@dataclass
class _User:
    id: int


@use_guards(_Guard)
@controller("/users")
class _Users:
    def __init__(self, repo: _Repository) -> None:
        self.repo = repo

    @get("/{id}")
    @set_metadata("token", Secret("private"))
    async def show(self, id: int) -> _User:
        return _User(id)


@ws_controller("/events")
class _Events:
    @on_connect
    async def join(self) -> None:
        pass

    @on_message("ping")
    async def ping(self) -> None:
        pass


@module(providers=[_Repository, _Guard], controllers=[_Users, _Events], exports=[_Repository])
class _App:
    pass


def test_manifest_is_available_without_starting_the_app() -> None:
    _events.clear()
    app = LaurenFactory.create(_App)

    manifest = describe_app(app)

    assert not _events
    assert get_all_routes(app) == ()
    assert manifest.manifest_version == 1
    assert manifest.app.root_module.endswith("._App")
    assert any(r.path == "/users/{id}" and r.method == "GET" for r in manifest.routes)
    assert any(g.path == "/events" and g.events[0].event == "ping" for g in manifest.websocket_gateways)
    assert any(p.token.endswith("._Repository") and p.scope == "singleton" for p in manifest.providers)
    assert app.describe().to_json() == manifest.to_json()


def test_manifest_contains_effective_chains_and_never_exposes_metadata_values_by_default() -> None:
    app = LaurenFactory.create(_App, global_guards=[_Guard])

    manifest = describe_app(app)
    route = next(r for r in manifest.routes if r.path == "/users/{id}")
    encoded = manifest.to_json()

    assert len(route.guards) == 2
    assert route.metadata_keys == ("token",)
    assert "private" not in encoded
    assert "private" not in describe_app(app, include_metadata_values=True).to_json()
    assert json.loads(encoded)["routes"]


def test_manifest_is_deterministic_even_when_modules_and_providers_use_sets() -> None:
    first = describe_app(LaurenFactory.create(_App)).to_json()
    second = describe_app(LaurenFactory.create(_App)).to_json()

    assert first == second


def test_reflected_route_exposes_its_compiled_policy_chain_after_startup() -> None:
    app = LaurenFactory.create(_App)
    import asyncio

    async def run() -> None:
        await app.startup()
        try:
            route = next(r for r in get_all_routes(app) if r.full_path == "/users/{id}")
            assert route.guards == (_Guard,)
            assert route.interceptors == ()
            assert route.middlewares == ()
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_manifest_describes_sessions_and_mounts_without_leaking_the_secret() -> None:
    from lauren import SessionConfig

    secret = "only-for-a-local-test-0123456789abcdef"
    app = LaurenFactory.create(_App, sessions=SessionConfig(secret=secret))
    app.mount("/legacy", object())

    manifest = describe_app(app, include_metadata_values=True)

    assert manifest.app.sessions["enabled"] is True
    assert manifest.app.sessions["cookie_name"] == "lauren_session"
    assert manifest.mounts[0].prefix == "/legacy"
    assert secret not in manifest.to_json()


def test_manifest_exposes_compiled_provider_dependencies_and_exported_tokens() -> None:
    manifest = describe_app(LaurenFactory.create(_App))

    provider = next(p for p in manifest.providers if p.token.endswith("._Users"))
    owner = next(m for m in manifest.modules if m.name.endswith("._App"))
    route = next(r for r in manifest.routes if r.path == "/users/{id}")

    assert provider.deps[0].token.endswith("._Repository")
    assert any(token.endswith("._Repository") for token in owner.exports)
    assert route.params[0].type == "int"
    assert route.params[0].required is True
    assert manifest.openapi.operations >= 1
    assert len(manifest.openapi.sha256) == 64


def test_manifest_reports_resolved_import_substitutions() -> None:
    from lauren.testing import Test

    @module()
    class Original:
        pass

    @module()
    class Replacement:
        pass

    @module(imports=[Original])
    class Root:
        pass

    app = Test.create_testing_module(Root).override_module(Original).with_module(Replacement).compile()
    manifest = describe_app(app)
    root = next(m for m in manifest.modules if m.name.endswith("Root"))
    assert root.imports[0].endswith("Replacement")
    assert all(not m.name.endswith("Original") for m in manifest.modules)


def test_manifest_reports_field_injection_and_alias_dependencies() -> None:
    from lauren import Token, use_existing, use_value

    @injectable()
    class FieldConsumer:
        repository: _Repository

    alias = Token("repo-alias")

    @module(providers=[_Repository, FieldConsumer, use_existing(provide=alias, existing=_Repository)])
    class Root:
        pass

    app = LaurenFactory.create(Root, global_providers=[use_value(provide="count", value=3)])
    manifest = describe_app(app)
    consumer = next(p for p in manifest.providers if p.token.endswith("FieldConsumer"))
    assert consumer.deps[0].name == "repository"
    alias_provider = next(p for p in manifest.providers if p.token == "Token(repo-alias)")
    assert alias_provider.deps[0].token.endswith("_Repository")
    assert manifest.globals.providers == ("'count'",)


def test_manifest_includes_only_public_typed_values_and_redacts_secrets() -> None:
    from lauren import MetadataKey

    public = MetadataKey[dict]("visibility.public", default={}, public=True)
    private = MetadataKey[str]("visibility.private", default="hidden")

    @controller("/visibility")
    @public({"enabled": True, "secret": Secret("do-not-publish")})
    class Visible:
        @get("/")
        @private("private-value")
        async def show(self) -> dict:
            return {}

    @module(controllers=[Visible])
    class Root:
        pass

    app = LaurenFactory.create(Root)
    route = describe_app(app).routes[0]
    assert route.metadata_values == {"visibility.public": {"enabled": True, "secret": "***"}}
    assert "private-value" not in describe_app(app).to_json()
    assert "do-not-publish" not in describe_app(app, include_metadata_values=True).to_json()
