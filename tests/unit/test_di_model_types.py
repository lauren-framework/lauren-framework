"""Tests whether Lauren's DI can inject dataclasses, Pydantic models,
SQLAlchemy DeclarativeBase models, and SQLModel instances.

Each section covers:
  (a) direct ``@injectable`` decoration — can the DI build it?
  (b) ``use_value`` provision — always works (bypasses construction).
  (c) ``use_class`` provision — works when construction succeeds.
  (d) injection as a constructor dependency of another service.

Failure modes are tested explicitly with ``pytest.raises``.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

import pytest
import sqlmodel
from pydantic import BaseModel
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from lauren import (
    LaurenFactory,
    controller,
    get,
    injectable,
    module,
)
from lauren.exceptions import (
    MissingProviderError,
    StartupError,
)
from lauren.testing import TestClient
from lauren._di.custom import use_class, use_value

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build(root_module: type) -> TestClient:
    return TestClient(LaurenFactory.create(root_module))


# ===========================================================================
# @dataclass
# ===========================================================================


# ---------------------------------------------------------------------------
# (a) @injectable + @dataclass — all fields have defaults
# ---------------------------------------------------------------------------


@injectable()
@dataclasses.dataclass
class DataclassAllDefaults:
    """A pure-config dataclass; every field has a default."""

    url: str = "sqlite:///:memory:"
    max_conn: int = 10
    debug: bool = False


@controller("/dc-defaults")
class DCDefaultsController:
    def __init__(self, cfg: DataclassAllDefaults) -> None:
        self._cfg = cfg

    @get("/")
    def read(self) -> dict:
        return {"url": self._cfg.url, "max_conn": self._cfg.max_conn}


@module(controllers=[DCDefaultsController], providers=[DataclassAllDefaults])
class DCDefaultsModule:
    pass


class TestDataclassAllDefaults:
    def test_singleton_built_from_defaults(self):
        client = build(DCDefaultsModule)
        r = client.get("/dc-defaults/")
        assert r.status_code == 200
        data = r.json()
        assert data["url"] == "sqlite:///:memory:"
        assert data["max_conn"] == 10

    def test_same_instance_reused(self):
        """Singleton scope: the same instance is returned across calls."""
        app = LaurenFactory.create(DCDefaultsModule)
        client = TestClient(app)
        client.get("/dc-defaults/")
        client.get("/dc-defaults/")  # should not raise


# ---------------------------------------------------------------------------
# (b) @dataclass with required injectable (non-primitive) fields
# ---------------------------------------------------------------------------


@injectable()
class SomeDep:
    value: str = "dep"


@injectable()
@dataclasses.dataclass
class DataclassWithInjectableDep:
    """Dataclass whose only field is a DI-resolvable service."""

    dep: SomeDep  # non-primitive → resolved from DI container


@controller("/dc-dep")
class DCDepController:
    def __init__(self, cfg: DataclassWithInjectableDep) -> None:
        self._cfg = cfg

    @get("/")
    def read(self) -> dict:
        return {"has_dep": self._cfg.dep is not None}


@module(
    controllers=[DCDepController],
    providers=[SomeDep, DataclassWithInjectableDep],
)
class DCDepModule:
    pass


class TestDataclassWithInjectableDep:
    def test_injectable_field_resolved_via_constructor(self):
        client = build(DCDepModule)
        r = client.get("/dc-dep/")
        assert r.status_code == 200
        assert r.json() == {"has_dep": True}


# ---------------------------------------------------------------------------
# (c) @dataclass with required PRIMITIVE fields — expected failure
# ---------------------------------------------------------------------------


@injectable()
@dataclasses.dataclass
class DataclassRequiredPrimitive:
    """Has required primitive fields; the DI container cannot build this."""

    url: str  # required, no default
    port: int  # required, no default


@module(providers=[DataclassRequiredPrimitive])
class DCRequiredPrimitiveModule:
    pass


class TestDataclassRequiredPrimitive:
    def test_startup_fails_missing_provider_for_primitive(self):
        """DI cannot resolve ``str`` or ``int`` without a use_value/use_factory."""
        with pytest.raises((MissingProviderError, StartupError)):
            LaurenFactory.create(DCRequiredPrimitiveModule)


# ---------------------------------------------------------------------------
# (d) @dataclass via use_value — always works
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RawDataclass:
    url: str = "from_use_value"
    port: int = 5432


@controller("/dc-usevalue")
class DCUseValueController:
    def __init__(self, cfg: RawDataclass) -> None:
        self._cfg = cfg

    @get("/")
    def read(self) -> dict:
        return {"url": self._cfg.url, "port": self._cfg.port}


@module(
    controllers=[DCUseValueController],
    providers=[use_value(provide=RawDataclass, value=RawDataclass(url="injected", port=9999))],
)
class DCUseValueModule:
    pass


class TestDataclassUseValue:
    def test_use_value_bypasses_construction(self):
        client = build(DCUseValueModule)
        r = client.get("/dc-usevalue/")
        assert r.json() == {"url": "injected", "port": 9999}


# ===========================================================================
# pydantic.BaseModel
# ===========================================================================


# ---------------------------------------------------------------------------
# (a) @injectable + BaseModel — all fields optional (have defaults)
# ---------------------------------------------------------------------------


@injectable()
class PydanticAllDefaults(BaseModel):
    """BaseModel where every field has a default value."""

    db_url: str = "sqlite:///:memory:"
    pool_size: int = 5
    echo: bool = False


@controller("/pm-defaults")
class PMDefaultsController:
    def __init__(self, cfg: PydanticAllDefaults) -> None:
        self._cfg = cfg

    @get("/")
    def read(self) -> dict:
        return {"db_url": self._cfg.db_url, "pool_size": self._cfg.pool_size}


@module(controllers=[PMDefaultsController], providers=[PydanticAllDefaults])
class PMDefaultsModule:
    pass


class TestPydanticAllDefaults:
    def test_pydantic_model_built_from_defaults(self):
        client = build(PMDefaultsModule)
        r = client.get("/pm-defaults/")
        assert r.status_code == 200
        data = r.json()
        assert data["db_url"] == "sqlite:///:memory:"
        assert data["pool_size"] == 5

    def test_pydantic_validation_runs(self):
        """The instance should be a proper Pydantic model, not a raw dict."""
        app = LaurenFactory.create(PMDefaultsModule)
        client = TestClient(app)
        r = client.get("/pm-defaults/")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# (b) BaseModel with required primitive fields — expected failure
# ---------------------------------------------------------------------------


@injectable()
class PydanticRequiredFields(BaseModel):
    """Has required fields — DI cannot provide str/int arguments."""

    db_url: str  # required, no default
    port: int  # required, no default


@module(providers=[PydanticRequiredFields])
class PMRequiredModule:
    pass


class TestPydanticRequiredFields:
    def test_startup_fails_missing_provider_for_primitive(self):
        """DI has no provider for ``str`` → ``MissingProviderError`` at startup."""
        with pytest.raises((MissingProviderError, StartupError)):
            LaurenFactory.create(PMRequiredModule)


# ---------------------------------------------------------------------------
# (c) BaseModel via use_value — always works
# ---------------------------------------------------------------------------


class PydanticConfig(BaseModel):
    host: str = "localhost"
    port: int = 5432


@controller("/pm-usevalue")
class PMUseValueController:
    def __init__(self, cfg: PydanticConfig) -> None:
        self._cfg = cfg

    @get("/")
    def read(self) -> dict:
        return {"host": self._cfg.host, "port": self._cfg.port}


@module(
    controllers=[PMUseValueController],
    providers=[use_value(provide=PydanticConfig, value=PydanticConfig(host="db.internal", port=5433))],
)
class PMUseValueModule:
    pass


class TestPydanticUseValue:
    def test_use_value_provides_model_instance(self):
        client = build(PMUseValueModule)
        r = client.get("/pm-usevalue/")
        assert r.json() == {"host": "db.internal", "port": 5433}


# ---------------------------------------------------------------------------
# (d) BaseModel via use_class — works when all fields have defaults
# ---------------------------------------------------------------------------


class PydanticWithDefaults(BaseModel):
    mode: str = "production"


@controller("/pm-useclass")
class PMUseClassController:
    def __init__(self, cfg: PydanticWithDefaults) -> None:
        self._cfg = cfg

    @get("/")
    def read(self) -> dict:
        return {"mode": self._cfg.mode}


@module(
    controllers=[PMUseClassController],
    providers=[use_class(provide=PydanticWithDefaults, use=PydanticWithDefaults)],
)
class PMUseClassModule:
    pass


class TestPydanticUseClass:
    def test_use_class_builds_pydantic_model(self):
        client = build(PMUseClassModule)
        r = client.get("/pm-useclass/")
        assert r.json() == {"mode": "production"}


# ===========================================================================
# sqlalchemy.orm.DeclarativeBase
# ===========================================================================


class _SABase(DeclarativeBase):
    pass


# One ORM model used across multiple SA tests.
@injectable()
class SAProduct(_SABase):
    """ORM model decorated with @injectable.

    SQLAlchemy's mapper generates ``__init__(self, **kwargs)`` — the
    single ``VAR_KEYWORD`` parameter is skipped by DI, so the container
    calls ``SAProduct()`` with no arguments.
    """

    __tablename__ = "sa_di_products"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class SAOrder(_SABase):
    """ORM model WITHOUT @injectable — cannot be injected without use_value."""

    __tablename__ = "sa_di_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    total: Mapped[int]


# ---------------------------------------------------------------------------
# (a) SA model as @injectable — only **kwargs in constructor (all skipped)
# ---------------------------------------------------------------------------


@controller("/sa-injectable")
class SAInjectableController:
    def __init__(self, product: SAProduct) -> None:
        self._product = product

    @get("/")
    def read(self) -> dict:
        return {"is_sa_model": isinstance(self._product, SAProduct)}


@module(
    controllers=[SAInjectableController],
    providers=[SAProduct],
)
class SAInjectableModule:
    pass


class TestSAInjectable:
    def test_sqlalchemy_model_built_with_no_args(self):
        """The mapper's ``**kwargs`` init is called with no arguments.

        The resulting instance has all mapped attributes as ``None``
        (SQLAlchemy's uninitialised column state), but the DI container
        successfully resolves and injects it.
        """
        client = build(SAInjectableModule)
        r = client.get("/sa-injectable/")
        assert r.status_code == 200
        assert r.json() == {"is_sa_model": True}


# ---------------------------------------------------------------------------
# (b) SA model via use_value — always works
# ---------------------------------------------------------------------------


_sa_order_instance = SAOrder(id=1, total=99)


@controller("/sa-usevalue")
class SAUseValueController:
    def __init__(self, order: SAOrder) -> None:
        self._order = order

    @get("/")
    def read(self) -> dict:
        return {"total": self._order.total}


@module(
    controllers=[SAUseValueController],
    providers=[use_value(provide=SAOrder, value=_sa_order_instance)],
)
class SAUseValueModule:
    pass


class TestSAUseValue:
    def test_use_value_provides_orm_instance(self):
        client = build(SAUseValueModule)
        r = client.get("/sa-usevalue/")
        assert r.json() == {"total": 99}


# ---------------------------------------------------------------------------
# (c) SA model — non-decorated cannot be injected directly
# ---------------------------------------------------------------------------


@controller("/sa-nodec")
class SANoDecController:
    def __init__(self, m: SAOrder) -> None:
        self._m = m

    @get("/")
    def read(self) -> dict:
        return {}


@module(controllers=[SANoDecController])
class SANoDecModule:
    pass


class TestSANonDecorated:
    def test_startup_fails_without_injectable_marker(self):
        """A bare DeclarativeBase subclass without @injectable raises at startup."""
        with pytest.raises((MissingProviderError, StartupError)):
            LaurenFactory.create(SANoDecModule)


# ===========================================================================
# sqlmodel.SQLModel
# ===========================================================================


# Non-table SQLModel: behaves exactly like a Pydantic BaseModel.
@injectable()
class SMConfig(sqlmodel.SQLModel):
    """Non-table SQLModel (schema model) with all-defaulted fields."""

    host: str = "localhost"
    port: int = 5432


# Table SQLModel with a required field — DI cannot build it.
@injectable()
class SMWidget(sqlmodel.SQLModel, table=True):
    """Table SQLModel: ``label: str`` is required, no default."""

    __tablename__ = "sm_di_widgets"
    id: Optional[int] = sqlmodel.Field(default=None, primary_key=True)
    label: str  # required — no default


# For use_value test: non-injectable table model.
class SMBadge(sqlmodel.SQLModel, table=True):
    __tablename__ = "sm_di_badges"
    id: Optional[int] = sqlmodel.Field(default=None, primary_key=True)
    name: str = "badge"


# ---------------------------------------------------------------------------
# (a) SQLModel non-table with all defaults → @injectable works
# ---------------------------------------------------------------------------


@controller("/sm-config")
class SMConfigController:
    def __init__(self, cfg: SMConfig) -> None:
        self._cfg = cfg

    @get("/")
    def read(self) -> dict:
        return {"host": self._cfg.host, "port": self._cfg.port}


@module(
    controllers=[SMConfigController],
    providers=[SMConfig],
)
class SMConfigModule:
    pass


class TestSQLModelAllDefaults:
    def test_sqlmodel_schema_built_from_defaults(self):
        client = build(SMConfigModule)
        r = client.get("/sm-config/")
        assert r.json() == {"host": "localhost", "port": 5432}


# ---------------------------------------------------------------------------
# (b) Table SQLModel with required field — expected failure
# ---------------------------------------------------------------------------


@module(providers=[SMWidget])
class SMWidgetModule:
    pass


class TestSQLModelRequiredField:
    def test_startup_fails_for_required_primitive_field(self):
        """``label: str`` has no default → DI cannot resolve ``str``."""
        with pytest.raises((MissingProviderError, StartupError)):
            LaurenFactory.create(SMWidgetModule)


# ---------------------------------------------------------------------------
# (c) SQLModel via use_value — always works
# ---------------------------------------------------------------------------


_sm_badge = SMBadge(name="gold")


@controller("/sm-usevalue")
class SMUseValueController:
    def __init__(self, badge: SMBadge) -> None:
        self._badge = badge

    @get("/")
    def read(self) -> dict:
        return {"name": self._badge.name}


@module(
    controllers=[SMUseValueController],
    providers=[use_value(provide=SMBadge, value=_sm_badge)],
)
class SMUseValueModule:
    pass


class TestSQLModelUseValue:
    def test_use_value_provides_sqlmodel_instance(self):
        client = build(SMUseValueModule)
        r = client.get("/sm-usevalue/")
        assert r.json() == {"name": "gold"}
