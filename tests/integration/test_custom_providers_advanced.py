"""Advanced integration tests for custom DI providers.

Complements ``test_custom_providers_integration.py`` with scenarios not
covered by the existing suite:

- Multi-binding with mixed provider types (use_value + use_class + use_factory)
- Protocol substitution via use_class(provide=Protocol, ...)
- Scope-violation detection at startup (SINGLETON consuming REQUEST)
- Async factory with injected dependencies
- use_existing alias chains to string tokens and factory results
- Global provider overrides and TRANSIENT global factories
- @post_construct on the ``use=`` class inside use_class
- Token(unique=False) identity sharing across provider lists
- use_factory returning None is a valid value
- use_class resolving Inject-annotated class-body fields on the ``use=`` class
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Optional, Protocol, runtime_checkable

import pytest

from lauren import (
    Inject,
    LaurenFactory,
    Scope,
    Token,
    controller,
    get,
    injectable,
    module,
    post_construct,
    use_class,
    use_existing,
    use_factory,
    use_value,
)
from lauren.exceptions import DIScopeViolationError, MissingProviderError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Module-level Protocol types used by TestMultiBinding.
#
# These must be at module level because ``from __future__ import annotations``
# makes ALL annotations lazy strings.  When LaurenFactory.create() runs
# inside a test function and calls get_type_hints(ProcService), Python
# evaluates the annotation string against the MODULE's globals — not the
# test function's locals.  A locally-defined IProcessor would be invisible
# to get_type_hints and the DI container would skip the parameter entirely.
# ---------------------------------------------------------------------------


@runtime_checkable
class _IItem(Protocol):
    """Protocol for the two-use_value multi-binding test."""

    def value(self) -> str: ...


@runtime_checkable
class _IProcessor(Protocol):
    """Protocol for the mixed use_value/use_class/use_factory multi-binding test."""

    def process(self) -> str: ...


# ═══════════════════════════════════════════════════════════════════════════
# TestMultiBinding
# ═══════════════════════════════════════════════════════════════════════════


class TestMultiBinding:
    """Multi-binding with use_value, use_class, and use_factory together."""

    def test_two_use_value_multi_collected_as_list(self):
        """Two use_value providers with multi=True are collected into list[T].

        The correct multi-binding pattern:
          - register providers with ``provide=Protocol, multi=True``
          - inject via ``list[Protocol]`` in the service constructor
        Protocol type (_IItem) must be at module level for get_type_hints to work.
        """

        class AlphaItem:
            def value(self) -> str:
                return "alpha"

        class BetaItem:
            def value(self) -> str:
                return "beta"

        @injectable()
        class ItemService:
            def __init__(self, items: list[_IItem]) -> None:
                self.items = items

        @controller("/items")
        class ItemController:
            def __init__(self, svc: ItemService) -> None:
                self.svc = svc

            @get("/")
            async def index(self) -> dict:
                return {"items": [i.value() for i in self.svc.items]}

        @module(
            controllers=[ItemController],
            providers=[
                ItemService,
                use_value(provide=_IItem, value=AlphaItem(), multi=True),
                use_value(provide=_IItem, value=BetaItem(), multi=True),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/items/")
        assert r.status_code == 200
        assert set(r.json()["items"]) == {"alpha", "beta"}

    def test_mixed_use_value_use_class_use_factory_multi(self):
        """use_value + use_class + use_factory all with multi=True for same Protocol.

        All three provider types contribute to the same multi-binding.
        The service injects ``list[_IProcessor]`` and receives all three.
        Protocol type (_IProcessor) must be at module level for get_type_hints.
        """

        class ValueProcessor:
            def process(self) -> str:
                return "from-value"

        class ClassProcessor:
            def process(self) -> str:
                return "from-class"

        class FactoryProcessor:
            def process(self) -> str:
                return "from-factory"

        value_proc = ValueProcessor()

        @injectable()
        class ProcService:
            def __init__(self, procs: list[_IProcessor]) -> None:
                self.procs = procs

            def run_all(self) -> list[str]:
                return [p.process() for p in self.procs]

        @controller("/proc")
        class ProcController:
            def __init__(self, svc: ProcService) -> None:
                self.svc = svc

            @get("/")
            async def index(self) -> dict:
                return {"results": self.svc.run_all()}

        @module(
            controllers=[ProcController],
            providers=[
                ProcService,
                use_value(provide=_IProcessor, value=value_proc, multi=True),
                use_class(provide=_IProcessor, use=ClassProcessor, multi=True),
                use_factory(
                    provide=_IProcessor,
                    factory=lambda: FactoryProcessor(),
                    multi=True,
                ),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/proc/")
        assert r.status_code == 200
        assert set(r.json()["results"]) == {
            "from-value",
            "from-class",
            "from-factory",
        }


# ═══════════════════════════════════════════════════════════════════════════
# TestUseClassProtocol
# ═══════════════════════════════════════════════════════════════════════════


class TestUseClassProtocol:
    """use_class where provide= is a runtime_checkable Protocol."""

    def test_use_class_provides_protocol_token(self):
        """A Protocol token can be the provide= target for use_class."""

        @runtime_checkable
        class IHasher(Protocol):
            def hash(self, data: str) -> str: ...

        class SHA256Hasher:
            def hash(self, data: str) -> str:
                return f"sha256:{data}"

        @injectable()
        class HashService:
            def __init__(self, hasher: IHasher) -> None:
                self.hasher = hasher

            def compute(self, v: str) -> str:
                return self.hasher.hash(v)

        @controller("/hash")
        class HashController:
            def __init__(self, svc: HashService) -> None:
                self.svc = svc

            @get("/")
            async def index(self) -> dict:
                return {"result": self.svc.compute("hello")}

        @module(
            controllers=[HashController],
            providers=[
                use_class(provide=IHasher, use=SHA256Hasher),
                HashService,
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/hash/")
        assert r.status_code == 200
        assert r.json() == {"result": "sha256:hello"}

    def test_use_class_transient_scope_fresh_instance_per_resolve(self):
        """use_class with scope=TRANSIENT creates a new instance each time."""

        class Counter:
            _seq = 0

            def __init__(self) -> None:
                Counter._seq += 1
                self.id = Counter._seq

        Counter._seq = 0  # reset before test

        @controller("/ctr")
        class CtrController:
            @get("/")
            async def index(
                self,
                a: Annotated[Counter, Inject("CTR")],
                b: Annotated[Counter, Inject("CTR")],
            ) -> dict:
                return {"same": a is b, "a": a.id, "b": b.id}

        @module(
            controllers=[CtrController],
            providers=[use_class(provide="CTR", use=Counter, scope=Scope.TRANSIENT)],
        )
        class AppModule:
            pass

        Counter._seq = 0
        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/ctr/")
        assert r.status_code == 200
        body = r.json()
        # TRANSIENT: two injections in the same request → different instances
        assert body["same"] is False
        assert body["a"] != body["b"]


# ═══════════════════════════════════════════════════════════════════════════
# TestScopeViolations
# ═══════════════════════════════════════════════════════════════════════════


class TestScopeViolations:
    """Custom providers that depend on narrower-scope services fail at startup."""

    def test_singleton_use_class_with_request_dep_raises(self):
        """SINGLETON use_class whose use= class depends on a REQUEST service fails."""

        @injectable(scope=Scope.REQUEST)
        class RequestSvc2:
            pass

        class ConcreteX:
            def __init__(self, req: RequestSvc2) -> None:
                self.req = req

        @module(
            providers=[
                RequestSvc2,
                use_class(provide="X2", use=ConcreteX, scope=Scope.SINGLETON),
            ],
            controllers=[],
        )
        class BadModule:
            pass

        with pytest.raises(DIScopeViolationError):
            LaurenFactory.create(BadModule)

    def test_singleton_use_factory_with_request_dep_raises(self):
        """SINGLETON use_factory with a REQUEST-scoped injected dep fails."""

        @injectable(scope=Scope.REQUEST)
        class RequestSvc:
            value = "req"

        def make_x(req: RequestSvc) -> str:
            return req.value

        @controller("/x")
        class XController:
            @get("/")
            async def index(self, v: Annotated[str, Inject("X")]) -> dict:
                return {"v": v}

        @module(
            controllers=[XController],
            providers=[
                RequestSvc,
                use_factory(
                    provide="X",
                    factory=make_x,
                    injects=[RequestSvc],
                    scope=Scope.SINGLETON,
                ),
            ],
        )
        class AppModule:
            pass

        with pytest.raises(DIScopeViolationError):
            LaurenFactory.create(AppModule)

    def test_request_factory_with_transient_dep_also_raises(self):
        """REQUEST factory depending on a TRANSIENT service is also a scope violation.

        The scope narrowing rule applies in both directions: no wider scope
        (SINGLETON or REQUEST) may depend on a narrower scope (TRANSIENT or
        REQUEST respectively).  REQUEST → TRANSIENT is therefore rejected at
        startup just like SINGLETON → REQUEST.
        """

        @injectable(scope=Scope.TRANSIENT)
        class TransientSvc:
            tag = "transient"

        def make_ctx(svc: TransientSvc) -> dict:
            return {"tag": svc.tag}

        @controller("/ctx")
        class CtxController:
            @get("/")
            async def index(self, ctx: Annotated[dict, Inject("CTX")]) -> dict:
                return ctx

        @module(
            controllers=[CtxController],
            providers=[
                TransientSvc,
                use_factory(
                    provide="CTX",
                    factory=make_ctx,
                    injects=[TransientSvc],
                    scope=Scope.REQUEST,
                ),
            ],
        )
        class AppModule:
            pass

        with pytest.raises(DIScopeViolationError):
            LaurenFactory.create(AppModule)


# ═══════════════════════════════════════════════════════════════════════════
# TestAsyncFactoryWithDeps
# ═══════════════════════════════════════════════════════════════════════════


class TestAsyncFactoryWithDeps:
    """async def factories that receive DI-resolved dependencies."""

    def test_async_factory_with_injected_class_dep(self):
        """Async factory receives an @injectable dep; result reflects dep value."""

        @injectable()
        class OptionsService:
            host = "db.prod"

        async def build_connection(opts: OptionsService) -> dict:
            await asyncio.sleep(0)  # prove it's awaited
            return {"host": opts.host, "async": True}

        @controller("/conn")
        class ConnController:
            @get("/")
            async def index(self, conn: Annotated[dict, Inject("CONN")]) -> dict:
                return conn

        @module(
            controllers=[ConnController],
            providers=[
                OptionsService,
                use_factory(
                    provide="CONN",
                    factory=build_connection,
                    injects=[OptionsService],
                ),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/conn/")
        assert r.status_code == 200
        assert r.json() == {"host": "db.prod", "async": True}

    def test_async_factory_request_scope_fresh_per_request(self):
        """Async REQUEST-scoped factory produces different values per request."""

        counter = {"n": 0}

        async def make_ctx() -> dict:
            await asyncio.sleep(0)
            counter["n"] += 1
            return {"seq": counter["n"]}

        @controller("/c")
        class CController:
            @get("/")
            async def index(self, ctx: Annotated[dict, Inject("CTX")]) -> dict:
                return ctx

        @module(
            controllers=[CController],
            providers=[
                use_factory(
                    provide="CTX",
                    factory=make_ctx,
                    scope=Scope.REQUEST,
                ),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        a = client.get("/c/").json()
        b = client.get("/c/").json()
        assert a["seq"] == 1
        assert b["seq"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# TestAliasChains
# ═══════════════════════════════════════════════════════════════════════════


class TestAliasChains:
    """use_existing chaining to string tokens and factory-produced values."""

    def test_alias_to_string_token(self):
        """use_existing pointing to a string token resolves correctly."""
        ALIAS = Token("ALIAS_X")

        @controller("/v")
        class VController:
            @get("/")
            async def index(self, v: Annotated[int, Inject(ALIAS)]) -> dict:
                return {"v": v}

        @module(
            controllers=[VController],
            providers=[
                use_value(provide="REAL", value=99),
                use_existing(provide=ALIAS, existing="REAL"),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/v/")
        assert r.status_code == 200
        assert r.json() == {"v": 99}

    def test_alias_chain_to_factory_result(self):
        """A → B → factory: injecting A traverses the full chain."""

        @controller("/chain")
        class ChainController:
            @get("/")
            async def index(self, v: Annotated[str, Inject("ALIAS")]) -> dict:
                return {"v": v}

        @module(
            controllers=[ChainController],
            providers=[
                use_factory(provide="BASE", factory=lambda: "computed"),
                use_existing(provide="ALIAS", existing="BASE"),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/chain/")
        assert r.status_code == 200
        assert r.json() == {"v": "computed"}

    def test_alias_to_nonexistent_target_fails_at_startup(self):
        """use_existing pointing to an unregistered token fails at startup."""

        @controller("/x")
        class XController:
            @get("/")
            async def index(self, v: Annotated[str, Inject("X")]) -> dict:
                return {"v": v}

        @module(
            controllers=[XController],
            providers=[use_existing(provide="X", existing="DOES_NOT_EXIST")],
        )
        class AppModule:
            pass

        with pytest.raises(MissingProviderError):
            LaurenFactory.create(AppModule)


# ═══════════════════════════════════════════════════════════════════════════
# TestGlobalProviderOverrides
# ═══════════════════════════════════════════════════════════════════════════


class TestGlobalProviderOverrides:
    """Global providers passed to LaurenFactory.create override module-local ones."""

    def test_global_use_value_provides_to_all_modules(self):
        """A global use_value is visible in modules that don't declare it locally.

        Global providers are *additive* — they register a binding that every
        module can consume without listing it in its own ``providers`` list.
        They do NOT override module-local providers.
        """

        class GlobalConfig:
            env = "production"

        @controller("/cfg")
        class CfgController:
            def __init__(self, cfg: GlobalConfig) -> None:
                self.cfg = cfg

            @get("/")
            async def index(self) -> dict:
                return {"env": self.cfg.env}

        # Note: GlobalConfig is NOT listed in providers — it comes from global_providers
        @module(controllers=[CfgController], providers=[])
        class AppModule:
            pass

        test_config = GlobalConfig()
        test_config.env = "test"

        client = TestClient(
            LaurenFactory.create(
                AppModule,
                global_providers=[use_value(provide=GlobalConfig, value=test_config)],
            )
        )
        r = client.get("/cfg/")
        assert r.status_code == 200
        assert r.json() == {"env": "test"}

    def test_global_transient_factory_fresh_per_injection(self):
        """A global TRANSIENT use_factory produces a new value for each injection."""

        seq = {"n": 0}

        def next_id() -> int:
            seq["n"] += 1
            return seq["n"]

        @controller("/ids")
        class IdsController:
            @get("/")
            async def index(
                self,
                a: Annotated[int, Inject("ID")],
                b: Annotated[int, Inject("ID")],
            ) -> dict:
                return {"a": a, "b": b, "same": a == b}

        @module(controllers=[IdsController], providers=[])
        class AppModule:
            pass

        seq["n"] = 0
        client = TestClient(
            LaurenFactory.create(
                AppModule,
                global_providers=[
                    use_factory(
                        provide="ID",
                        factory=next_id,
                        scope=Scope.TRANSIENT,
                    )
                ],
            )
        )
        r = client.get("/ids/")
        assert r.status_code == 200
        body = r.json()
        assert body["same"] is False
        assert body["a"] != body["b"]


# ═══════════════════════════════════════════════════════════════════════════
# TestPostConstructWithUseClass
# ═══════════════════════════════════════════════════════════════════════════


class TestPostConstructWithUseClass:
    """@post_construct on the ``use=`` class is honoured by use_class."""

    def test_post_construct_called_on_use_class_instance(self):
        """Lifecycle hook fires when use_class builds the concrete class."""

        class IService(Protocol):
            def status(self) -> str: ...

        class ConcreteService:
            ready: bool = False

            @post_construct
            async def setup(self) -> None:
                self.ready = True

            def status(self) -> str:
                return "ready" if self.ready else "not-ready"

        @controller("/svc")
        class SvcController:
            def __init__(self, svc: IService) -> None:
                self.svc = svc

            @get("/")
            async def index(self) -> dict:
                return {"status": self.svc.status()}

        @module(
            controllers=[SvcController],
            providers=[use_class(provide=IService, use=ConcreteService)],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/svc/")
        assert r.status_code == 200
        assert r.json() == {"status": "ready"}


# ═══════════════════════════════════════════════════════════════════════════
# TestTokenSharing
# ═══════════════════════════════════════════════════════════════════════════


class TestTokenSharing:
    """Token(unique=False) identity semantics across provider lists."""

    def test_non_unique_tokens_same_name_resolve_same_value(self):
        """Two Token(unique=False) with the same name are the same key."""
        # Create two separate Token objects with the same name — they must
        # be equal and resolve to the same registered value.
        tok_a = Token("SHARED_KEY", unique=False)
        tok_b = Token("SHARED_KEY", unique=False)

        assert tok_a == tok_b  # sanity: same name = same key

        @controller("/s")
        class SController:
            @get("/")
            async def index(self, v: Annotated[str, Inject(tok_b)]) -> dict:
                return {"v": v}

        @module(
            controllers=[SController],
            # Register with tok_a, inject via tok_b — must resolve
            providers=[use_value(provide=tok_a, value="shared!")],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/s/")
        assert r.status_code == 200
        assert r.json() == {"v": "shared!"}

    def test_unique_tokens_same_name_are_distinct_keys(self):
        """Two default Token("X") instances are different keys and cannot substitute.

        Since the tokens are distinct, tok2 has no registered provider and
        LaurenFactory.create must fail at startup (MissingProviderError when
        the DI container compiles, or UnresolvableParameterError when the
        ASGI layer compiles the handler signature — both are StartupErrors).
        """
        tok1 = Token("UNIQUE_KEY")  # unique=True by default
        tok2 = Token("UNIQUE_KEY")  # different identity

        assert tok1 != tok2  # sanity: unique tokens are distinct

        # Inject via service constructor so the DI layer (not the ASGI extractor)
        # handles the missing token — producing a clean MissingProviderError.
        @injectable()
        class UsesTok2:
            def __init__(self, v: Annotated[str, Inject(tok2)]) -> None:
                self.v = v

        @controller("/u")
        class UController:
            def __init__(self, svc: UsesTok2) -> None:
                self.svc = svc

            @get("/")
            async def index(self) -> dict:
                return {"v": self.svc.v}

        @module(
            controllers=[UController],
            # Register tok1, but inject tok2 — they are different; must fail
            providers=[
                UsesTok2,
                use_value(provide=tok1, value="from-tok1"),
            ],
        )
        class AppModule:
            pass

        with pytest.raises(MissingProviderError):
            LaurenFactory.create(AppModule)


# ═══════════════════════════════════════════════════════════════════════════
# TestFactoryNoneValue
# ═══════════════════════════════════════════════════════════════════════════


class TestFactoryNoneValue:
    """use_factory / use_value returning None is a valid resolved value."""

    def test_factory_returning_none_is_valid(self):
        """A factory that returns None does not raise; None is injected."""

        @controller("/n")
        class NController:
            @get("/")
            async def index(
                self,
                v: Annotated[Optional[str], Inject("MAYBE")],
            ) -> dict:
                return {"value": v}

        @module(
            controllers=[NController],
            providers=[use_factory(provide="MAYBE", factory=lambda: None)],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/n/")
        assert r.status_code == 200
        assert r.json() == {"value": None}

    def test_use_value_none_is_valid(self):
        """use_value(value=None) is legal and None is injected."""

        @controller("/nv")
        class NVController:
            @get("/")
            async def index(
                self,
                v: Annotated[Optional[str], Inject("NULL_VAL")],
            ) -> dict:
                return {"value": v}

        @module(
            controllers=[NVController],
            providers=[use_value(provide="NULL_VAL", value=None)],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/nv/")
        assert r.status_code == 200
        assert r.json() == {"value": None}


# ═══════════════════════════════════════════════════════════════════════════
# TestUseClassInjectField
# ═══════════════════════════════════════════════════════════════════════════


class TestUseClassInjectField:
    """use_class resolves Inject-annotated class-body fields on the ``use=`` class."""

    def test_inject_field_on_use_class_resolved(self):
        """Class-body ``Annotated[X, Inject("TOKEN")]`` fields are wired by use_class."""

        class DbClient:
            def query(self) -> str:
                return "rows"

        class ConcreteRepo:
            db: Annotated[DbClient, Inject("DB")]

            def fetch(self) -> str:
                return self.db.query()

        class IRepo(Protocol):
            def fetch(self) -> str: ...

        @controller("/repo")
        class RepoController:
            def __init__(self, repo: IRepo) -> None:
                self.repo = repo

            @get("/")
            async def index(self) -> dict:
                return {"data": self.repo.fetch()}

        mock_db = DbClient()

        @module(
            controllers=[RepoController],
            providers=[
                use_value(provide="DB", value=mock_db),
                use_class(provide=IRepo, use=ConcreteRepo),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/repo/")
        assert r.status_code == 200
        assert r.json() == {"data": "rows"}
