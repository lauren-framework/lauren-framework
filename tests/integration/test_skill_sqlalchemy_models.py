"""Integration tests for the SQLAlchemy model definition skill."""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from lauren import (
    Json,
    LaurenFactory,
    Path,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
    post_construct,
    pre_destruct,
)
from lauren.testing import TestClient
from pydantic import BaseModel


class Base(DeclarativeBase):
    pass


class UserModel(Base):
    __tablename__ = "users_skill_test"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False)


@injectable(scope=Scope.SINGLETON)
class DatabaseService:
    def __init__(self) -> None:
        self._engine = create_engine("sqlite:///:memory:", echo=False)

    @post_construct
    async def create_tables(self) -> None:
        Base.metadata.create_all(self._engine)

    @pre_destruct
    async def drop_tables(self) -> None:
        Base.metadata.drop_all(self._engine)

    def get_session(self) -> Session:
        return Session(self._engine)

    def create_user(self, name: str, email: str) -> UserModel:
        with self.get_session() as session:
            user = UserModel(name=name, email=email)
            session.add(user)
            session.commit()
            session.refresh(user)
            return user

    def get_user(self, user_id: int) -> UserModel | None:
        with self.get_session() as session:
            return session.get(UserModel, user_id)

    def list_users(self) -> list[UserModel]:
        with self.get_session() as session:
            return list(session.query(UserModel).all())

    def delete_user(self, user_id: int) -> bool:
        with self.get_session() as session:
            user = session.get(UserModel, user_id)
            if user is None:
                return False
            session.delete(user)
            session.commit()
            return True


class CreateUserBody(BaseModel):
    name: str
    email: str


@controller("/users")
class UserController:
    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    @get("/")
    async def list_users(self) -> list[dict]:
        users = self._db.list_users()
        return [{"id": u.id, "name": u.name, "email": u.email} for u in users]

    @post("/")
    async def create_user(self, body: Json[CreateUserBody]) -> dict:
        user = self._db.create_user(body.name, body.email)
        return {"id": user.id, "name": user.name, "email": user.email}

    @get("/{user_id}")
    async def get_user(self, user_id: Path[int]) -> dict:
        from lauren.exceptions import RouteNotFoundError

        user = self._db.get_user(user_id)
        if user is None:
            raise RouteNotFoundError(f"User {user_id} not found")
        return {"id": user.id, "name": user.name, "email": user.email}


@module(controllers=[UserController], providers=[DatabaseService])
class DatabaseModule:
    pass


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(DatabaseModule))


class TestSQLAlchemyModels:
    def test_create_user_returns_id(self):
        client = build_app()
        r = client.post("/users/", json={"name": "Alice", "email": "alice@example.com"})
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Alice"
        assert data["email"] == "alice@example.com"
        assert isinstance(data["id"], int)
        assert data["id"] >= 1

    def test_get_user_by_id(self):
        client = build_app()
        create_r = client.post("/users/", json={"name": "Bob", "email": "bob@example.com"})
        user_id = create_r.json()["id"]

        r = client.get(f"/users/{user_id}")
        assert r.status_code == 200
        assert r.json()["name"] == "Bob"

    def test_list_users(self):
        client = build_app()
        client.post("/users/", json={"name": "Carol", "email": "carol@example.com"})
        client.post("/users/", json={"name": "Dave", "email": "dave@example.com"})

        r = client.get("/users/")
        assert r.status_code == 200
        names = [u["name"] for u in r.json()]
        assert "Carol" in names
        assert "Dave" in names

    def test_get_nonexistent_user_returns_404(self):
        client = build_app()
        r = client.get("/users/99999")
        assert r.status_code == 404

    def test_tables_created_at_startup(self):
        """Verifies post_construct fires and creates tables before first request."""
        client = build_app()
        # If tables didn't exist, this would raise an OperationalError
        r = client.get("/users/")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
