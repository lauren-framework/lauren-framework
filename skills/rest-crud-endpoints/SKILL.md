---
name: rest-crud-endpoints
description: Scaffolds a complete REST CRUD endpoint set for a resource in a Lauren framework application. Use when building list, create, read, update, and delete routes with Pydantic validation, proper HTTP status codes (201 for create, 204 for delete), and an in-memory or database-backed repository.
---

> Use `codemap find "controller"` to locate existing controllers before adding new ones.

# REST CRUD Endpoint Scaffolding

This skill scaffolds a complete CRUD API for a resource. The pattern uses:

- A **repository** injectable singleton that owns the data store (swap with a DB session for production).
- **Pydantic models** for create/update/response shapes — separated so clients cannot inject `id` or `done` fields.
- Standard HTTP status codes: `200` list/get/update, `201` create, `204` delete, `404` not found.

## Pydantic models

```python
from __future__ import annotations
from pydantic import BaseModel

class CreateTask(BaseModel):
    title: str
    description: str = ""

class UpdateTask(BaseModel):
    title: str | None = None
    description: str | None = None
    done: bool | None = None

class TaskResponse(BaseModel):
    id: int
    title: str
    description: str
    done: bool
```

## Repository

```python
from lauren import injectable, Scope
from lauren.exceptions import RouteNotFoundError

@injectable(scope=Scope.SINGLETON)
class TaskRepository:
    def __init__(self) -> None:
        self._store: dict[int, dict] = {}
        self._next_id = 1

    def create(self, title: str, description: str = "") -> dict:
        task = {"id": self._next_id, "title": title, "description": description, "done": False}
        self._store[self._next_id] = task
        self._next_id += 1
        return task

    def get(self, task_id: int) -> dict:
        if task_id not in self._store:
            raise RouteNotFoundError(f"Task {task_id} not found")
        return self._store[task_id]

    def list_all(self) -> list[dict]:
        return list(self._store.values())

    def update(self, task_id: int, **fields) -> dict:
        task = self.get(task_id)
        task.update({k: v for k, v in fields.items() if v is not None})
        return task

    def delete(self, task_id: int) -> None:
        self.get(task_id)  # raises RouteNotFoundError if missing
        del self._store[task_id]
```

## Controller

```python
from lauren import controller, get, post, put, delete, Path, Json
from lauren.types import Response

@controller("/tasks")
class TaskController:
    def __init__(self, repo: TaskRepository) -> None:
        self._repo = repo

    @get("/")
    async def list_tasks(self) -> list[TaskResponse]:
        return [TaskResponse(**t) for t in self._repo.list_all()]

    @post("/")
    async def create_task(self, body: Json[CreateTask]) -> Response:
        task = self._repo.create(body.title, body.description)
        return Response.json(TaskResponse(**task).model_dump(), status=201)

    @get("/{task_id}")
    async def get_task(self, task_id: Path[int]) -> TaskResponse:
        return TaskResponse(**self._repo.get(task_id))

    @put("/{task_id}")
    async def update_task(self, task_id: Path[int], body: Json[UpdateTask]) -> TaskResponse:
        task = self._repo.update(task_id, **body.model_dump(exclude_none=True))
        return TaskResponse(**task)

    @delete("/{task_id}")
    async def delete_task(self, task_id: Path[int]) -> Response:
        self._repo.delete(task_id)
        return Response.no_content()
```

## Module wiring

```python
from lauren import module

@module(controllers=[TaskController], providers=[TaskRepository])
class TaskModule:
    pass
```

## Key points

- `RouteNotFoundError` (HTTP 404) is in `lauren.exceptions` and is the correct error for missing resources.
- Returning `Response.no_content()` produces HTTP 204 with no body.
- Use `Response.json(..., status=201)` for created resources.
- `UpdateTask` uses `exclude_none=True` so only supplied fields are patched.
- To use a real database, swap `TaskRepository` for a service that injects a DB session provider.
