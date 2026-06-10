"""Unit tests for lauren.reflect static readers.

No ASGI app or event loop is required — every test works purely on
decorated classes built in-process.
"""

from __future__ import annotations


from lauren import (
    Scope,
    controller,
    delete,
    exception_handler,
    get,
    injectable,
    module,
    on_message,
    post,
    set_metadata,
    use_exception_handlers,
    use_guards,
    use_interceptors,
    use_middlewares,
    ws_controller,
)
from lauren.reflect import (
    ReflectedController,
    ReflectedModule,
    get_controller_metadata,
    get_module_metadata,
    reflect_controller,
    reflect_encoder,
    reflect_exception_handlers,
    reflect_guards,
    reflect_injectable,
    reflect_middlewares,
    reflect_module,
    reflect_routes,
    reflect_user_metadata,
    reflect_ws_controller,
    reflect_ws_messages,
)
from lauren.serialization import StdlibJSONEncoder


# ---------------------------------------------------------------------------
# Helpers — tiny guard / injectable stubs
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class _GuardA:
    async def can_activate(self, ctx):
        return True


@injectable(scope=Scope.SINGLETON)
class _GuardB:
    async def can_activate(self, ctx):
        return True


@injectable(scope=Scope.SINGLETON)
class _Interceptor:
    async def intercept(self, ctx, ch):
        return await ch.handle()


@injectable(scope=Scope.SINGLETON)
class _Middleware:
    async def dispatch(self, req, call_next):
        return await call_next(req)


@exception_handler(ValueError)
class _ExcHandler:
    async def catch(self, exc, req):
        from lauren.types import Response

        return Response.json({"error": str(exc)}, status=400)


# ---------------------------------------------------------------------------
# reflect_controller
# ---------------------------------------------------------------------------


class TestReflectController:
    def test_returns_meta_for_decorated_class(self):
        @controller("/items")
        class Items:
            pass

        meta = reflect_controller(Items)
        assert meta is not None
        assert meta.prefix == "/items"

    def test_returns_none_for_plain_class(self):
        class Plain:
            pass

        assert reflect_controller(Plain) is None

    def test_no_inheritance(self):
        @controller("/base")
        class Base:
            pass

        class Child(Base):
            pass

        assert reflect_controller(Child) is None

    def test_tags_and_description(self):
        @controller("/x", tags=["a", "b"], description="desc")
        class X:
            pass

        meta = reflect_controller(X)
        assert meta.tags == ("a", "b")
        assert meta.description == "desc"


# ---------------------------------------------------------------------------
# reflect_module
# ---------------------------------------------------------------------------


class TestReflectModule:
    def test_returns_meta_for_decorated_class(self):
        @module(controllers=[])
        class App:
            pass

        meta = reflect_module(App)
        assert meta is not None
        assert meta.controllers == ()

    def test_returns_none_for_plain_class(self):
        class Plain:
            pass

        assert reflect_module(Plain) is None

    def test_no_inheritance(self):
        @module()
        class Base:
            pass

        class Child(Base):
            pass

        assert reflect_module(Child) is None

    def test_module_with_providers_and_imports(self):
        @injectable(scope=Scope.SINGLETON)
        class Svc:
            pass

        @module(providers=[Svc])
        class Inner:
            pass

        @module(imports=[Inner])
        class Outer:
            pass

        outer_meta = reflect_module(Outer)
        assert outer_meta is not None
        assert Inner in outer_meta.imports


# ---------------------------------------------------------------------------
# reflect_injectable
# ---------------------------------------------------------------------------


class TestReflectInjectable:
    def test_returns_meta_for_injectable_class(self):
        @injectable(scope=Scope.SINGLETON)
        class Svc:
            pass

        meta = reflect_injectable(Svc)
        assert meta is not None
        assert meta.scope == Scope.SINGLETON

    def test_returns_none_for_plain_class(self):
        class Plain:
            pass

        assert reflect_injectable(Plain) is None

    def test_transient_scope(self):
        @injectable(scope=Scope.TRANSIENT)
        class T:
            pass

        assert reflect_injectable(T).scope == Scope.TRANSIENT

    def test_no_inheritance(self):
        @injectable(scope=Scope.SINGLETON)
        class Base:
            pass

        class Child(Base):
            pass

        assert reflect_injectable(Child) is None


# ---------------------------------------------------------------------------
# reflect_ws_controller
# ---------------------------------------------------------------------------


class TestReflectWsController:
    def test_returns_meta_for_ws_controller(self):
        @ws_controller("/ws/chat")
        class Chat:
            pass

        meta = reflect_ws_controller(Chat)
        assert meta is not None
        assert meta.path == "/ws/chat"

    def test_returns_none_for_plain_class(self):
        class Plain:
            pass

        assert reflect_ws_controller(Plain) is None

    def test_no_inheritance(self):
        @ws_controller("/base")
        class Base:
            pass

        class Child(Base):
            pass

        assert reflect_ws_controller(Child) is None


# ---------------------------------------------------------------------------
# reflect_routes
# ---------------------------------------------------------------------------


class TestReflectRoutes:
    def test_single_route(self):
        @controller("/users")
        class Users:
            @get("/{id}")
            async def get_user(self):
                pass

        routes = reflect_routes(Users)
        assert len(routes) == 1
        assert routes[0].method == "GET"
        assert routes[0].path == "/{id}"

    def test_full_path_combines_prefix_and_path(self):
        @controller("/api/v1")
        class Api:
            @post("/items")
            async def create(self):
                pass

        routes = reflect_routes(Api)
        assert routes[0].full_path == "/api/v1/items"

    def test_multiple_methods_on_class(self):
        @controller("/things")
        class Things:
            @get("/")
            async def list_things(self):
                pass

            @post("/")
            async def create_thing(self):
                pass

            @delete("/{id}")
            async def delete_thing(self):
                pass

        routes = reflect_routes(Things)
        methods = {r.method for r in routes}
        assert methods == {"GET", "POST", "DELETE"}

    def test_empty_prefix_produces_correct_full_path(self):
        @controller()
        class Root:
            @get("/health")
            async def health(self):
                pass

        routes = reflect_routes(Root)
        assert routes[0].full_path == "/health"

    def test_no_routes_returns_empty_tuple(self):
        @controller("/empty")
        class Empty:
            pass

        assert reflect_routes(Empty) == ()

    def test_undecorated_class_returns_empty_tuple(self):
        class Plain:
            @get("/x")
            async def x(self):
                pass

        # route meta on the method, but no controller prefix needed
        routes = reflect_routes(Plain)
        assert len(routes) == 1
        assert routes[0].full_path == "/x"

    def test_no_inheritance(self):
        @controller("/parent")
        class Parent:
            @get("/p")
            async def p(self):
                pass

        class Child(Parent):
            pass

        # Child.__dict__ has no routes of its own
        assert reflect_routes(Child) == ()

    def test_route_meta_fields_preserved(self):
        @controller("/x")
        class X:
            @get("/y", summary="Sum", tags=["t1"], deprecated=True)
            async def y(self):
                pass

        route = reflect_routes(X)[0]
        assert route.summary == "Sum"
        assert route.tags == ("t1",)
        assert route.deprecated is True

    def test_prefix_slash_normalization(self):
        @controller("/api/")
        class Api:
            @get("/items/")
            async def items(self):
                pass

        route = reflect_routes(Api)[0]
        assert route.full_path == "/api/items"

    def test_handler_reference(self):
        @controller("/x")
        class X:
            @get("/z")
            async def z(self):
                pass

        route = reflect_routes(X)[0]
        assert route.handler is X.__dict__["z"]


# ---------------------------------------------------------------------------
# reflect_ws_messages
# ---------------------------------------------------------------------------


class TestReflectWsMessages:
    def test_returns_messages(self):
        @ws_controller("/ws")
        class Gw:
            @on_message("chat.send")
            async def send(self):
                pass

        msgs = reflect_ws_messages(Gw)
        assert len(msgs) == 1
        assert msgs[0].event == "chat.send"

    def test_empty_when_no_messages(self):
        @ws_controller("/ws")
        class Gw:
            pass

        assert reflect_ws_messages(Gw) == ()

    def test_multiple_messages(self):
        @ws_controller("/ws")
        class Gw:
            @on_message("a")
            async def a(self):
                pass

            @on_message("b")
            async def b(self):
                pass

        events = {m.event for m in reflect_ws_messages(Gw)}
        assert events == {"a", "b"}

    def test_no_inheritance(self):
        @ws_controller("/ws")
        class Base:
            @on_message("ping")
            async def ping(self):
                pass

        class Child(Base):
            pass

        assert reflect_ws_messages(Child) == ()


# ---------------------------------------------------------------------------
# reflect_exception_handlers
# ---------------------------------------------------------------------------


class TestReflectExceptionHandlers:
    def test_class_with_use_exception_handlers(self):
        @use_exception_handlers(_ExcHandler)
        @controller("/x")
        class X:
            pass

        handlers = reflect_exception_handlers(X)
        assert _ExcHandler in handlers

    def test_function_with_use_exception_handlers(self):
        @use_exception_handlers(_ExcHandler)
        @get("/y")
        async def handler():
            pass

        handlers = reflect_exception_handlers(handler)
        assert _ExcHandler in handlers

    def test_returns_empty_for_undecorated_class(self):
        class X:
            pass

        assert reflect_exception_handlers(X) == ()

    def test_no_inheritance_for_classes(self):
        @use_exception_handlers(_ExcHandler)
        @controller("/base")
        class Base:
            pass

        class Child(Base):
            pass

        assert reflect_exception_handlers(Child) == ()


# ---------------------------------------------------------------------------
# get_controller_metadata
# ---------------------------------------------------------------------------


class TestGetControllerMetadata:
    def test_returns_reflected_controller(self):
        @controller("/items")
        class Items:
            @get("/{id}")
            async def get_item(self):
                pass

        result = get_controller_metadata(Items)
        assert isinstance(result, ReflectedController)
        assert result.cls is Items
        assert result.meta.prefix == "/items"
        assert len(result.routes) == 1

    def test_returns_none_for_non_controller(self):
        class Plain:
            pass

        assert get_controller_metadata(Plain) is None

    def test_includes_guards(self):
        @use_guards(_GuardA)
        @controller("/secure")
        class Secure:
            pass

        result = get_controller_metadata(Secure)
        assert _GuardA in result.guards

    def test_includes_interceptors(self):
        @use_interceptors(_Interceptor)
        @controller("/x")
        class X:
            pass

        result = get_controller_metadata(X)
        assert _Interceptor in result.interceptors

    def test_includes_exception_handlers(self):
        @use_exception_handlers(_ExcHandler)
        @controller("/x")
        class X:
            pass

        result = get_controller_metadata(X)
        assert _ExcHandler in result.exception_handlers


# ---------------------------------------------------------------------------
# get_module_metadata
# ---------------------------------------------------------------------------


class TestGetModuleMetadata:
    def test_returns_reflected_module(self):
        @injectable(scope=Scope.SINGLETON)
        class Svc:
            pass

        @module(providers=[Svc])
        class App:
            pass

        result = get_module_metadata(App)
        assert isinstance(result, ReflectedModule)
        assert result.cls is App
        assert Svc in result.meta.providers

    def test_returns_none_for_non_module(self):
        class Plain:
            pass

        assert get_module_metadata(Plain) is None


# ---------------------------------------------------------------------------
# reflect_user_metadata
# ---------------------------------------------------------------------------


class TestReflectUserMetadata:
    def test_returns_full_dict_when_no_key(self):
        @set_metadata("limit", 100)
        @controller("/x")
        class X:
            pass

        result = reflect_user_metadata(X)
        assert result == {"limit": 100}

    def test_returns_single_value_for_key(self):
        @set_metadata("role", "admin")
        @controller("/x")
        class X:
            pass

        assert reflect_user_metadata(X, "role") == "admin"

    def test_returns_default_for_missing_key(self):
        @controller("/x")
        class X:
            pass

        assert reflect_user_metadata(X, "missing", 42) == 42

    def test_works_on_function(self):
        @set_metadata("scope", "public")
        @get("/z")
        async def z():
            pass

        assert reflect_user_metadata(z, "scope") == "public"

    def test_empty_dict_for_undecorated(self):
        class Plain:
            pass

        assert reflect_user_metadata(Plain) == {}

    def test_no_inheritance(self):
        @set_metadata("k", "v")
        @controller("/base")
        class Base:
            pass

        class Child(Base):
            pass

        assert reflect_user_metadata(Child) == {}


# ---------------------------------------------------------------------------
# reflect_encoder
# ---------------------------------------------------------------------------


class TestReflectEncoder:
    def test_returns_encoder_on_controller(self):
        from lauren import use_encoder

        enc = StdlibJSONEncoder()

        @use_encoder(enc)
        @controller("/fast")
        class Fast:
            pass

        assert reflect_encoder(Fast) is enc

    def test_returns_encoder_on_method(self):
        from lauren import use_encoder

        enc = StdlibJSONEncoder()

        @use_encoder(enc)
        @get("/fast")
        async def fast():
            pass

        assert reflect_encoder(fast) is enc

    def test_returns_none_when_absent(self):
        @controller("/x")
        class X:
            pass

        assert reflect_encoder(X) is None

    def test_no_inheritance(self):
        from lauren import use_encoder

        enc = StdlibJSONEncoder()

        @use_encoder(enc)
        @controller("/base")
        class Base:
            pass

        class Child(Base):
            pass

        assert reflect_encoder(Child) is None


# ---------------------------------------------------------------------------
# Own-dict rule regression
# ---------------------------------------------------------------------------


class TestOwnDictRule:
    def test_reflect_guards_no_inheritance(self):
        @use_guards(_GuardA)
        @controller("/parent")
        class Parent:
            pass

        class Child(Parent):
            pass

        assert reflect_guards(Child) == ()

    def test_reflect_controller_no_inheritance(self):
        @controller("/parent")
        class Parent:
            pass

        class Child(Parent):
            pass

        assert reflect_controller(Child) is None

    def test_reflect_routes_no_inheritance(self):
        @controller("/parent")
        class Parent:
            @get("/x")
            async def x(self):
                pass

        class Child(Parent):
            pass

        assert reflect_routes(Child) == ()

    def test_reflect_injectable_no_inheritance(self):
        @injectable(scope=Scope.SINGLETON)
        class Base:
            pass

        class Child(Base):
            pass

        assert reflect_injectable(Child) is None

    def test_reflect_ws_controller_no_inheritance(self):
        @ws_controller("/ws")
        class Base:
            pass

        class Child(Base):
            pass

        assert reflect_ws_controller(Child) is None

    def test_reflect_middlewares_no_inheritance(self):
        @use_middlewares(_Middleware)
        @controller("/base")
        class Base:
            pass

        class Child(Base):
            pass

        assert reflect_middlewares(Child) == ()
