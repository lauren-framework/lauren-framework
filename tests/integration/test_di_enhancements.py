"""Integration tests for the three DI / controller enhancements.

Drives real :class:`LaurenApp` instances end-to-end to verify:

1. **Field-annotation injection** — a controller / service declares its
   dependencies on the class body; the framework resolves them and
   sets them as attributes before ``__init__`` runs.

2. **Function injectables** — ``@injectable()`` on a function creates
   a factory provider whose return value is the dependency; consumers
   depend on it via ``Depends[fn]``.

3. **Static/classmethod routes** — ``@get`` / ``@post`` stacked with
   ``@staticmethod`` or ``@classmethod`` register and dispatch
   correctly in either decorator order.

The combined-features tests prove that a controller can mix all three
styles in one class, which is the real-world shape a migrating
codebase ends up with.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from lauren import (
    Depends,
    LaurenFactory,
    Path,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Feature 1 — field-annotation injection
# ---------------------------------------------------------------------------


class TestFieldAnnotationInjection:
    def test_controller_receives_field_annotated_dep(self):
        @injectable()
        class Config:
            def __init__(self) -> None:
                self.name = "app-1"

        @controller("/api")
        class Api:
            cfg: Config

            @get("/name")
            async def name(self) -> dict:
                return {"name": self.cfg.name}

        @module(controllers=[Api], providers=[Config])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).get("/api/name")
        assert r.status_code == 200
        assert r.json() == {"name": "app-1"}

    def test_field_is_set_after_construction(self):
        # New contract: class-body-annotated DI fields land on the
        # instance AFTER ``cls(**kwargs)`` returns. ``__init__`` cannot
        # read them; if it needs the value, take it as a parameter.
        @injectable()
        class Config:
            def __init__(self) -> None:
                self.greeting = "hi"

        @controller("/api")
        class Api:
            cfg: Config  # injected post-construction

            @get("/prepared")
            async def prepared(self) -> dict:
                # By the time a route fires the field is in place.
                return {"v": self.cfg.greeting.upper()}

        @module(controllers=[Api], providers=[Config])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).get("/api/prepared")
        assert r.json() == {"v": "HI"}

    def test_mixed_field_and_init_param(self):
        @injectable()
        class A:
            def __init__(self) -> None:
                self.tag = "A"

        @injectable()
        class B:
            def __init__(self) -> None:
                self.tag = "B"

        @controller("/mix")
        class Api:
            a: A

            def __init__(self, b: B) -> None:
                self.b = b

            @get("/")
            async def show(self) -> dict:
                return {"a": self.a.tag, "b": self.b.tag}

        @module(controllers=[Api], providers=[A, B])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).get("/mix/")
        assert r.json() == {"a": "A", "b": "B"}


# ---------------------------------------------------------------------------
# Feature 2 — function injectables
# ---------------------------------------------------------------------------


class TestFunctionInjectable:
    def test_function_factory_used_by_controller(self):
        @injectable()
        class Config:
            def __init__(self) -> None:
                self.host = "db.example.com"

        @injectable()
        def make_session(cfg: Config) -> str:
            # Stand-in for a real async_sessionmaker; the point is
            # that the function body freely consumes injected args.
            return f"AsyncSess({cfg.host})"

        @controller("/api")
        class Api:
            sess: Depends[make_session]

            @get("/sess")
            async def show(self) -> dict:
                return {"sess": self.sess}

        @module(controllers=[Api], providers=[Config, make_session])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).get("/api/sess")
        assert r.json() == {"sess": "AsyncSess(db.example.com)"}

    def test_async_function_factory_awaited(self):
        @injectable()
        async def make_token() -> str:
            return "ASYNC-OK"

        @controller("/api")
        class Api:
            tok: Depends[make_token]

            @get("/t")
            async def show(self) -> dict:
                return {"t": self.tok}

        @module(controllers=[Api], providers=[make_token])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).get("/api/t")
        assert r.json() == {"t": "ASYNC-OK"}

    def test_chained_function_providers(self):
        @injectable()
        def a() -> int:
            return 1

        @injectable()
        def b(x: Depends[a]) -> int:
            return x + 1

        @injectable()
        def c(y: Depends[b]) -> int:
            return y + 1

        @controller("/api")
        class Api:
            n: Depends[c]

            @get("/n")
            async def show(self) -> dict:
                return {"n": self.n}

        @module(controllers=[Api], providers=[a, b, c])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).get("/api/n")
        assert r.json() == {"n": 3}

    def test_request_scope_function_provider(self):
        # A request-scoped function factory produces a fresh value per
        # request. Two controllers in the same request see the same
        # instance; across requests they differ.
        counter = {"n": 0}

        @injectable(scope=Scope.REQUEST)
        def request_id() -> int:
            counter["n"] += 1
            return counter["n"]

        @controller("/api")
        class Api:
            rid_a: Depends[request_id]

            @get("/show")
            async def show(
                self,
                rid_b: Depends[request_id],
            ) -> dict:
                # Field injection and Depends in the handler \u2014 both
                # hit the same request-scoped instance.
                return {"a": self.rid_a, "b": rid_b}

        @module(controllers=[Api], providers=[request_id])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        client = TestClient(app)
        r1 = client.get("/api/show").json()
        r2 = client.get("/api/show").json()
        # Same request \u2192 same value.
        assert r1["a"] == r1["b"]
        assert r2["a"] == r2["b"]
        # Different requests \u2192 different values.
        assert r1["a"] != r2["a"]


# ---------------------------------------------------------------------------
# Feature 3 — staticmethod / classmethod routes
# ---------------------------------------------------------------------------


class TestStaticAndClassmethodRoutes:
    def test_static_route_no_receiver(self):
        @controller("/api")
        class Api:
            @get("/s")
            @staticmethod
            async def s() -> dict:
                return {"kind": "static"}

        @module(controllers=[Api])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).get("/api/s")
        assert r.json() == {"kind": "static"}

    def test_classmethod_receives_cls(self):
        @controller("/api")
        class Api:
            @get("/c")
            @classmethod
            async def c(cls) -> dict:
                return {"kind": "classmethod", "cls": cls.__name__}

        @module(controllers=[Api])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).get("/api/c")
        assert r.json() == {"kind": "classmethod", "cls": "Api"}

    def test_decorator_order_above_or_below(self):
        @controller("/api")
        class Api:
            # @get above @staticmethod \u2014 marker on descriptor
            @get("/above")
            @staticmethod
            async def above() -> dict:
                return {"order": "above"}

            # @staticmethod above @get \u2014 marker on inner function
            @staticmethod
            @get("/below")
            async def below() -> dict:
                return {"order": "below"}

        @module(controllers=[Api])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        client = TestClient(app)
        assert client.get("/api/above").json() == {"order": "above"}
        assert client.get("/api/below").json() == {"order": "below"}

    def test_static_route_with_path_param(self):
        @controller("/api")
        class Api:
            @get("/items/{item_id}")
            @staticmethod
            async def get_item(item_id: Path[int]) -> dict:
                return {"id": item_id, "doubled": item_id * 2}

        @module(controllers=[Api])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).get("/api/items/21")
        assert r.json() == {"id": 21, "doubled": 42}

    def test_classmethod_with_json_body(self):
        from lauren import Json

        class Payload(BaseModel):
            x: int

        @controller("/api")
        class Api:
            @post("/echo")
            @classmethod
            async def echo(cls, body: Json[Payload]) -> dict:
                # Classmethod handler receives ``cls`` as the first arg
                # and the validated Pydantic body as the declared
                # extractor — both DI paths coexist.
                return {"x": body.x, "cls": cls.__name__}

        @module(controllers=[Api])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).post("/api/echo", json={"x": 7})
        assert r.json() == {"x": 7, "cls": "Api"}


# ---------------------------------------------------------------------------
# Combined scenarios — the prompt's canonical UserRepo example
# ---------------------------------------------------------------------------


class TestCombinedFeatures:
    def test_userrepo_pattern_from_prompt(self):
        # The exact shape the prompt called out: a function provider
        # stands in for a sessionmaker, and a class depends on it via
        # a field-level Depends[fn].
        @injectable()
        class ConfigService:
            def __init__(self) -> None:
                self.db_url = "sqlite://:memory:"

        @injectable()
        def async_sessionmaker(cfg: ConfigService) -> str:
            return f"AsyncSessionmaker({cfg.db_url})"

        @injectable()
        class UserRepo:
            async_sess_mkr: Depends[async_sessionmaker]
            cfg: ConfigService

            def get_one_user(self, user_id: int) -> dict:
                return {
                    "user_id": user_id,
                    "sess": self.async_sess_mkr,
                    "db": self.cfg.db_url,
                }

        @controller("/users")
        class UserController:
            repo: UserRepo

            @get("/{user_id}")
            @staticmethod
            async def ping() -> dict:
                # Staticmethod route alongside field-injected deps on the
                # controller class \u2014 all three features in one shape.
                return {"ok": True}

            @get("/{user_id}/full")
            async def full(self, user_id: Path[int]) -> dict:
                return self.repo.get_one_user(user_id)

        @module(
            controllers=[UserController],
            providers=[ConfigService, async_sessionmaker, UserRepo],
        )
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        client = TestClient(app)

        # Static route works.
        assert client.get("/users/42").json() == {"ok": True}

        # Field-injected function provider flows through two layers.
        full = client.get("/users/42/full").json()
        assert full == {
            "user_id": 42,
            "sess": "AsyncSessionmaker(sqlite://:memory:)",
            "db": "sqlite://:memory:",
        }

    def test_user_supplied_new_plus_fields(self):
        @injectable()
        class Config:
            def __init__(self) -> None:
                self.tag = "tag-1"

        @injectable()
        class Service:
            # Field-level injection (post-construction).
            cfg: Config

            def __new__(cls, cfg: Config):
                # ``__new__`` receives DI kwargs through Python's
                # normal call protocol — ``cls(**kwargs)`` reaches
                # ``__new__`` first with the same dict.
                inst = super().__new__(cls)
                inst.from_new = f"new:{cfg.tag}"
                return inst

            def __init__(self, cfg: Config) -> None:
                # ``__init__`` takes the resolved cfg as a parameter.
                # The class-body-annotated ``cfg`` field is NOT yet on
                # ``self`` at this point — it lands after this
                # ``__init__`` returns.
                self.from_init = f"init:{cfg.tag}"

        @controller("/api")
        class Api:
            svc: Service

            @get("/")
            async def show(self) -> dict:
                return {
                    "from_new": self.svc.from_new,
                    "from_init": self.svc.from_init,
                }

        @module(controllers=[Api], providers=[Config, Service])
        class M:
            pass

        app = asyncio.run(LaurenFactory.create(M))
        r = TestClient(app).get("/api/")
        assert r.json() == {
            "from_new": "new:tag-1",
            "from_init": "init:tag-1",
        }
