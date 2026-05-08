# Authenticated CRUD Controller

Full CRUD with JWT guard, Pydantic request/response models, and a DI service layer.

```python
# app/items/schemas.py
from pydantic import BaseModel

class CreateItem(BaseModel):
    name: str
    price: float

class ItemOut(BaseModel):
    id: int
    name: str
    price: float
    owner_id: str
```

```python
# app/items/item_service.py
from lauren import injectable, Scope
from lauren_guards import JwtBearerGuard

@injectable(scope=Scope.SINGLETON)
class ItemService:
    def __init__(self) -> None:
        self._store: dict[int, dict] = {}
        self._next_id = 1

    def create(self, owner_id: str, data: dict) -> dict:
        item = {"id": self._next_id, "owner_id": owner_id, **data}
        self._store[self._next_id] = item
        self._next_id += 1
        return item

    def get(self, item_id: int) -> dict | None:
        return self._store.get(item_id)

    def list_for_user(self, owner_id: str) -> list[dict]:
        return [i for i in self._store.values() if i["owner_id"] == owner_id]

    def delete(self, item_id: int, owner_id: str) -> bool:
        item = self._store.get(item_id)
        if not item or item["owner_id"] != owner_id:
            return False
        del self._store[item_id]
        return True
```

```python
# app/items/item_controller.py
from lauren import controller, get, post, delete, use_guards, Json
from lauren.exceptions import NotFoundError, ForbiddenError
from lauren.types import ExecutionContext
from lauren_guards import JwtBearerGuard

from app.items.schemas import CreateItem, ItemOut
from app.items.item_service import ItemService

@use_guards(JwtBearerGuard)
@controller("/api/items")
class ItemController:
    def __init__(self, svc: ItemService) -> None:
        self._svc = svc

    @get("/")
    async def list_items(self, exec_ctx: ExecutionContext) -> list[ItemOut]:
        owner_id = exec_ctx.request.state.user_id
        return self._svc.list_for_user(owner_id)

    @post("/")
    async def create_item(self, body: Json[CreateItem], exec_ctx: ExecutionContext) -> ItemOut:
        owner_id = exec_ctx.request.state.user_id
        item = self._svc.create(owner_id, body.model_dump())
        return ItemOut(**item), 201

    @get("/{item_id}")
    async def get_item(self, item_id: int, exec_ctx: ExecutionContext) -> ItemOut:
        item = self._svc.get(item_id)
        if not item:
            raise NotFoundError(f"Item {item_id} not found")
        if item["owner_id"] != exec_ctx.request.state.user_id:
            raise ForbiddenError("Not your item")
        return ItemOut(**item)

    @delete("/{item_id}")
    async def delete_item(self, item_id: int, exec_ctx: ExecutionContext) -> dict:
        owner_id = exec_ctx.request.state.user_id
        if not self._svc.delete(item_id, owner_id):
            raise NotFoundError(f"Item {item_id} not found or not owned by you")
        return {}, 204
```

```python
# app/items/item_module.py
from lauren import module
from app.items.item_controller import ItemController
from app.items.item_service import ItemService

@module(controllers=[ItemController], providers=[ItemService])
class ItemModule: ...
```

`JwtBearerGuard` sets `request.state.user_id` (from the JWT `sub` claim) before any handler runs. Identity is never read from the request body.
