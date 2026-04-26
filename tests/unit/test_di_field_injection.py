"""Unit tests for class-body annotation-based dependency injection.

Exercises :class:`DIContainer` in isolation to confirm:

* Fields declared as ``attr: SomeProvider`` on a ``@injectable()`` class
  body are resolved and set on the instance **after** construction
  completes (the framework constructs through ``cls(**kwargs)`` so
  Python's normal call protocol — metaclass ``__call__``,
  ``__signature__`` overrides, ``__new__`` + ``__init__`` — runs
  unmodified). If you need a value inside ``__init__``, declare it as
  an ``__init__`` parameter; class-body annotations are post-construct
  attribute injection.
* Class-level defaults (``attr: int = 5``) opt the attribute out of DI.
* The classic ``__init__(self, dep: ...)`` style still works unchanged,
  and may be mixed with class-body annotations on the same class.
* User-supplied ``__new__`` receives DI kwargs as well.
* The DI compiler flags a missing provider for a required field at
  startup (not at runtime).
* ``ClassVar`` / stringified annotations lauren can't resolve are
  silently skipped so they don't interfere with regular class usage.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from lauren import Scope, injectable
from lauren._di import DIContainer
from lauren.exceptions import MissingProviderError


# ---------------------------------------------------------------------------
# Field-based injection
# ---------------------------------------------------------------------------


@injectable()
class _ConfigService:
    def __init__(self) -> None:
        self.db_url = "sqlite://:memory:"


@injectable()
class _AuditLogger:
    def __init__(self) -> None:
        self.entries: list[str] = []


class TestFieldInjection:
    @pytest.mark.asyncio
    async def test_field_is_set_after_construction(self):
        # The framework constructs via ``cls(**kwargs)``, so any
        # class-body-annotated DI fields land on the instance AFTER
        # ``__init__`` runs. Reading ``self.cfg`` from inside
        # ``__init__`` would therefore raise ``AttributeError`` —
        # users that need a dep during ``__init__`` should declare it
        # as an ``__init__`` parameter (see
        # :meth:`test_mix_of_init_param_and_field_annotation`).
        @injectable()
        class Service:
            cfg: _ConfigService

        c = DIContainer()
        c.register(_ConfigService)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        # The field was injected post-construction; subsequent reads
        # see the resolved provider.
        assert svc.cfg.db_url == "sqlite://:memory:"

    @pytest.mark.asyncio
    async def test_init_cannot_read_class_field_annotation(self):
        # Lock in the documented contract change: a user-written
        # ``__init__`` that tries to consume a class-body-annotated
        # field must see ``AttributeError``. The right fix is to take
        # the value as an ``__init__`` parameter instead.
        @injectable()
        class Service:
            cfg: _ConfigService

            def __init__(self) -> None:
                # This was the old guarantee — it no longer holds.
                self.observed = self.cfg.db_url  # type: ignore[has-type]

        c = DIContainer()
        c.register(_ConfigService)
        c.register(Service)
        c.compile()
        with pytest.raises(AttributeError):
            await c.resolve(Service)

    @pytest.mark.asyncio
    async def test_multiple_fields_injected(self):
        @injectable()
        class Service:
            cfg: _ConfigService
            audit: _AuditLogger

        c = DIContainer()
        c.register(_ConfigService)
        c.register(_AuditLogger)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert isinstance(svc.cfg, _ConfigService)
        assert isinstance(svc.audit, _AuditLogger)

    @pytest.mark.asyncio
    async def test_field_injection_is_per_instance(self):
        # REQUEST scope creates a fresh instance each time the request
        # cache is absent \u2014 proves field values aren't shared via some
        # class-level shortcut.
        @injectable(scope=Scope.TRANSIENT)
        class Holder:
            cfg: _ConfigService

        c = DIContainer()
        c.register(_ConfigService)
        c.register(Holder)
        c.compile()
        a = await c.resolve(Holder)
        b = await c.resolve(Holder)
        assert a is not b
        # Both hold the same (singleton) cfg but the surrounding objects
        # are distinct.
        assert a.cfg is b.cfg

    def test_class_level_default_opts_out_of_injection(self):
        # An ``attr: int = 0`` on the class body is a class attribute,
        # not a DI field. The compiler must leave it alone, which means
        # the container happily builds the service with no matching
        # provider for ``int``.
        @injectable()
        class Service:
            retries: int = 3
            cfg: _ConfigService

        c = DIContainer()
        c.register(_ConfigService)
        c.register(Service)
        c.compile()  # succeeds \u2014 no provider for int required

    def test_missing_provider_for_field_fails_compile(self):
        class _NotRegistered:
            pass

        @injectable()
        class Service:
            dep: _NotRegistered

        c = DIContainer()
        c.register(Service)
        with pytest.raises(MissingProviderError):
            c.compile()

    def test_classvar_annotations_are_skipped(self):
        # ``ClassVar[T]`` is an annotation, but the value is a class-level
        # default (the ``=`` is still forbidden for DI consideration).
        # We simply rely on the existing "default present" rule to skip
        # it \u2014 that test verifies the skip is correct.
        @injectable()
        class Service:
            version: ClassVar[str] = "1.0"
            cfg: _ConfigService

        c = DIContainer()
        c.register(_ConfigService)
        c.register(Service)
        c.compile()

    @pytest.mark.asyncio
    async def test_mix_of_init_param_and_field_annotation(self):
        @injectable()
        class Service:
            cfg: _ConfigService

            def __init__(self, audit: _AuditLogger) -> None:
                self.audit = audit

        c = DIContainer()
        c.register(_ConfigService)
        c.register(_AuditLogger)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert svc.cfg.db_url == "sqlite://:memory:"
        assert isinstance(svc.audit, _AuditLogger)

    @pytest.mark.asyncio
    async def test_user_new_receives_di_kwargs(self):
        @injectable()
        class Service:
            def __new__(cls, cfg: _ConfigService):
                inst = super().__new__(cls)
                inst.from_new = cfg.db_url
                return inst

            def __init__(self, cfg: _ConfigService) -> None:
                self.cfg = cfg

        c = DIContainer()
        c.register(_ConfigService)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert svc.from_new == "sqlite://:memory:"
        assert svc.cfg.db_url == "sqlite://:memory:"

    @pytest.mark.asyncio
    async def test_new_accepts_only_matching_kwargs(self):
        # When a class defines BOTH ``__new__`` and ``__init__``,
        # ``inspect.signature(cls)`` reports the ``__new__`` signature
        # (Python's own callable-resolution rule). The container
        # therefore plans deps from ``__new__``; ``__init__`` will
        # receive the same args via Python's standard call protocol.
        # If the deps disagree, the user should declare the contract
        # in ``__new__`` and accept it as the source of truth.
        @injectable()
        class Service:
            def __new__(cls, cfg: _ConfigService):
                inst = super().__new__(cls)
                return inst

            def __init__(self, cfg: _ConfigService) -> None:
                self.cfg = cfg

        c = DIContainer()
        c.register(_ConfigService)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert svc.cfg.db_url == "sqlite://:memory:"
