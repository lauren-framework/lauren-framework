# Background Job with Lifecycle Hooks

Fire-and-forget queue processor that starts on app startup and drains cleanly on shutdown.

```python
# app/jobs/job_processor.py
import asyncio
import logging
from lauren import injectable, Scope, post_construct, pre_destruct

logger = logging.getLogger(__name__)

@injectable(scope=Scope.SINGLETON)
class JobProcessor:
    """Processes items from an async queue in the background."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._running = False

    @post_construct
    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._worker(), name="job-processor")
        logger.info("JobProcessor started")

    @pre_destruct
    async def stop(self) -> None:
        self._running = False
        await self._queue.join()          # drain in-flight items
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("JobProcessor stopped")

    async def enqueue(self, job: dict) -> None:
        await self._queue.put(job)

    async def _worker(self) -> None:
        while self._running or not self._queue.empty():
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                try:
                    await self._process(job)
                except Exception:
                    logger.exception("Job failed: %s", job)
                finally:
                    self._queue.task_done()
            except asyncio.TimeoutError:
                continue

    async def _process(self, job: dict) -> None:
        logger.info("Processing job: %s", job)
        await asyncio.sleep(0.1)          # replace with real work
```

```python
# app/jobs/job_controller.py
from lauren import controller, post, Json
from app.jobs.job_processor import JobProcessor

class JobRequest(BaseModel):
    type: str
    payload: dict

@controller("/api/jobs")
class JobController:
    def __init__(self, processor: JobProcessor) -> None:
        self._processor = processor

    @post("/")
    async def submit(self, body: Json[JobRequest]) -> dict:
        await self._processor.enqueue(body.model_dump())
        return {"queued": True}, 202
```

```python
# app/jobs/job_module.py
from lauren import module
from app.jobs.job_controller import JobController
from app.jobs.job_processor import JobProcessor

@module(controllers=[JobController], providers=[JobProcessor])
class JobModule: ...
```

## Inline background task (per-request)

For simpler one-off tasks that don't need a persistent queue:

```python
from lauren import controller, post, Json
from lauren.background import BackgroundTasks

@controller("/api/notifications")
class NotificationController:
    @post("/send")
    async def send(self, body: Json[NotifyRequest], tasks: BackgroundTasks) -> dict:
        tasks.add_task(self._notify, body.email, body.message)
        return {"accepted": True}, 202

    async def _notify(self, email: str, message: str) -> None:
        # runs after the response is sent
        await smtp_client.send(email, message)
```

`BackgroundTasks` is injected automatically when declared as a handler parameter typed `BackgroundTasks`.
