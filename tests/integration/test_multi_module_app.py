"""End-to-end verification that a multi-module application composes correctly.

Exercises the scenario most real apps hit:

* A ``CoreModule`` that owns cross-cutting singletons (Config, Clock) and
  exports them.
* Two feature modules (``UserModule``, ``OrderModule``) each with their own
  providers, controllers, and private helpers that must stay private.
* An ``AuthModule`` that ``OrderModule`` imports but ``UserModule`` doesn't.
* A root ``AppModule`` that wires everything together.

The test asserts: correct cross-module resolution, strict module
encapsulation (private providers stay private), shared singleton identity
across modules, per-request controller construction / destruction, route
collection, and end-to-end HTTP behaviour.
"""

# No ``from __future__ import annotations`` \u2014 classes are declared at
# module scope here, so ``get_type_hints`` needs live references.

from dataclasses import dataclass

import pytest

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
    post_construct,
    pre_destruct,
)
from lauren.exceptions import MissingProviderError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Core module \u2014 cross-cutting singletons.
# ---------------------------------------------------------------------------


@injectable()
class Config:
    def __init__(self):
        self.env = "test"
        self.max_items = 25


@injectable()
class Clock:
    def __init__(self):
        self.calls = 0

    def now(self) -> int:
        self.calls += 1
        return self.calls


@module(providers=[Config, Clock], exports=[Config, Clock])
class CoreModule:
    pass


# ---------------------------------------------------------------------------
# Auth module \u2014 exports Authenticator but keeps TokenStore private.
# ---------------------------------------------------------------------------


@injectable()
class TokenStore:
    def __init__(self):
        self.tokens = {"admin-token": "admin", "user-token": "alice"}

    def resolve(self, token: str) -> str | None:
        return self.tokens.get(token)


@injectable()
class Authenticator:
    def __init__(self, store: TokenStore):
        self.store = store

    def login(self, token: str) -> str | None:
        return self.store.resolve(token)


@module(providers=[TokenStore, Authenticator], exports=[Authenticator])
class AuthModule:
    pass


# ---------------------------------------------------------------------------
# User module \u2014 depends on Config (from Core), does NOT use Auth.
# ---------------------------------------------------------------------------


@injectable()
class UserRepo:
    def __init__(self, config: Config):
        self.config = config
        self.users = {1: "alice", 2: "bob"}

    def get(self, uid: int) -> str | None:
        return self.users.get(uid)


@controller("/users", tags=["users"])
@injectable(scope=Scope.REQUEST)
class UserController:
    def __init__(self, repo: UserRepo, clock: Clock):
        self.repo = repo
        self.clock = clock
        self.constructed_at = -1

    @post_construct
    def _init(self) -> None:
        self.constructed_at = self.clock.now()

    @get("/{uid}")
    async def show(self, uid: Path[int]) -> dict:
        return {"uid": uid, "name": self.repo.get(uid), "at": self.constructed_at}


@module(
    imports=[CoreModule],
    providers=[UserRepo],
    controllers=[UserController],
    exports=[UserRepo],  # exported so OrderModule can consume it.
)
class UserModule:
    pass


# ---------------------------------------------------------------------------
# Order module \u2014 depends on Core, Auth, and (re-exported) UserRepo.
# ---------------------------------------------------------------------------


@dataclass
class Order:
    id: int
    owner: str


@injectable()
class OrderRepo:
    def __init__(self, users: UserRepo):
        self.users = users
        self.orders: dict[int, Order] = {}
        self._next_id = 1

    def create(self, owner_id: int) -> Order | None:
        name = self.users.get(owner_id)
        if name is None:
            return None
        o = Order(id=self._next_id, owner=name)
        self.orders[o.id] = o
        self._next_id += 1
        return o


@controller("/orders", tags=["orders"])
@injectable(scope=Scope.REQUEST)
class OrderController:
    destroyed: list[int] = []

    def __init__(self, repo: OrderRepo, auth: Authenticator):
        self.repo = repo
        self.auth = auth

    @pre_destruct
    def _done(self) -> None:
        OrderController.destroyed.append(id(self))

    @post("/{uid}")
    async def create(self, uid: Path[int]) -> dict:
        order = self.repo.create(uid)
        if order is None:
            return {"error": "user not found"}, 404
        return {"id": order.id, "owner": order.owner}

    @get("/lookup/{token}")
    async def lookup(self, token: Path[str]) -> dict:
        who = self.auth.login(token)
        return {"who": who}


@module(
    imports=[CoreModule, AuthModule, UserModule],
    providers=[OrderRepo],
    controllers=[OrderController],
)
class OrderModule:
    pass


# ---------------------------------------------------------------------------
# Root module.
# ---------------------------------------------------------------------------


@module(imports=[UserModule, OrderModule])
class AppModule:
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMultiModuleApp:
    @pytest.mark.asyncio
    async def test_all_routes_are_registered(self):
        app = LaurenFactory.create(AppModule)
        paths = {(r.method, r.path_template) for r in app.routes()}
        assert ("GET", "/users/{uid}") in paths
        assert ("POST", "/orders/{uid}") in paths
        assert ("GET", "/orders/lookup/{token}") in paths

    @pytest.mark.asyncio
    async def test_user_endpoint_resolves_cross_module_deps(self):
        app = LaurenFactory.create(AppModule)
        client = TestClient(app)
        r = client.get("/users/1")
        assert r.status_code == 200
        body = r.json()
        assert body["uid"] == 1 and body["name"] == "alice"
        # clock.now() was called in UserController.@post_construct
        assert body["at"] >= 1

    @pytest.mark.asyncio
    async def test_order_creation_traverses_three_modules(self):
        """OrderController -> OrderRepo -> UserRepo (from UserModule) ->
        Config (from CoreModule). Every hop crosses a module boundary."""
        app = LaurenFactory.create(AppModule)
        r = TestClient(app).post("/orders/2")  # user 2 = "bob"
        assert r.status_code == 200
        data = r.json()
        assert data["owner"] == "bob"
        assert data["id"] == 1

    @pytest.mark.asyncio
    async def test_authenticator_reachable_via_exported_module(self):
        app = LaurenFactory.create(AppModule)
        r = TestClient(app).get("/orders/lookup/admin-token")
        assert r.status_code == 200
        assert r.json() == {"who": "admin"}

    @pytest.mark.asyncio
    async def test_clock_singleton_shared_across_modules(self):
        """A singleton declared in CoreModule must be the same instance
        for every consumer, regardless of which module they live in."""
        app = LaurenFactory.create(AppModule)
        client = TestClient(app)
        # UserController uses Clock via @post_construct; each request builds
        # a fresh controller so clock.calls increments monotonically.
        a = client.get("/users/1").json()["at"]
        b = client.get("/users/2").json()["at"]
        assert b == a + 1  # same Clock singleton

    @pytest.mark.asyncio
    async def test_private_provider_is_not_reachable_from_other_module(self):
        """TokenStore is declared in AuthModule and NOT exported. A
        controller in a different module cannot depend on it."""

        @controller("/leak")
        class Leaker:
            def __init__(self, store: TokenStore):
                self.store = store

            @get("/")
            async def root(self) -> dict:
                return {}

        @module(controllers=[Leaker], imports=[AuthModule])
        class LeakMod:
            pass

        @module(imports=[LeakMod])
        class BadRoot:
            pass

        with pytest.raises(MissingProviderError) as ei:
            LaurenFactory.create(BadRoot)
        assert "TokenStore" in str(ei.value)
        assert "visible from module LeakMod" in str(ei.value)

    @pytest.mark.asyncio
    async def test_controller_lifecycle_fires_per_request(self):
        app = LaurenFactory.create(AppModule)
        OrderController.destroyed.clear()
        client = TestClient(app)
        client.post("/orders/1")
        client.post("/orders/2")
        client.post("/orders/1")
        # @pre_destruct fires once per request.
        assert len(OrderController.destroyed) == 3

    @pytest.mark.asyncio
    async def test_openapi_aggregates_all_modules(self):
        app = LaurenFactory.create(AppModule, openapi_url="/openapi.json")
        schema = app.openapi()
        assert "/users/{uid}" in schema["paths"]
        assert "/orders/{uid}" in schema["paths"]
        assert "/orders/lookup/{token}" in schema["paths"]
        tag_names = {t["name"] for t in schema.get("tags", [])}
        assert {"users", "orders"} <= tag_names

    @pytest.mark.asyncio
    async def test_depends_injection_at_endpoint_crosses_modules(self):
        """``Depends[X]`` on an endpoint in module A pulls X from module B
        as long as B exports X and A imports B."""

        @controller("/greet")
        class Greeter:
            @get("/{uid}")
            async def h(
                self,
                uid: Path[int],
                repo: Depends[UserRepo],
            ) -> dict:
                return {"who": repo.get(uid)}

        @module(controllers=[Greeter], imports=[UserModule])
        class GreetMod:
            pass

        @module(imports=[GreetMod])
        class R:
            pass

        app = LaurenFactory.create(R)
        r = TestClient(app).get("/greet/1")
        assert r.json() == {"who": "alice"}
