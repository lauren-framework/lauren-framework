---
name: background-task-scheduler
description: Schedules fire-and-forget background jobs using Lauren's built-in BackgroundTasks. Shows the Lauren-native pattern and the Celery integration pattern. Use when you need to queue work after an HTTP response returns without blocking the client.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Celery / ARQ Background Task Scheduler

## Overview

Lauren provides `BackgroundTasks` as a first-class request-scoped parameter.
Tasks run after the HTTP response is sent, in the same event loop. For
distributed task queues (Celery, ARQ, Dramatiq) inject your queue client as a
singleton and enqueue from the handler.

## Lauren native BackgroundTasks

```python
from pydantic import BaseModel
from lauren import controller, post, module, injectable, Scope, Json
from lauren.background import BackgroundTasks

class JobRequest(BaseModel):
    user_id: str
    job_type: str

@injectable(scope=Scope.SINGLETON)
class JobService:
    def __init__(self):
        self.completed_jobs: list[str] = []

    async def process_job(self, user_id: str, job_type: str) -> None:
        # Simulates async work — replace with DB writes, email sends, etc.
        self.completed_jobs.append(f"{job_type}:{user_id}")

@controller("/jobs")
class JobController:
    def __init__(self, svc: JobService) -> None:
        self._svc = svc

    @post("/submit")
    async def submit(self, body: Json[JobRequest], tasks: BackgroundTasks) -> dict:
        tasks.add_task(self._svc.process_job, body.user_id, body.job_type)
        return {"status": "queued", "user_id": body.user_id}

@module(controllers=[JobController], providers=[JobService])
class JobModule:
    pass
```

## Celery integration pattern

```python
from celery import Celery
from lauren import injectable, Scope, controller, post, module, Json
from pydantic import BaseModel

celery_app = Celery("tasks", broker="redis://localhost:6379/0")

@celery_app.task
def process_job_task(user_id: str, job_type: str) -> str:
    # runs in a Celery worker process
    return f"{job_type}:{user_id}"

@injectable(scope=Scope.SINGLETON)
class CeleryJobService:
    def enqueue(self, user_id: str, job_type: str) -> str:
        result = process_job_task.delay(user_id, job_type)
        return result.id

@controller("/jobs")
class CeleryJobController:
    def __init__(self, svc: CeleryJobService) -> None:
        self._svc = svc

    @post("/submit")
    async def submit(self, body: Json[BaseModel]) -> dict:
        task_id = self._svc.enqueue(body.user_id, body.job_type)
        return {"task_id": task_id, "status": "queued"}
```

## ARQ integration pattern

```python
from arq import create_pool
from arq.connections import RedisSettings
from lauren import injectable, Scope, post_construct

@injectable(scope=Scope.SINGLETON)
class ARQService:
    _pool = None

    @post_construct
    async def connect(self) -> None:
        self._pool = await create_pool(RedisSettings())

    async def enqueue(self, func_name: str, *args) -> str:
        job = await self._pool.enqueue_job(func_name, *args)
        return job.job_id
```

## Important constraints

- `BackgroundTasks` is **request-scoped** — only capture `Scope.SINGLETON`
  instances or plain values (IDs, strings) in task arguments.
- Tasks run before the ASGI response cycle closes, so they are awaited before
  the next request begins in test mode.
- For tasks that must outlive the process restart, use Celery/ARQ with Redis.
