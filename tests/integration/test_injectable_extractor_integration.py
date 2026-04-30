"""Integration tests for injectable instance-method extractors.

Verifies the end-to-end path:

* ``@injectable`` extractor resolved from the DI container.
* Constructor dependencies injected automatically — no manual
  ``container.resolve()`` calls inside ``extract()``.
* Existing ``@classmethod`` extractors continue to work unchanged.
* A non-injectable instance-method extractor raises ``StartupError``
  at factory time, not at request time.
* Pipes on injectable extractors still run.
"""

from __future__ import annotations

import pytest

from lauren import (
    Json,
    LaurenFactory,
    Response,
    Scope,
    controller,
    get,
    injectable,
    module,
    pipe,
    post,
    post_construct,
)
from lauren.exceptions import UnauthorizedError
from lauren.extractors import Extraction, ExtractionMarker
from lauren.testing import TestClient
from lauren.types import ExecutionContext, Request


# ---------------------------------------------------------------------------
# Shared service that acts as a "token store" dependency
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class TokenStore:
    @post_construct
    async def init(self) -> None:
        self._valid: set[str] = {"secret-token"}

    def is_valid(self, token: str) -> bool:
        return token in self._valid

    def add(self, token: str) -> None:
        self._valid.add(token)


# ---------------------------------------------------------------------------
# Injectable extractor — uses TokenStore from __init__
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class BearerPrincipal(ExtractionMarker):
    """Validate an Authorization: Bearer <token> header via TokenStore."""

    source = "bearer_principal"

    def __init__(self, store: TokenStore) -> None:
        self._store = store

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> object:
        header = execution_context.request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise UnauthorizedError("missing or malformed Authorization header")
        token = header[len("Bearer ") :]
        if not self._store.is_valid(token):
            raise UnauthorizedError("invalid token")
        return token


# ---------------------------------------------------------------------------
# Classic classmethod extractor — backward-compat
# ---------------------------------------------------------------------------


class EchoExtractor(ExtractionMarker):
    source = "echo_marker"

    @classmethod
    async def extract(
        cls,
        request: Request,
        extraction: Extraction,
        *,
        container: object | None,
        request_cache: dict[type, object] | None,
    ) -> object:
        return f"echo:{extraction.name}"


# ---------------------------------------------------------------------------
# Controller + module
# ---------------------------------------------------------------------------


@controller("/secure")
class SecureController:
    @get("/")
    async def hello(self, principal: BearerPrincipal) -> Response:
        return Response.json({"token": principal})

    @get("/echo")
    async def echo(self, tag: EchoExtractor) -> Response:
        return Response.json({"tag": tag})

    @post("/register")
    async def register(self, body: Json[dict]) -> Response:
        return Response.json({"registered": body.get("token")})


@module(
    providers=[TokenStore, BearerPrincipal],
    controllers=[SecureController],
)
class SecureModule:
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInjectableExtractorEndToEnd:
    @pytest.fixture
    def client(self):
        app = LaurenFactory.create(SecureModule)
        return TestClient(app)

    def test_valid_token_admitted(self, client):
        r = client.get("/secure/", headers={"Authorization": "Bearer secret-token"})
        assert r.status_code == 200
        assert r.json()["token"] == "secret-token"

    def test_invalid_token_returns_401(self, client):
        r = client.get("/secure/", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_missing_header_returns_401(self, client):
        r = client.get("/secure/")
        assert r.status_code == 401

    def test_classmethod_extractor_still_works(self, client):
        r = client.get("/secure/echo")
        assert r.status_code == 200
        assert r.json()["tag"] == "echo:tag"

    def test_injectable_extractor_singleton_shared(self):
        """The singleton extractor instance shares state across requests."""
        app = LaurenFactory.create(SecureModule)
        client = TestClient(app)
        import asyncio

        store = asyncio.run(app.container.resolve(TokenStore))
        store.add("dynamic-token")

        r = client.get("/secure/", headers={"Authorization": "Bearer dynamic-token"})
        assert r.status_code == 200
        assert r.json()["token"] == "dynamic-token"


class TestInjectablePipeEndToEnd:
    """Injectable class-based pipes work with DI constructor deps."""

    @pytest.fixture
    def client(self):
        @injectable(scope=Scope.SINGLETON)
        class Multiplier:
            def __init__(self) -> None:
                self._factor = 3

            def transform(self, value: int) -> int:
                return value * self._factor

        @controller("/math")
        class MathController:
            @get("/{n}")
            async def triple(
                self,
                n: int = pipe(Multiplier),
            ) -> Response:
                return Response.json({"result": n})

        @module(
            providers=[Multiplier],
            controllers=[MathController],
        )
        class MathModule:
            pass

        app = LaurenFactory.create(MathModule)
        return TestClient(app)

    def test_injectable_pipe_triples_value(self, client):
        r = client.get("/math/7")
        assert r.status_code == 200
        assert r.json()["result"] == 21


class TestStartupValidation:
    """Startup validation: classmethod backward compat, injectable rules."""

    def test_non_injectable_instance_method_valid(self):
        """Non-injectable instance-method extractors start up and handle requests."""

        class SimpleExtractor(ExtractionMarker):
            source = "simple_ni"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return f"simple:{extraction.name}"

        @controller("/simple_ni")
        class SimpleController:
            @get("/")
            async def handler(self, x: SimpleExtractor) -> Response:
                return Response.json({"x": x})

        @module(controllers=[SimpleController])
        class SimpleModule:
            pass

        app = LaurenFactory.create(SimpleModule)
        r = TestClient(app).get("/simple_ni/")
        assert r.status_code == 200
        assert r.json()["x"] == "simple:x"

    def test_injectable_instance_method_valid(self):
        """@injectable + instance method is valid — no StartupError."""

        @injectable()
        class GoodExtractor(ExtractionMarker):
            source = "good_ext"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return "ok"

        @controller("/good")
        class GoodController:
            @get("/")
            async def handler(self, x: GoodExtractor) -> Response:
                return Response.json({"x": x})

        @module(providers=[GoodExtractor], controllers=[GoodController])
        class GoodModule:
            pass

        app = LaurenFactory.create(GoodModule)
        r = TestClient(app).get("/good/")
        assert r.status_code == 200
        assert r.json()["x"] == "ok"

    def test_classmethod_without_injectable_still_valid(self):
        """@classmethod extractors without @injectable remain valid (backward compat)."""

        class LegacyExtractor(ExtractionMarker):
            source = "legacy_ext"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "legacy"

        @controller("/legacy")
        class LegacyController:
            @get("/")
            async def handler(self, x: LegacyExtractor) -> Response:
                return Response.json({"x": x})

        @module(controllers=[LegacyController])
        class LegacyModule:
            pass

        app = LaurenFactory.create(LegacyModule)
        r = TestClient(app).get("/legacy/")
        assert r.status_code == 200
        assert r.json()["x"] == "legacy"

    # ------------------------------------------------------------------
    # E1 — child inherits instance method; parent has @injectable
    # With the new design, the child itself doesn't need @injectable —
    # it will be instantiated no-arg via the process-wide cache.
    # ------------------------------------------------------------------

    def test_inherited_instance_method_no_injectable_in_own_dict_valid(self):
        """Child inherits instance-method extract; no @injectable → no-arg cache."""

        @injectable()
        class Parent(ExtractionMarker):
            source = "e1"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return "from_parent"

        class Child(Parent):
            source = "e1"
            # @injectable only in Parent.__dict__ — Child uses no-arg cache

        @controller("/e1")
        class E1Controller:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({"x": x})

        @module(controllers=[E1Controller])
        class E1Module:
            pass

        app = LaurenFactory.create(E1Module)
        r = TestClient(app).get("/e1/")
        assert r.status_code == 200
        assert r.json()["x"] == "from_parent"

    def test_explicitly_injectable_child_with_inherited_extract_valid(self):
        """Child re-decorates with @injectable and inherits extract → DI-resolved."""

        @injectable()
        class Parent(ExtractionMarker):
            source = "e1_ok"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return "from_parent_im"

        @injectable()  # explicit re-decoration so DI resolves Child
        class Child(Parent):
            source = "e1_ok"

        @controller("/e1ok")
        class E1OkController:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({"x": x})

        @module(providers=[Child], controllers=[E1OkController])
        class E1OkModule:
            pass

        app = LaurenFactory.create(E1OkModule)
        r = TestClient(app).get("/e1ok/")
        assert r.status_code == 200
        assert r.json()["x"] == "from_parent_im"

    # ------------------------------------------------------------------
    # E2 — multi-level: child without @injectable → no-arg cache
    # ------------------------------------------------------------------

    def test_multi_level_inherited_instance_method_valid(self):
        """Multi-level inheritance: child without @injectable uses no-arg cache."""

        @injectable()
        class Grandparent(ExtractionMarker):
            source = "e2"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return "grandparent"

        class Parent(Grandparent):
            pass

        class Child(Parent):
            source = "e2"
            # No @injectable anywhere in own dict → no-arg cache

        @controller("/e2")
        class E2Controller:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({"x": x})

        @module(controllers=[E2Controller])
        class E2Module:
            pass

        app = LaurenFactory.create(E2Module)
        r = TestClient(app).get("/e2/")
        assert r.status_code == 200
        assert r.json()["x"] == "grandparent"

    def test_instance_method_no_injectable_valid(self):
        """Standalone extractor with no @injectable uses no-arg cache."""

        class Standalone(ExtractionMarker):
            source = "standalone_ni"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return "standalone_value"

        @controller("/sb_ni")
        class SBController:
            @get("/")
            async def handler(self, x: Standalone) -> Response:
                return Response.json({"x": x})

        @module(controllers=[SBController])
        class SBModule:
            pass

        app = LaurenFactory.create(SBModule)
        r = TestClient(app).get("/sb_ni/")
        assert r.status_code == 200
        assert r.json()["x"] == "standalone_value"

    # ------------------------------------------------------------------
    # Override: classmethod overridden by instance method — no @injectable → no-arg cache
    # ------------------------------------------------------------------

    def test_override_classmethod_with_instance_valid(self):
        class Parent(ExtractionMarker):
            source = "override_ni"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "parent_cm"

        class Child(Parent):
            source = "override_ni"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return "child_im"  # instance method, no @injectable → no-arg cache

        @controller("/override_ni")
        class OBController:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({"x": x})

        @module(controllers=[OBController])
        class OBModule:
            pass

        app = LaurenFactory.create(OBModule)
        r = TestClient(app).get("/override_ni/")
        assert r.status_code == 200
        assert r.json()["x"] == "child_im"

    # ------------------------------------------------------------------
    # Override: instance method overridden by classmethod → classmethod path
    # ------------------------------------------------------------------

    def test_override_instance_method_with_classmethod_no_startup_error(self):
        @injectable()
        class Parent(ExtractionMarker):
            source = "override_ok"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return "parent_im"

        class Child(Parent):
            source = "override_ok"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "child_cm"

        @controller("/override_ok")
        class OOController:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({"x": x})

        @module(controllers=[OOController])
        class OOModule:
            pass

        app = LaurenFactory.create(OOModule)
        r = TestClient(app).get("/override_ok/")
        assert r.status_code == 200
        assert r.json()["x"] == "child_cm"

    # ------------------------------------------------------------------
    # B1 — @staticmethod without @injectable → no StartupError
    # ------------------------------------------------------------------

    def test_staticmethod_without_injectable_no_startup_error(self):
        class StaticExt(ExtractionMarker):
            source = "static_valid"

            @staticmethod
            async def extract(
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "static_ok"

        @controller("/static_valid")
        class SVController:
            @get("/")
            async def handler(self, x: StaticExt) -> Response:
                return Response.json({"x": x})

        @module(controllers=[SVController])
        class SVModule:
            pass

        app = LaurenFactory.create(SVModule)
        r = TestClient(app).get("/static_valid/")
        assert r.status_code == 200
        assert r.json()["x"] == "static_ok"


class TestInheritanceDetectionEndToEnd:
    """Full HTTP round-trip tests for inheritance-based method detection."""

    # ------------------------------------------------------------------
    # A4 end-to-end: child overrides classmethod with @injectable instance method
    # ------------------------------------------------------------------

    def test_override_classmethod_with_injectable_instance_method(self):
        class Parent(ExtractionMarker):
            source = "a4_e2e"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "parent_cm"

        @injectable(scope=Scope.SINGLETON)
        class Child(Parent):
            source = "a4_e2e"

            def __init__(self) -> None:
                self.call_count = 0

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                self.call_count += 1
                return f"child_im:{self.call_count}"

        @controller("/a4")
        class A4Controller:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({"x": x})

        @module(providers=[Child], controllers=[A4Controller])
        class A4Module:
            pass

        app = LaurenFactory.create(A4Module)
        client = TestClient(app)
        assert client.get("/a4/").json()["x"] == "child_im:1"
        assert client.get("/a4/").json()["x"] == "child_im:2"  # same singleton

    # ------------------------------------------------------------------
    # A5 end-to-end: child overrides @injectable instance method with classmethod
    # ------------------------------------------------------------------

    def test_override_instance_method_with_classmethod_end_to_end(self):
        @injectable()
        class Parent(ExtractionMarker):
            source = "a5_e2e"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return "parent_im"

        class Child(Parent):
            source = "a5_e2e"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "child_cm"

        @controller("/a5")
        class A5Controller:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({"x": x})

        @module(controllers=[A5Controller])
        class A5Module:
            pass

        app = LaurenFactory.create(A5Module)
        assert TestClient(app).get("/a5/").json()["x"] == "child_cm"

    # ------------------------------------------------------------------
    # A1 end-to-end: inherited classmethod from parent
    # ------------------------------------------------------------------

    def test_inherited_classmethod_end_to_end(self):
        class Parent(ExtractionMarker):
            source = "a1_e2e"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return request.method

        class Child(Parent):
            source = "a1_e2e"

        @controller("/a1")
        class A1Controller:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({"method": x})

        @module(controllers=[A1Controller])
        class A1Module:
            pass

        app = LaurenFactory.create(A1Module)
        assert TestClient(app).get("/a1/").json()["method"] == "GET"


class TestExecutionContextInExtractors:
    """Verify that the ExecutionContext passed to extract() carries correct fields.

    The handler info (handler_class, handler_func, route_template, metadata)
    should be the same as what guards receive — extractors are now first-class
    participants in the execution pipeline.
    """

    def test_execution_context_has_request(self):
        """execution_context.request is the current request object."""
        received: list[ExecutionContext] = []

        class MethodEcho(ExtractionMarker):
            source = "method_echo"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                received.append(execution_context)
                return execution_context.request.method

        @controller("/method_echo")
        class MEController:
            @get("/")
            async def handler(self, m: MethodEcho) -> Response:
                return Response.json({"m": m})

        @module(controllers=[MEController])
        class MEModule:
            pass

        app = LaurenFactory.create(MEModule)
        r = TestClient(app).get("/method_echo/")
        assert r.json()["m"] == "GET"
        assert received[0].request.method == "GET"

    def test_execution_context_has_handler_class_and_func(self):
        """execution_context carries the controller class and handler function."""
        received: list[ExecutionContext] = []

        class HandlerInfo(ExtractionMarker):
            source = "handler_info"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                received.append(execution_context)
                return "captured"

        @controller("/hinfo")
        class HInfoController:
            @get("/")
            async def my_handler(self, info: HandlerInfo) -> Response:
                return Response.json({"info": info})

        @module(controllers=[HInfoController])
        class HInfoModule:
            pass

        app = LaurenFactory.create(HInfoModule)
        TestClient(app).get("/hinfo/")
        ctx = received[0]
        assert ctx.handler_class is HInfoController
        assert ctx.handler_func.__name__ == "my_handler"

    def test_execution_context_has_route_template(self):
        """execution_context.route_template matches the declared path."""
        received: list[ExecutionContext] = []

        class TemplateCapture(ExtractionMarker):
            source = "tpl_capture"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                received.append(execution_context)
                return "ok"

        @controller("/items")
        class ItemController:
            @get("/{item_id}")
            async def get_item(self, info: TemplateCapture) -> Response:
                return Response.json({"info": info})

        @module(controllers=[ItemController])
        class ItemModule:
            pass

        app = LaurenFactory.create(ItemModule)
        TestClient(app).get("/items/42")
        assert received[0].route_template == "/items/{item_id}"

    def test_execution_context_has_route_metadata(self):
        """execution_context.metadata carries handler-level metadata."""
        from lauren import set_metadata

        MY_KEY = "test.my_meta"
        received: list[ExecutionContext] = []

        class MetaCapture(ExtractionMarker):
            source = "meta_capture"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                received.append(execution_context)
                return execution_context.get_metadata(MY_KEY)

        @controller("/meta_test")
        class MetaController:
            @get("/")
            @set_metadata(MY_KEY, "hello-from-meta")
            async def handler(self, tag: MetaCapture) -> Response:
                return Response.json({"tag": tag})

        @module(controllers=[MetaController])
        class MetaModule:
            pass

        app = LaurenFactory.create(MetaModule)
        r = TestClient(app).get("/meta_test/")
        assert r.json()["tag"] == "hello-from-meta"

    def test_injectable_extractor_also_receives_execution_context(self):
        """@injectable extractors receive ExecutionContext identical to guards."""

        @injectable(scope=Scope.SINGLETON)
        class RouteAwareExtractor(ExtractionMarker):
            source = "route_aware"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return execution_context.route_template or "unknown"

        @controller("/route_aware")
        class RAController:
            @get("/{slug}")
            async def handler(self, template: RouteAwareExtractor) -> Response:
                return Response.json({"template": template})

        @module(
            providers=[RouteAwareExtractor],
            controllers=[RAController],
        )
        class RAModule:
            pass

        app = LaurenFactory.create(RAModule)
        r = TestClient(app).get("/route_aware/hello")
        assert r.json()["template"] == "/route_aware/{slug}"
