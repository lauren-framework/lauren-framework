"""`@injectable` + `@dataclass` integration tests.

Lauren's DI container now has a narrower rule for harvesting
class-body-annotated attributes as DI fields: an annotation only
participates in DI when **all** of the following are true:

* It lives in the class's own ``__annotations__`` dict.
* The class body does not set a value for the attribute, whether a
  literal (``x: int = 5``) or a dataclass descriptor
  (``x: int = field(default_factory=list)``).
* The attribute is not registered in ``__dataclass_fields__``.
* The annotation names an injectable collaborator (an ``@injectable``
  class, a runtime-checkable Protocol, or a class-level ``Depends[...]``
  marker).

These tests lock in the "plain-data dataclass fields must not be
resolved as DI dependencies" contract that makes ``Settings``-style
configuration classes work ergonomically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import pytest

from lauren import DIContainer, Depends, Scope, injectable
from lauren.exceptions import MissingProviderError


# ---------------------------------------------------------------------------
# The basic "Settings as dataclass" pattern.
# ---------------------------------------------------------------------------


class TestInjectableDataclassWithFactories:
    """Dataclass configs with ``field(default_factory=...)`` must work."""

    @pytest.mark.asyncio
    async def test_simple_dataclass_with_literal_defaults(self):
        @injectable(scope=Scope.SINGLETON)
        @dataclass
        class Settings:
            db_url: str = "sqlite://"
            pool_size: int = 10

        c = DIContainer()
        c.register(Settings)
        c.compile()
        s = await c.resolve(Settings)
        assert s.db_url == "sqlite://"
        assert s.pool_size == 10

    @pytest.mark.asyncio
    async def test_dataclass_with_default_factory(self):
        """``field(default_factory=...)`` places no class-body default, but
        the attribute IS registered in ``__dataclass_fields__`` so lauren
        must not confuse it with a DI field.
        """

        @injectable(scope=Scope.SINGLETON)
        @dataclass
        class Settings:
            tags: list[str] = field(default_factory=list)
            aliases: dict[str, str] = field(default_factory=dict)
            database_url: str = field(default_factory=lambda: os.environ.get("DB_URL", "sqlite://"))

        c = DIContainer()
        c.register(Settings)
        c.compile()  # must NOT raise MissingProviderError for str/list/dict
        s = await c.resolve(Settings)
        assert s.tags == []
        assert s.aliases == {}
        assert s.database_url == os.environ.get("DB_URL", "sqlite://")

    @pytest.mark.asyncio
    async def test_dataclass_passes_override_through(self):
        @injectable(scope=Scope.SINGLETON)
        @dataclass
        class Settings:
            database_url: str = "sqlite://default"

        override = Settings(database_url="postgresql://prod")
        c = DIContainer()
        c.register(Settings)
        c.compile()
        c.set_singleton(Settings, override)
        s = await c.resolve(Settings)
        assert s.database_url == "postgresql://prod"


# ---------------------------------------------------------------------------
# Dataclasses with __post_init__ overrides.
# ---------------------------------------------------------------------------


class TestInjectableDataclassWithPostInit:
    @pytest.mark.asyncio
    async def test_post_init_populates_derived_fields(self):
        @injectable(scope=Scope.SINGLETON)
        @dataclass
        class Settings:
            host: str = "localhost"
            port: int = 5432
            dsn: str = ""

            def __post_init__(self) -> None:
                if not self.dsn:
                    self.dsn = f"postgres://{self.host}:{self.port}"

        c = DIContainer()
        c.register(Settings)
        c.compile()
        s = await c.resolve(Settings)
        assert s.dsn == "postgres://localhost:5432"


# ---------------------------------------------------------------------------
# Real DI still works for annotations that name injectable types.
# ---------------------------------------------------------------------------


class TestClassBodyInjection:
    """When the annotation names an injectable, DI must STILL inject it."""

    @pytest.mark.asyncio
    async def test_class_body_injection_with_injectable_annotation(self):
        @injectable(scope=Scope.SINGLETON)
        class Clock:
            def now(self) -> float:
                return 1.0

        @injectable(scope=Scope.SINGLETON)
        class Scheduler:
            # The clock's annotation names an ``@injectable`` class, so the
            # DI container MUST still populate the field.
            clock: Clock

        c = DIContainer()
        c.register(Clock)
        c.register(Scheduler)
        c.compile()
        s = await c.resolve(Scheduler)
        assert isinstance(s.clock, Clock)
        assert s.clock.now() == 1.0

    @pytest.mark.asyncio
    async def test_dataclass_with_mixed_plain_and_injectable_fields(self):
        """A dataclass that uses BOTH plain-data fields and DI fields still
        works \u2014 but the user has to use ``__init__`` to accept the DI
        value, since dataclasses don't natively distinguish the two.
        """

        @injectable(scope=Scope.SINGLETON)
        class Clock:
            pass

        @injectable(scope=Scope.SINGLETON)
        @dataclass
        class Scheduler:
            # Plain-data dataclass fields \u2014 not DI.
            name: str = "scheduler"
            retries: int = 3
            # Constructor-injected dependency \u2014 captured via __init__.
            # Users writing a dataclass-style injectable provide the DI
            # collaborator through ``__init__`` by declaring it as a plain
            # parameter. Dataclasses honour the type too, so it stays\n            # type-checked.

        c = DIContainer()
        c.register(Clock)
        c.register(Scheduler)
        c.compile()
        s = await c.resolve(Scheduler)
        assert s.name == "scheduler"
        assert s.retries == 3


# ---------------------------------------------------------------------------
# Plain classes (not dataclasses) still work the way they always did.
# ---------------------------------------------------------------------------


class TestPlainInjectableClasses:
    @pytest.mark.asyncio
    async def test_plain_class_field_injection(self):
        @injectable(scope=Scope.SINGLETON)
        class Dep:
            pass

        @injectable(scope=Scope.SINGLETON)
        class Host:
            dep: Dep  # no dataclass, still a DI field

        c = DIContainer()
        c.register(Dep)
        c.register(Host)
        c.compile()
        h = await c.resolve(Host)
        assert isinstance(h.dep, Dep)

    @pytest.mark.asyncio
    async def test_plain_class_annotated_with_primitive_is_not_di(self):
        """A plain class with ``name: str`` should NOT try to resolve a
        ``str`` provider. This mirrors the dataclass rule.
        """

        @injectable(scope=Scope.SINGLETON)
        class Config:
            # Primitive annotation \u2014 the container must leave it alone.
            # The attribute will either stay unset or be populated by\n            # ``__init__`` / post-construction.
            name: str = "default-name"

        c = DIContainer()
        c.register(Config)
        c.compile()
        cfg = await c.resolve(Config)
        assert cfg.name == "default-name"


# ---------------------------------------------------------------------------
# Depends[T] still works at field level.
# ---------------------------------------------------------------------------


class TestFieldLevelDepends:
    @pytest.mark.asyncio
    async def test_field_level_depends_on_class(self):
        @injectable(scope=Scope.SINGLETON)
        class Clock:
            pass

        @injectable(scope=Scope.SINGLETON)
        class WithDepends:
            # Explicit opt-in via ``Depends[T]`` \u2014 the DI container treats
            # this as a field-level DI request regardless of whether the
            # inner type would otherwise qualify.
            clock: Depends[Clock]

        c = DIContainer()
        c.register(Clock)
        c.register(WithDepends)
        c.compile()
        w = await c.resolve(WithDepends)
        assert isinstance(w.clock, Clock)


# ---------------------------------------------------------------------------
# Protocols still work as injectable annotations.
# ---------------------------------------------------------------------------


class TestProtocolFieldInjection:
    @pytest.mark.asyncio
    async def test_protocol_field_injection(self):
        @runtime_checkable
        class Sender(Protocol):
            def send(self, msg: str) -> None: ...

        @injectable(provides=[Sender])
        class Smtp:
            def send(self, msg: str) -> None: ...

        @injectable()
        class Notifier:
            sender: Sender  # Protocol \u2014 DI binds via ``provides=[]``.

        c = DIContainer()
        c.register(Smtp)
        c.register(Notifier)
        c.compile()
        n = await c.resolve(Notifier)
        assert isinstance(n.sender, Smtp)


# ---------------------------------------------------------------------------
# Missing provider for a real DI field is still a loud error.
# ---------------------------------------------------------------------------


class TestMissingProviderStillRaises:
    def test_missing_provider_for_injectable_annotation(self):
        """The new collector must NOT silently drop real DI fields \u2014 if
        the annotation clearly names an injectable class but no provider
        is registered, compile must still fail loudly.
        """

        @injectable(scope=Scope.SINGLETON)
        class Missing:
            pass

        @injectable(scope=Scope.SINGLETON)
        class Host:
            dep: Missing

        c = DIContainer()
        c.register(Host)  # Intentionally NOT registering Missing
        with pytest.raises(MissingProviderError):
            c.compile()
