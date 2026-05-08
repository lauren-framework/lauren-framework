"""Integration tests for multi-database read/write routing (Skill 12).

Two separate SQLite in-memory databases simulate a primary (write) and a
replica (read). The DatabaseRouter owns both engines and routes accordingly.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


class ItemModel(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)


# ---------------------------------------------------------------------------
# Router (non-Lauren version for direct unit testing)
# ---------------------------------------------------------------------------


class DatabaseRouter:
    """Owns write and read engines separately."""

    def __init__(self, write_url: str, read_url: str) -> None:
        self._write_engine = create_engine(write_url)
        self._read_engine = create_engine(read_url)

    def setup_schema(self) -> None:
        Base.metadata.create_all(self._write_engine)
        Base.metadata.create_all(self._read_engine)

    @property
    def write_engine(self):
        return self._write_engine

    @property
    def read_engine(self):
        return self._read_engine

    def write_session(self) -> Session:
        return Session(self._write_engine)

    def read_session(self) -> Session:
        return Session(self._read_engine)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMultiDatabaseRouting:
    def _make_router(self) -> DatabaseRouter:
        # Each call to create_engine("sqlite:///:memory:") is independent
        router = DatabaseRouter("sqlite:///:memory:", "sqlite:///:memory:")
        router.setup_schema()
        return router

    def test_write_engine_is_separate_from_read_engine(self):
        router = self._make_router()
        assert router.write_engine is not router.read_engine

    def test_write_goes_to_primary(self):
        router = self._make_router()
        with router.write_session() as s:
            s.add(ItemModel(name="alpha"))
            s.commit()

        with router.write_session() as s:
            count = s.query(ItemModel).count()
        assert count == 1

    def test_read_engine_is_isolated_from_write(self):
        """Replica starts empty — writes to primary are not visible there."""
        router = self._make_router()
        with router.write_session() as s:
            s.add(ItemModel(name="beta"))
            s.commit()

        with router.read_session() as s:
            count = s.query(ItemModel).count()
        # Replica DB has no rows — different in-memory DB
        assert count == 0

    def test_read_engine_serves_its_own_data(self):
        """Insert directly into the replica engine — can be read back."""
        router = self._make_router()
        with router.read_session() as s:
            s.add(ItemModel(name="replica-item"))
            s.commit()

        with router.read_session() as s:
            rows = s.query(ItemModel).all()
        assert len(rows) == 1
        assert rows[0].name == "replica-item"

    def test_multiple_writes_accumulate_on_primary(self):
        router = self._make_router()
        names = ["item-1", "item-2", "item-3"]
        for name in names:
            with router.write_session() as s:
                s.add(ItemModel(name=name))
                s.commit()

        with router.write_session() as s:
            stored = [r.name for r in s.query(ItemModel).all()]

        assert stored == names
