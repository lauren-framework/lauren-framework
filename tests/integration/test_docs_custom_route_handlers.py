"""Integration tests for docs/guides/custom-route-handlers.md.

Every code snippet in the guide is exercised here so that documentation
examples stay correct as the framework evolves.
"""

from __future__ import annotations

import functools
import os
from typing import Annotated

from lauren import (
    Inject,
    LaurenFactory,
    controller,
    get,
    injectable,
    module,
    use_value,
)
from lauren.testing import TestClient


# ===========================================================================
# Binding styles — instance method
# ===========================================================================


class TestInstanceMethodBinding:
    """§ Binding styles / Instance method (default)."""

    def test_instance_handler_called_with_di_repo(self):
        @injectable()
        class UserRepository:
            async def find(self, uid: int) -> dict:
                return {"id": uid, "name": "alice"}

        @controller("/users")
        class UserController:
            def __init__(self, repo: UserRepository) -> None:
                self.repo = repo

            @get("/{id}")
            async def get_user(self, id: int) -> dict:
                return await self.repo.find(id)

        @module(providers=[UserRepository], controllers=[UserController])
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/users/42")
        assert r.status_code == 200
        assert r.json() == {"id": 42, "name": "alice"}


# ===========================================================================
# Binding styles — @staticmethod
# ===========================================================================


class TestStaticmethodBinding:
    """§ Binding styles / @staticmethod."""

    def test_static_handler_no_receiver(self):
        @controller("/health")
        class HealthController:
            @get("/")
            @staticmethod
            async def ping() -> dict:
                return {"status": "ok"}

        @module(controllers=[HealthController])
        class AppModule:
            pass

        r = TestClient(LaurenFactory.create(AppModule)).get("/health/")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_static_handler_with_di_inject(self):
        """@staticmethod + Inject() — DI still resolves request-level params."""

        @controller("/ver")
        class VersionController:
            @get("/")
            @staticmethod
            def version(
                app_version: Annotated[str, Inject("APP_VERSION")],
            ) -> dict:
                return {"version": app_version}

        @module(
            providers=[use_value(provide="APP_VERSION", value="2.0.0")],
            controllers=[VersionController],
        )
        class AppModule:
            pass

        r = TestClient(LaurenFactory.create(AppModule)).get("/ver/")
        assert r.status_code == 200
        assert r.json() == {"version": "2.0.0"}

    def test_static_outer_get_inner_both_orderings_produce_same_result(self):
        """Both @staticmethod/@get orderings register the route identically."""

        @controller("/ord")
        class OrderController:
            # preferred ordering
            @staticmethod
            @get("/a")
            def handler_a() -> dict:
                return {"order": "a"}

            # alternative ordering — also works
            @get("/b")
            @staticmethod
            def handler_b() -> dict:
                return {"order": "b"}

        @module(controllers=[OrderController])
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/ord/a").json() == {"order": "a"}
        assert client.get("/ord/b").json() == {"order": "b"}


# ===========================================================================
# Binding styles — @classmethod
# ===========================================================================


class TestClassmethodBinding:
    """§ Binding styles / @classmethod."""

    def test_classmethod_handler_receives_cls(self):
        @controller("/config")
        class ConfigController:
            _env: str = "production"

            @get("/env")
            @classmethod
            async def get_env(cls) -> dict:
                return {"env": cls._env}

        @module(controllers=[ConfigController])
        class AppModule:
            pass

        r = TestClient(LaurenFactory.create(AppModule)).get("/config/env")
        assert r.status_code == 200
        assert r.json() == {"env": "production"}

    def test_classmethod_cls_is_the_concrete_controller_class(self):
        received: list[type] = []

        @controller("/cls")
        class ClsController:
            @get("/")
            @classmethod
            def which(cls) -> dict:
                received.append(cls)
                return {"cls": cls.__name__}

        @module(controllers=[ClsController])
        class AppModule:
            pass

        TestClient(LaurenFactory.create(AppModule)).get("/cls/")
        assert received == [ClsController]


# ===========================================================================
# Custom decorators — @functools.wraps
# ===========================================================================


class TestCustomDecoratorsWithWraps:
    """§ Writing your own decorators / Minimal decorator skeleton."""

    def test_decorator_with_wraps_preserves_route(self):
        """@functools.wraps copies __dict__ (markers) and sets __wrapped__."""

        def timing(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                result = await fn(*args, **kwargs)
                return result

            return wrapper

        @controller("/ord")
        class OrderController:
            @get("/{id}")
            @timing
            async def get_order(self, id: int) -> dict:
                return {"id": id}

        @module(controllers=[OrderController])
        class AppModule:
            pass

        r = TestClient(LaurenFactory.create(AppModule)).get("/ord/7")
        assert r.status_code == 200
        assert r.json() == {"id": 7}

    def test_decorator_with_wraps_preserves_di_injection(self):
        """@functools.wraps preserves __wrapped__ so inspect.signature follows
        the chain to the real parameter list and DI injection works."""

        @injectable()
        class Greeter:
            def greet(self, name: str) -> str:
                return f"hi {name}"

        def audit(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                return await fn(*args, **kwargs)

            return wrapper

        @controller("/greet")
        class GreetController:
            @get("/")
            @audit
            async def hello(self, svc: Greeter) -> dict:
                return {"msg": svc.greet("world")}

        @module(providers=[Greeter], controllers=[GreetController])
        class AppModule:
            pass

        r = TestClient(LaurenFactory.create(AppModule)).get("/greet/")
        assert r.status_code == 200
        assert r.json() == {"msg": "hi world"}

    def test_get_outer_then_wraps_inner_works(self):
        """Decorator order: @get outer, @functools.wraps inner — both valid."""

        def noop(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                return fn(*args, **kwargs)

            return wrapper

        @controller("/noop")
        class NoopController:
            @get("/")
            @noop
            def hello(self) -> dict:
                return {"ok": True}

        @module(controllers=[NoopController])
        class AppModule:
            pass

        r = TestClient(LaurenFactory.create(AppModule)).get("/noop/")
        assert r.status_code == 200


# ===========================================================================
# Custom decorators — without @wraps (error cases)
# ===========================================================================


class TestCustomDecoratorsWithoutWraps:
    """§ Writing your own decorators / What breaks without @wraps."""

    def test_no_wraps_outer_causes_silent_404(self):
        """Outer decorator without @wraps discards __lauren_route__ → 404."""

        def bad_outer(fn):
            def wrapper(*args, **kwargs):  # no @wraps
                return fn(*args, **kwargs)

            return wrapper

        @controller("/bad")
        class BadController:
            @bad_outer  # outer: wraps AFTER @get, discards the marker
            @get("/lost")
            def handler(self) -> dict:
                return {"reached": True}

        @module(controllers=[BadController])
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/bad/lost").status_code == 404

    def test_no_wraps_inner_with_di_params_causes_runtime_error(self):
        """Inner decorator without @wraps erases signature → DI args missing → 500."""

        @injectable()
        class Dep:
            value = "injected"

        def bad_inner(fn):
            def wrapper(*args, **kwargs):  # no @wraps — (*args, **kwargs) hides signature
                return fn(*args, **kwargs)

            return wrapper

        @controller("/bad2")
        class Bad2Controller:
            @get("/di")
            @bad_inner  # inner: @get lands on wrapper, but signature is (*args, **kwargs)
            def handler(self, dep: Dep) -> dict:
                return {"value": dep.value}

        @module(providers=[Dep], controllers=[Bad2Controller])
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        # Route IS registered (marker survived on wrapper) but DI param is
        # invisible → handler is called without dep → TypeError at runtime.
        assert client.get("/bad2/di").status_code == 500


# ===========================================================================
# Environment-conditional implementations
# ===========================================================================


class TestFeatureFlagDecorator:
    """§ Environment-conditional implementations / Feature-flagged handler.

    Note: ``wrapped`` mirrors the ``async``-ness of ``fn`` via ``functools.wraps``
    so that Python 3.11 (which doesn't follow ``__wrapped__`` in
    ``inspect.iscoroutinefunction``) correctly detects the coroutine flag.
    """

    def test_feature_flag_not_set_uses_fallback(self):
        """When the env var is absent, the fallback implementation runs."""

        def feature(flag: str, fallback):
            def decorator(fn):
                if os.environ.get(flag):
                    return fn

                # Use async wrapped when fn is async so is_coroutine detection
                # works on Python 3.11 (iscoroutinefunction doesn't follow
                # __wrapped__ until 3.12).
                @functools.wraps(fn)
                async def wrapped(*args, **kwargs):
                    return await fallback(*args, **kwargs)

                return wrapped

            return decorator

        async def _stable(self, id: int) -> dict:
            return {"id": id, "source": "stable"}

        async def _experimental(self, id: int) -> dict:
            return {"id": id, "source": "experimental"}

        @controller("/items")
        class ItemController:
            @get("/{id}")
            @feature("_TEST_EXPERIMENTAL_OFF", fallback=_stable)
            async def get_item(self, id: int) -> dict:
                return await _experimental(self, id)

        @module(controllers=[ItemController])
        class AppModule:
            pass

        os.environ.pop("_TEST_EXPERIMENTAL_OFF", None)
        r = TestClient(LaurenFactory.create(AppModule)).get("/items/5")
        assert r.status_code == 200
        assert r.json() == {"id": 5, "source": "stable"}

    def test_feature_flag_set_uses_decorated_handler(self):
        """When the env var is present, the original (experimental) handler runs.

        The env var must be set BEFORE the class body is evaluated because
        the ``@feature`` decorator runs at class-definition time, not at
        request time — this is the "zero per-request overhead" design.
        """

        def feature(flag: str, fallback):
            def decorator(fn):
                if os.environ.get(flag):
                    return fn

                @functools.wraps(fn)
                async def wrapped(*args, **kwargs):
                    return await fallback(*args, **kwargs)

                return wrapped

            return decorator

        async def _stable2(self, id: int) -> dict:
            return {"id": id, "source": "stable"}

        os.environ["_TEST_EXPERIMENTAL_ON"] = "1"
        try:

            @controller("/items2")
            class ItemController2:
                @get("/{id}")
                @feature("_TEST_EXPERIMENTAL_ON", fallback=_stable2)
                async def get_item(self, id: int) -> dict:
                    return {"id": id, "source": "experimental"}

            @module(controllers=[ItemController2])
            class AppModule:
                pass

            r = TestClient(LaurenFactory.create(AppModule)).get("/items2/9")
            assert r.status_code == 200
            assert r.json() == {"id": 9, "source": "experimental"}
        finally:
            os.environ.pop("_TEST_EXPERIMENTAL_ON", None)


class TestClassBodyConditional:
    """§ Environment-conditional implementations / Class body if/else."""

    def test_development_mode_returns_platform_info(self):
        """When APP_ENV is not 'production', the dev handler is selected."""
        import sys
        import platform

        # Simulate development mode (default when APP_ENV is absent)
        _prod_mode = os.environ.get("_TEST_APP_ENV", "development") == "production"

        @controller("/diag")
        class DiagnosticsController:
            if _prod_mode:  # noqa: E701

                @get("/debug")
                @staticmethod
                async def debug_info() -> dict:
                    return {"detail": "disabled in production"}
            else:

                @get("/debug")
                @staticmethod
                async def debug_info() -> dict:  # type: ignore[misc]
                    return {
                        "python": sys.version,
                        "platform": platform.platform(),
                    }

        @module(controllers=[DiagnosticsController])
        class AppModule:
            pass

        os.environ.pop("_TEST_APP_ENV", None)
        r = TestClient(LaurenFactory.create(AppModule)).get("/diag/debug")
        assert r.status_code == 200
        body = r.json()
        # In dev mode the response has real system info, not the disabled message
        assert "python" in body
        assert "platform" in body


# ===========================================================================
# Custom descriptor (advanced)
# ===========================================================================


class TestCustomDescriptor:
    """§ Custom descriptors (advanced) / retry_on_error."""

    def test_retry_descriptor_calls_handler_on_success(self):
        class retry_on_error:
            def __init__(self, fn, *, retries: int = 3) -> None:
                self._fn = fn
                self._retries = retries
                functools.update_wrapper(self, fn)

            def __call__(self, *args, **kwargs):
                last_exc: Exception | None = None
                for _ in range(self._retries):
                    try:
                        return self._fn(*args, **kwargs)
                    except Exception as exc:
                        last_exc = exc
                raise last_exc  # type: ignore[misc]

            def __get__(self, obj, objtype=None):
                if obj is None:
                    return self
                return functools.partial(self, obj)

        def retry(retries: int = 3):
            def decorator(fn):
                return retry_on_error(fn, retries=retries)

            return decorator

        @controller("/pay")
        class PaymentController:
            @get("/ok")
            @retry(retries=3)
            def get_payment(self) -> dict:
                return {"paid": True}

        @module(controllers=[PaymentController])
        class AppModule:
            pass

        r = TestClient(LaurenFactory.create(AppModule)).get("/pay/ok")
        assert r.status_code == 200
        assert r.json() == {"paid": True}

    def test_retry_descriptor_retries_on_transient_error(self):
        """Descriptor retries up to *n* times and succeeds on the last attempt."""

        call_count: list[int] = [0]

        class retry_on_error:
            def __init__(self, fn, *, retries: int = 3) -> None:
                self._fn = fn
                self._retries = retries
                functools.update_wrapper(self, fn)

            def __call__(self, *args, **kwargs):
                last_exc: Exception | None = None
                for _ in range(self._retries):
                    try:
                        return self._fn(*args, **kwargs)
                    except Exception as exc:
                        last_exc = exc
                raise last_exc  # type: ignore[misc]

            def __get__(self, obj, objtype=None):
                if obj is None:
                    return self
                return functools.partial(self, obj)

        def retry(retries: int = 3):
            def decorator(fn):
                return retry_on_error(fn, retries=retries)

            return decorator

        @controller("/flaky")
        class FlakyController:
            @get("/")
            @retry(retries=3)
            def flaky(self) -> dict:
                call_count[0] += 1
                if call_count[0] < 3:
                    raise RuntimeError("transient")
                return {"attempts": call_count[0]}

        @module(controllers=[FlakyController])
        class AppModule:
            pass

        call_count[0] = 0
        r = TestClient(LaurenFactory.create(AppModule)).get("/flaky/")
        assert r.status_code == 200
        assert r.json() == {"attempts": 3}
