# NOTE: ``from __future__ import annotations`` is intentionally omitted.
# This file tests explicitly-quoted string annotations such as
# ``sess: "Depends[get_async_db_sess]"``.  Under PEP 563 every annotation
# becomes a string automatically, making the two forms indistinguishable at
# runtime.  Without the future import, ``attr: SomeType`` is evaluated
# eagerly while ``attr: "SomeType"`` stores a raw string — only by omitting
# the import can we exercise the string-eval path in ``_safe_class_hints``
# via an explicit quote.
"""Tests that class-body annotations written as explicit string literals
(``attr: "Depends[factory]"``) are resolved correctly for dependency
injection.

Covers both module-level and method-local provider definitions so that
both the fast-path (module globals) and the frame-stack fallback inside
``_safe_class_hints`` are exercised.
"""

import pytest

from lauren import Depends, Scope, controller, get, injectable, module
from lauren._di import DIContainer
from lauren.testing import TestClient
from lauren import LaurenFactory


# ---------------------------------------------------------------------------
# Module-level providers (fast-path: resolved via module globals)
# ---------------------------------------------------------------------------


@injectable()
def get_async_db_sess() -> str:
    """Simulates a database session factory."""
    return "async-db-session"


@injectable()
async def get_token() -> str:
    """Simulates an async token factory."""
    return "bearer-token"


@injectable()
def get_base_value() -> int:
    return 10


@injectable()
def get_doubled(base: "Depends[get_base_value]") -> int:  # type: ignore[valid-type]
    return base * 2


# ---------------------------------------------------------------------------
# Unit tests — DIContainer in isolation
# ---------------------------------------------------------------------------


class TestExplicitStringAnnotationDepends:
    @pytest.mark.asyncio
    async def test_string_annotation_resolved_from_module_globals(self):
        """``attr: "Depends[fn]"`` where ``fn`` is at module level should
        resolve via the module-globals fast path."""

        @injectable()
        class Service:
            sess: "Depends[get_async_db_sess]"  # type: ignore[assignment]

        c = DIContainer()
        c.register(get_async_db_sess)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert svc.sess == "async-db-session"

    @pytest.mark.asyncio
    async def test_string_annotation_async_factory(self):
        """An async factory referenced via a string annotation is awaited
        and its result injected."""

        @injectable()
        class Service:
            tok: "Depends[get_token]"  # type: ignore[assignment]

        c = DIContainer()
        c.register(get_token)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert svc.tok == "bearer-token"

    @pytest.mark.asyncio
    async def test_string_annotation_resolved_from_local_scope(self):
        """``attr: "Depends[fn]"`` where ``fn`` is a method-local name
        should resolve via the frame-stack fallback in ``_safe_class_hints``.
        """

        @injectable()
        def local_factory() -> str:
            return "local-value"

        @injectable()
        class Service:
            val: "Depends[local_factory]"  # type: ignore[assignment]

        c = DIContainer()
        c.register(local_factory)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert svc.val == "local-value"

    @pytest.mark.asyncio
    async def test_mixed_quoted_and_unquoted_annotations(self):
        """A class may mix ``attr: Type`` (unquoted, eagerly evaluated) with
        ``attr: "Depends[fn]"`` (quoted string); both should be injected."""

        @injectable()
        class Config:
            def __init__(self) -> None:
                self.name = "cfg"

        @injectable()
        class Service:
            cfg: Config  # unquoted — eagerly evaluated at class-definition time
            sess: "Depends[get_async_db_sess]"  # type: ignore[assignment]

        c = DIContainer()
        c.register(get_async_db_sess)
        c.register(Config)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert isinstance(svc.cfg, Config)
        assert svc.sess == "async-db-session"

    @pytest.mark.asyncio
    async def test_chained_string_annotation_depends(self):
        """String annotations resolve correctly through a chain of
        function providers (``Depends[a]`` → ``Depends[b]`` → …)."""

        @injectable()
        class Service:
            doubled: "Depends[get_doubled]"  # type: ignore[assignment]

        c = DIContainer()
        c.register(get_base_value)
        c.register(get_doubled)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert svc.doubled == 20

    @pytest.mark.asyncio
    async def test_transient_scope_with_string_annotation(self):
        """Each ``TRANSIENT`` resolution produces a distinct instance even
        when the field is declared with an explicit string annotation."""

        @injectable(scope=Scope.TRANSIENT)
        class Holder:
            sess: "Depends[get_async_db_sess]"  # type: ignore[assignment]

        c = DIContainer()
        c.register(get_async_db_sess)
        c.register(Holder)
        c.compile()
        a = await c.resolve(Holder)
        b = await c.resolve(Holder)
        assert a is not b
        assert a.sess == b.sess == "async-db-session"


# ---------------------------------------------------------------------------
# Integration tests — full LaurenApp via TestClient
# ---------------------------------------------------------------------------


class TestStringAnnotationDependsIntegration:
    def test_controller_field_with_string_depends_annotation(self):
        """End-to-end: a controller field declared as ``attr: "Depends[fn]"``
        is injected and the value is visible in handler responses."""

        @injectable()
        def make_conn() -> str:
            return "pg://localhost/mydb"

        @controller("/api")
        class Api:
            conn: "Depends[make_conn]"  # type: ignore[assignment]

            @get("/conn")
            async def show(self) -> dict:
                return {"conn": self.conn}

        @module(controllers=[Api], providers=[make_conn])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/api/conn")
        assert r.status_code == 200
        assert r.json() == {"conn": "pg://localhost/mydb"}

    def test_async_factory_with_string_depends_annotation(self):
        """An async factory referenced by a string annotation is awaited;
        the controller receives the resolved value."""

        @injectable()
        async def fetch_config() -> str:
            return "config-loaded"

        @controller("/cfg")
        class CfgCtrl:
            cfg: "Depends[fetch_config]"  # type: ignore[assignment]

            @get("/")
            async def show(self) -> dict:
                return {"cfg": self.cfg}

        @module(controllers=[CfgCtrl], providers=[fetch_config])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/cfg/")
        assert r.status_code == 200
        assert r.json() == {"cfg": "config-loaded"}

    def test_service_field_with_string_depends_annotation(self):
        """An ``@injectable()`` service class may also use string-annotation
        ``Depends`` fields; the resolved value propagates to controllers that
        depend on the service."""

        @injectable()
        def db_url() -> str:
            return "sqlite://:memory:"

        @injectable()
        class Repo:
            url: "Depends[db_url]"  # type: ignore[assignment]

            def find_all(self) -> list:
                return [self.url]

        @controller("/items")
        class ItemCtrl:
            repo: Repo

            @get("/")
            async def index(self) -> dict:
                return {"items": self.repo.find_all()}

        @module(controllers=[ItemCtrl], providers=[db_url, Repo])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/items/")
        assert r.status_code == 200
        assert r.json() == {"items": ["sqlite://:memory:"]}
