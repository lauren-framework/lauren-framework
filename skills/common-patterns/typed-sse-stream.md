# Typed SSE Stream

`StreamingResponse[T]` negotiates between SSE, NDJSON, and JSON Lines based on the client's `Accept` header. Use it for homogeneous streams (same Pydantic model every event).

## Schema

```python
from pydantic import BaseModel

class ProgressEvent(BaseModel):
    step: str
    percent: int
    message: str
```

## Controller

```python
from lauren import controller, get
from lauren.streaming import StreamingResponse

@controller("/api/jobs")
class JobController:
    @get("/{job_id}/progress")
    async def stream_progress(self, job_id: str) -> StreamingResponse[ProgressEvent]:
        async def generate():
            for i in range(0, 101, 10):
                yield ProgressEvent(
                    step="processing",
                    percent=i,
                    message=f"Step {i}% complete",
                )
                await asyncio.sleep(0.5)

        return StreamingResponse[ProgressEvent](generate())
```

**SSE client** (`Accept: text/event-stream`):
```
event: data
data: {"step": "processing", "percent": 0, "message": "Step 0% complete"}

event: data
data: {"step": "processing", "percent": 10, "message": "Step 10% complete"}
```

**NDJSON client** (`Accept: application/x-ndjson`):
```
{"step": "processing", "percent": 0, "message": "Step 0% complete"}
{"step": "processing", "percent": 10, "message": "Step 10% complete"}
```

## Raw EventStream (explicit SSE control)

Use `EventStream` when you need custom SSE fields (`id`, `retry`, specific `event` names):

```python
from lauren import EventStream, ServerSentEvent, controller, get

@controller("/api/chat")
class ChatController:
    @get("/stream")
    async def stream(self) -> EventStream:
        async def generate():
            async for token in llm.stream("Hello"):
                yield ServerSentEvent(event="token", data=token)
            yield ServerSentEvent(event="done", data="")

        return EventStream(generate(), keep_alive=15.0)
```

`keep_alive=15.0` emits SSE comment pings every 15 seconds to keep the connection alive through proxies.

## Client disconnect cleanup

```python
async def generate():
    resource = await acquire_expensive_resource()
    try:
        async for chunk in resource:
            yield ServerSentEvent(event="data", data=chunk)
    finally:
        await resource.release()    # always runs, even on disconnect
```

The runtime catches client-disconnect exceptions silently; the `finally` block is your cleanup hook.
