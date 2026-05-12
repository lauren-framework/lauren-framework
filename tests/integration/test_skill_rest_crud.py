"""Integration tests for skill 21: REST CRUD Endpoint Scaffolding."""

from __future__ import annotations

from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    Json,
    Path,
    Scope,
    controller,
    delete,
    get,
    injectable,
    module,
    post,
    put,
)
from lauren.exceptions import RouteNotFoundError
from lauren.testing import TestClient
from lauren.types import Response


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class TaskRepository:
    def __init__(self) -> None:
        self._store: dict[int, dict] = {}
        self._next_id = 1

    def create(self, title: str, description: str = "") -> dict:
        task = {
            "id": self._next_id,
            "title": title,
            "description": description,
            "done": False,
        }
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
        self.get(task_id)
        del self._store[task_id]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


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


@module(controllers=[TaskController], providers=[TaskRepository])
class TaskModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(TaskModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRestCrud:
    def test_list_empty(self) -> None:
        client = build_app()
        r = client.get("/tasks/")
        assert r.status_code == 200
        assert r.json() == []

    def test_create_returns_201(self) -> None:
        client = build_app()
        r = client.post("/tasks/", json={"title": "Buy milk"})
        assert r.status_code == 201
        body = r.json()
        assert body["id"] == 1
        assert body["title"] == "Buy milk"
        assert body["description"] == ""
        assert body["done"] is False

    def test_create_with_description(self) -> None:
        client = build_app()
        r = client.post("/tasks/", json={"title": "Read book", "description": "Chapter 1"})
        assert r.status_code == 201
        assert r.json()["description"] == "Chapter 1"

    def test_list_after_create(self) -> None:
        client = build_app()
        client.post("/tasks/", json={"title": "Task A"})
        client.post("/tasks/", json={"title": "Task B"})
        r = client.get("/tasks/")
        assert r.status_code == 200
        titles = [t["title"] for t in r.json()]
        assert "Task A" in titles
        assert "Task B" in titles

    def test_get_existing_task(self) -> None:
        client = build_app()
        client.post("/tasks/", json={"title": "Groceries"})
        r = client.get("/tasks/1")
        assert r.status_code == 200
        assert r.json()["title"] == "Groceries"

    def test_get_missing_task_returns_404(self) -> None:
        client = build_app()
        r = client.get("/tasks/999")
        assert r.status_code == 404

    def test_update_title(self) -> None:
        client = build_app()
        client.post("/tasks/", json={"title": "Old title"})
        r = client.put("/tasks/1", json={"title": "New title"})
        assert r.status_code == 200
        assert r.json()["title"] == "New title"

    def test_update_done_flag(self) -> None:
        client = build_app()
        client.post("/tasks/", json={"title": "Complete me"})
        r = client.put("/tasks/1", json={"done": True})
        assert r.status_code == 200
        assert r.json()["done"] is True

    def test_update_missing_task_returns_404(self) -> None:
        client = build_app()
        r = client.put("/tasks/42", json={"title": "Ghost"})
        assert r.status_code == 404

    def test_delete_returns_204(self) -> None:
        client = build_app()
        client.post("/tasks/", json={"title": "Delete me"})
        r = client.delete("/tasks/1")
        assert r.status_code == 204

    def test_delete_removes_task(self) -> None:
        client = build_app()
        client.post("/tasks/", json={"title": "Gone"})
        client.delete("/tasks/1")
        r = client.get("/tasks/1")
        assert r.status_code == 404

    def test_delete_missing_task_returns_404(self) -> None:
        client = build_app()
        r = client.delete("/tasks/99")
        assert r.status_code == 404

    def test_ids_are_sequential(self) -> None:
        client = build_app()
        r1 = client.post("/tasks/", json={"title": "First"})
        r2 = client.post("/tasks/", json={"title": "Second"})
        assert r1.json()["id"] == 1
        assert r2.json()["id"] == 2
