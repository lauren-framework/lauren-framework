# Quickstart

A complete Lauren application — routing, DI, validation, guards, OpenAPI — in under five minutes.

## 1. Define your domain

We'll build a tiny user service. Start with a Pydantic model for the request body and a service that holds business logic.

```python title="app/models.py"
from pydantic import BaseModel

class CreateUser(BaseModel):
    name: str
    age: int

class UserOut(BaseModel):
    id: int
    name: str
    age: int
```

## 2. Add an injectable service

`@injectable` turns a plain class into a DI provider. Lauren resolves its constructor parameters automatically through the same container at startup.

```python title="app/services.py"
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class UserRepository:
    def __init__(self) -> None:
        self._users: dict[int, dict] = {}
        self._next_id = 1

    def create(self, name: str, age: int) -> dict:
        user = {"id": self._next_id, "name": name, "age": age}
        self._users[self._next_id] = user
        self._next_id += 1
        return user

    def get(self, user_id: int) -> dict | None:
        return self._users.get(user_id)
```

## 3. Write a controller

Controllers are classes decorated with `@controller(prefix)`. Their methods become HTTP handlers when decorated with `@get`, `@post`, etc. Lauren auto-promotes `@controller` classes to **request-scoped injectables**, so you can take any DI dependency in `__init__`.

```python title="app/controllers.py"
from lauren import controller, get, post, Path, Json
from lauren.exceptions import HTTPError

from .models import CreateUser, UserOut
from .services import UserRepository


class NotFoundError(HTTPError):
    status_code = 404
    code = "not_found"


@controller("/users", tags=["users"])
class UserController:
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    @get("/{id}")
    async def get_user(self, id: Path[int]) -> UserOut:
        user = self.repo.get(id)
        if user is None:
            raise NotFoundError("user not found", detail={"id": id})
        return UserOut(**user)

    @post("/")
    async def create(self, body: Json[CreateUser]) -> tuple[UserOut, int]:
        user = self.repo.create(body.name, body.age)
        return UserOut(**user), 201
```

A few things to notice:

* `id: Path[int]` extracts the path variable, parses it as `int`, and rejects non-numeric values with a 422.
* `body: Json[CreateUser]` reads the JSON body and Pydantic-validates it. Validation errors become `ExtractorError` → HTTP 422.
* The first handler returns a Pydantic model directly — Lauren auto-serializes via `model_dump(mode="json")`.
* The second returns `(model, 201)` — Lauren builds a 201 response with the model as JSON. You can also return `(body, status, headers)`.

## 4. Wire everything into a module

Modules group controllers and providers. They're the unit of dependency visibility — a provider declared here is visible to everything declared here, plus anything explicitly imported from another module's `exports`.

```python title="app/main.py"
import asyncio
from lauren import LaurenFactory, module
from lauren.logging import default_logger

from .controllers import UserController
from .services import UserRepository


@module(
    controllers=[UserController],
    providers=[UserRepository],
)
class AppModule:
    pass


async def build_app():
    return await LaurenFactory.create(AppModule, logger=default_logger())


app = asyncio.run(build_app())
```

## 5. Run it

```bash
uvicorn app.main:app --reload
```

Then:

```bash
$ curl -X POST localhost:8000/users/ \
    -H 'Content-Type: application/json' \
    -d '{"name":"Ada","age":36}'
{"id":1,"name":"Ada","age":36}

$ curl localhost:8000/users/1
{"id":1,"name":"Ada","age":36}

$ curl -i localhost:8000/users/999
HTTP/1.1 404 Not Found
{"error":{"code":"not_found","message":"user not found","detail":{"id":999}}}
```

## 6. Inspect the OpenAPI

```python
import json
print(json.dumps(app.openapi(), indent=2))
```

You'll see an OpenAPI 3.1 document with both routes, the `UserOut` and `CreateUser` schemas under `components.schemas`, and the `users` tag.

## 7. Test it

Lauren ships an in-process `TestClient` — no need for a real socket.

```python title="tests/test_users.py"
from lauren.testing import TestClient
from app.main import app

def test_create_then_fetch():
    c = TestClient(app)
    r = c.post("/users/", json={"name": "Ada", "age": 36})
    assert r.status_code == 201
    user_id = r.json()["id"]

    r = c.get(f"/users/{user_id}")
    assert r.status_code == 200
    assert r.json()["name"] == "Ada"

def test_404_envelope():
    c = TestClient(app)
    r = c.get("/users/999")
    assert r.status_code == 404
    assert r.json() == {
        "error": {"code": "not_found", "message": "user not found", "detail": {"id": 999}}
    }
```

## What you got

In a few dozen lines:

* A radix-tree-routed HTTP API with O(depth) lookup;
* A DI container that wires `UserRepository` into `UserController` automatically;
* Pydantic-validated request bodies, typed path parameters;
* Structured error envelopes;
* Auto-generated OpenAPI 3.1;
* In-process tests with no real network involved.

Next up: read [Why Lauren?](why-lauren.md) for the design philosophy, or jump to [Prominent Features](features.md) for a guided tour.
