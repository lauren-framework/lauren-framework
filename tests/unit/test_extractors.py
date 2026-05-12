"""Unit tests for extractors."""

from __future__ import annotations


import pytest
from pydantic import BaseModel

from lauren import (
    Json,
    Path,
    Query,
)
from lauren.exceptions import ExtractorError, ExtractorFieldError
from lauren.extractors import (
    Bytes,
    FieldDescriptor,
    Extraction,
    extract_parameter,
    parse_extractor_hint,
)
from lauren.types import ExecutionContext, Headers, Request


def make_request(
    method: str = "GET",
    path: str = "/",
    *,
    query: bytes = b"",
    headers: list[tuple[str, str]] | None = None,
    path_params: dict[str, str] | None = None,
    body: bytes = b"",
) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        method=method,
        path=path,
        raw_query_string=query,
        headers=Headers(headers or []),
        path_params=path_params,
        receive=receive,
    )


class TestParseHints:
    def test_path_int(self):
        src, inner, reads, marker, *_rest = parse_extractor_hint(Path[int])
        assert src == "path"
        assert inner is int
        assert reads is False
        assert marker is Path

    def test_query_str(self):
        src, inner, *_rest = parse_extractor_hint(Query[str])
        assert src == "query"
        assert inner is str

    def test_json_model(self):
        class User(BaseModel):
            name: str

        src, inner, reads, *_rest = parse_extractor_hint(Json[User])
        assert src == "json"
        assert inner is User
        assert reads is True

    def test_bare_type_returns_none(self):
        src, *_rest = parse_extractor_hint(int)
        assert src is None

    def test_bytes_extractor(self):
        src, inner, reads, *_rest = parse_extractor_hint(Bytes)
        assert src == "bytes"
        assert reads is True


class TestPathExtraction:
    @pytest.mark.asyncio
    async def test_extract_path_int(self):
        req = make_request(path_params={"id": "42"})
        ext = Extraction(
            name="id",
            source="path",
            inner_type=int,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        val = await extract_parameter(req, ext)
        assert val == 42

    @pytest.mark.asyncio
    async def test_path_missing_raises(self):
        req = make_request()
        ext = Extraction(
            name="id",
            source="path",
            inner_type=int,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        with pytest.raises(ExtractorFieldError):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_path_invalid_int(self):
        req = make_request(path_params={"id": "notanint"})
        ext = Extraction(
            name="id",
            source="path",
            inner_type=int,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        with pytest.raises(ExtractorError):
            await extract_parameter(req, ext)


class TestQueryExtraction:
    @pytest.mark.asyncio
    async def test_basic_query(self):
        req = make_request(query=b"q=hello")
        ext = Extraction(
            name="q",
            source="query",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == "hello"

    @pytest.mark.asyncio
    async def test_query_list(self):
        req = make_request(query=b"tag=a&tag=b&tag=c")
        ext = Extraction(
            name="tag",
            source="query",
            inner_type=list[str],
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_query_int_with_ge(self):
        req = make_request(query=b"page=0")
        fd = FieldDescriptor(ge=1)
        ext = Extraction(
            name="page",
            source="query",
            inner_type=int,
            field_descriptor=fd,
            default=None,
            has_default=False,
        )
        with pytest.raises(ExtractorFieldError):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_query_default(self):
        req = make_request()
        ext = Extraction(
            name="q",
            source="query",
            inner_type=str,
            field_descriptor=None,
            default="default",
            has_default=True,
        )
        assert await extract_parameter(req, ext) == "default"

    @pytest.mark.asyncio
    async def test_query_alias(self):
        req = make_request(query=b"user-id=7")
        fd = FieldDescriptor(alias="user-id")
        ext = Extraction(
            name="user_id",
            source="query",
            inner_type=int,
            field_descriptor=fd,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == 7


class TestHeaderExtraction:
    @pytest.mark.asyncio
    async def test_header_basic(self):
        req = make_request(headers=[("X-Request-Id", "abc")])
        ext = Extraction(
            name="x-request-id",
            source="header",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == "abc"

    @pytest.mark.asyncio
    async def test_header_with_default(self):
        req = make_request()
        ext = Extraction(
            name="x-missing",
            source="header",
            inner_type=str,
            field_descriptor=None,
            default="fallback",
            has_default=True,
        )
        assert await extract_parameter(req, ext) == "fallback"

    @pytest.mark.asyncio
    async def test_header_underscore_to_hyphen(self):
        req = make_request(headers=[("x-api-key", "secret")])
        ext = Extraction(
            name="x_api_key",
            source="header",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == "secret"


class TestCookieExtraction:
    @pytest.mark.asyncio
    async def test_cookie_basic(self):
        req = make_request(headers=[("cookie", "session=abc; theme=dark")])
        ext = Extraction(
            name="session",
            source="cookie",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == "abc"


class TestJsonExtraction:
    @pytest.mark.asyncio
    async def test_json_dict(self):
        req = make_request(body=b'{"k":"v"}')
        ext = Extraction(
            name="body",
            source="json",
            inner_type=dict,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        assert await extract_parameter(req, ext) == {"k": "v"}

    @pytest.mark.asyncio
    async def test_json_pydantic_validation(self):
        class User(BaseModel):
            name: str
            age: int

        req = make_request(body=b'{"name":"Alice","age":30}')
        ext = Extraction(
            name="user",
            source="json",
            inner_type=User,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        u = await extract_parameter(req, ext)
        assert u.name == "Alice"
        assert u.age == 30

    @pytest.mark.asyncio
    async def test_json_validation_error(self):
        class User(BaseModel):
            name: str
            age: int

        req = make_request(body=b'{"name":"Alice","age":"not-int"}')
        ext = Extraction(
            name="user",
            source="json",
            inner_type=User,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        with pytest.raises(ExtractorError):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_json_malformed(self):
        req = make_request(body=b"not json")
        ext = Extraction(
            name="body",
            source="json",
            inner_type=dict,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        with pytest.raises(ExtractorError):
            await extract_parameter(req, ext)


class TestBytesExtraction:
    @pytest.mark.asyncio
    async def test_raw_bytes(self):
        req = make_request(body=b"\x00\x01\x02")
        ext = Extraction(
            name="body",
            source="bytes",
            inner_type=bytes,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        assert await extract_parameter(req, ext) == b"\x00\x01\x02"


class TestCustomExtractor:
    """Verify the custom-extractor hook via :meth:`ExtractionMarker.extract`."""

    @pytest.mark.asyncio
    async def test_custom_extract_classmethod_invoked(self):
        from lauren.extractors import ExtractionMarker

        class Echo(ExtractionMarker):
            source = "echo"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return f"echo:{extraction.name}:{request.method}"

        req = make_request(method="POST")
        ext = Extraction(
            name="x",
            source="echo",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=Echo,
        )
        value = await extract_parameter(req, ext)
        assert value == "echo:x:POST"

    @pytest.mark.asyncio
    async def test_custom_extract_httperror_propagates(self):
        from lauren.exceptions import UnauthorizedError
        from lauren.extractors import ExtractionMarker

        class AuthMe(ExtractionMarker):
            source = "auth_me"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                raise UnauthorizedError("nope")

        req = make_request()
        ext = Extraction(
            name="u",
            source="auth_me",
            inner_type=object,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=AuthMe,
        )
        with pytest.raises(UnauthorizedError):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_bare_marker_class_parsed(self):
        from lauren.extractors import ExtractionMarker

        class Marker(ExtractionMarker):
            source = "marker"

        src, inner, reads, cls, *_rest = parse_extractor_hint(Marker)
        assert src == "marker"
        assert cls is Marker


class TestInjectableExtractor:
    """Verify the injectable instance-method extractor dispatch path.

    These tests use a minimal mock DI container so they stay pure-unit with
    no real module graph needed.
    """

    # ------------------------------------------------------------------
    # Minimal mock container
    # ------------------------------------------------------------------

    class _MockContainer:
        """Minimal stand-in for a DI container."""

        def __init__(self, instances: dict) -> None:
            self._instances = instances  # {cls: pre-built instance}

        def has_provider(self, token, owning_module=None) -> bool:
            return token in self._instances

        async def resolve(
            self,
            token,
            *,
            request_cache=None,
            framework_values=None,
            owning_module=None,
        ):
            if token in self._instances:
                return self._instances[token]
            raise KeyError(f"No provider for {token!r}")

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_injectable_instance_method_called(self):
        """@injectable extractor receives (ExecutionContext, Extraction)."""
        from lauren import injectable
        from lauren.extractors import ExtractionMarker

        @injectable()
        class HeaderEcho(ExtractionMarker):
            source = "header_echo"

            def __init__(self, prefix: str = "echo") -> None:
                self._prefix = prefix

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return f"{self._prefix}:{extraction.name}:{execution_context.request.method}"

        instance = HeaderEcho(prefix="test")
        container = self._MockContainer({HeaderEcho: instance})

        req = make_request(method="DELETE")
        ctx = ExecutionContext(request=req)
        ext = Extraction(
            name="x",
            source="header_echo",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=HeaderEcho,
        )
        value = await extract_parameter(req, ext, container=container, execution_context=ctx)
        assert value == "test:x:DELETE"

    @pytest.mark.asyncio
    async def test_injectable_extractor_receives_injected_deps(self):
        """The instance returned by the container carries its own state."""
        from lauren import injectable
        from lauren.extractors import ExtractionMarker

        @injectable()
        class SentinelExtractor(ExtractionMarker):
            source = "sentinel"

            def __init__(self) -> None:
                self.calls: list[str] = []

            async def extract(
                self,
                request: Request,
                extraction: Extraction,
            ) -> object:
                self.calls.append(extraction.name)
                return len(self.calls)

        inst = SentinelExtractor()
        container = self._MockContainer({SentinelExtractor: inst})

        req = make_request()
        ext = Extraction(
            name="n",
            source="sentinel",
            inner_type=int,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=SentinelExtractor,
        )
        assert await extract_parameter(req, ext, container=container) == 1
        assert await extract_parameter(req, ext, container=container) == 2
        # Confirm same instance was used both times:
        assert inst.calls == ["n", "n"]

    @pytest.mark.asyncio
    async def test_injectable_extractor_no_container_raises(self):
        """Missing container raises MissingProviderError, not a confusing crash."""
        from lauren import injectable
        from lauren.exceptions import MissingProviderError
        from lauren.extractors import ExtractionMarker

        @injectable()
        class NoContainer(ExtractionMarker):
            source = "no_container"

            async def extract(
                self,
                request: Request,
                extraction: Extraction,
            ) -> object:
                return "never"

        req = make_request()
        ext = Extraction(
            name="x",
            source="no_container",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=NoContainer,
        )
        with pytest.raises(MissingProviderError, match="@injectable"):
            await extract_parameter(req, ext, container=None)

    @pytest.mark.asyncio
    async def test_injectable_extractor_httperror_propagates(self):
        """HTTPErrors raised inside inject-method extractors bubble up unchanged."""
        from lauren import injectable
        from lauren.exceptions import ForbiddenError
        from lauren.extractors import ExtractionMarker

        @injectable()
        class StrictExtractor(ExtractionMarker):
            source = "strict"

            async def extract(
                self,
                request: Request,
                extraction: Extraction,
            ) -> object:
                raise ForbiddenError("not allowed")

        inst = StrictExtractor()
        container = self._MockContainer({StrictExtractor: inst})

        req = make_request()
        ext = Extraction(
            name="u",
            source="strict",
            inner_type=object,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=StrictExtractor,
        )
        with pytest.raises(ForbiddenError):
            await extract_parameter(req, ext, container=container)

    @pytest.mark.asyncio
    async def test_classmethod_extractor_unchanged(self):
        """Existing classmethod extractors are unaffected by the new path."""
        from lauren.extractors import ExtractionMarker

        class Classic(ExtractionMarker):
            source = "classic"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "classic_ok"

        req = make_request()
        ext = Extraction(
            name="y",
            source="classic",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=Classic,
        )
        # No container needed — classmethod dispatch handles None gracefully.
        assert await extract_parameter(req, ext) == "classic_ok"

    @pytest.mark.asyncio
    async def test_injectable_classmethod_extractor_uses_classmethod_path(self):
        """@injectable + @classmethod keeps using the classmethod path."""
        from lauren import injectable
        from lauren.extractors import ExtractionMarker

        @injectable()
        class Hybrid(ExtractionMarker):
            source = "hybrid"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "hybrid_classmethod"

        req = make_request()
        ext = Extraction(
            name="z",
            source="hybrid",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=Hybrid,
        )
        assert await extract_parameter(req, ext) == "hybrid_classmethod"


class TestExtractMethodDetectionInheritance:
    """Unit tests for MRO-based classmethod/instance-method detection.

    Every case exercises the detection logic in ``_extract_raw`` (and, where
    noted, the startup validation in ``_compile_handler_signature``).  A
    minimal mock container is used throughout so there is no module graph.
    """

    class _MockContainer:
        def __init__(self, instances: dict) -> None:
            self._instances = instances

        def has_provider(self, token, owning_module=None) -> bool:
            return token in self._instances

        async def resolve(
            self,
            token,
            *,
            request_cache=None,
            framework_values=None,
            owning_module=None,
        ):
            if token in self._instances:
                return self._instances[token]
            raise KeyError(f"No provider for {token!r}")

    def _ext(self, source: str, marker_cls) -> "Extraction":
        return Extraction(
            name="x",
            source=source,
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=marker_cls,
        )

    # ------------------------------------------------------------------
    # A1 — classmethod defined on parent, child inherits (no override)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_inherited_classmethod_uses_classmethod_path(self):
        from lauren.extractors import ExtractionMarker

        class Parent(ExtractionMarker):
            source = "a1"

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
            source = "a1"
            # inherits extract — no override

        req = make_request()
        assert await extract_parameter(req, self._ext("a1", Child)) == "parent_cm"

    # ------------------------------------------------------------------
    # A2a — instance method on parent, child explicitly @injectable
    # A2b — instance method on parent, child inherits @injectable via MRO
    #        (hasattr traverses MRO, so no explicit @injectable needed on child)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_inherited_instance_method_child_explicit_injectable(self):
        from lauren import injectable
        from lauren.extractors import ExtractionMarker

        @injectable()
        class Parent(ExtractionMarker):
            source = "a2a"

            async def extract(
                self,
                request: Request,
                extraction: Extraction,
            ) -> object:
                return "parent_im"

        @injectable()
        class Child(Parent):
            source = "a2a"

        inst = Child()
        container = self._MockContainer({Child: inst})
        req = make_request()
        assert await extract_parameter(req, self._ext("a2a", Child), container=container) == "parent_im"

    def test_injectable_own_dict_check_not_hasattr(self):
        """__dict__ check: Child that inherits @injectable is NOT treated as injectable.

        The DI container enforces a no-inheritance rule (MetadataInheritanceError),
        so the extractor uses ``marker_cls.__dict__`` to check injectable — not
        ``hasattr``.  A class that merely inherits ``__lauren_injectable__`` is not
        injectable for the purposes of extractor dispatch.  The startup validator
        (in ``_compile_handler_signature``) catches this at factory time; here we
        confirm the attribute check directly without going through the full factory.
        """
        from lauren import injectable
        from lauren.extractors import ExtractionMarker

        @injectable()
        class Parent(ExtractionMarker):
            source = "a2b"

            async def extract(
                self,
                request: Request,
                extraction: Extraction,
            ) -> object:
                return "from_parent"

        class Child(Parent):
            source = "a2b"

        # Parent has it in own dict; Child does not.
        assert "__lauren_injectable__" in Parent.__dict__
        assert "__lauren_injectable__" not in Child.__dict__
        # hasattr still returns True (MRO), but the extractor doesn't use hasattr.
        import builtins

        assert builtins.hasattr(Child, "__lauren_injectable__")

    # ------------------------------------------------------------------
    # A3 — grandparent defines classmethod; parent + child just inherit
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_grandparent_classmethod_found_via_mro(self):
        from lauren.extractors import ExtractionMarker

        class Grandparent(ExtractionMarker):
            source = "a3"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "grandparent"

        class Parent(Grandparent):
            pass

        class Child(Parent):
            source = "a3"

        req = make_request()
        assert await extract_parameter(req, self._ext("a3", Child)) == "grandparent"

    # ------------------------------------------------------------------
    # A4 — child overrides parent classmethod with instance method (@injectable)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_override_classmethod_with_instance_method(self):
        from lauren import injectable
        from lauren.extractors import ExtractionMarker

        class Parent(ExtractionMarker):
            source = "a4"

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

        @injectable()
        class Child(Parent):
            source = "a4"

            async def extract(
                self,
                request: Request,
                extraction: Extraction,
            ) -> object:
                return "child_im"

        inst = Child()
        container = self._MockContainer({Child: inst})
        req = make_request()
        result = await extract_parameter(req, self._ext("a4", Child), container=container)
        assert result == "child_im"

    # ------------------------------------------------------------------
    # A5 — child overrides parent instance method with classmethod
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_override_instance_method_with_classmethod(self):
        from lauren import injectable
        from lauren.extractors import ExtractionMarker

        @injectable()
        class Parent(ExtractionMarker):
            source = "a5"

            async def extract(
                self,
                request: Request,
                extraction: Extraction,
            ) -> object:
                return "parent_im"

        class Child(Parent):
            source = "a5"

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

        req = make_request()
        # No container needed — child's classmethod path is used.
        assert await extract_parameter(req, self._ext("a5", Child)) == "child_cm"

    # ------------------------------------------------------------------
    # A6 — both parent and child define classmethod; child's version wins
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_override_classmethod_child_wins(self):
        from lauren.extractors import ExtractionMarker

        class Parent(ExtractionMarker):
            source = "a6"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "parent"

        class Child(Parent):
            source = "a6"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "child"

        req = make_request()
        assert await extract_parameter(req, self._ext("a6", Child)) == "child"

    # ------------------------------------------------------------------
    # A7 — both parent and child define instance method (@injectable); child wins
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_override_instance_method_child_wins(self):
        from lauren import injectable
        from lauren.extractors import ExtractionMarker

        @injectable()
        class Parent(ExtractionMarker):
            source = "a7"

            async def extract(
                self,
                request: Request,
                extraction: Extraction,
            ) -> object:
                return "parent"

        @injectable()
        class Child(Parent):
            source = "a7"

            async def extract(
                self,
                request: Request,
                extraction: Extraction,
            ) -> object:
                return "child"

        inst = Child()
        container = self._MockContainer({Child: inst})
        req = make_request()
        result = await extract_parameter(req, self._ext("a7", Child), container=container)
        assert result == "child"

    # ------------------------------------------------------------------
    # B1 — @staticmethod: treated as non-instance → classmethod path
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_staticmethod_takes_classmethod_path(self):
        from lauren.extractors import ExtractionMarker

        class StaticExt(ExtractionMarker):
            source = "b1"

            @staticmethod
            async def extract(
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "static_ok"

        req = make_request()
        assert await extract_parameter(req, self._ext("b1", StaticExt)) == "static_ok"

    # ------------------------------------------------------------------
    # B2 — parent classmethod overridden by child @staticmethod
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_override_classmethod_with_staticmethod(self):
        from lauren.extractors import ExtractionMarker

        class Parent(ExtractionMarker):
            source = "b2"

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
            source = "b2"

            @staticmethod
            async def extract(
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "child_static"

        req = make_request()
        assert await extract_parameter(req, self._ext("b2", Child)) == "child_static"

    # ------------------------------------------------------------------
    # B3 — parent @staticmethod overridden by child @staticmethod
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_override_staticmethod_child_wins(self):
        from lauren.extractors import ExtractionMarker

        class Parent(ExtractionMarker):
            source = "b3"

            @staticmethod
            async def extract(
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "parent_static"

        class Child(Parent):
            source = "b3"

            @staticmethod
            async def extract(
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: dict[type, object] | None,
            ) -> object:
                return "child_static"

        req = make_request()
        assert await extract_parameter(req, self._ext("b3", Child)) == "child_static"

    # ------------------------------------------------------------------
    # F1 — inherited classmethod declares owning_module param; it is forwarded
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_inherited_classmethod_owning_module_forwarded(self):
        from lauren.extractors import ExtractionMarker

        received: list = []

        class Parent(ExtractionMarker):
            source = "f1"

            @classmethod
            async def extract(
                cls,
                request,
                extraction,
                *,
                container,
                request_cache,
                owning_module=None,
            ):
                received.append(owning_module)
                return "f1_ok"

        class Child(Parent):
            source = "f1"

        sentinel = object()
        req = make_request()
        await extract_parameter(req, self._ext("f1", Child), owning_module=sentinel)
        assert received == [sentinel]

    # ------------------------------------------------------------------
    # F2 — inherited instance method receives ExecutionContext correctly
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_inherited_instance_method_receives_execution_context(self):
        """ExecutionContext is passed as the first arg to inherited instance methods."""
        from lauren import injectable
        from lauren.extractors import ExtractionMarker

        received_ctxs: list = []

        @injectable()
        class Parent(ExtractionMarker):
            source = "f2"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                received_ctxs.append(execution_context)
                return "f2_ok"

        @injectable()
        class Child(Parent):
            source = "f2"

        inst = Child()
        container = self._MockContainer({Child: inst})
        req = make_request(method="PATCH")
        ctx = ExecutionContext(request=req, route_template="/f2/{id}")
        await extract_parameter(
            req,
            self._ext("f2", Child),
            container=container,
            execution_context=ctx,
        )
        assert len(received_ctxs) == 1
        assert received_ctxs[0] is ctx
        assert received_ctxs[0].request.method == "PATCH"
        assert received_ctxs[0].route_template == "/f2/{id}"


class TestUnifiedExtractorSignature:
    """Unit tests for the canonical extract(self, execution_context, extraction) API.

    Covers:
    - Non-injectable extractors (no-arg, process-wide cache)
    - Injectable extractors with the new unified signature
    - ExecutionContext fields are propagated correctly
    - Error handling for both forms
    """

    def _ext(self, source: str, marker_cls) -> Extraction:
        return Extraction(
            name="v",
            source=source,
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=marker_cls,
        )

    # ------------------------------------------------------------------
    # Non-injectable instance-method extractor (no-arg cache)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_non_injectable_extractor_works(self):
        """Instance-method extractor without @injectable is instantiated no-arg."""
        from lauren.extractors import ExtractionMarker

        class Echo(ExtractionMarker):
            source = "echo_plain"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                return f"echo:{extraction.name}"

        req = make_request()
        assert await extract_parameter(req, self._ext("echo_plain", Echo)) == "echo:v"

    @pytest.mark.asyncio
    async def test_non_injectable_extractor_same_instance_reused(self):
        """Non-injectable extractors reuse the same process-wide instance."""
        from lauren.extractors import ExtractionMarker, _EXTRACTOR_INSTANCE_CACHE

        class CallCounter(ExtractionMarker):
            source = "call_counter"

            def __init__(self) -> None:
                self.calls = 0

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                self.calls += 1
                return self.calls

        # Clear cache in case a prior test left a stale entry
        _EXTRACTOR_INSTANCE_CACHE.pop(CallCounter, None)

        req = make_request()
        ext = self._ext("call_counter", CallCounter)
        v1 = await extract_parameter(req, ext)
        v2 = await extract_parameter(req, ext)
        assert v1 == 1
        assert v2 == 2  # same instance, counter persisted

    @pytest.mark.asyncio
    async def test_non_injectable_extractor_httperror_propagates(self):
        """HTTPErrors from non-injectable extractors bubble up unchanged."""
        from lauren.exceptions import UnauthorizedError
        from lauren.extractors import ExtractionMarker

        class AlwaysDeny(ExtractionMarker):
            source = "always_deny"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                raise UnauthorizedError("denied")

        req = make_request()
        with pytest.raises(UnauthorizedError):
            await extract_parameter(req, self._ext("always_deny", AlwaysDeny))

    # ------------------------------------------------------------------
    # ExecutionContext fields received correctly
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_execution_context_fields_propagated(self):
        """ExecutionContext is passed as the first arg with all fields intact."""
        from lauren.extractors import ExtractionMarker

        captured: list = []

        class CtxCapture(ExtractionMarker):
            source = "ctx_capture"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                captured.append(execution_context)
                return "ok"

        sentinel_handler = lambda: None  # noqa: E731
        req = make_request(method="POST")
        ctx = ExecutionContext(
            request=req,
            handler_class=object,
            handler_func=sentinel_handler,
            route_template="/items/{id}",
        )
        await extract_parameter(
            req,
            self._ext("ctx_capture", CtxCapture),
            execution_context=ctx,
        )
        assert len(captured) == 1
        assert captured[0] is ctx
        assert captured[0].request is req
        assert captured[0].handler_class is object
        assert captured[0].handler_func is sentinel_handler
        assert captured[0].route_template == "/items/{id}"

    @pytest.mark.asyncio
    async def test_injectable_extractor_receives_execution_context(self):
        """@injectable extractor also receives ExecutionContext as first arg."""
        from lauren import injectable
        from lauren.extractors import ExtractionMarker

        class _MockContainer:
            def __init__(self, inst):
                self._inst = inst

            async def resolve(self, token, **_):
                return self._inst

        captured: list = []

        @injectable()
        class InjectableCapture(ExtractionMarker):
            source = "inj_capture"

            async def extract(
                self,
                execution_context: ExecutionContext,
                extraction: Extraction,
            ) -> object:
                captured.append(execution_context)
                return "inj_ok"

        inst = InjectableCapture()
        container = _MockContainer(inst)
        req = make_request()
        ctx = ExecutionContext(request=req, route_template="/inj/{x}")
        await extract_parameter(
            req,
            self._ext("inj_capture", InjectableCapture),
            container=container,
            execution_context=ctx,
        )
        assert captured[0].route_template == "/inj/{x}"

    @pytest.mark.asyncio
    async def test_execution_context_none_does_not_crash_for_classmethods(self):
        """Classmethod extractors are unaffected when execution_context is None."""
        from lauren.extractors import ExtractionMarker

        class CM(ExtractionMarker):
            source = "cm_no_ctx"

            @classmethod
            async def extract(
                cls,
                request: Request,
                extraction: Extraction,
                *,
                container: object | None,
                request_cache: object | None,
            ) -> object:
                return request.method

        req = make_request(method="PATCH")
        # No execution_context passed — classmethod path ignores it
        result = await extract_parameter(req, self._ext("cm_no_ctx", CM))
        assert result == "PATCH"


class TestFormExtraction:
    @pytest.mark.asyncio
    async def test_form_basic(self):
        req = make_request(body=b"name=alice&age=30")
        ext = Extraction(
            name="form",
            source="form",
            inner_type=dict,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        val = await extract_parameter(req, ext)
        assert val["name"] == ["alice"]


# ---------------------------------------------------------------------------
# Additional coverage tests for uncovered lines
# ---------------------------------------------------------------------------


class TestFieldDescriptorValidateEdgeCases:
    """Cover gt, lt, min/max_length, and pattern branches in validate()."""

    def test_gt_violation(self):
        fd = FieldDescriptor(gt=5.0)
        with pytest.raises(ExtractorFieldError, match="5.0"):
            fd.validate("x", 5.0)  # exactly 5 is NOT > 5

    def test_lt_violation(self):
        fd = FieldDescriptor(lt=5.0)
        with pytest.raises(ExtractorFieldError, match="5.0"):
            fd.validate("x", 5.0)  # exactly 5 is NOT < 5

    def test_min_length_violation(self):
        fd = FieldDescriptor(min_length=5)
        with pytest.raises(ExtractorFieldError, match="too short"):
            fd.validate("name", "abc")

    def test_max_length_violation(self):
        fd = FieldDescriptor(max_length=3)
        with pytest.raises(ExtractorFieldError, match="too long"):
            fd.validate("name", "abcd")

    def test_pattern_violation(self):
        fd = FieldDescriptor(pattern=r"^\d+$")
        with pytest.raises(ExtractorFieldError, match="pattern"):
            fd.validate("code", "abc")

    def test_pattern_ok(self):
        fd = FieldDescriptor(pattern=r"^\d+$")
        assert fd.validate("code", "123") == "123"

    def test_none_value_with_default(self):
        fd = FieldDescriptor(default="fallback")
        assert fd.validate("x", None) == "fallback"


class TestParamSpecEdgeCases:
    def test_paramspec_default_no_descriptor(self):
        from lauren.extractors import _ParamSpec

        ps = _ParamSpec()
        assert ps.default is ...

    def test_paramspec_or_two_descriptors_raises(self):
        from lauren.extractors import _ParamSpec

        ps1 = _ParamSpec(field_descriptor=FieldDescriptor(ge=1))
        ps2 = _ParamSpec(field_descriptor=FieldDescriptor(le=100))
        with pytest.raises(TypeError, match="at most one FieldDescriptor"):
            _ = ps1 | ps2

    def test_paramspec_or_with_second_descriptor_raises_when_already_has_one(self):
        from lauren.extractors import _ParamSpec

        ps = _ParamSpec(field_descriptor=FieldDescriptor(ge=1))
        with pytest.raises(TypeError, match="at most one FieldDescriptor"):
            _ = ps | FieldDescriptor(le=100)

    def test_paramspec_or_returns_not_implemented_for_non_callable(self):
        from lauren.extractors import _ParamSpec

        ps = _ParamSpec()
        # "42" is neither callable, pipe, FieldDescriptor, nor _ParamSpec
        result = ps.__or__(42)
        assert result is NotImplemented

    def test_paramspec_or_with_pipe_adds_pipe(self):
        from lauren.extractors import _ParamSpec, pipe

        @pipe
        def my_pipe(v, ctx):
            return v

        ps = _ParamSpec()
        result = ps | my_pipe
        assert my_pipe in result.pipes


class TestCoerceScalarEdgeCases:
    """Cover union, list[T], bool, and error paths in _coerce_scalar."""

    def test_coerce_bool_true(self):
        from lauren.extractors import _coerce_scalar

        assert _coerce_scalar("true", bool) is True
        assert _coerce_scalar("1", bool) is True
        assert _coerce_scalar("false", bool) is False
        assert _coerce_scalar("no", bool) is False

    def test_coerce_list_int(self):
        from lauren.extractors import _coerce_scalar

        result = _coerce_scalar("1,2,3", list[int])
        assert result == [1, 2, 3]

    def test_coerce_union_first_branch_wins(self):
        from lauren.extractors import _coerce_scalar
        from typing import Union

        # int | str: "42" coerces to int
        result = _coerce_scalar("42", Union[int, str])
        assert result == 42

    def test_coerce_union_fallback_to_second_branch(self):
        from lauren.extractors import _coerce_scalar
        from typing import Union

        # int | str: "abc" fails int, falls back to str
        result = _coerce_scalar("abc", Union[int, str])
        assert result == "abc"

    def test_coerce_none_returns_none(self):
        from lauren.extractors import _coerce_scalar

        assert _coerce_scalar(None, int) is None

    def test_coerce_float_error(self):
        from lauren.extractors import _coerce_scalar

        with pytest.raises(ExtractorFieldError):
            _coerce_scalar("not-a-float", float)

    def test_coerce_int_error(self):
        from lauren.extractors import _coerce_scalar

        with pytest.raises(ExtractorFieldError):
            _coerce_scalar("not-an-int", int)


class TestExtractionStateMissing:
    """Cover state extraction fallback (lines 1335-1346)."""

    @pytest.mark.asyncio
    async def test_state_missing_with_default(self):
        from lauren.types import Request

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        req = Request(method="GET", path="/", receive=receive)
        ext = Extraction(
            name="current_user",
            source="state",
            inner_type=str,
            field_descriptor=None,
            default="anonymous",
            has_default=True,
        )
        result = await extract_parameter(req, ext)
        assert result == "anonymous"

    @pytest.mark.asyncio
    async def test_state_missing_required_raises(self):
        from lauren.types import Request

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        req = Request(method="GET", path="/", receive=receive)
        ext = Extraction(
            name="required_state",
            source="state",
            inner_type=str,
            field_descriptor=None,
            default=...,
            has_default=False,
        )
        with pytest.raises(ExtractorFieldError, match="missing state"):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_state_from_app_state(self):
        from lauren.types import Request, AppState

        app_state = AppState({"theme": "dark"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        req = Request(method="GET", path="/", receive=receive, app_state=app_state)
        ext = Extraction(
            name="theme",
            source="state",
            inner_type=str,
            field_descriptor=None,
            default=...,
            has_default=False,
        )
        result = await extract_parameter(req, ext)
        assert result == "dark"


class TestExtractionDependsMissingContainer:
    """Cover 'depends' with no container (line 1350)."""

    @pytest.mark.asyncio
    async def test_depends_without_container_raises(self):
        req = make_request()
        ext = Extraction(
            name="svc",
            source="depends",
            inner_type=object,
            field_descriptor=None,
            default=...,
            has_default=False,
        )
        # Without a container, Depends raises some kind of error
        with pytest.raises(Exception, match="DI container|Depends"):
            await extract_parameter(req, ext, container=None)


class TestCustomExtractorEdgeCases:
    """Cover custom extractor error wrapping (lines 1444-1464)."""

    @pytest.mark.asyncio
    async def test_custom_extractor_non_http_error_wrapped(self):
        """Non-HTTPError/StartupError exceptions from custom extractors
        get wrapped in ExtractorError."""
        from lauren.extractors import ExtractionMarker

        class BoomExtractor(ExtractionMarker):
            source = "boom"

            async def extract(self, execution_context, extraction):
                raise ValueError("raw error from custom extractor")

        req = make_request()
        ext = Extraction(
            name="boom_param",
            source="boom",
            inner_type=str,
            field_descriptor=None,
            default=...,
            has_default=False,
            marker_cls=BoomExtractor,
        )
        with pytest.raises(ExtractorError, match="custom extractor BoomExtractor failed"):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_custom_extractor_http_error_not_wrapped(self):
        """HTTPError from a custom extractor propagates unchanged."""
        from lauren.extractors import ExtractionMarker
        from lauren.exceptions import UnauthorizedError

        class AuthExtractor(ExtractionMarker):
            source = "auth"

            async def extract(self, execution_context, extraction):
                raise UnauthorizedError("not authenticated")

        req = make_request()
        ext = Extraction(
            name="auth_param",
            source="auth",
            inner_type=str,
            field_descriptor=None,
            default=...,
            has_default=False,
            marker_cls=AuthExtractor,
        )
        with pytest.raises(UnauthorizedError):
            await extract_parameter(req, ext)


class TestCookieExtractionWithFdDefault:
    """Cover cookie fd.default fallback (lines 1280-1282)."""

    @pytest.mark.asyncio
    async def test_cookie_fd_default_when_missing(self):
        req = make_request()
        fd = FieldDescriptor(default="no-session")
        ext = Extraction(
            name="session",
            source="cookie",
            inner_type=str,
            field_descriptor=fd,
            default=...,
            has_default=False,
        )
        result = await extract_parameter(req, ext)
        assert result == "no-session"


class TestHeaderExtractionWithFdDefault:
    """Cover header fd.default fallback (lines 1266-1268)."""

    @pytest.mark.asyncio
    async def test_header_fd_default_when_missing(self):
        req = make_request()
        fd = FieldDescriptor(default="none")
        ext = Extraction(
            name="x_trace_id",
            source="header",
            inner_type=str,
            field_descriptor=fd,
            default=...,
            has_default=False,
        )
        result = await extract_parameter(req, ext)
        assert result == "none"


class TestQueryModelOptionalNoFields:
    """Cover Query[OptionalModel] with no fields present."""

    @pytest.mark.asyncio
    async def test_optional_model_with_no_query_params_returns_none(self):
        from pydantic import BaseModel

        class Filters(BaseModel):
            tag: str | None = None
            limit: int = 10

        req = make_request()
        ext = Extraction(
            name="filters",
            source="query",
            inner_type=Filters | None,
            field_descriptor=None,
            default=None,
            has_default=True,
        )
        result = await extract_parameter(req, ext)
        # Optional model with no query params and has_default should return default
        assert result is None


class TestFormPydanticModel:
    """Cover form extraction with a pydantic model (lines 1311-1312)."""

    @pytest.mark.asyncio
    async def test_form_with_pydantic_model(self):
        from pydantic import BaseModel

        class CreateForm(BaseModel):
            name: str
            age: int

        req = make_request(body=b"name=alice&age=30")
        ext = Extraction(
            name="data",
            source="form",
            inner_type=CreateForm,
            field_descriptor=None,
            default=...,
            has_default=False,
            reads_body=True,
        )
        result = await extract_parameter(req, ext)
        assert isinstance(result, CreateForm)
        assert result.name == "alice"
        assert result.age == 30


class TestMissingJsonBody:
    """Cover missing JSON body with default (line 1202)."""

    @pytest.mark.asyncio
    async def test_empty_json_body_with_default(self):
        req = make_request(body=b"")
        ext = Extraction(
            name="payload",
            source="json",
            inner_type=dict,
            field_descriptor=None,
            default=None,
            has_default=True,
        )
        result = await extract_parameter(req, ext)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_json_body_without_default_raises(self):
        req = make_request(body=b"")
        ext = Extraction(
            name="payload",
            source="json",
            inner_type=dict,
            field_descriptor=None,
            default=...,
            has_default=False,
        )
        with pytest.raises(ExtractorFieldError, match="missing JSON body"):
            await extract_parameter(req, ext)


class TestQueryListWithFd:
    """Cover query list extraction with FieldDescriptor (line 1246)."""

    @pytest.mark.asyncio
    async def test_query_list_with_fd(self):
        req = make_request(query=b"tag=a&tag=b")
        fd = FieldDescriptor()
        ext = Extraction(
            name="tag",
            source="query",
            inner_type=list[str],
            field_descriptor=fd,
            default=...,
            has_default=False,
        )
        result = await extract_parameter(req, ext)
        assert result == ["a", "b"]


class TestQueryFdDefaultFallback:
    """Cover query fd.default fallback when key missing (line 1252)."""

    @pytest.mark.asyncio
    async def test_query_fd_default_used_when_key_missing(self):
        req = make_request()  # no query params
        fd = FieldDescriptor(default="all")
        ext = Extraction(
            name="status",
            source="query",
            inner_type=str,
            field_descriptor=fd,
            default=...,
            has_default=False,
        )
        result = await extract_parameter(req, ext)
        assert result == "all"


class TestPipeArityUnknown:
    """Cover pipe with arity unknown (TypeError from inspect, line 1113-1114)."""

    @pytest.mark.asyncio
    async def test_pipe_with_args_kwargs_only(self):
        """A pipe whose signature raises TypeError in inspect.signature
        defaults to arity=2 and gets both value and ctx."""
        from lauren.extractors import pipe

        received: list = []

        def flexible_pipe(*args, **kwargs):
            received.append(args)
            return args[0]

        # Tag it as a pipe
        marked = pipe(flexible_pipe)

        req = make_request(query=b"x=42")
        ext = Extraction(
            name="x",
            source="query",
            inner_type=str,
            field_descriptor=None,
            default=...,
            has_default=False,
            pipes=(marked,),
        )
        result = await extract_parameter(req, ext)
        assert result == "42"


class TestUnknownExtractorSource:
    """Cover the fallback 'unknown source' ExtractorError at the end of _extract_raw."""

    @pytest.mark.asyncio
    async def test_unknown_source_raises(self):
        req = make_request()
        ext = Extraction(
            name="x",
            source="definitely_unknown_source",
            inner_type=str,
            field_descriptor=None,
            default=...,
            has_default=False,
            marker_cls=None,
        )
        with pytest.raises(ExtractorError, match="unknown extractor source"):
            await extract_parameter(req, ext)


# ---------------------------------------------------------------------------
# Additional coverage for extractors.py
# ---------------------------------------------------------------------------


class TestParseExtractorHintNestedAnnotated:
    """Cover lines 804-812 (inner_src not None, inner_fd not None)."""

    def test_nested_annotated_with_fd_merges_fd(self):
        """Annotated[Path[int], FieldDescriptor(default=0)] merges inner FD."""
        from typing import Annotated
        from lauren.extractors import parse_extractor_hint, FieldDescriptor

        # Create Path[int] which is Annotated[int, Path]
        # Then wrap it: Annotated[Path[int], FieldDescriptor(default=0)]
        inner = Path[int]  # Annotated[int, Path]
        outer = Annotated[inner, FieldDescriptor(default=0)]
        src, typ, reads_body, marker, fd, pipes = parse_extractor_hint(outer)
        assert src == "path"
        assert typ is int
        # The FieldDescriptor from the outer Annotated is applied
        assert fd is not None

    def test_nested_annotated_with_param_spec(self):
        """Annotated[Path[int], PathField(ge=0)] uses _ParamSpec merging."""
        from typing import Annotated
        from lauren.extractors import parse_extractor_hint
        from lauren import Path
        from lauren.extractors import PathField

        # PathField creates a FieldDescriptor; it acts as a _ParamSpec
        inner = Path[int]  # Annotated[int, Path]
        outer = Annotated[inner, PathField(ge=0)]
        src, typ, reads_body, marker, fd, pipes = parse_extractor_hint(outer)
        assert src == "path"
        assert fd is not None

    def test_multiple_field_descriptors_raises(self):
        """Two FieldDescriptor entries in Annotated[...] raises ExtractorError."""
        from typing import Annotated
        from lauren.extractors import parse_extractor_hint, FieldDescriptor

        fd1 = FieldDescriptor(default=0)
        fd2 = FieldDescriptor(default=1)
        ann = Annotated[int, fd1, fd2]
        with pytest.raises(ExtractorError, match="multiple FieldDescriptor"):
            parse_extractor_hint(ann)

    def test_param_spec_with_fd_raises_when_fd_already_set(self):
        """_ParamSpec in Annotated when fd already set raises ExtractorError."""
        from typing import Annotated
        from lauren.extractors import (
            parse_extractor_hint,
            FieldDescriptor,
            PathField,
        )

        # A _ParamSpec that has a field_descriptor + pre-existing FD from another arg
        fd_existing = FieldDescriptor(default=5)
        param_spec = PathField(ge=0)  # Returns a _ParamSpec with a FieldDescriptor

        ann = Annotated[int, fd_existing, param_spec]
        with pytest.raises(ExtractorError, match="multiple FieldDescriptor"):
            parse_extractor_hint(ann)

    def test_list_extraction_marker_origin(self):
        """list[SomeMarker] returns the marker's source and the list type."""
        from lauren.extractors import parse_extractor_hint
        from lauren.extractors import UploadFile

        ann = list[UploadFile]
        src, typ, reads_body, marker, fd, pipes = parse_extractor_hint(ann)
        assert src == "upload_file"  # UploadFile.source
        assert reads_body is True


class TestCoerceScalarUnionEdgeCases:
    """Cover lines 1012, 1017-1019 in _coerce_scalar."""

    def test_union_none_branch_skipped(self):
        """Union[str, None] skips NoneType branch."""
        from lauren.extractors import _coerce_scalar
        from typing import Union

        result = _coerce_scalar("hello", Union[str, None])
        assert result == "hello"

    def test_union_all_fail_raises_last_error(self):
        """Union[int, float] where neither works raises the last error."""
        from lauren.extractors import _coerce_scalar, ExtractorFieldError
        from typing import Union

        # Try to coerce "not_a_number" to Union[int, float]
        with pytest.raises(ExtractorFieldError):
            _coerce_scalar("not_a_number", Union[int, float])

    def test_union_first_branch_succeeds(self):
        """Union[int, float] where int succeeds returns int."""
        from lauren.extractors import _coerce_scalar
        from typing import Union

        result = _coerce_scalar("42", Union[int, float])
        assert result == 42
        assert isinstance(result, int)


class TestIsPydanticModelType:
    """Cover _is_pydantic_model_type edge cases."""

    def test_optional_model_is_true(self):
        """Optional[SomeModel] is recognized as a pydantic model type."""
        from lauren.extractors import _is_pydantic_model_type
        from pydantic import BaseModel
        from typing import Optional

        class M(BaseModel):
            x: int = 1

        assert _is_pydantic_model_type(Optional[M])

    def test_non_model_is_false(self):
        from lauren.extractors import _is_pydantic_model_type

        assert not _is_pydantic_model_type(str)
        assert not _is_pydantic_model_type(int)
        assert not _is_pydantic_model_type(dict)

    def test_plain_model_is_true(self):
        from lauren.extractors import _is_pydantic_model_type
        from pydantic import BaseModel

        class M(BaseModel):
            x: int = 1

        assert _is_pydantic_model_type(M)


class TestPipeArityEdgeCases:
    """Cover lines 1113-1114 (unknown arity falls back to 2)."""

    @pytest.mark.asyncio
    async def test_pipe_with_unknown_arity_still_works(self):
        """A pipe whose signature can't be inspected still runs correctly."""
        from lauren.extractors import pipe, Extraction, extract_parameter

        # Create a pipe with a lambda (lambdas have unknown arity from signature perspective)
        # but we can test that it passes value and context
        results = []

        def my_pipe(value, ctx=None):
            results.append(value)
            return value.upper()

        req = make_request(query=b"name=hello")
        ext = Extraction(
            name="name",
            source="query",
            inner_type=str,
            field_descriptor=None,
            default=...,
            has_default=False,
            marker_cls=None,
            pipes=(pipe(my_pipe),),
        )
        result = await extract_parameter(req, ext)
        assert result == "HELLO"

    @pytest.mark.asyncio
    async def test_pipe_raises_extractor_error_preserved(self):
        """A pipe that raises ExtractorError propagates it unchanged."""
        from lauren.extractors import pipe, Extraction, extract_parameter

        def bad_pipe(value, ctx=None):
            raise ExtractorError("pipe intentionally failed")

        req = make_request(query=b"n=1")
        ext = Extraction(
            name="n",
            source="query",
            inner_type=str,
            field_descriptor=None,
            default=...,
            has_default=False,
            marker_cls=None,
            pipes=(pipe(bad_pipe),),
        )
        with pytest.raises(ExtractorError, match="pipe intentionally failed"):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_pipe_raises_non_extractor_wrapped(self):
        """A pipe that raises a non-ExtractorError wraps in ExtractorError."""
        from lauren.extractors import pipe, Extraction, extract_parameter

        def bad_pipe(value, ctx=None):
            raise ValueError("some internal error")

        req = make_request(query=b"n=1")
        ext = Extraction(
            name="n",
            source="query",
            inner_type=str,
            field_descriptor=None,
            default=...,
            has_default=False,
            marker_cls=None,
            pipes=(pipe(bad_pipe),),
        )
        with pytest.raises(ExtractorError):
            await extract_parameter(req, ext)
