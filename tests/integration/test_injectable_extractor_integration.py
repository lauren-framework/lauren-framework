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
from lauren.exceptions import StartupError, UnauthorizedError
from lauren.extractors import _ExtractorMarker
from lauren.testing import TestClient


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
class BearerPrincipal(_ExtractorMarker):
    """Validate an Authorization: Bearer <token> header via TokenStore."""

    source = "bearer_principal"

    def __init__(self, store: TokenStore) -> None:
        self._store = store

    async def extract(self, request, extraction):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise UnauthorizedError("missing or malformed Authorization header")
        token = header[len("Bearer ") :]
        if not self._store.is_valid(token):
            raise UnauthorizedError("invalid token")
        return token


# ---------------------------------------------------------------------------
# Classic classmethod extractor — backward-compat
# ---------------------------------------------------------------------------


class EchoExtractor(_ExtractorMarker):
    source = "echo_marker"

    @classmethod
    async def extract(cls, request, extraction, *, container, request_cache):
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
    """Instance-method extractor without @injectable raises at startup."""

    def test_non_injectable_instance_method_raises_startup_error(self):
        class BadExtractor(_ExtractorMarker):
            source = "bad"

            async def extract(self, request, extraction):
                return "never"

        @controller("/bad")
        class BadController:
            @get("/")
            async def handler(self, x: BadExtractor) -> Response:
                return Response.json({})

        @module(controllers=[BadController])
        class BadModule:
            pass

        with pytest.raises(StartupError, match="instance method.*@injectable"):
            LaurenFactory.create(BadModule)

    def test_injectable_instance_method_does_not_raise(self):
        """Control: @injectable + instance method is valid — no StartupError."""

        @injectable()
        class GoodExtractor(_ExtractorMarker):
            source = "good_ext"

            async def extract(self, request, extraction):
                return "ok"

        @controller("/good")
        class GoodController:
            @get("/")
            async def handler(self, x: GoodExtractor) -> Response:
                return Response.json({"x": x})

        @module(providers=[GoodExtractor], controllers=[GoodController])
        class GoodModule:
            pass

        # Should not raise
        app = LaurenFactory.create(GoodModule)
        client = TestClient(app)
        r = client.get("/good/")
        assert r.status_code == 200
        assert r.json()["x"] == "ok"

    def test_classmethod_without_injectable_still_valid(self):
        """@classmethod extractors without @injectable remain valid."""

        class LegacyExtractor(_ExtractorMarker):
            source = "legacy_ext"

            @classmethod
            async def extract(cls, request, extraction, *, container, request_cache):
                return "legacy"

        @controller("/legacy")
        class LegacyController:
            @get("/")
            async def handler(self, x: LegacyExtractor) -> Response:
                return Response.json({"x": x})

        @module(controllers=[LegacyController])
        class LegacyModule:
            pass

        # Should not raise
        app = LaurenFactory.create(LegacyModule)
        client = TestClient(app)
        r = client.get("/legacy/")
        assert r.status_code == 200
        assert r.json()["x"] == "legacy"

    # ------------------------------------------------------------------
    # E1 — inherited instance method without @injectable in child's own dict
    #
    # Lauren's DI container enforces a strict no-inheritance rule for
    # @injectable (MetadataInheritanceError).  The extractor startup
    # validator uses the same __dict__-only check so misconfigurations
    # are caught at startup rather than producing a confusing 500 at
    # request time.
    # ------------------------------------------------------------------

    def test_inherited_instance_method_no_injectable_in_own_dict_raises(self):
        """Child inherits instance-method extract but @injectable is on parent only."""

        @injectable()
        class Parent(_ExtractorMarker):
            source = "e1"

            async def extract(self, request, extraction):
                return "from_parent"

        class Child(Parent):
            source = "e1"
            # @injectable is in Parent.__dict__, not Child.__dict__ → StartupError

        @controller("/e1")
        class E1Controller:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({})

        @module(controllers=[E1Controller])
        class E1Module:
            pass

        with pytest.raises(StartupError, match="instance method.*@injectable"):
            LaurenFactory.create(E1Module)

    def test_explicitly_injectable_child_with_inherited_extract_valid(self):
        """Child re-decorates with @injectable and inherits extract → valid."""

        @injectable()
        class Parent(_ExtractorMarker):
            source = "e1_ok"

            async def extract(self, request, extraction):
                return "from_parent_im"

        @injectable()  # explicit re-decoration on Child
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
    # E2 — multi-level: child not explicitly @injectable → StartupError
    # ------------------------------------------------------------------

    def test_multi_level_inherited_instance_method_no_injectable_raises(self):
        """Grandparent's @injectable does NOT count for Child without re-decoration."""

        @injectable()
        class Grandparent(_ExtractorMarker):
            source = "e2"

            async def extract(self, request, extraction):
                return "grandparent"

        class Parent(Grandparent):
            pass

        class Child(Parent):
            source = "e2"
            # @injectable only on Grandparent's __dict__, not Child's

        @controller("/e2")
        class E2Controller:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({})

        @module(controllers=[E2Controller])
        class E2Module:
            pass

        with pytest.raises(StartupError, match="instance method.*@injectable"):
            LaurenFactory.create(E2Module)

    def test_instance_method_no_injectable_anywhere_in_mro_raises(self):
        """Standalone class with no @injectable anywhere raises at startup."""

        class Standalone(_ExtractorMarker):
            source = "standalone_bad"

            async def extract(self, request, extraction):
                return "never"

        @controller("/sb")
        class SBController:
            @get("/")
            async def handler(self, x: Standalone) -> Response:
                return Response.json({})

        @module(controllers=[SBController])
        class SBModule:
            pass

        with pytest.raises(StartupError, match="instance method.*@injectable"):
            LaurenFactory.create(SBModule)

    # ------------------------------------------------------------------
    # Override: classmethod overridden by instance method — no @injectable → error
    # ------------------------------------------------------------------

    def test_override_classmethod_with_instance_no_injectable_raises(self):
        class Parent(_ExtractorMarker):
            source = "override_bad"

            @classmethod
            async def extract(cls, request, extraction, *, container, request_cache):
                return "parent_cm"

        class Child(Parent):
            source = "override_bad"

            async def extract(self, request, extraction):
                return "child_im"  # instance method but no @injectable

        @controller("/override_bad")
        class OBController:
            @get("/")
            async def handler(self, x: Child) -> Response:
                return Response.json({})

        @module(controllers=[OBController])
        class OBModule:
            pass

        with pytest.raises(StartupError, match="instance method.*@injectable"):
            LaurenFactory.create(OBModule)

    # ------------------------------------------------------------------
    # Override: instance method overridden by classmethod → no error
    # ------------------------------------------------------------------

    def test_override_instance_method_with_classmethod_no_startup_error(self):
        @injectable()
        class Parent(_ExtractorMarker):
            source = "override_ok"

            async def extract(self, request, extraction):
                return "parent_im"

        class Child(Parent):
            source = "override_ok"

            @classmethod
            async def extract(cls, request, extraction, *, container, request_cache):
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
        class StaticExt(_ExtractorMarker):
            source = "static_valid"

            @staticmethod
            async def extract(request, extraction, *, container, request_cache):
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
        class Parent(_ExtractorMarker):
            source = "a4_e2e"

            @classmethod
            async def extract(cls, request, extraction, *, container, request_cache):
                return "parent_cm"

        @injectable(scope=Scope.SINGLETON)
        class Child(Parent):
            source = "a4_e2e"

            def __init__(self) -> None:
                self.call_count = 0

            async def extract(self, request, extraction):
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
        class Parent(_ExtractorMarker):
            source = "a5_e2e"

            async def extract(self, request, extraction):
                return "parent_im"

        class Child(Parent):
            source = "a5_e2e"

            @classmethod
            async def extract(cls, request, extraction, *, container, request_cache):
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
        class Parent(_ExtractorMarker):
            source = "a1_e2e"

            @classmethod
            async def extract(cls, request, extraction, *, container, request_cache):
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
