---
name: retry-dead-letter-queue
description: Provides an in-process retry queue with exponential backoff and a dead-letter queue for tasks that exhaust their retry budget. Use when you need resilient task processing with failure isolation without an external message broker.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Retry & Dead-Letter Queue Handling

## Overview

`RetryQueue` is a simple in-process queue with automatic retry and exponential
backoff. Tasks that exceed `max_attempts` are moved to `_dlq` (dead-letter
queue). Inspect or replay DLQ items from an admin endpoint or a monitoring job.

## Data model

```python
from dataclasses import dataclass

@dataclass
class QueueTask:
    id: str
    payload: dict
    attempts: int = 0
    max_attempts: int = 3
    last_error: str = ""
```

## RetryQueue

```python
import asyncio
from typing import Callable
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class RetryQueue:
    def __init__(self):
        self._queue: list[QueueTask] = []
        self._dlq: list[QueueTask] = []
        self.processed: list[str] = []

    def enqueue(self, task_id: str, payload: dict, max_attempts: int = 3) -> QueueTask:
        task = QueueTask(id=task_id, payload=payload, max_attempts=max_attempts)
        self._queue.append(task)
        return task

    async def process_next(self, handler: Callable) -> QueueTask | None:
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
                # exponential backoff — keep small for tests
                delay = 2 ** (task.attempts - 1) * 0.01
                await asyncio.sleep(delay)
                self._queue.append(task)
            else:
                self._dlq.append(task)
        return task

    async def drain(self, handler: Callable) -> None:
        """Process all queued tasks until the queue is empty."""
        while self._queue:
            await self.process_next(handler)

    @property
    def dlq(self) -> list[QueueTask]:
        return list(self._dlq)

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    def replay_dlq(self) -> int:
        """Move all DLQ tasks back to the main queue for reprocessing."""
        count = len(self._dlq)
        for task in self._dlq:
            task.attempts = 0
            task.last_error = ""
            self._queue.append(task)
        self._dlq.clear()
        return count
```

## Controller

```python
from lauren import controller, post, get, module, Json
from pydantic import BaseModel

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

    @get("/dlq")
    async def get_dlq(self) -> dict:
        return {"dlq": [{"id": t.id, "error": t.last_error, "attempts": t.attempts} for t in self._queue.dlq]}

    @post("/dlq/replay")
    async def replay_dlq(self) -> dict:
        count = self._queue.replay_dlq()
        return {"replayed": count}

@module(controllers=[QueueController], providers=[RetryQueue])
class QueueModule:
    pass
```

## Notes

- This is an **in-process** queue — tasks are lost on process restart. For
  durability use Redis Streams, RabbitMQ, or AWS SQS.
- The backoff multiplier (`0.01`) is small for tests; use `1.0` in production
  for proper exponential backoff (1 s, 2 s, 4 s, …).
- Integrate `RetryQueue.drain` with `SchedulerService` (see `cron-interval-jobs`
  skill) to process tasks on a fixed interval.
