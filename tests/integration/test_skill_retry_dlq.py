"""Integration tests for the Retry & Dead-Letter Queue skill (Skill 38).

Tests verify that tasks succeed when the handler works, are retried on failure,
and end up in the DLQ after exhausting max_attempts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel

from lauren import (
    Json,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


@dataclass
class QueueTask:
    id: str
    payload: dict
    attempts: int = 0
    max_attempts: int = 3
    last_error: str = ""


@injectable(scope=Scope.SINGLETON)
class RetryQueue:
    def __init__(self) -> None:
        self._queue: list[QueueTask] = []
        self._dlq: list[QueueTask] = []
        self.processed: list[str] = []

    def enqueue(self, task_id: str, payload: dict, max_attempts: int = 3) -> QueueTask:
        task = QueueTask(id=task_id, payload=payload, max_attempts=max_attempts)
        self._queue.append(task)
        return task

    async def process_next(self, handler) -> QueueTask | None:
        if not self._queue:
            return None
        task = self._queue.pop(0)
        task.attempts += 1
        try:
            await handler(task)
            self.processed.append(task.id)
        except Exception as e:
            task.last_error = str(e)
            if task.attempts < task.max_attempts:
                self._queue.append(task)
            else:
                self._dlq.append(task)
        return task

    async def drain(self, handler) -> None:
        while self._queue:
            await self.process_next(handler)

    @property
    def dlq(self) -> list[QueueTask]:
        return list(self._dlq)

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    def replay_dlq(self) -> int:
        count = len(self._dlq)
        for task in self._dlq:
            task.attempts = 0
            task.last_error = ""
            self._queue.append(task)
        self._dlq.clear()
        return count


# ---------------------------------------------------------------------------
# Controller & Module
# ---------------------------------------------------------------------------


class EnqueueRequest(BaseModel):
    task_id: str
    payload: dict
    max_attempts: int = 3


@controller("/queue")
class QueueController:
    def __init__(self, queue: RetryQueue) -> None:
        self._queue = queue

    @post("/enqueue")
    async def enqueue(self, body: Json[EnqueueRequest]) -> dict:
        task = self._queue.enqueue(body.task_id, body.payload, body.max_attempts)
        return {"task_id": task.id, "queue_size": self._queue.queue_size}

    @get("/size")
    async def size(self) -> dict:
        return {"queue_size": self._queue.queue_size, "dlq_size": len(self._queue.dlq)}

    @get("/dlq")
    async def get_dlq(self) -> dict:
        return {
            "dlq": [
                {"id": t.id, "error": t.last_error, "attempts": t.attempts}
                for t in self._queue.dlq
            ]
        }

    @get("/processed")
    async def get_processed(self) -> dict:
        return {"processed": self._queue.processed}

    @post("/dlq/replay")
    async def replay_dlq(self) -> dict:
        count = self._queue.replay_dlq()
        return {"replayed": count}


@module(controllers=[QueueController], providers=[RetryQueue])
class QueueModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(QueueModule))


async def _succeeding_handler(task: QueueTask) -> None:
    pass  # always succeeds


async def _failing_handler(task: QueueTask) -> None:
    raise RuntimeError("intentional failure")


async def _conditional_handler(task: QueueTask) -> None:
    if task.attempts < 2:
        raise RuntimeError("not yet")
    # succeeds on second attempt


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryQueueUnit:
    def test_enqueue_adds_to_queue(self) -> None:
        queue = RetryQueue()
        task = queue.enqueue("t1", {"data": "x"})
        assert task.id == "t1"
        assert queue.queue_size == 1

    def test_successful_processing(self) -> None:
        queue = RetryQueue()
        queue.enqueue("t1", {"data": "x"})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(queue.process_next(_succeeding_handler))
        finally:
            loop.close()

        assert "t1" in queue.processed
        assert queue.queue_size == 0
        assert len(queue.dlq) == 0

    def test_failed_task_re_queued(self) -> None:
        queue = RetryQueue()
        queue.enqueue("t1", {}, max_attempts=3)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(queue.process_next(_failing_handler))
        finally:
            loop.close()

        # re-queued because attempts (1) < max_attempts (3)
        assert queue.queue_size == 1
        assert len(queue.dlq) == 0

    def test_task_goes_to_dlq_after_max_attempts(self) -> None:
        queue = RetryQueue()
        queue.enqueue("t1", {}, max_attempts=3)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(queue.drain(_failing_handler))
        finally:
            loop.close()

        assert queue.queue_size == 0
        assert len(queue.dlq) == 1
        assert queue.dlq[0].id == "t1"
        assert queue.dlq[0].attempts == 3

    def test_dlq_task_has_error_message(self) -> None:
        queue = RetryQueue()
        queue.enqueue("t1", {}, max_attempts=1)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(queue.drain(_failing_handler))
        finally:
            loop.close()

        assert queue.dlq[0].last_error == "intentional failure"

    def test_replay_dlq_moves_back_to_queue(self) -> None:
        queue = RetryQueue()
        queue.enqueue("t1", {}, max_attempts=1)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(queue.drain(_failing_handler))
        finally:
            loop.close()

        assert len(queue.dlq) == 1
        replayed = queue.replay_dlq()
        assert replayed == 1
        assert len(queue.dlq) == 0
        assert queue.queue_size == 1

    def test_drain_processes_all_tasks(self) -> None:
        queue = RetryQueue()
        for i in range(5):
            queue.enqueue(f"t{i}", {})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(queue.drain(_succeeding_handler))
        finally:
            loop.close()

        assert len(queue.processed) == 5
        assert queue.queue_size == 0

    def test_process_next_returns_none_on_empty(self) -> None:
        queue = RetryQueue()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(queue.process_next(_succeeding_handler))
        finally:
            loop.close()
        assert result is None


class TestRetryQueueController:
    def test_enqueue_endpoint(self) -> None:
        client = build_app()
        r = client.post("/queue/enqueue", json={"task_id": "t1", "payload": {"x": 1}})
        assert r.status_code == 200
        body = r.json()
        assert body["task_id"] == "t1"
        assert body["queue_size"] == 1

    def test_size_endpoint(self) -> None:
        client = build_app()
        client.post("/queue/enqueue", json={"task_id": "t1", "payload": {}})
        r = client.get("/queue/size")
        assert r.status_code == 200
        body = r.json()
        assert body["queue_size"] == 1
        assert body["dlq_size"] == 0

    def test_dlq_endpoint_initially_empty(self) -> None:
        client = build_app()
        r = client.get("/queue/dlq")
        assert r.status_code == 200
        assert r.json()["dlq"] == []

    def test_multiple_enqueues(self) -> None:
        client = build_app()
        for i in range(3):
            client.post(
                "/queue/enqueue", json={"task_id": f"t{i}", "payload": {"i": i}}
            )
        r = client.get("/queue/size")
        assert r.json()["queue_size"] == 3
