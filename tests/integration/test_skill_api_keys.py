"""Integration tests for the API key generation and scoped permissions skill."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field


from lauren import (
    ExecutionContext,
    Json,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
    set_metadata,
    use_guards,
)
from lauren.exceptions import ForbiddenError, UnauthorizedError
from lauren.testing import TestClient
from pydantic import BaseModel

SCOPE_KEY = "required_scope"
API_KEY_HEADER = "x-api-key"


@dataclass
class ApiKeyRecord:
    key_hash: str
    owner: str
    scopes: list[str] = field(default_factory=list)


@injectable(scope=Scope.SINGLETON)
class ApiKeyService:
    def __init__(self) -> None:
        self._store: dict[str, ApiKeyRecord] = {}

    def create_key(self, owner: str, scopes: list[str]) -> str:
        raw = os.urandom(32).hex()
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        self._store[key_hash] = ApiKeyRecord(key_hash=key_hash, owner=owner, scopes=scopes)
        return raw

    def lookup(self, raw_key: str) -> ApiKeyRecord | None:
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        return self._store.get(key_hash)

    def has_scope(self, record: ApiKeyRecord, scope: str) -> bool:
        return scope in record.scopes


@injectable(scope=Scope.SINGLETON)
class ApiKeyGuard:
    def __init__(self, svc: ApiKeyService) -> None:
        self._svc = svc

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        raw_key = ctx.request.headers.get(API_KEY_HEADER, "")
        if not raw_key:
            raise UnauthorizedError("Missing X-API-Key header")

        record = self._svc.lookup(raw_key)
        if record is None:
            raise UnauthorizedError("Invalid API key")

        required_scope = ctx.get_metadata(SCOPE_KEY, "")
        if required_scope and not self._svc.has_scope(record, required_scope):
            raise ForbiddenError(
                f"API key lacks scope '{required_scope}'",
                detail={"required": required_scope, "owner": record.owner},
            )

        ctx.request.state.api_key_owner = record.owner
        return True


class CreateKeyBody(BaseModel):
    owner: str
    scopes: list[str]


@controller("/keys")
class KeyManagementController:
    def __init__(self, svc: ApiKeyService) -> None:
        self._svc = svc

    @post("/create")
    async def create(self, body: Json[CreateKeyBody]) -> dict:
        raw = self._svc.create_key(body.owner, body.scopes)
        return {"api_key": raw}


@use_guards(ApiKeyGuard)
@controller("/data")
class DataController:
    @get("/read")
    @set_metadata(SCOPE_KEY, "read")
    async def read_data(self, ctx: ExecutionContext) -> dict:
        return {"data": "some data", "owner": ctx.request.state.api_key_owner}

    @get("/write")
    @set_metadata(SCOPE_KEY, "write")
    async def write_data(self) -> dict:
        return {"status": "written"}

    @get("/open")
    async def open_data(self) -> dict:
        return {"data": "open"}


@module(
    controllers=[KeyManagementController, DataController],
    providers=[ApiKeyService, ApiKeyGuard],
)
class ApiKeyModule:
    pass


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(ApiKeyModule))


class TestApiKeyAuth:
    def _create_key(self, client: TestClient, owner: str, scopes: list[str]) -> str:
        r = client.post("/keys/create", json={"owner": owner, "scopes": scopes})
        assert r.status_code == 200
        return r.json()["api_key"]

    def test_key_creation_returns_raw_key(self):
        client = build_app()
        r = client.post("/keys/create", json={"owner": "svc-a", "scopes": ["read"]})
        assert r.status_code == 200
        assert len(r.json()["api_key"]) > 10

    def test_read_scope_allows_read_endpoint(self):
        client = build_app()
        key = self._create_key(client, "reader", ["read"])
        r = client.get("/data/read", headers={API_KEY_HEADER: key})
        assert r.status_code == 200
        assert r.json()["owner"] == "reader"

    def test_read_scope_denied_on_write_endpoint(self):
        client = build_app()
        key = self._create_key(client, "reader", ["read"])
        r = client.get("/data/write", headers={API_KEY_HEADER: key})
        assert r.status_code == 403

    def test_write_scope_allows_write_endpoint(self):
        client = build_app()
        key = self._create_key(client, "writer", ["read", "write"])
        r = client.get("/data/write", headers={API_KEY_HEADER: key})
        assert r.status_code == 200

    def test_missing_key_returns_401(self):
        client = build_app()
        r = client.get("/data/read")
        assert r.status_code == 401

    def test_invalid_key_returns_401(self):
        client = build_app()
        r = client.get("/data/read", headers={API_KEY_HEADER: "fake-key-xyz"})
        assert r.status_code == 401

    def test_open_endpoint_requires_valid_key_but_no_scope(self):
        client = build_app()
        key = self._create_key(client, "any", [])
        r = client.get("/data/open", headers={API_KEY_HEADER: key})
        assert r.status_code == 200

    def test_each_created_key_is_unique(self):
        client = build_app()
        key1 = self._create_key(client, "svc1", ["read"])
        key2 = self._create_key(client, "svc2", ["read"])
        assert key1 != key2
