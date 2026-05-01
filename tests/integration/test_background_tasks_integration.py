"""Integration tests for BackgroundTasks.

Drives real ``LaurenApp`` instances through ``TestClient``. Background tasks run
synchronously in the same event loop before ``TestClient.get/post/...`` returns, so
side effects are directly assertable without extra waiting.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from lauren import (
    BackgroundTaskComplete,
    BackgroundTaskFailed,
    BackgroundTaskStarted,
    BackgroundTasks,
    LaurenFactory,
    TaskHandle,
    controller,
    get,
    injectable,
    module,
    post,
    use_guards,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(ctrl_cls: type, providers: list | None = None) -> TestClient:
    @module(controllers=[ctrl_cls], providers=providers or [])
    class M:
        pass

    return TestClient(LaurenFactory.create(M))


class Item(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# TestBackgroundTasksBasic
# ---------------------------------------------------------------------------


class TestBackgroundTasksBasic:
    def test_no_param_no_tasks_run(self) -> None:
        """Handler without BackgroundTasks — baseline, no errors."""
        results: list[int] = []

        @controller("/")
        class C:
            @get("/ping")
            async def ping(self) -> dict:
                return {"ok": True}

        client = _build(C)
        r = client.get("/ping")
        assert r.status_code == 200
        assert results == []

    def test_async_task_runs_after_response(self) -> None:
        results: list[str] = []

        async def notify(msg: str) -> None:
            results.append(msg)

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(notify, "sent")
                return {"ok": True}

        client = _build(C)
        r = client.post("/")
        assert r.status_code == 200
        assert results == ["sent"]

    def test_sync_task_runs_after_response(self) -> None:
        results: list[str] = []

        def sync_work(msg: str) -> None:
            results.append(msg)

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(sync_work, "sync")
                return {"ok": True}

        client = _build(C)
        r = client.post("/")
        assert r.status_code == 200
        assert results == ["sync"]

    def test_response_not_delayed_by_task(self) -> None:
        """Response body is correct even when a task is queued."""

        @controller("/")
        class C:
            @get("/")
            async def index(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: None)
                return {"value": 42}

        client = _build(C)
        r = client.get("/")
        assert r.json() == {"value": 42}

    def test_multiple_tasks_all_run(self) -> None:
        results: list[int] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: results.append(1))
                tasks.add_task(lambda: results.append(2))
                tasks.add_task(lambda: results.append(3))
                return {}

        client = _build(C)
        client.post("/")
        assert sorted(results) == [1, 2, 3]

    def test_tasks_run_in_declaration_order(self) -> None:
        order: list[int] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: order.append(1))
                tasks.add_task(lambda: order.append(2))
                tasks.add_task(lambda: order.append(3))
                return {}

        client = _build(C)
        client.post("/")
        assert order == [1, 2, 3]

    def test_handle_is_done_after_request_completes(self) -> None:
        handles: list[TaskHandle] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                h = tasks.add_task(lambda: None)
                handles.append(h)
                return {}

        client = _build(C)
        client.post("/")
        assert handles[0].status == "done"


# ---------------------------------------------------------------------------
# TestBackgroundTasksEdgeCases
# ---------------------------------------------------------------------------


class TestBackgroundTasksEdgeCases:
    def test_task_exception_does_not_affect_response_status(self) -> None:
        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: (_ for _ in ()).throw(RuntimeError("bad")))
                return {"ok": True}

        client = _build(C)
        r = client.post("/")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_task_exception_does_not_stop_other_tasks(self) -> None:
        results: list[int] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: (_ for _ in ()).throw(RuntimeError("bad")))
                tasks.add_task(lambda: results.append(99))
                return {}

        client = _build(C)
        client.post("/")
        assert results == [99]

    def test_guard_rejection_no_tasks_run(self) -> None:
        """Guard returns False (403) — handler never runs, no tasks queued."""
        results: list[int] = []

        class RejectGuard:
            async def can_activate(self, ctx: Any) -> bool:
                return False

        @use_guards(RejectGuard)
        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: results.append(1))
                return {}

        client = _build(C)
        r = client.post("/")
        assert r.status_code == 403
        assert results == []

    def test_handler_raises_exception_tasks_added_before_raise_still_run(
        self,
    ) -> None:
        """Tasks added before a handler exception still run."""
        results: list[int] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: results.append(42))
                raise ValueError("handler error")

        client = _build(C)
        r = client.post("/")
        # Handler raised → 500 response
        assert r.status_code == 500
        # Tasks that were added still ran
        assert results == [42]

    def test_404_route_no_tasks_run(self) -> None:
        results: list[int] = []

        @controller("/")
        class C:
            @get("/exists")
            async def index(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: results.append(1))
                return {}

        client = _build(C)
        r = client.get("/does-not-exist")
        assert r.status_code == 404
        assert results == []

    def test_multiple_background_tasks_params_same_instance(self) -> None:
        """Two BackgroundTasks params in one handler → same object."""
        ids: list[int] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, t1: BackgroundTasks, t2: BackgroundTasks) -> dict:
                ids.append(id(t1))
                ids.append(id(t2))
                return {}

        client = _build(C)
        client.post("/")
        assert ids[0] == ids[1]

    def test_no_tasks_added_noop(self) -> None:
        """Handler declares BackgroundTasks but adds nothing — no error."""

        @controller("/")
        class C:
            @get("/")
            async def index(self, tasks: BackgroundTasks) -> dict:
                return {"ok": True}

        client = _build(C)
        r = client.get("/")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# TestBackgroundTasksHandlerStyles
# ---------------------------------------------------------------------------


class TestBackgroundTasksHandlerStyles:
    def test_classmethod_handler_with_background_tasks(self) -> None:
        results: list[str] = []

        @controller("/")
        class C:
            @post("/")
            @classmethod
            async def create(cls, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: results.append("classmethod"))
                return {}

        client = _build(C)
        client.post("/")
        assert results == ["classmethod"]

    def test_static_handler_with_background_tasks(self) -> None:
        results: list[str] = []

        @controller("/")
        class C:
            @post("/")
            @staticmethod
            async def create(tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: results.append("static"))
                return {}

        client = _build(C)
        client.post("/")
        assert results == ["static"]

    def test_sync_handler_with_background_tasks(self) -> None:
        results: list[str] = []

        @controller("/")
        class C:
            @post("/")
            def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: results.append("sync"))
                return {}

        client = _build(C)
        client.post("/")
        assert results == ["sync"]


# ---------------------------------------------------------------------------
# TestBackgroundTasksDI
# ---------------------------------------------------------------------------


class TestBackgroundTasksDI:
    def test_task_uses_singleton_service_passed_as_arg(self) -> None:
        from lauren import Scope

        @injectable(scope=Scope.SINGLETON)
        class MyService:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def record(self, msg: str) -> None:
                self.calls.append(msg)

        results: list[str] = []

        @controller("/")
        class C:
            def __init__(self, svc: MyService) -> None:
                self._svc = svc

            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                svc = self._svc
                tasks.add_task(lambda: results.append(svc.calls.__class__.__name__))
                return {}

        @module(controllers=[C], providers=[MyService])
        class M:
            pass

        client = TestClient(LaurenFactory.create(M))
        client.post("/")
        assert results == ["list"]

    def test_task_captures_value_not_request_scoped_instance(self) -> None:
        """Passing a plain value (not a service) to a task is fine."""
        captured: list[int] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                value = 123
                tasks.add_task(lambda v=value: captured.append(v))
                return {}

        client = _build(C)
        client.post("/")
        assert captured == [123]

    def test_task_with_no_args(self) -> None:
        calls: list[int] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: calls.append(1))
                return {}

        client = _build(C)
        client.post("/")
        assert calls == [1]


# ---------------------------------------------------------------------------
# TestBackgroundTasksSignalsIntegration
# ---------------------------------------------------------------------------


class TestBackgroundTasksSignalsIntegration:
    def test_started_signal_fired(self) -> None:
        started: list[BackgroundTaskStarted] = []

        async def work() -> None:
            pass

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(work)
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        app.signals.on(BackgroundTaskStarted)(started.append)
        client = TestClient(app)
        client.post("/")
        assert len(started) == 1
        assert started[0].func is work

    def test_complete_signal_fired_with_positive_duration(self) -> None:
        complete: list[BackgroundTaskComplete] = []

        async def work() -> None:
            pass

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(work)
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        app.signals.on(BackgroundTaskComplete)(complete.append)
        client = TestClient(app)
        client.post("/")
        assert len(complete) == 1
        assert complete[0].duration_s >= 0.0

    def test_failed_signal_fired_on_task_error(self) -> None:
        failed: list[BackgroundTaskFailed] = []

        async def boom() -> None:
            raise ValueError("expected")

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(boom)
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        app.signals.on(BackgroundTaskFailed)(failed.append)
        client = TestClient(app)
        client.post("/")
        assert len(failed) == 1
        assert isinstance(failed[0].error, ValueError)


# ---------------------------------------------------------------------------
# TestBackgroundTasksShutdown
# ---------------------------------------------------------------------------


class TestBackgroundTasksShutdown:
    def test_tasks_complete_before_arena_releases_request(self) -> None:
        """Tasks run before the request is returned to the arena pool.

        We verify indirectly: if the request's BackgroundTasks instance was
        already cleared/recycled when tasks run, the task_id would be empty.
        This test ensures the task_id captured during add_task is still valid
        when _run executes.
        """
        task_ids_during_run: list[str] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                h = tasks.add_task(
                    lambda: task_ids_during_run.append(h.task_id)  # type: ignore[name-defined]
                )
                return {}

        client = _build(C)
        client.post("/")
        assert len(task_ids_during_run) == 1
        assert task_ids_during_run[0]  # non-empty

    def test_tasks_are_in_flight_during_execution(self) -> None:
        """The in-flight task set includes the request task during bg execution.

        We can only check that the request completes without error here
        (the actual asyncio.Task object is internal). This test exercises the
        full path without raising.
        """
        finished: list[bool] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: finished.append(True))
                return {}

        client = _build(C)
        r = client.post("/")
        assert r.status_code == 200
        assert finished == [True]


# ---------------------------------------------------------------------------
# TestBackgroundTasksTaskHandle
# ---------------------------------------------------------------------------


class TestBackgroundTasksTaskHandle:
    def test_handle_task_id_in_response_body(self) -> None:
        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                h = tasks.add_task(lambda: None)
                return {"task_id": h.task_id}

        client = _build(C)
        r = client.post("/")
        data = r.json()
        assert "task_id" in data
        assert len(data["task_id"]) > 0

    def test_handle_status_accessible_after_run(self) -> None:
        handles: list[TaskHandle] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                h = tasks.add_task(lambda: None)
                handles.append(h)
                return {}

        client = _build(C)
        client.post("/")
        assert handles[0].status == "done"


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


class TestBackgroundTasksAdditional:
    def test_task_added_after_exception_not_run(self) -> None:
        """Handler raises before any task is added → no tasks run."""
        results: list[int] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                raise ValueError("early error")
                tasks.add_task(lambda: results.append(1))  # unreachable
                return {}

        client = _build(C)
        r = client.post("/")
        assert r.status_code == 500
        assert results == []

    def test_concurrent_requests_independent_background_tasks(self) -> None:
        """Two sequential requests each get fresh BackgroundTasks instances."""
        request_results: list[list[int]] = []

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                local: list[int] = []
                tasks.add_task(lambda lst=local: lst.append(1))
                request_results.append(local)
                return {}

        client = _build(C)
        client.post("/")
        client.post("/")
        # Each request has its own list
        assert len(request_results) == 2
        assert request_results[0] == [1]
        assert request_results[1] == [1]
        assert request_results[0] is not request_results[1]

    def test_background_tasks_with_global_middleware_chain(self) -> None:
        """BackgroundTasks still works when global middleware is present."""
        from lauren import CallNext, Request, Response, middleware

        results: list[str] = []

        @middleware()
        class LogMiddleware:
            async def dispatch(self, req: Request, call_next: CallNext) -> Response:
                return await call_next(req)

        @controller("/")
        class C:
            @post("/")
            async def create(self, tasks: BackgroundTasks) -> dict:
                tasks.add_task(lambda: results.append("done"))
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M, global_middlewares=[LogMiddleware])
        client = TestClient(app)
        client.post("/")
        assert results == ["done"]
